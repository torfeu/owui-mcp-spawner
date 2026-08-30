"""Usage tracking: every tool call with time, instance and function.

Recorded in the **runner**, not in the shared proxy: with one port per instance
the client talks to the subprocess directly and the manager never sees the
traffic. Only `tools/call` is recorded — a client sends `initialize` and
`tools/list` on every connection regardless of use, so counting those would
make every instance look equally busy.

Two tables, because "ever used" and "used within the retention window" are
different questions. `calls` is the event log and gets pruned; `totals` is
never pruned, so a function that ran once a year ago stays recognisable as
"used before" even after its event is gone. Without that split the retention
setting would quietly falsify the most valuable answer: *never used?*

Stored in SQLite (stdlib, no new dependency) rather than JSON: the previous
JSON counter rewrote the whole file on every call, which is right for three
fields and absurd for an event log.

Never stores arguments, results or caller addresses — time, instance and
function name only.
"""
import asyncio
import sqlite3
import threading
import time
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "runtime" / "usage.db"
DEFAULT_RETENTION_DAYS = 30

# Written straight away while idle, batched while busy: a chat fires ten calls
# in a row, and one transaction for the burst beats ten. Anything that arrives
# during a write goes into the next batch.
BATCH_PAUSE = 0.2
MAX_BATCH = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    ts       INTEGER NOT NULL,
    instance TEXT    NOT NULL,
    tool     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_instance ON calls(instance, ts);
CREATE TABLE IF NOT EXISTS totals (
    instance   TEXT NOT NULL,
    tool       TEXT NOT NULL,
    calls      INTEGER NOT NULL,
    first_call INTEGER,
    last_call  INTEGER,
    PRIMARY KEY (instance, tool)
);
"""

_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()
_pending: list[tuple[int, str, str]] = []
_flusher: asyncio.Task | None = None


def _connect() -> sqlite3.Connection:
    """One connection per process, WAL so readers and writers don't block.

    Ten runners write into this file while the manager reads it. WAL plus a
    busy timeout is what makes that work — without them a concurrent write
    fails outright with "database is locked", in the middle of a tool call.
    """
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


# ── writing (runner side) ────────────────────────────────────────────────────

def record_call(instance_id: str, tool_name: str) -> None:
    """Queue one tool call. Returns immediately and never raises.

    The timestamp is taken here, not when the batch is written — otherwise ten
    calls of one chat would all land on the same second and the spacing between
    them would be lost.
    """
    try:
        _pending.append((int(time.time()), instance_id, tool_name))
        _schedule_flush()
    except Exception:
        pass


def _schedule_flush() -> None:
    global _flusher
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop (tests, sync callers): write through.
        _write_batch(_drain())
        return
    if _flusher is None or _flusher.done():
        _flusher = loop.create_task(_flush_loop())


def _drain() -> list[tuple[int, str, str]]:
    """Take up to MAX_BATCH queued events — one transaction shouldn't be endless."""
    batch = _pending[:MAX_BATCH]
    del _pending[: len(batch)]
    return batch


async def _flush_loop() -> None:
    while _pending:
        batch = _drain()
        if batch:
            # In a thread: if another process holds the write lock, the wait
            # must not stall the runner's event loop mid tool call.
            await asyncio.to_thread(_write_batch, batch)
        if not _pending:
            return
        await asyncio.sleep(BATCH_PAUSE)


async def flush() -> None:
    """Write everything still queued. Called on runner shutdown."""
    while _pending:
        await asyncio.to_thread(_write_batch, _drain())


def _write_batch(batch: list[tuple[int, str, str]]) -> None:
    if not batch:
        return
    try:
        with _conn_lock:
            conn = _connect()
            with conn:
                conn.executemany(
                    "INSERT INTO calls (ts, instance, tool) VALUES (?, ?, ?)", batch
                )
                conn.executemany(
                    """INSERT INTO totals (instance, tool, calls, first_call, last_call)
                       VALUES (?, ?, 1, ?, ?)
                       ON CONFLICT(instance, tool) DO UPDATE SET
                           calls = calls + 1,
                           last_call = excluded.last_call""",
                    [(instance, tool, ts, ts) for ts, instance, tool in batch],
                )
    except Exception:
        # Usage tracking must never break a tool call.
        pass


