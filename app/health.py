"""Is the instance still answering, or only still running?

The watchdog checks the pid, which answers "the process exists". That is a
different question from "the MCP server still replies": a runner whose event
loop is blocked by a synchronous tool call, or one whose port was recycled by
another instance, keeps its pid and stays green in the dashboard while every
call from a chat times out.

So the probe is the request OpenWebUI itself makes — initialize, then
tools/list, over the instance's own MCP endpoint. Nothing is inferred from a
TCP connect: an open port is exactly what a wedged process still has.

Auto-restart is off by default, and reluctant when it is on: a number of
consecutive failures before the first restart, a hard cap per instance, and a
growing wait in between. A tool that is broken for good must not restart itself
every minute and flood the runtime log — that log is where the reason for the
breakage is written.
"""
import asyncio
import time

from .auth import mcp_bearer_token
from .config_store import get_all_states, load_all_configs
from .logger import get_manager_logger
from .schema import MCPStatus
from .settings_store import load_settings

logger = get_manager_logger()

HEALTH_INTERVAL = 60          # seconds between passes
HEALTH_TIMEOUT = 8.0          # per instance; a runner that needs longer is not well
MAX_PARALLEL = 4              # probes at once, so a pass cannot stampede the box
MAX_RESTARTS = 3              # per instance, until it has been healthy again
# How long to wait after the first, second and third restart before another one
# is allowed. The first restart itself is not delayed — there is nothing to back
# off from yet.
RESTART_BACKOFF = (60, 300, 900)
# How long an instance has to stay healthy before it is forgiven its restarts.
# Without this an instance that fails once per hour would restart forever.
RESTART_RESET_AFTER = 30 * 60

DEFAULT_FAILURES_BEFORE_RESTART = 3

# An instance in `required` identity mode answers the check with an *empty*
# catalog, deliberately: the runner hides the tools from a caller it cannot
# identify rather than erroring. The server did answer, so the verdict is
# healthy — but "0 tools" on its own reads like an instance that has none.
IDENTITY_NOTE = ("no tools listed because this instance requires a verified user "
                 "and the check carries none — the answer itself was fine")

_health: dict[str, dict] = {}
_lock = asyncio.Lock()


# ── settings ─────────────────────────────────────────────────────────────────

def enabled() -> bool:
    return bool(load_settings().get("health_check_enabled", True))


def autorestart() -> bool:
    return bool(load_settings().get("health_autorestart", False))


