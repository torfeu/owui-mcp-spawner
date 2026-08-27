"""Who has been here? — the users the runners have actually seen.

Assigning rights means knowing whom to assign them to, and nobody wants to
copy a UUID out of a log by hand. Every verified identity is therefore
recorded once: who they are, when they first appeared and when last. The
dashboard offers that list; a user who has never called can still be typed in
by hand, because a rule may well exist before its first use.

Written by the runners, read by the manager — the same split as the usage
database, and for the same reason: with one port per instance the manager is
not in the data path. Its own SQLite file rather than a table inside
`usage.db`, whose promise is that it stores no caller information at all. That
promise stays true.

Deliberately small. This is a *roster*, not an audit trail: one row per user,
overwritten as they return. No per-call history, no addresses, no tokens.
Deleting a row forgets a person entirely, and they reappear on their next call
— which is the honest behaviour for something that only exists to fill a
dropdown.
"""
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .logger import get_manager_logger

logger = get_manager_logger()

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "runtime" / "identities.db"

# A busy chat calls a dozen tools a minute, all from the same person. Writing
# on every call would be a row rewrite per call for no new information.
REFRESH_SECONDS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    sub           TEXT PRIMARY KEY,
    email         TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    last_instance TEXT NOT NULL DEFAULT ''
);
"""

_conn: Optional[sqlite3.Connection] = None
_conn_lock = threading.Lock()
# sub -> (written_at, claims) so a returning caller costs nothing
_recent: dict[str, tuple[float, tuple]] = {}


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=3.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _conn = conn
    return _conn


def close() -> None:
    """Drop the process-wide connection (used by tests)."""
    global _conn
    with _conn_lock:
        if _conn is not None:
            _conn.close()
            _conn = None
    _recent.clear()


def record(identity, instance_id: str = "") -> None:
    """Note that *identity* was here. Never raises — this is bookkeeping.

    A failure to write the roster must not turn into a failed tool call: the
    user is verified either way, and the only loss is a missing dropdown entry.
    """
    if identity is None or not getattr(identity, "sub", ""):
        return
    claims = (identity.email, identity.name, identity.role, identity.source, instance_id)
    seen = _recent.get(identity.sub)
    if seen and claims == seen[1] and time.time() - seen[0] < REFRESH_SECONDS:
        return

    now = int(time.time())
    try:
        with _conn_lock:
            conn = _connect()
            conn.execute(
                """
                INSERT INTO identities (sub, email, name, role, source,
                                        first_seen, last_seen, last_instance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sub) DO UPDATE SET
                    email = excluded.email,
                    name = excluded.name,
                    role = excluded.role,
                    source = excluded.source,
                    last_seen = excluded.last_seen,
                    last_instance = excluded.last_instance
                """,
                (identity.sub, identity.email, identity.name, identity.role,
                 identity.source, now, now, instance_id),
            )
            conn.commit()
        _recent[identity.sub] = (time.time(), claims)
    except Exception as e:
        logger.warning(f"Could not record identity: {e}")


def known() -> list[dict]:
    """Every user seen so far, most recent first.

    Reading never creates the file: an installation where nobody has called
    yet has no roster, and the manager asking about it should not conjure an
    empty database into `runtime/`. Only `record()` brings one into being.
    """
    if not DB_PATH.exists():
        return []
    try:
        with _conn_lock:
            rows = _connect().execute(
                """
                SELECT sub, email, name, role, source, first_seen, last_seen, last_instance
                FROM identities ORDER BY last_seen DESC
                """
            ).fetchall()
    except Exception as e:
        logger.warning(f"Could not read the identity roster: {e}")
        return []
    return [
        {"sub": r[0], "email": r[1], "name": r[2], "role": r[3], "source": r[4],
         "first_seen": r[5], "last_seen": r[6], "last_instance": r[7]}
        for r in rows
    ]


def forget(sub: str) -> bool:
    """Remove one user from the roster. True when a row was deleted.

    Does not revoke anything — rules live in the policy. A forgotten user
    reappears on their next call.
    """
    if not DB_PATH.exists():
        return False
    try:
        with _conn_lock:
            conn = _connect()
            deleted = conn.execute("DELETE FROM identities WHERE sub = ?", (sub,)).rowcount
            conn.commit()
        _recent.pop(sub, None)
        return bool(deleted)
    except Exception as e:
        logger.warning(f"Could not forget identity: {e}")
        return False
