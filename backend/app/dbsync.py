"""Run SQLite on local disk, mirrored to durable storage.

Why this exists
---------------
Azure Files cannot host a SQLite database. The mount does not honour the
byte-range locks SQLite takes, so even creating the schema on an empty file
fails with "database is locked" - verified on the deployment with a single
replica, a clean share and no data at all, so it is not lock contention
between processes and not a corrupt file. Container Apps exposes no mount
options, so `nobrl` is not available to work around it.

The share is perfectly good at holding *files*. openpyxl rewrites a whole
workbook, the archive is write-once PDFs, and a database file copied while
nothing is writing to it is just bytes. What it cannot do is serve as the
backing store for a process that wants fine-grained locks.

So the database lives on the container's own disk, which is a real filesystem,
and this module moves it between there and the share:

    pull()   share -> local, once, before anything opens the database
    push()   local -> share, a consistent snapshot, repeatedly

What is at risk, and what is not
--------------------------------
The filings themselves are not at risk. The per-period workbooks are written
directly to the share by openpyxl and never pass through here, and the source
PDFs are archived there too. Losing the mirror entirely would cost accounts,
sessions, the queue and the audit trail - bad, and recoverable by re-uploading
the documents that had not yet been posted.

The window of loss is the time since the last push. push() runs on a timer, on
a clean shutdown, and after every post, which is the moment the audit trail
gains the entry worth keeping.

One writer, still
-----------------
This is safe only while exactly one replica runs: two would each mirror their
own copy and the later push would silently discard the other's rows. That was
already required - workbook.py serialises writes in-process and singleton.py
takes a kernel lock - and the deployment pins maxReplicas to 1. The difference
here is that the failure would be quiet, so it is worth restating.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
from pathlib import Path

from . import db

log = logging.getLogger("gst.dbsync")

_timer: threading.Timer | None = None
_lock = threading.Lock()


def mirror() -> Path | None:
    """Where the durable copy lives, or None when mirroring is off.

    Off by default and everywhere except the one deployment shape that needs
    it: a laptop and the test suite both have a real filesystem under the
    database and should not pay for a copy they cannot benefit from.
    """
    configured = os.environ.get("GST_DB_MIRROR", "").strip()
    return Path(configured) if configured else None


def interval_seconds() -> int:
    raw = os.environ.get("GST_DB_MIRROR_INTERVAL", "120")
    try:
        return max(15, int(raw))
    except ValueError:
        return 120


def pull() -> None:
    """Bring the durable copy down to local disk, before anything opens it.

    A plain file copy rather than a SQLite backup: nothing is connected to
    either side yet, and the source is a snapshot that was itself taken
    consistently by push().
    """
    target = mirror()
    if target is None:
        return

    local = db.path()
    local.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists() or target.stat().st_size == 0:
        log.info("No mirrored database at %s; starting empty.", target)
        return

    try:
        shutil.copyfile(target, local)
        log.info("Restored %s bytes from %s", local.stat().st_size, target)
    except OSError:
        # Refusing to start would be worse: an unreadable mirror is a reason to
        # look at the share, not a reason for nobody to be able to file.
        log.exception("Could not restore the mirrored database; starting empty.")


def push(reason: str = "") -> bool:
    """Write a consistent snapshot of the live database to durable storage.

    Two steps, and the order is the whole point.

    First a SQLite backup from the live database to a second file **on local
    disk**. Through the backup API rather than a file copy because this runs
    while the application is serving, and a byte-for-byte copy of a database
    being written to can land mid-transaction and restore as corrupt.

    Then a plain byte copy of that snapshot onto the share. This is the step
    that must not be a SQLite operation: writing a database *to* Azure Files
    needs the same locks reading one there does, and they are exactly what the
    share does not provide. An earlier version pointed the backup straight at
    the share and produced a zero-byte file every time - silently, because the
    failure is a lock that never arrives rather than an error.

    The copy lands on a temporary name and is moved into place, so a push
    interrupted halfway cannot leave a half-written mirror where a good one
    was.
    """
    target = mirror()
    if target is None:
        return False

    local = db.path()
    if not local.exists():
        return False

    snapshot = local.with_name(local.name + ".snapshot")
    staging = target.with_name(target.name + ".partial")
    with _lock:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)

            snapshot.unlink(missing_ok=True)
            live = sqlite3.connect(str(local))
            copy = sqlite3.connect(str(snapshot))
            try:
                live.backup(copy)
            finally:
                copy.close()
                live.close()

            if snapshot.stat().st_size == 0:
                raise RuntimeError("the snapshot came out empty")

            shutil.copyfile(snapshot, staging)
            os.replace(staging, target)
            log.info("Mirrored %s bytes to %s%s", target.stat().st_size, target,
                     f" ({reason})" if reason else "")
            return True
        except Exception:
            log.exception("Could not mirror the database to %s", target)
            for debris in (staging,):
                try:
                    debris.unlink(missing_ok=True)
                except OSError:
                    pass
            return False
        finally:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass


def start() -> None:
    """Begin pushing on a timer. Idempotent."""
    global _timer
    if mirror() is None or _timer is not None:
        return

    def tick() -> None:
        global _timer
        push("timer")
        _timer = threading.Timer(interval_seconds(), tick)
        _timer.daemon = True
        _timer.start()

    _timer = threading.Timer(interval_seconds(), tick)
    _timer.daemon = True
    _timer.start()
    log.info("Mirroring the database to %s every %ss", mirror(), interval_seconds())


def stop() -> None:
    """Cancel the timer and take one final snapshot."""
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None
    push("shutdown")
