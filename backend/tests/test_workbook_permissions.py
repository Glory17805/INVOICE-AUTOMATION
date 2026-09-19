"""The working copy must be writable, whatever the master's permissions are.

A read-only master is the normal case, not an odd one: it is a template that
nobody should edit in place, and on a mounted file share the mode a file ends
up with is decided by the mount rather than by whoever created it.

`shutil.copy2` carried the master's mode across, so the working copy came out
read-only and posting failed at `wb.save()` - reporting "Permission denied" on
the period workbook, which names the file that cannot be written rather than
the master it inherited the problem from. That cost a live deployment a
500 on the one action that matters.
"""

from __future__ import annotations

import stat
import sys

import pytest

from app import workbook

PERIOD = "May-26"


def _read_only(path):
    path.chmod(path.stat().st_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)


def writable(path) -> bool:
    """Owner-write bit, rather than os.access.

    os.access answers "could *this* process write it", and root can write
    anything regardless of mode - so under a root test runner, which is what
    CI and every container here use, it reports success on exactly the file
    that broke production. The mode bit is the thing being asserted.
    """
    return bool(path.stat().st_mode & stat.S_IWUSR)


@pytest.fixture
def scratch_workbooks(tmp_path, monkeypatch):
    monkeypatch.setattr(workbook, "WORKBOOK_DIR", tmp_path / "workbook")
    return tmp_path


def test_a_read_only_master_still_yields_a_writable_working_copy(scratch_workbooks):
    """The defect, stated directly."""
    master = workbook.source_workbook()
    if not master.exists():
        pytest.skip("needs the master workbook, which is not in the repository")

    # Stand in for a read-only template without touching the real one.
    local_master = scratch_workbooks / "master.xlsx"
    local_master.write_bytes(master.read_bytes())
    _read_only(local_master)
    assert not writable(local_master), "the fixture failed to make it read-only"

    import app.workbook as module
    original = module.source_workbook
    module.source_workbook = lambda: local_master
    try:
        target = workbook.ensure_working_copy(PERIOD)
    finally:
        module.source_workbook = original

    assert target.exists()
    assert writable(target), (
        "the working copy inherited the master's read-only mode; posting will fail "
        "at wb.save() with Permission denied"
    )


def test_an_existing_read_only_copy_is_repaired_rather_than_left(scratch_workbooks):
    """Copies made before the fix already exist, and hold posted rows.

    Deleting and recreating them would discard a period's filings, so the
    repair has to happen in place.
    """
    master = workbook.source_workbook()
    if not master.exists():
        pytest.skip("needs the master workbook, which is not in the repository")
    if sys.platform == "win32":
        pytest.skip("Windows read-only flags do not model POSIX mode bits closely enough")

    target = workbook.workbook_path(PERIOD)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(master.read_bytes())
    _read_only(target)
    assert not writable(target)

    assert workbook.ensure_working_copy(PERIOD) == target
    assert writable(target), "an existing read-only working copy was left unusable"