# ── the window (manager side) ────────────────────────────────────────────────
#
# Calendar days, not multiples of 86400 seconds counted back from now. The
# statistics view answers "which day was busy", and a bucket whose edges slide
# with the time of day cannot: the same burst lands in a different bucket
# depending on when the page is opened. So "7 days" means the last seven local
# dates including today, and every bucket is one date.
#
# The shortest window is the exception. "The last 24 hours" is a question about
# right now, not about a date, and a single day-bucket is a number rather than a
# shape — so that one window is cut into 24 hourly buckets and does roll with
# the clock. `bucket_unit()` is what the rest of the module branches on.

HOURS_IN_SHORT_WINDOW = 24


def bucket_unit(days: int) -> str:
    """`"hour"` for the 24h window, `"day"` for everything longer."""
    return "hour" if max(1, days) <= 1 else "day"


def bucket_labels(days: int) -> list[str]:
    """The buckets of the window, oldest first: ISO dates, or `date + "T" + hour`."""
    days = max(1, days)
    if bucket_unit(days) == "hour":
        current = datetime.now().replace(minute=0, second=0, microsecond=0)
        return [
            (current - timedelta(hours=HOURS_IN_SHORT_WINDOW - 1 - i)).strftime("%Y-%m-%dT%H")
            for i in range(HOURS_IN_SHORT_WINDOW)
        ]
    today = date.today()
    return [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]


def window_start(days: int) -> int:
    """Start of the window as unix time: the first hour, or local midnight."""
    days = max(1, days)
    if bucket_unit(days) == "hour":
        current = datetime.now().replace(minute=0, second=0, microsecond=0)
        return int((current - timedelta(hours=HOURS_IN_SHORT_WINDOW - 1)).timestamp())
    first = date.today() - timedelta(days=days - 1)
    # combine() over a naive date applies that date's UTC offset, so a window
    # spanning a DST change stays aligned to midnight on both sides of it.
    return int(datetime.combine(first, clock.min).timestamp())


# ── reading (manager side) ───────────────────────────────────────────────────

def read_usage(instance_id: str) -> dict:
    """Totals of one instance: `{calls, last_call, last_tool}`, empty if unused."""
    if not DB_PATH.exists():
        return {}  # reading must not create the database — only recording does
    try:
        with _conn_lock:
            row = _connect().execute(
                """SELECT SUM(calls), MAX(last_call) FROM totals WHERE instance = ?""",
                (instance_id,),
            ).fetchone()
            if not row or not row[0]:
                return {}
            last_tool = _connect().execute(
                """SELECT tool FROM totals WHERE instance = ?
                   ORDER BY last_call DESC LIMIT 1""",
                (instance_id,),
            ).fetchone()
        return {
            "calls": int(row[0]),
            "last_call": int(row[1] or 0),
            "last_tool": last_tool[0] if last_tool else "",
        }
    except Exception:
        return {}


