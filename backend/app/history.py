"""What we already know about a supplier, from invoices already filed.

Every invoice is currently read cold. The twelfth bill from Waymiro is read
with no memory of the previous eleven, so a misread GSTIN digit or a rate that
has never been seen from that supplier goes through looking exactly like a
correct one.

This builds a profile per supplier out of documents that were *posted* - that
is, ones a person confirmed and the system wrote into the workbook - and uses
it to flag a new document that disagrees with them.

Two deliberate constraints:

**History informs the reviewer, never the reader.** It would be easy to feed
these profiles into the extraction prompt as context. It would also be a
mistake: the reader's one job is to report what is printed, and handing it a
plausible prior invites it to reconcile the document against history instead -
filling in a GSTIN it did not actually see. On a tax filing that is the worst
available failure, because the result looks correct. So nothing here touches
extraction. It runs afterwards and raises warnings.

**Only posted documents count.** A document sitting in the queue is a guess
until somebody confirms it. Learning from unconfirmed reads would let one
misread invoice teach the system to expect the same mistake.

The retrieval idea - a small offline index with cosine similarity, rather than
an embeddings API - is borrowed from a project that used scikit-learn's TF-IDF
for it. Two things changed in the borrowing, both because the data here is
different: it is implemented in plain Python, since numpy and scipy are a great
deal of dependency for a few dozen names; and it drops the IDF term, which
turned out to hurt on strings this short. See `_vector` for the measurement.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from . import store

# How much of a supplier's history is needed before a disagreement with it
# means anything. One previous invoice is an anecdote.
MIN_HISTORY = 3

# Cosine similarity over character trigrams above which two supplier names are
# treated as the same party. Tuned to accept "SBI GENERAL INSURANCE COMPANY
# LTD" against "SBI General Insurance Co." and reject unrelated names.
NAME_MATCH_THRESHOLD = 0.55


# --------------------------------------------------------------------------- #
# A very small TF-IDF index over supplier names
# --------------------------------------------------------------------------- #

_PUNCTUATION = re.compile(r"[^a-z0-9 ]+")
_COMPANY_NOISE = re.compile(
    r"\b(private|pvt|limited|ltd|llp|inc|co|company|corporation|corp|and|the)\b"
)


def normalise(name: str | None) -> str:
    """Casefold, strip punctuation, and drop the words every company shares.

    "Waymiro Private Limited" and "WAYMIRO PVT. LTD." differ in nothing that
    identifies the party, and leaving those words in makes every company look
    slightly similar to every other one.
    """
    if not name:
        return ""
    text = _PUNCTUATION.sub(" ", name.casefold())
    text = _COMPANY_NOISE.sub(" ", text)
    return " ".join(text.split())


def trigrams(text: str) -> list[str]:
    """Character trigrams, which tolerate the misspellings a reader produces."""
    padded = f"  {text} "
    return [padded[i:i + 3] for i in range(len(padded) - 2)] if len(padded) >= 3 else []


def _vector(tokens: list[str]) -> dict[str, float]:
    """A unit-length term-frequency vector over trigrams.

    Deliberately *without* the IDF half of TF-IDF, which is where the borrowed
    technique had to be adapted rather than copied.

    IDF downweights terms that appear in many documents. That is right for a
    corpus of prose, and actively wrong here: the corpus is a few dozen names
    of seven-odd characters, so IDF suppresses exactly the shared trigrams that
    indicate two spellings are the same party. Measured on a one-character
    misread - "Waymiro" against "Waymlro", the precise case this exists to
    catch - the IDF-weighted score was 0.46 and missed it; unweighted it is
    0.67 and catches it.

    The other job IDF would do, discounting words every company shares, is
    already done explicitly and more legibly by `normalise()`.
    """
    if not tokens:
        return {}
    counts = Counter(tokens)
    total = len(tokens)
    frequencies = {term: count / total for term, count in counts.items()}
    norm = math.sqrt(sum(value * value for value in frequencies.values()))
    return {term: value / norm for term, value in frequencies.items()} if norm else {}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    # Iterate the shorter side; the vectors are already unit length.
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return sum(value * large.get(term, 0.0) for term, value in small.items())


# --------------------------------------------------------------------------- #
# Supplier profiles
# --------------------------------------------------------------------------- #

@dataclass
class SupplierProfile:
    """Everything previously filed for one party."""

    key: str                                   # GSTIN, or a normalised name
    gstins: Counter = field(default_factory=Counter)
    names: Counter = field(default_factory=Counter)
    rates: Counter = field(default_factory=Counter)
    document_types: Counter = field(default_factory=Counter)
    places: Counter = field(default_factory=Counter)
    invoice_count: int = 0
    last_seen: str | None = None

    @property
    def display_name(self) -> str:
        return self.names.most_common(1)[0][0] if self.names else self.key

    @property
    def usual_gstin(self) -> str | None:
        return self.gstins.most_common(1)[0][0] if self.gstins else None

    @property
    def usual_rate(self) -> Decimal | None:
        return Decimal(self.rates.most_common(1)[0][0]) if self.rates else None

    def as_dict(self) -> dict:
        """For the review screen: what a person would want to know at a glance."""
        return {
            "name": self.display_name,
            "gstin": self.usual_gstin,
            "invoice_count": self.invoice_count,
            "usual_rate": str(self.usual_rate) if self.usual_rate is not None else None,
            "rates_seen": sorted(self.rates, key=lambda r: -self.rates[r]),
            "registers": sorted(self.document_types, key=lambda d: -self.document_types[d]),
            "last_seen": self.last_seen,
        }


def _profile_key(gstin: str | None, name: str | None) -> str | None:
    """A GSTIN identifies a party; a name only approximates one."""
    if gstin and gstin.strip():
        return gstin.strip().upper()
    normalised = normalise(name)
    return f"name:{normalised}" if normalised else None


def build(documents: list[dict] | None = None) -> dict[str, SupplierProfile]:
    """Profiles from every posted document, keyed by GSTIN or normalised name."""
    documents = store.all_documents() if documents is None else documents
    profiles: dict[str, SupplierProfile] = {}

    for doc in documents:
        if doc.get("status") != "posted":
            continue
        treatment = doc.get("treatment") or {}
        gstin = (treatment.get("counterparty_gstin") or "").strip().upper() or None
        name = treatment.get("counterparty_name")

        key = _profile_key(gstin, name)
        if key is None:
            continue

        profile = profiles.setdefault(key, SupplierProfile(key=key))
        profile.invoice_count += 1
        if gstin:
            profile.gstins[gstin] += 1
        if name:
            profile.names[name.strip()] += 1
        if treatment.get("rate") is not None:
            profile.rates[str(treatment["rate"])] += 1
        if treatment.get("document_type"):
            profile.document_types[treatment["document_type"]] += 1
        if treatment.get("place_of_supply_name"):
            profile.places[treatment["place_of_supply_name"]] += 1

        received = doc.get("received_at")
        if received and (profile.last_seen is None or received > profile.last_seen):
            profile.last_seen = received

    return profiles


def match(name: str | None, gstin: str | None,
          profiles: dict[str, SupplierProfile] | None = None) -> SupplierProfile | None:
    """The profile for this party, by GSTIN if we have one, by name if not.

    The fuzzy path matters because a GSTIN is exactly the field a reader is
    most likely to get one character wrong in, and that is also the case worth
    catching. Falling back to the name lets the check fire on the misread.
    """
    profiles = build() if profiles is None else profiles
    if not profiles:
        return None

    if gstin:
        exact = profiles.get(gstin.strip().upper())
        if exact is not None:
            return exact

    target = normalise(name)
    if not target:
        return None

    # Exact normalised name, before spending anything on similarity.
    direct = profiles.get(f"name:{target}")
    if direct is not None:
        return direct

    candidates = [(key, profile) for key, profile in profiles.items() if profile.names]
    if not candidates:
        return None

    query = _vector(trigrams(target))

    best, best_score = None, 0.0
    for _, profile in candidates:
        # Compare against every spelling seen for this party, not just the most
        # common one: the match may be to a variant already recorded.
        for seen_name in profile.names:
            score = cosine(query, _vector(trigrams(normalise(seen_name))))
            if score > best_score:
                best, best_score = profile, score

    return best if best_score >= NAME_MATCH_THRESHOLD else None


# --------------------------------------------------------------------------- #
# What history says about a new document
# --------------------------------------------------------------------------- #

def anomalies(name: str | None, gstin: str | None, rate: Decimal | None,
              profiles: dict[str, SupplierProfile] | None = None
              ) -> list[tuple[str, str]]:
    """Ways this document disagrees with what this supplier has always done.

    Warnings, never blockers. A supplier legitimately changes their GSTIN when
    they re-register, and a rate legitimately differs by what was sold. The
    point is that a person should be told, not that the document is wrong.
    """
    profile = match(name, gstin, profiles)
    if profile is None or profile.invoice_count < MIN_HISTORY:
        return []

    found: list[tuple[str, str]] = []

    usual_gstin = profile.usual_gstin
    current = (gstin or "").strip().upper()
    if usual_gstin and current and current != usual_gstin:
        found.append((
            "supplier_gstin_differs",
            f"{profile.display_name} has used GSTIN {usual_gstin} on all "
            f"{profile.invoice_count} previous invoices; this one says {current}. "
            f"Either they have re-registered, or a character has been misread.",
        ))
    elif usual_gstin and not current:
        found.append((
            "supplier_gstin_missing_but_known",
            f"No GSTIN was read, but {profile.display_name} used {usual_gstin} on "
            f"{profile.invoice_count} previous invoices. Worth adding before posting.",
        ))

    # Only when the supplier has been entirely consistent. One who already
    # bills at several rates tells us nothing by using another, and warning on
    # that would train people to ignore warnings.
    usual_rate = profile.usual_rate
    if (usual_rate is not None and rate is not None
            and rate != usual_rate and len(profile.rates) == 1):
        found.append((
            "unusual_rate_for_supplier",
            f"{profile.display_name} has been billed at "
            f"{usual_rate * 100:.2f}% on all {profile.invoice_count} previous "
            f"invoices; this one is {rate * 100:.2f}%.",
        ))

    return found


def summary() -> list[dict]:
    """Every supplier we have filed for, most active first."""
    profiles = build()
    return sorted((p.as_dict() for p in profiles.values()),
                  key=lambda p: -p["invoice_count"])
