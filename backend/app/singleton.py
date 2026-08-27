"""One backend process per data directory, enforced by the operating system.

`workbook.py` serialises access with a `threading.RLock`. That is correct within
one process and worth nothing across two: a lock object in process A is invisible
to process B, so two backends pointed at the same `data/` directory will both
read a register, both append a row at what each believes is the first free line,
and both save - and the second save wins. The row the first one wrote is gone,
with no error anywhere.

That is a silent, unrecoverable loss of a tax record, and it is one `--workers 2`
away. Rather than document the constraint and hope, this takes an exclusive
kernel lock on a file in the data directory. A second process cannot acquire it
and refuses to start with an explanation.

The lock is held by an open file handle, so the OS drops it when the process
exits for any reason - including a crash or a kill. There is no stale lock file
to clean up by hand, which is the failure mode that makes PID-file schemes
annoying.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Locking a byte well past the pid text keeps the lock region and the readable
# contents from overlapping, which matters on Windows where a locked byte range
# cannot be rewritten.
_LOCK_OFFSET = 4096

_handle = None  # module-level: the lock lives as long as the process does


class AlreadyRunning(RuntimeError):
    """Another backend already holds this data directory."""


def _take_lock(handle) -> None:
    """Claim the byte range, raising OSError if someone else holds it."""
    handle.seek(_LOCK_OFFSET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _previous_holder(path: Path) -> str:
    """Whatever the last holder recorded, for the error message only."""
    try:
        recorded = path.read_text(encoding="utf-8").strip().split("\n")[0]
    except OSError:
        return ""
    return recorded.strip()


def acquire(path: Path) -> None:
    """Claim this data directory for the current process.

    Raises AlreadyRunning if another live process holds it. Calling twice from
    the same process is a no-op, so an app that reloads does not fight itself.
    """
    global _handle
    if _handle is not None:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _previous_holder(path)

    # Deliberately not a context manager: the lock lasts as long as the handle
    # does, so closing it here would release the claim immediately. It is held
    # in a module-level variable for the life of the process and released by
    # release(), or by the OS when the process exits.
    handle = open(path, "r+b" if path.exists() else "w+b")  # noqa: SIM115
    try:
        _take_lock(handle)
    except OSError as exc:
        handle.close()
        held_by = f" (held by pid {previous})" if previous else ""
        raise AlreadyRunning(
            f"Another backend is already using {path.parent}{held_by}.\n"
            f"Only one process may write these workbooks: a second one would "
            f"silently overwrite rows the first has posted.\n"
            f"Stop the running backend, or point this one at a different data "
            f"directory, and start it again."
        ) from exc

    handle.seek(0)
    handle.write(f"{os.getpid()}\n".encode().ljust(64, b" "))
    handle.flush()
    _handle = handle


def release() -> None:
    """Give up the claim. The OS would do this at exit anyway."""
    global _handle
    if _handle is None:
        return
    try:
        _handle.seek(_LOCK_OFFSET)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        # Losing the lock on the way out is not worth failing a shutdown over.
        pass
    finally:
        _handle.close()
        _handle = None