def usage_summary(days: int = 7) -> dict:
    """Per instance and per function: totals plus the calls within *days*.

    Feeds the statistics view. Instances never used simply do not appear —
    the caller pairs this with the instance list to spot them.
    """
    since = window_start(days)
    result: dict[str, dict] = {}
    if not DB_PATH.exists():
        return {}
    try:
        with _conn_lock:
            conn = _connect()
            for instance, tool, calls, first_call, last_call in conn.execute(
                "SELECT instance, tool, calls, first_call, last_call FROM totals"
            ):
                entry = result.setdefault(
                    instance,
                    {"calls": 0, "recent": 0, "first_call": 0,
                     "last_call": 0, "last_tool": "", "tools": {}},
                )
                entry["calls"] += calls
                # first_call is nullable in the table (legacy rows never get it
                # backfilled). Folding a None through min() would either raise
                # or turn the answer into 0 — "since 1970". Ignore the missing
                # ones and keep the earliest real timestamp.
                if first_call and (not entry["first_call"] or first_call < entry["first_call"]):
                    entry["first_call"] = first_call
                if (last_call or 0) > entry["last_call"]:
                    entry["last_call"] = last_call or 0
                    entry["last_tool"] = tool
                entry["tools"][tool] = {"calls": calls, "recent": 0, "last_call": last_call}
            for instance, tool, recent in conn.execute(
                """SELECT instance, tool, COUNT(*) FROM calls WHERE ts >= ?
                   GROUP BY instance, tool""",
                (since,),
            ):
                entry = result.get(instance)
                if not entry:
                    continue
                entry["recent"] += recent
                if tool in entry["tools"]:
                    entry["tools"][tool]["recent"] = recent
    except Exception:
        return {}
    return result


def bucket_matrix(days: int = 30) -> dict[str, list[int]]:
    """Calls per bucket for every instance, oldest first.

    One grouped query for all instances rather than one per instance: the
    statistics view draws a chart per row, and with a dozen instances the
    per-instance variant was a dozen scans of the same index.

    Bucketing happens in SQLite via `localtime` so it agrees with
    `bucket_labels()` — doing it in Python would mean carrying every single
    timestamp across the boundary just to divide it.
    """
    days = max(1, days)
    labels = bucket_labels(days)
    position = {label: i for i, label in enumerate(labels)}
    fmt = "%Y-%m-%dT%H" if bucket_unit(days) == "hour" else "%Y-%m-%d"
    result: dict[str, list[int]] = {}
    if not DB_PATH.exists():
        return result
    try:
        with _conn_lock:
            rows = _connect().execute(
                f"""SELECT instance,
                           strftime('{fmt}', ts, 'unixepoch', 'localtime') AS bucket,
                           COUNT(*)
                    FROM calls WHERE ts >= ?
                    GROUP BY instance, bucket""",
                (window_start(days),),
            ).fetchall()
    except Exception:
        return {}
    for instance, bucket, count in rows:
        index = position.get(bucket)
        if index is None:
            continue  # a clock change can push a row just outside the window
        result.setdefault(instance, [0] * len(labels))[index] = count
    return result


def bucket_counts(instance_id: str, days: int = 30) -> list[int]:
    """Calls per bucket for one instance, oldest first."""
    return bucket_matrix(days).get(instance_id, [0] * len(bucket_labels(days)))


def forget(instance_id: str) -> None:
    """Drop everything recorded for a deleted instance."""
    try:
        with _conn_lock:
            conn = _connect()
            with conn:
                conn.execute("DELETE FROM calls WHERE instance = ?", (instance_id,))
                conn.execute("DELETE FROM totals WHERE instance = ?", (instance_id,))
    except Exception:
        pass


def prune(days: int | None = None) -> int:
    """Delete events older than the retention window; returns rows removed.

    Runs in the manager, not in the runners — otherwise ten processes would
    attempt the same housekeeping. `totals` is deliberately left alone.
    """
    if days is None:
        days = retention_days()
    if days <= 0:
        return 0  # keep forever
    cutoff = int(time.time()) - days * 86400
    try:
        with _conn_lock:
            conn = _connect()
            with conn:
                cursor = conn.execute("DELETE FROM calls WHERE ts < ?", (cutoff,))
                removed = cursor.rowcount or 0
            # The write-ahead log grows with every transaction and is only
            # trimmed when nothing reads it — the daily housekeeping is the
            # natural moment, with ten runners writing all day.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return removed
    except Exception:
        return 0


def retention_days() -> int:
    """Retention window from the settings file (0 = keep forever)."""
    try:
        from .settings_store import load_settings

        value = load_settings().get("usage_retention_days", DEFAULT_RETENTION_DAYS)
        return int(value)
    except Exception:
        return DEFAULT_RETENTION_DAYS
