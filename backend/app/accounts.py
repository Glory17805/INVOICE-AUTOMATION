"""Users, passwords, sessions and the record of who did what.

Until now the API was guarded by one shared secret, which identified a
deployment rather than a person. For a system that files tax returns, "who
posted this row?" is a question an auditor may reasonably ask, and a shared key
has no answer to it. This module is that answer.

Design notes worth knowing:

- Passwords are hashed with scrypt from the standard library. No new dependency,
  memory-hard, and the parameters are stored alongside each hash so they can be
  raised later without invalidating existing passwords.
- Only the *hash* of a session token is stored. A copy of the database is not a
  set of live sessions.
- The first account created becomes the administrator, because somebody has to
  be, and a fresh install with no way in is worse than a first-run signup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from . import db

# scrypt parameters. n is the work factor; raising it later is safe because the
# values used are recorded in each stored hash.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32

SESSION_DAYS = 7
RESET_MINUTES = 60

ROLES = ("admin", "user")


class AccountError(ValueError):
    """Something the caller can fix, phrased for the person reading it."""


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #

def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def _expired(value: str) -> bool:
    try:
        return datetime.fromisoformat(value) <= _now()
    except ValueError:
        return True


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$key`, carrying its own parameters."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                         n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(key_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, bytes.fromhex(key_hex))


def check_password_quality(password: str) -> None:
    """The floor, not a policy.

    Long beats complicated: a length minimum with no character-class rules is
    both stronger in practice and less likely to be written on a sticky note.
    """
    if len(password or "") < 10:
        raise AccountError("Choose a password of at least 10 characters.")


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

def _public(row) -> dict:
    keys = row.keys()
    approved = bool(row["approved"]) if "approved" in keys else True
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "approved": approved,
        # Two different states wear the same "cannot sign in" face, and an
        # administrator needs to tell them apart: somebody who asked to join and
        # is waiting, versus somebody who was deliberately switched off.
        "awaiting_approval": not approved,
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def count_users() -> int:
    with db.LOCK, db.connect() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def list_users() -> list[dict]:
    with db.LOCK, db.connect() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    return [_public(row) for row in rows]


def get_user(user_id: str) -> dict | None:
    with db.LOCK, db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(row) if row else None


def find_by_email(email: str) -> dict | None:
    with db.LOCK, db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email.strip(),)).fetchone()
    return _public(row) if row else None


def create_user(email: str, name: str, password: str, role: str = "user",
                approved: bool = True) -> dict:
    """Create an account.

    `approved=False` is the self-signup case: the account exists and its owner
    can be told so, but it cannot sign in until an administrator lets it in.
    """
    email = (email or "").strip()
    name = (name or "").strip() or email.split("@")[0]
    if "@" not in email or len(email) < 5:
        raise AccountError("That does not look like an email address.")
    if role not in ROLES:
        raise AccountError(f"Unknown role {role!r}.")
    check_password_quality(password)

    record = {
        "id": uuid.uuid4().hex[:12],
        "email": email,
        "name": name,
        "role": role,
        "password_hash": hash_password(password),
        "created_at": _stamp(),
        "approved": 1 if approved else 0,
    }
    with db.LOCK, db.connect() as c:
        if c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise AccountError("An account with that email already exists.")
        c.execute(
            "INSERT INTO users (id, email, name, role, password_hash, is_active, "
            "                   created_at, approved) "
            "VALUES (:id, :email, :name, :role, :password_hash, 1, :created_at, :approved)",
            record,
        )
        row = c.execute("SELECT * FROM users WHERE id = ?", (record["id"],)).fetchone()
        return _public(row)


def approve_user(user_id: str) -> dict:
    """Let a self-registered account in."""
    with db.LOCK, db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AccountError("No such user.")
        c.execute("UPDATE users SET approved = 1, is_active = 1 WHERE id = ?", (user_id,))
        return _public(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def pending_users() -> list[dict]:
    return [user for user in list_users() if user["awaiting_approval"]]


def update_user(user_id: str, *, name: str | None = None, email: str | None = None,
                role: str | None = None, is_active: bool | None = None) -> dict:
    with db.LOCK, db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AccountError("No such user.")

        if role is not None and role not in ROLES:
            raise AccountError(f"Unknown role {role!r}.")

        new_email = (email or row["email"]).strip()
        if new_email.casefold() != row["email"].casefold():
            if "@" not in new_email:
                raise AccountError("That does not look like an email address.")
            if c.execute("SELECT 1 FROM users WHERE email = ? AND id <> ?",
                         (new_email, user_id)).fetchone():
                raise AccountError("An account with that email already exists.")

        # Never leave the system with no way back in.
        becoming_powerless = (
            (role is not None and role != "admin" and row["role"] == "admin")
            or (is_active is False and row["role"] == "admin")
        )
        if becoming_powerless and _other_active_admins(c, user_id) == 0:
            raise AccountError(
                "This is the only active administrator. Promote someone else first."
            )

        c.execute(
            "UPDATE users SET name = ?, email = ?, role = ?, is_active = ? WHERE id = ?",
            (
                (name or row["name"]).strip(),
                new_email,
                role or row["role"],
                1 if (row["is_active"] if is_active is None else is_active) else 0,
                user_id,
            ),
        )
        if is_active is False:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return _public(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def _other_active_admins(connection, excluding: str) -> int:
    """Administrators who could actually sign in and undo this.

    An unapproved admin cannot sign in, so it is not a way back into the system
    and must not count as one.
    """
    return connection.execute(
        "SELECT COUNT(*) AS n FROM users "
        "WHERE role = 'admin' AND is_active = 1 AND approved = 1 AND id <> ?",
        (excluding,),
    ).fetchone()["n"]


def set_password(user_id: str, password: str) -> None:
    check_password_quality(password)
    with db.LOCK, db.connect() as c:
        if c.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise AccountError("No such user.")
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                  (hash_password(password), user_id))
        # Changing a password ends every other session: that is the point of
        # changing it after a scare.
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def delete_user(user_id: str) -> None:
    with db.LOCK, db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AccountError("No such user.")
        if row["role"] == "admin" and _other_active_admins(c, user_id) == 0:
            raise AccountError("This is the only administrator. Promote someone else first.")
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate(email: str, password: str) -> dict:
    """Check credentials and open a session. Raises AccountError if they fail."""
    with db.LOCK, db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", ((email or "").strip(),)).fetchone()

    # One message for both causes: telling an attacker which half was wrong
    # turns a password guess into an account-enumeration oracle.
    generic = AccountError("Those details do not match an account.")
    if row is None:
        # Spend comparable time anyway, so a missing account is not measurably
        # faster to reject than a wrong password.
        hash_password(password or "")
        raise generic
    if not verify_password(password or "", row["password_hash"]):
        raise generic

    person = _public(row)
    # Told apart on purpose: "still waiting" and "switched off" call for
    # different things from the person reading it. Both are only ever shown
    # after the password was correct, so neither leaks who has an account.
    if person["awaiting_approval"]:
        raise AccountError(
            "Your account is waiting for an administrator to approve it. "
            "You will be able to sign in once they do."
        )
    if not person["is_active"]:
        raise AccountError("This account has been disabled. Ask an administrator.")

    return person


def open_session(user_id: str, user_agent: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    with db.LOCK, db.connect() as c:
        c.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, user_agent) "
            "VALUES (?, ?, ?, ?, ?)",
            (_token_hash(token), user_id, _stamp(),
             _stamp(_now() + timedelta(days=SESSION_DAYS)), (user_agent or "")[:200]),
        )
        c.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_stamp(), user_id))
    return token


def user_for_token(token: str) -> dict | None:
    """The account behind a session token, or None if it is not a live one."""
    if not token:
        return None
    with db.LOCK, db.connect() as c:
        row = c.execute(
            "SELECT s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        if _expired(row["expires_at"]):
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
            return None
        if not row["is_active"]:
            return None

        # Slide the expiry forward on use, so somebody working daily is not
        # signed out mid-week by a clock that started when they first signed in.
        # Written only when it has moved by more than an hour, to keep a read
        # from becoming a write on every single request.
        fresh = _now() + timedelta(days=SESSION_DAYS)
        try:
            current = datetime.fromisoformat(row["expires_at"])
            stale = (fresh - current).total_seconds() > 3600
        except ValueError:
            stale = True
        if stale:
            c.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                      (_stamp(fresh), _token_hash(token)))
    return _public(row)


def sessions_for(user_id: str, current_token: str | None = None) -> list[dict]:
    """Every live session on an account, so somebody can see and end them."""
    now_hash = _token_hash(current_token) if current_token else None
    with db.LOCK, db.connect() as c:
        rows = c.execute(
            "SELECT token_hash, created_at, expires_at, user_agent FROM sessions "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [
        {
            # An identifier for revoking, which is not the token itself.
            "id": row["token_hash"][:16],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "user_agent": row["user_agent"] or "",
            "current": row["token_hash"] == now_hash,
        }
        for row in rows
        if not _expired(row["expires_at"])
    ]


def revoke_session(user_id: str, session_id: str) -> bool:
    """End one session by the short id `sessions_for` handed out."""
    with db.LOCK, db.connect() as c:
        rows = c.execute("SELECT token_hash FROM sessions WHERE user_id = ?",
                         (user_id,)).fetchall()
        match = next((r["token_hash"] for r in rows if r["token_hash"][:16] == session_id), None)
        if match is None:
            return False
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (match,))
        return True


def close_session(token: str) -> None:
    with db.LOCK, db.connect() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def purge_expired() -> int:
    with db.LOCK, db.connect() as c:
        cursor = c.execute("DELETE FROM sessions WHERE expires_at <= ?", (_stamp(),))
        c.execute("DELETE FROM password_resets WHERE expires_at <= ?", (_stamp(),))
        return cursor.rowcount


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #

def begin_reset(email: str) -> tuple[str, dict] | None:
    """Issue a reset token, or None if no such account.

    The caller must not tell the requester which it was - see the route.
    """
    user = find_by_email(email)
    if user is None or not user["is_active"]:
        return None
    token = secrets.token_urlsafe(32)
    with db.LOCK, db.connect() as c:
        c.execute("DELETE FROM password_resets WHERE user_id = ?", (user["id"],))
        c.execute(
            "INSERT INTO password_resets (token_hash, user_id, created_at, expires_at, used) "
            "VALUES (?, ?, ?, ?, 0)",
            (_token_hash(token), user["id"], _stamp(),
             _stamp(_now() + timedelta(minutes=RESET_MINUTES))),
        )
    return token, user


def complete_reset(token: str, password: str) -> dict:
    check_password_quality(password)
    with db.LOCK, db.connect() as c:
        row = c.execute(
            "SELECT * FROM password_resets WHERE token_hash = ?", (_token_hash(token),)
        ).fetchone()
        if row is None or row["used"] or _expired(row["expires_at"]):
            raise AccountError("That reset link has expired or has already been used.")
        user_id = row["user_id"]
        c.execute("UPDATE password_resets SET used = 1 WHERE token_hash = ?",
                  (_token_hash(token),))
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                  (hash_password(password), user_id))
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return _public(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


# --------------------------------------------------------------------------- #
# Activity
# --------------------------------------------------------------------------- #

def record(user: dict | None, action: str, detail: dict | str | None = None) -> None:
    """Append to the audit trail. Never raises - a log must not break the act."""
    try:
        payload = detail if isinstance(detail, str) or detail is None else json.dumps(detail)
        with db.LOCK, db.connect() as c:
            c.execute(
                "INSERT INTO activity (at, user_id, user_email, action, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (_stamp(), (user or {}).get("id"), (user or {}).get("email"), action, payload),
            )
    except Exception:
        pass


def prune_activity(keep_days: int = 400) -> int:
    """Drop audit entries past the retention period.

    The default outlives a financial year plus a comfortable margin, so a
    question asked about last year's filings can still be answered. Unbounded
    growth was the alternative, and an audit trail nobody has set a policy on is
    a policy by accident.
    """
    cutoff = _stamp(_now() - timedelta(days=keep_days))
    try:
        with db.LOCK, db.connect() as c:
            return c.execute("DELETE FROM activity WHERE at < ?", (cutoff,)).rowcount
    except Exception:
        return 0


# What the dashboard's activity feed is for: what happened to invoices. Sign-ins,
# failed sign-ins, signups and settings changes are all recorded and all stay in
# the audit trail - they are simply not what someone opening the dashboard is
# looking for, and at six failed sign-ins to four uploads they crowded out the
# thing the feed exists to show.
DOCUMENT_ACTIONS = (
    "uploaded",
    "posted",
    "posted_with_override",
    "document_deleted",
    "unposted",
    "revised",
    "reprocess",
    "folder_ingested",
    "period_reset",
    "workbook_downloaded",
)


def activity(limit: int = 100, actions: tuple[str, ...] | None = None) -> list[dict]:
    """The audit trail, newest first.

    `actions` narrows it to particular event types. Passing None returns
    everything, which is what the Audit screen wants - the filtering here is a
    view concern, and nothing is ever excluded from what gets recorded.
    """
    capped = max(1, min(limit, 500))
    with db.LOCK, db.connect() as c:
        if actions:
            marks = ",".join("?" for _ in actions)
            rows = c.execute(
                f"SELECT at, user_email, action, detail FROM activity "
                f"WHERE action IN ({marks}) ORDER BY id DESC LIMIT ?",
                (*actions, capped),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT at, user_email, action, detail FROM activity ORDER BY id DESC LIMIT ?",
                (capped,),
            ).fetchall()
    return [dict(row) for row in rows]
