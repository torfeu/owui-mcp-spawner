"""Slowing down password guessing against the manager API.

There is no login route to protect. The browser sends the password as a Bearer
token on *every* request and app/auth.py decides per request, so the counter
has to live where that decision is made — and it is keyed by client address,
because that is the one thing an attacker cannot vary while guessing. The token
changes with every try; the address does not.

Three rejected credentials from one address cost 60 seconds, the next three
120, then 240, doubling. **Deliberately without a ceiling**: the point is to
make guessing hopeless, not to stay polite. Locking yourself out is survivable
— a manager restart clears every counter, and whoever can restart the manager
is not the attacker. For the same reason the counters live in memory only.

Two things do *not* count. A request carrying no credential at all: the UI asks
the API before anyone has logged in, and reloading the page must never cost a
strike. And a valid credential that merely lacks the scope for a route (403) —
that is a configured client at the wrong door, not a guess.

The client address is taken from the connection, never from X-Forwarded-For.
A header the caller writes would let an attacker dodge their own counter and
run someone else's address into a block.
"""
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .logger import get_manager_logger

logger = get_manager_logger()

FAILURES_PER_STEP = 3        # rejected credentials before the next block
FIRST_DELAY_SECONDS = 60     # 60 · 120 · 240 · …
FORGET_AFTER_SECONDS = 900   # quiet for 15 minutes and the slate is clean

# Not a ceiling on the policy, just arithmetic hygiene: 2**20 · 60 s is over a
# year, and the exponent stops growing before it turns into a silly number.
_MAX_DOUBLINGS = 20


@dataclass
class _Record:
    failures: int = 0
    blocked_until: float = 0.0
    doublings: int = 0
    last_failure: float = field(default_factory=time.time)


_records: dict[str, _Record] = {}
_lock = threading.Lock()


def address_of(request) -> str:
    """The connection's address — the key everything else hangs off."""
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def blocked_for(address: str, *, now: Optional[float] = None) -> int:
    """Seconds this address still has to wait, 0 when it may try."""
    now = time.time() if now is None else now
    with _lock:
        record = _records.get(address)
        if record is None or record.blocked_until <= now:
            return 0
        return int(math.ceil(record.blocked_until - now))


def record_failure(address: str, *, now: Optional[float] = None) -> int:
    """Count one rejected credential. Returns the new block in seconds, or 0."""
    now = time.time() if now is None else now
    with _lock:
        record = _records.get(address)
        # Quiet time is measured from the end of the last block, not from the
        # last attempt. Otherwise every block longer than the forget window
        # would wipe itself out while being served, and the doubling would
        # cycle 60 · 120 · … · 60 forever for anyone patient enough to wait.
        quiet_since = max(record.last_failure, record.blocked_until) if record else 0.0
        if record is None or now - quiet_since > FORGET_AFTER_SECONDS:
            record = _Record(last_failure=now)
            _records[address] = record

        record.last_failure = now
        record.failures += 1
        if record.failures < FAILURES_PER_STEP:
            return 0

        record.failures = 0
        delay = FIRST_DELAY_SECONDS * (2 ** min(record.doublings, _MAX_DOUBLINGS))
        record.doublings += 1
        record.blocked_until = now + delay

    logger.warning(f"Locking out {address} for {delay}s after repeated failed authentication")
    return delay


def reset(address: str) -> None:
    """Forget an address — called on every successful authentication."""
    with _lock:
        _records.pop(address, None)


def clear_all() -> None:
    """Drop every counter. For tests, and for a manager restart's fresh start."""
    with _lock:
        _records.clear()
