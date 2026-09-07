"""Per-vendor overrides, for the invoices the general reader gets wrong.

The label vocabulary and the table parser are deliberately general: they are
meant to handle a supplier nobody has seen before without anyone editing code.
Most of the time they do. But generality has a floor - two vendors can use the
same word for different things, and one vendor can print a layout no rule
guesses correctly - and the honest answer for those is a small, explicit
description of that vendor's invoice rather than a cleverer guess.

A template is a JSON file naming a supplier and what is special about their
bills. It is found by matching something on the document itself: their GSTIN
where it is known, or a phrase only their invoices carry.

Nothing is required of a template. It adds label aliases to the vocabulary and
column hints to the table parser for that one document; everything it does not
mention keeps working the way it works for everyone else. So the cost of
adding a vendor is a few lines, not a parser.

    {
      "name": "Sharma Traders",
      "match": {"gstin": "37AAACS1234F1Z5"},
      "labels": {"invoice_number": ["challan no"]},
      "columns": {"taxable": ["net value"]},
      "notes": "Prints the challan number where others print an invoice number."
    }
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("gst.templates")

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"


@dataclass
class Template:
    name: str
    labels: dict[str, list[str]] = field(default_factory=dict)
    columns: dict[str, list[str]] = field(default_factory=dict)
    match_gstin: str | None = None
    match_text: list[str] = field(default_factory=list)
    notes: str = ""
    path: Path | None = None

    def matches(self, text: str) -> bool:
        """Whether this template describes the document in hand.

        A GSTIN is the strong signal - it identifies one business - so it is
        checked first and alone is enough. Phrases are the fallback for a
        vendor whose GSTIN is not known yet, and every one of them must appear:
        a single common phrase would claim invoices it knows nothing about.
        """
        if self.match_gstin and self.match_gstin.upper() in text.upper():
            return True
        if not self.match_text:
            return False
        haystack = " ".join(text.casefold().split())
        return all(" ".join(p.casefold().split()) in haystack for p in self.match_text)


def _load_one(path: Path) -> Template | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Ignoring template %s: %s", path.name, exc)
        return None

    match = data.get("match") or {}
    template = Template(
        name=str(data.get("name") or path.stem),
        labels={k: list(v) for k, v in (data.get("labels") or {}).items()},
        columns={k: list(v) for k, v in (data.get("columns") or {}).items()},
        match_gstin=(match.get("gstin") or None),
        match_text=list(match.get("text") or []),
        notes=str(data.get("notes") or ""),
        path=path,
    )
    if not template.match_gstin and not template.match_text:
        log.warning("Ignoring template %s: it matches nothing, so it would never apply.",
                    path.name)
        return None
    return template


@lru_cache(maxsize=1)
def _all() -> tuple[Template, ...]:
    if not TEMPLATE_DIR.is_dir():
        return ()
    found = [_load_one(p) for p in sorted(TEMPLATE_DIR.glob("*.json"))]
    templates = tuple(t for t in found if t)
    if templates:
        log.info("Loaded %d vendor template(s): %s",
                 len(templates), ", ".join(t.name for t in templates))
    return templates


def refresh() -> None:
    """Forget the loaded templates, so an edited file takes effect."""
    _all.cache_clear()


def all_templates() -> tuple[Template, ...]:
    return _all()


def identify(text: str) -> Template | None:
    """The template describing this document, if one does.

    A GSTIN match beats a phrase match, because it identifies a business
    rather than a turn of phrase two vendors might share.
    """
    candidates = [t for t in _all() if t.matches(text)]
    if not candidates:
        return None
    upper = text.upper()
    by_gstin = [t for t in candidates if t.match_gstin and t.match_gstin.upper() in upper]
    chosen = (by_gstin or candidates)[0]
    if len(candidates) > 1:
        log.info("%d templates matched; using %s.", len(candidates), chosen.name)
    return chosen


# --------------------------------------------------------------------------- #
# Applying one
# --------------------------------------------------------------------------- #

_SAFE_ALIAS = re.compile(r"^[\w #/&.%()-]{1,60}$")


def _clean(aliases: list[str]) -> list[str]:
    """Aliases that could not break the matcher.

    A template is a config file, and a config file that can inject a regex
    into the label scanner is a config file that can silently stop the reader
    matching anything at all.
    """
    return [a.strip() for a in aliases if a.strip() and _SAFE_ALIAS.match(a.strip())]


def label_aliases(template: Template | None, base: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """The label vocabulary with a template's additions folded in.

    Additions, never replacements: a vendor who calls it a "challan no" often
    still prints "Invoice Date" like everybody else, and a template that
    replaced the vocabulary would lose every field it forgot to mention.
    """
    if not template or not template.labels:
        return base
    merged = dict(base)
    for field_name, aliases in template.labels.items():
        extra = tuple(a.casefold() for a in _clean(aliases))
        if extra:
            merged[field_name] = tuple(dict.fromkeys(extra + merged.get(field_name, ())))
    return merged


def column_aliases(template: Template | None, base: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """The table's column vocabulary with a template's additions folded in."""
    if not template or not template.columns:
        return base
    merged = dict(base)
    for role, headings in template.columns.items():
        extra = tuple(h.casefold() for h in _clean(headings))
        if extra:
            merged[role] = tuple(dict.fromkeys(extra + merged.get(role, ())))
    return merged


def describe(template: Template | None) -> str:
    if not template:
        return "no vendor template - read with the general rules"
    detail = f"template: {template.name}"
    return f"{detail} ({template.notes})" if template.notes else detail
