"""The mirrored-database path, which exists because Azure Files cannot host SQLite.

These matter more than most: on that deployment the mirror is the only durable
copy of the accounts, the queue and the audit trail. A push that silently wrote
nothing would not be noticed until a container restarted.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import accounts, db, dbsync, store


@pytest.fixture
def mirrored(tmp_path, monkeypatch):
    """A local database with a durable mirror beside it, as the deployment has.

    SQLite is pinned rather than inherited. Mirroring exists only for the
    SQLite deployment - a managed PostgreSQL needs none of it - so under the
    Postgres test run these would otherwise write rows to Postgres while
    dbsync looked for a SQLite file that was never created, and fail in a way
    that says nothing about either engine.
    """
    local = tmp_path / "local" / "store.db"
    remote = tmp_path / "durable" / "store.db"
    local.parent.mkdir(parents=True, exist_ok=True)
    remote.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GST_SQLITE_PATH", str(local))
    monkeypatch.setenv("GST_DB_MIRROR", str(remote))
    db.forget()
    store._prepared.clear()
    yield local, remote
    db.forget()
    store._prepared.clear()


def test_the_database_goes_where_it_is_told(mirrored):
    local, _ = mirrored
    assert db.path() == local


def test_mirroring_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("GST_DB_MIRROR", raising=False)
    assert dbsync.mirror() is None
    # And a push with nowhere to push to is a no-op, not an error: every
    # non-Azure deployment calls this on every post.
    assert dbsync.push("nowhere") is False


def count_users_in(path) -> int:
    """Read the mirror without leaving a handle on it.

    Closing matters here rather than being tidiness: Windows refuses to replace
    a file another handle still has open, so a leaked connection makes the NEXT
    push fail with access denied - and the failure looks like a bug in push()
    rather than in the test that read it. (Linux, where this deploys, allows
    replacing an open file, so the same leak would pass there and fail here.)
    """
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT count(*) FROM users").fetchone()[0]
    finally:
        connection.close()


def test_a_push_captures_rows_written_since_the_last_one(mirrored):
    _, remote = mirrored
    accounts.create_user("first@ira.test", "First", "a-long-enough-passphrase-1")
    assert dbsync.push("test") is True
    assert count_users_in(remote) == 1

    accounts.create_user("second@ira.test", "Second", "a-long-enough-passphrase-2")
    assert dbsync.push("test") is True
    assert count_users_in(remote) == 2


def test_a_pull_restores_what_a_previous_container_left(mirrored):
    local, _ = mirrored
    accounts.create_user("survivor@ira.test", "Survivor", "a-long-enough-passphrase-3")
    store.add({"id": "d1", "status": "ready", "period": "May-26"})
    dbsync.push("test")

    # The container goes away; its local disk goes with it.
    local.unlink()
    db.forget()
    store._prepared.clear()
    assert not local.exists()

    dbsync.pull()

    assert [u["email"] for u in accounts.list_users()] == ["survivor@ira.test"]
    assert [d["id"] for d in store.all_documents()] == ["d1"]


def test_a_pull_with_no_mirror_yet_starts_empty_rather_than_failing(mirrored):
    _, remote = mirrored
    assert not remote.exists()
    dbsync.pull()          # must not raise - this is every first deployment
    assert accounts.count_users() == 0


def test_an_interrupted_push_cannot_replace_a_good_mirror(mirrored, monkeypatch):
    """The staging file is the point: a half-written mirror is worse than a stale one."""
    _, remote = mirrored
    accounts.create_user("kept@ira.test", "Kept", "a-long-enough-passphrase-4")
    dbsync.push("good")
    good = remote.read_bytes()

    def explode(*_a, **_k):
        raise OSError("the share went away mid-write")

    monkeypatch.setattr(dbsync.os, "replace", explode)
    accounts.create_user("lost@ira.test", "Lost", "a-long-enough-passphrase-5")
    assert dbsync.push("interrupted") is False

    # The previous good mirror is untouched, and no debris is left behind.
    assert remote.read_bytes() == good
    assert not remote.with_name(remote.name + ".partial").exists()


def test_sqlite_is_never_opened_on_the_mirror(mirrored, monkeypatch):
    """The mirror must only ever be written as bytes, never as a database.

    This is the bug the live deployment found and the local one could not:
    pointing SQLite's backup straight at the share produced a zero-byte file
    on every push, silently, because writing a database to Azure Files needs
    the same locks reading one there does. A Docker volume stands in for the
    share in every other test and is a real filesystem, so it happily accepted
    what Azure refuses.

    Asserting on the paths SQLite is asked to open is white-box, and it is the
    only way to state the constraint that actually matters.
    """
    _, remote = mirrored
    accounts.create_user("bytes@ira.test", "Bytes", "a-long-enough-passphrase-7")

    opened: list[str] = []
    real_connect = sqlite3.connect

    def watching(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(dbsync.sqlite3, "connect", watching)
    assert dbsync.push("test") is True

    mirror_dir = str(remote.parent)
    on_the_mirror = [p for p in opened if p.startswith(mirror_dir)]
    assert not on_the_mirror, f"SQLite was opened on the mirror: {on_the_mirror}"
    assert remote.stat().st_size > 0


def test_a_failing_push_never_stops_the_caller(mirrored, monkeypatch):
    """Posting must not fail because the share is unreachable."""
    _, remote = mirrored
    accounts.create_user("anyone@ira.test", "Anyone", "a-long-enough-passphrase-6")

    monkeypatch.setattr(dbsync, "mirror", lambda: remote.parent / "nope" / "x" / "store.db")
    monkeypatch.setattr(dbsync.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    assert dbsync.push("doomed") is False
