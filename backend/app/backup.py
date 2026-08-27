"""Backups of everything that cannot be recreated.

Gap D1, and the only blocker on the register that can be closed by writing
code rather than by making a decision.

What is irreplaceable here is narrow but total: the per-period workbooks (a
client's filed returns), the SQLite queue and audit trail (who posted what, and
when), and the archived source PDFs (the paper trail behind every row). Losing
the directory loses a company's filing history, and nothing else in the system
holds a second copy.

Two things this is careful about:

* **The database is copied through SQLite, not through the filesystem.** A live
  SQLite file plus its write-ahead log copied byte-for-byte can land mid
  transaction and restore as a corrupt database. `Connection.backup()` takes a
  consistent snapshot of a database that is being written to.
* **A backup nobody has restored is a hope, not a backup.** Every archive is
  reopened and its manifest checked before the run reports success, and
  `verify()` will do the same to any archive on demand.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from . import db
from .config import ARCHIVE_DIR, DATA_DIR, WORKBOOK_DIR, setting

MANIFEST = "manifest.json"


def backup_dir() -> Path:
    """Where archives are written.

    Defaults to a sibling of the data directory rather than a child: a backup
    living inside the thing it backs up is lost with it, and is also swept up
    by anything that clears the data directory.
    """
    configured = setting("GST_BACKUP_DIR")
    if configured:
        return Path(configured).expanduser()
    return DATA_DIR.parent / "backups"


def keep_count() -> int:
    raw = setting("GST_BACKUP_KEEP", "14")
    try:
        return max(1, int(raw))
    except ValueError:
        return 14


def _snapshot_database(destination: Path) -> bool:
    """Consistent copy of the SQLite database, or False if there is none yet."""
    source = db.path()
    if not Path(source).exists():
        return False
    # `with sqlite3.connect(...)` commits a transaction; it does NOT close the
    # connection. Leaving these open kept a handle on the temporary file, and
    # Windows then refused to delete the directory it lived in - so a run that
    # had written a perfectly good archive still ended in an exception.
    live = sqlite3.connect(str(source))
    copy = sqlite3.connect(str(destination))
    try:
        live.backup(copy)
    finally:
        copy.close()
        live.close()
    return True


def _add_tree(archive: zipfile.ZipFile, root: Path, arc_root: str) -> int:
    count = 0
    if not root.exists():
        return 0
    for path in sorted(root.rglob("*")):
        if path.is_file():
            archive.write(path, f"{arc_root}/{path.relative_to(root).as_posix()}")
            count += 1
    return count


def create(note: str = "") -> Path:
    """Write one archive and verify it. Returns the path written."""
    target_dir = backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"gst-backup-{stamp}.zip"

    counts: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as scratch:
        db_copy = Path(scratch) / "store.db"
        has_db = _snapshot_database(db_copy)

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            if has_db:
                archive.write(db_copy, "database/store.db")
                counts["database"] = 1
            counts["workbooks"] = _add_tree(archive, WORKBOOK_DIR, "workbooks")
            counts["archive"] = _add_tree(archive, ARCHIVE_DIR, "archive")

            manifest = {
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "data_dir": str(DATA_DIR),
                "counts": counts,
                "note": note,
                "format": 1,
            }
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2))

    verify(target)
    prune()
    return target


def verify(path: Path) -> dict:
    """Reopen an archive and check it is readable and complete.

    Raises if it is not. A corrupt archive that reports success is worse than
    no archive, because it is trusted.
    """
    with zipfile.ZipFile(path) as archive:
        broken = archive.testzip()
        if broken is not None:
            raise RuntimeError(f"{path.name} is corrupt at {broken}")
        names = set(archive.namelist())
        if MANIFEST not in names:
            raise RuntimeError(f"{path.name} has no manifest; it was not written by this tool")
        manifest = json.loads(archive.read(MANIFEST))
        expected = manifest.get("counts", {})
        if expected.get("database") and "database/store.db" not in names:
            raise RuntimeError(f"{path.name} claims a database but does not contain one")
    return manifest


def prune() -> list[Path]:
    """Delete all but the most recent `keep_count()` archives."""
    target_dir = backup_dir()
    if not target_dir.exists():
        return []
    archives = sorted(target_dir.glob("gst-backup-*.zip"), key=lambda p: p.name, reverse=True)
    removed = []
    for stale in archives[keep_count():]:
        stale.unlink(missing_ok=True)
        removed.append(stale)
    return removed


def restore(path: Path, into: Path | None = None) -> Path:
    """Unpack an archive so its contents can be inspected or copied back.

    Deliberately does NOT overwrite a live data directory. Restoring is rare,
    consequential and done under supervision; a command that silently replaces
    a running system's workbooks is the wrong shape for it. This unpacks
    somewhere safe and tells you where.
    """
    verify(path)
    destination = into or (backup_dir() / f"restored-{path.stem}")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with zipfile.ZipFile(path) as archive:
        archive.extractall(destination)
    return destination


def latest() -> Path | None:
    archives = sorted(backup_dir().glob("gst-backup-*.zip"), key=lambda p: p.name)
    return archives[-1] if archives else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the GST automation data directory.")
    sub = parser.add_subparsers(dest="command")

    made = sub.add_parser("create", help="write a new archive (default)")
    made.add_argument("--note", default="", help="why this one was taken")

    checked = sub.add_parser("verify", help="check an archive, or the most recent one")
    checked.add_argument("path", nargs="?")

    sub.add_parser("list", help="show what has been kept")

    back = sub.add_parser("restore", help="unpack an archive somewhere safe")
    back.add_argument("path")
    back.add_argument("--into", default=None)

    args = parser.parse_args()
    command = args.command or "create"

    if command == "create":
        written = create(note=getattr(args, "note", ""))
        size = written.stat().st_size / 1024
        print(f"Wrote {written}  ({size:,.0f} KB), verified.")
        kept = sorted(backup_dir().glob("gst-backup-*.zip"))
        print(f"Keeping {len(kept)} of the last {keep_count()}.")

    elif command == "verify":
        path = Path(args.path) if args.path else latest()
        if not path:
            raise SystemExit("No archives found.")
        manifest = verify(path)
        print(f"{path.name}: intact. Taken {manifest['created_at']}, "
              f"{manifest['counts'].get('workbooks', 0)} workbook file(s), "
              f"{manifest['counts'].get('archive', 0)} archived document(s).")

    elif command == "list":
        archives = sorted(backup_dir().glob("gst-backup-*.zip"))
        if not archives:
            print(f"No archives in {backup_dir()}")
        for path in archives:
            print(f"  {path.name}  {path.stat().st_size / 1024:>9,.0f} KB")

    elif command == "restore":
        where = restore(Path(args.path), Path(args.into) if args.into else None)
        print(f"Unpacked to {where}")
        print("Nothing live was touched. Copy what you need back by hand.")


if __name__ == "__main__":
    main()
