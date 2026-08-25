"""A small JSON-backed document store.

Every document is tracked from the moment it arrives to the moment it is
posted - the "nothing slips through" promise in the proposal. A flat JSON file
is deliberate: the workbook is the system of record, and this store only holds
the queue around it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from .config import STORE_PATH, ensure_dirs

_LOCK = RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read() -> dict[str, Any]:
    ensure_dirs()
    if not STORE_PATH.exists():
        return {"documents": []}
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"documents": []}


def _write(data: dict[str, Any]) -> None:
    ensure_dirs()
    tmp = STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STORE_PATH)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def all_documents() -> list[dict]:
    with _LOCK:
        docs = _read()["documents"]
    return sorted(docs, key=lambda d: d.get("received_at", ""), reverse=True)


def get(doc_id: str) -> dict | None:
    with _LOCK:
        for doc in _read()["documents"]:
            if doc["id"] == doc_id:
                return doc
    return None


def add(document: dict) -> dict:
    document.setdefault("id", new_id())
    document.setdefault("received_at", _now())
    document.setdefault("history", [])
    document["history"].append({"at": _now(), "event": "received"})
    with _LOCK:
        data = _read()
        data["documents"].append(document)
        _write(data)
    return document


def update(doc_id: str, changes: dict, event: str | None = None) -> dict | None:
    with _LOCK:
        data = _read()
        for doc in data["documents"]:
            if doc["id"] != doc_id:
                continue
            doc.update(changes)
            if event:
                doc.setdefault("history", []).append({"at": _now(), "event": event})
            _write(data)
            return doc
    return None


def delete(doc_id: str) -> bool:
    with _LOCK:
        data = _read()
        before = len(data["documents"])
        data["documents"] = [d for d in data["documents"] if d["id"] != doc_id]
        if len(data["documents"]) == before:
            return False
        _write(data)
        return True


def clear() -> None:
    with _LOCK:
        _write({"documents": []})