def failures_before_restart() -> int:
    raw = load_settings().get("health_failures_before_restart", DEFAULT_FAILURES_BEFORE_RESTART)
    try:
        return max(1, min(20, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_FAILURES_BEFORE_RESTART


# ── state ────────────────────────────────────────────────────────────────────

def for_instance(instance_id: str) -> dict | None:
    """What the dashboard puts in the row, or None if never probed."""
    entry = _health.get(instance_id)
    if not entry:
        return None
    return {"status": entry["status"], "checked_at": entry["checked_at"],
            "tools": entry["tools"], "error": entry["error"], "note": entry["note"],
            "failures": entry["failures"], "restarts": entry["restarts"]}


def snapshot() -> dict:
    return {instance_id: for_instance(instance_id) for instance_id in list(_health)}


def forget(instance_id: str) -> None:
    """Drop an instance's history — it was deleted, or deliberately stopped.

    Keeping it would let a stale failure count trigger a restart the moment the
    instance is started again.
    """
    _health.pop(instance_id, None)


# ── the probe ────────────────────────────────────────────────────────────────

def _probe_url(inst) -> str:
    # Same rule as the process manager's port check: a runner bound to 0.0.0.0
    # is reached over the loopback, not over the wildcard address.
    host = "127.0.0.1" if inst.host in ("0.0.0.0", "::", "") else inst.host
    return f"http://{host}:{inst.port}{inst.endpoint}"


def _reason(exc: BaseException) -> str:
    """The cause, not the wrapper.

    The SDK runs its transport in a task group, so a refused connection arrives
    as an ExceptionGroup whose own message is "unhandled errors in a TaskGroup
    (1 sub-exception)" — true, and useless. This string ends up in a tooltip in
    the dashboard and in the manager log, where it is the only pointer anyone
    gets, so it has to name what actually went wrong.
    """
    nested = getattr(exc, "exceptions", None)
    if nested:
        parts = [_reason(e) for e in nested]
        unique = [p for i, p in enumerate(parts) if p and p not in parts[:i]]
        return "; ".join(unique)
    text = str(exc).strip()
    return text or exc.__class__.__name__


async def probe(inst) -> tuple[bool, int | None, str]:
    """(healthy, tool count, reason). Never raises."""
    from mcp import ClientSession
    from mcp.client import streamable_http as transport

    token = mcp_bearer_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with transport.httpx2.AsyncClient(headers=headers, timeout=HEALTH_TIMEOUT) as client:
            async with transport.streamable_http_client(_probe_url(inst), http_client=client) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    served = getattr(getattr(init, "server_info", None), "name", "")
                    if served and served != inst.id:
                        # Ports are reassigned and recycled. Without this the
                        # row would go green because *something* answered.
                        return False, None, f"Port {inst.port} now serves '{served}'"
                    result = await session.list_tools()
                    return True, len(result.tools), ""
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return False, None, _reason(e)[:200]


# ── one pass ─────────────────────────────────────────────────────────────────

def _restart_allowed(entry: dict, now: float) -> bool:
    if entry["restarts"] >= MAX_RESTARTS:
        return False
    if entry["last_restart"] == 0.0:
        return True
    index = min(max(entry["restarts"] - 1, 0), len(RESTART_BACKOFF) - 1)
    return now - entry["last_restart"] >= RESTART_BACKOFF[index]


async def _record(inst, healthy: bool, tools: int | None, reason: str,
                  identity_required: bool = False) -> None:
    from .process_manager import restart_instance

    now = time.time()
    entry = _health.setdefault(inst.id, {
        "status": "unknown", "checked_at": 0.0, "tools": None, "error": "", "note": "",
        "failures": 0, "restarts": 0, "last_restart": 0.0, "healthy_since": 0.0,
    })
    entry["checked_at"] = now

    if healthy:
        entry["status"] = "ok"
        entry["tools"] = tools
        entry["error"] = ""
        entry["note"] = IDENTITY_NOTE if (identity_required and not tools) else ""
        entry["failures"] = 0
        if not entry["healthy_since"]:
            entry["healthy_since"] = now
        # Forgiven only after a stretch of good behaviour — resetting on the
        # first good probe would let an instance flap through restart after
        # restart without ever reaching the cap.
        elif entry["restarts"] and now - entry["healthy_since"] >= RESTART_RESET_AFTER:
            entry["restarts"] = 0
            entry["last_restart"] = 0.0
        return

    entry["status"] = "failing"
    entry["error"] = reason
    entry["tools"] = None
    entry["note"] = ""
    entry["failures"] += 1
    entry["healthy_since"] = 0.0
    logger.warning(f"Health check: '{inst.id}' did not answer ({reason}) "
                   f"[{entry['failures']} in a row]")

    if not autorestart() or entry["failures"] < failures_before_restart():
        return
    if not _restart_allowed(entry, now):
        if entry["restarts"] >= MAX_RESTARTS:
            logger.error(f"Health check: '{inst.id}' stays unhealthy after {MAX_RESTARTS} "
                         f"restarts — leaving it alone, see its runtime log")
        return

    entry["restarts"] += 1
    entry["last_restart"] = now
    entry["failures"] = 0
    logger.warning(f"Health check: restarting '{inst.id}' "
                   f"(attempt {entry['restarts']} of {MAX_RESTARTS})")
    # restart_instance takes the instance's spawn lock itself, so this cannot
    # race a start that a person triggered a moment earlier.
    await asyncio.to_thread(restart_instance, inst.id)


async def check_once() -> None:
    """Probe every running instance once."""
    from .process_manager import start_in_progress

    states = {inst.id: inst for inst in get_all_states()}

    # An instance that was deleted, or deliberately stopped, must not keep a
    # red badge or a failure count — the count would trigger a restart the
    # moment somebody starts it again.
    for instance_id in list(_health):
        inst = states.get(instance_id)
        if inst is None or inst.status not in (MCPStatus.running, MCPStatus.starting):
            _health.pop(instance_id, None)

    # A start in flight is skipped rather than judged: its port is not open
    # yet, and calling that unhealthy would be a race, not a diagnosis.
    candidates = [inst for inst in states.values()
                  if inst.status == MCPStatus.running and inst.pid
                  and not start_in_progress(inst.id)]

    if not candidates:
        return

    # Which instances hide their catalog from an unidentified caller. One
    # directory scan for the whole pass, once a minute.
    try:
        configs = load_all_configs()
    except Exception:                                      # pragma: no cover - unreadable configs
        configs = {}
    required = {instance_id for instance_id, cfg in configs.items()
                if getattr(cfg.identity_mode, "value", cfg.identity_mode) == "required"}

    gate = asyncio.Semaphore(MAX_PARALLEL)

    async def one(inst) -> None:
        async with gate:
            healthy, tools, reason = await probe(inst)
        async with _lock:
            await _record(inst, healthy, tools, reason, inst.id in required)

    await asyncio.gather(*(one(inst) for inst in candidates), return_exceptions=True)


async def health_loop() -> None:
    while True:
        await asyncio.sleep(HEALTH_INTERVAL)
        if not enabled():
            continue
        try:
            await check_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Health check pass failed: {e}")
