"""One category as one MCP endpoint.

The middle ground between registering every instance separately (precise, but
maintenance per instance) and the router tool (one registration, but indirect —
a model has to go through `find_tools`/`call_tool` to reach anything).

    http://<manager-host>:<manager-port>/mcp/category/<name>

serves the tools of *every running instance* in that category as one catalog,
named `<instance-id>.<tool>`. Registering that one URL in OpenWebUI or in an
agent CLI gives the model the tools directly, under their own names, with their
own schemas.

Three properties, each a decision that was made deliberately:

**It checks no rights of its own.** The caller's headers are forwarded verbatim
to the instance, which resolves the identity and applies its access rules
exactly as it does for a direct connection — the same arrangement as the router
tool. The consequence is accepted: the listing shows tools whose *call* the
identity may later be refused. Filtering the listing would mean re-implementing
the policy here, in a second place, where it could drift.

**A stopped instance is simply absent** from the listing, and calling one of its
tools gives a short sentence saying so. On the other end of this is usually a
small model, and a model that gets no usable answer invents a cause.

**No session is held upstream.** Every listing and every call opens its own MCP
session to the instance and closes it again. That is the whole session-handling
problem of this endpoint, deleted rather than solved — and it costs nothing,
because the runner shares one `tools_instance` across all sessions anyway: an
MCP session upstream carries no state worth keeping.
"""
import asyncio
import re
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote, unquote

from . import agent_identity
from .auth import mcp_bearer_token
from .config_store import get_all_states
from .health import _probe_url as instance_url, _reason
from .logger import get_manager_logger
from .schema import MCPStatus
from .settings_store import load_settings

logger = get_manager_logger()

# The path segment that separates a category endpoint from an instance
# endpoint. `/mcp/<instance-id>` is what the shared-port proxy and the manager
# port serve; the segment sits in front of it, so an instance whose id is
# literally that word would be shadowed — which is why `require_available_id`
# reserves whatever this is set to. Instance ids are free-form apart from it.
DEFAULT_SEGMENT = "category"

# What a segment may look like: one path element, no slash, nothing that needs
# escaping in a URL. Deliberately narrower than an instance id — this word ends
# up in every category URL anybody registers.
SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")


def segment() -> str:
    """The configured segment, or `category`.

    Read on every request like the on/off switch next to it: changing it takes
    effect at once. A stored value that does not match `SEGMENT_PATTERN` is
    ignored rather than obeyed — the route below refuses to write one, but a
    hand-edited settings file must not be able to break every URL at once.
    """
    raw = load_settings().get("category_url_segment")
    if isinstance(raw, str) and SEGMENT_PATTERN.fullmatch(raw.strip()):
        return raw.strip()
    return DEFAULT_SEGMENT


def prefix() -> str:
    """`/mcp/<segment>/` — what the dispatcher matches and every URL starts with."""
    return "/mcp/" + segment() + "/"

# `instance-id` + this + `tool-name`. A dot, decided on 2026-08-31 and measured
# on all three layers it has to survive: the MCP protocol lists and calls such
# names unchanged, OpenWebUI passes `spec['name']` through unfiltered, and the
# local model calls them and repeats them verbatim. Instance ids cannot contain
# a dot (see MCPConfig.id_safe), so splitting at the first one is exact.
SEPARATOR = "."

# Listing is a fan-out: every instance of the category is asked at once, but not
# more than this many at a time, so a category of thirty cannot stampede the box.
MAX_PARALLEL = 4
LIST_TIMEOUT = 10.0
# Calls have no read timeout on purpose — a tool that legitimately runs for two
# minutes must not be cut off by the endpoint in front of it. The connect
# timeout still catches an instance that is not listening.
CONNECT_TIMEOUT = 5.0

# Sent by us, meaningless to the instance, or set by the client library itself.
# `mcp-session-id` is the sharpest of them: it identifies the caller's session
# with *this* endpoint and would be read upstream as a session that never
# existed there.
_SKIP_UPSTREAM_HEADERS = {
    "host", "content-length", "content-type", "transfer-encoding", "connection",
    "accept", "accept-encoding", "mcp-session-id", "mcp-protocol-version",
    "last-event-id",
}


# ── on or off ────────────────────────────────────────────────────────────────

def enabled() -> bool:
    """Whether this manager serves category endpoints at all. **Off by default.**

    Off, deliberately: one endpoint reaches the tools of a whole category at
    once, and an upgrade must not quietly open a door that nobody asked for.
    The same reasoning the shared port already follows — a new way in is
    switched on by a person, not by a version number. Turn it on under
    Settings → System.

    Read on every request rather than cached: switching it takes effect now,
    not after a restart, and the settings file is small.
    """
    return bool(load_settings().get("category_endpoints_enabled", False))


# ── which categories exist ───────────────────────────────────────────────────

def _instances_by_category() -> dict[str, list]:
    """Every category that at least one instance carries, with its instances.

    Derived from the instances themselves rather than from a setting: a
    category exists exactly as long as something is in it, so there is no
    second place that can fall out of step with the dashboard.
    """
    grouped: dict[str, list] = {}
    for inst in get_all_states():
        name = (inst.category or "").strip()
        if name:
            grouped.setdefault(name, []).append(inst)
    return grouped


def categories() -> list[str]:
    return sorted(_instances_by_category(), key=str.lower)


def resolve(segment: str) -> Optional[str]:
    """The category a URL segment refers to, or None.

    Compared case-insensitively: the segment is typed by hand into OpenWebUI
    and into agent configuration files, and "Recht" vs "recht" is not a
    distinction worth a 404. The stored spelling wins in every answer.
    """
    wanted = unquote(segment).strip().lower()
    if not wanted:
        return None
    for name in _instances_by_category():
        if name.lower() == wanted:
            return name
    return None


def members(category: str, running_only: bool = True) -> list:
    """The instances of *category*, newest state, sorted by id."""
    found = _instances_by_category().get(category, [])
    if running_only:
        found = [i for i in found if i.status == MCPStatus.running]
    return sorted(found, key=lambda i: i.id)


def endpoint_path(category: str) -> str:
    return prefix() + quote(category, safe="")


# ── talking to the instances ─────────────────────────────────────────────────

def forwardable(headers: dict) -> dict:
    """The caller's headers, minus the ones that belong to this hop.

    Everything else travels on — the Authorization header above all, because it
    is what the instance uses to work out who is calling. Passing rights
    through instead of judging them here is the point of the endpoint.
    """
    return {k: v for k, v in (headers or {}).items()
            if str(k).lower() not in _SKIP_UPSTREAM_HEADERS}


@asynccontextmanager
async def _session(inst, headers: dict, read_timeout: Optional[float]):
    """An initialised MCP session to one instance, closed on the way out."""
    from mcp import ClientSession
    from mcp.client import streamable_http as transport

    timeout = transport.httpx2.Timeout(
        connect=CONNECT_TIMEOUT, read=read_timeout, write=30.0, pool=CONNECT_TIMEOUT
    )
    async with transport.httpx2.AsyncClient(headers=headers, timeout=timeout) as client:
        async with transport.streamable_http_client(
            instance_url(inst), http_client=client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


async def _list_one(inst, headers: dict) -> list:
    """One instance's tools, or an empty list — never an exception.

    An instance that does not answer drops out of the catalog with a line in
    the manager log. The alternative, failing the whole listing, would let one
    broken instance take a working category off the air.
    """
    async def ask() -> list:
        async with _session(inst, headers, LIST_TIMEOUT) as session:
            return list((await session.list_tools()).tools)

    try:
        # wait_for rather than asyncio.timeout: the project supports 3.10, and
        # the context-manager form only arrived in 3.11.
        return await asyncio.wait_for(ask(), LIST_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"Category endpoint: '{inst.id}' did not list its tools ({_reason(e)})")
        return []


# ── the MCP server for one category ──────────────────────────────────────────

def build_server(category: str):
    """Wire one category up as an MCP server.

    Separate from serving it so the two handlers can be driven in a test
    without a socket. The instance list is read inside the handlers, not
    captured here: an instance started, stopped or recategorised while a client
    is connected shows up on that client's next listing.
    """
    from mcp.server import Server
    from mcp import types

    def answer(text: str) -> "types.CallToolResult":
        """A refusal travels as plain content, not as an error result.

        Same reasoning as in the runner: at the other end is a model that has
        to *read* why nothing happened, and an error result is what clients
        hand to their own error handling instead of to the model.
        """
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    def request_headers(ctx) -> dict:
        request = getattr(ctx, "request", None)
        raw = getattr(request, "headers", None)
        if raw is None:
            return {}
        try:
            return dict(raw.items())
        except Exception:
            return {}

    async def handle_list_tools(ctx, params) -> "types.ListToolsResult":
        headers = forwardable(request_headers(ctx))
        instances = members(category)
        if not instances:
            return types.ListToolsResult(tools=[])

        gate = asyncio.Semaphore(MAX_PARALLEL)

        async def one(inst):
            async with gate:
                return inst, await _list_one(inst, headers)

        collected = await asyncio.gather(*(one(i) for i in instances))
        tools: list = []
        for inst, found in collected:
            for tool in found:
                tools.append(types.Tool(
                    name=f"{inst.id}{SEPARATOR}{tool.name}",
                    description=tool.description,
                    inputSchema=tool.input_schema,
                ))
        logger.info(f"Category '{category}': listed {len(tools)} tool(s) "
                    f"from {len(instances)} running instance(s)")
        return types.ListToolsResult(tools=tools)

    async def handle_call_tool(ctx, params) -> "types.CallToolResult":
        name = params.name
        instance_id, _, tool_name = name.partition(SEPARATOR)
        if not tool_name:
            return answer(
                f"'{name}' is not a tool of this endpoint. Tools here are named "
                f"'<instance>{SEPARATOR}<tool>' — call tools/list to see them."
            )

        inst = next((i for i in members(category, running_only=False)
                     if i.id == instance_id), None)
        if inst is None:
            return answer(
                f"No instance '{instance_id}' in category '{category}'. "
                f"Call tools/list to see what this endpoint offers right now."
            )
        if inst.status != MCPStatus.running:
            return answer(
                f"Instance '{instance_id}' is not running, so '{name}' cannot be "
                f"called. Nothing is wrong with the arguments."
            )

        headers = forwardable(request_headers(ctx))
        try:
            async with _session(inst, headers, None) as session:
                result = await session.call_tool(tool_name, params.arguments or {})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = _reason(e)
            logger.error(f"Category '{category}': call to '{name}' failed ({reason})")
            return answer(f"Instance '{instance_id}' did not answer: {reason}")

        # Handed back exactly as it arrived — content blocks, structured
        # content and the error flag alike. Re-wrapping it as text here would
        # throw away everything a tool returns that is not a string.
        if isinstance(result, types.CallToolResult):
            return result
        return answer(str(result))

    return Server(
        f"category:{category}",
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


# ── serving them ─────────────────────────────────────────────────────────────

_managers: dict[str, object] = {}
_stops: dict[str, asyncio.Event] = {}
_tasks: dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()


async def _serve(category: str, ready: asyncio.Event, stop: asyncio.Event) -> None:
    """Hold one category's session manager open for as long as it is wanted.

    In its own task, and that is not a detail: `run()` opens an anyio task
    group, and a task group has to be left by the same task that entered it.
    Entering it from whichever request happened to arrive first and leaving it
    from the shutdown handler would raise from inside anyio at the worst
    possible moment.
    """
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    try:
        session_manager = StreamableHTTPSessionManager(build_server(category))
        async with session_manager.run():
            _managers[category] = session_manager
            ready.set()
            await stop.wait()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Category endpoint '{category}' stopped unexpectedly: {e!r}")
    finally:
        # Set unconditionally: a waiter that is never released would hang the
        # request that started this task, and through it the client.
        ready.set()
        _managers.pop(category, None)


async def _session_manager(category: str):
    """The running session manager for *category*, started on first use."""
    async with _lock:
        existing = _managers.get(category)
        if existing is not None:
            return existing
        ready, stop = asyncio.Event(), asyncio.Event()
        _stops[category] = stop
        _tasks[category] = asyncio.create_task(_serve(category, ready, stop))
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
        except (asyncio.TimeoutError, TimeoutError):
            logger.error(f"Category endpoint '{category}' did not come up")
            return None
        return _managers.get(category)


async def stop_all() -> None:
    """Wind every category endpoint down. Called from the manager's shutdown."""
    async with _lock:
        for stop in _stops.values():
            stop.set()
        tasks = list(_tasks.values())
        _stops.clear()
        _tasks.clear()
    for task in tasks:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            task.cancel()
        except Exception:
            pass
    _managers.clear()


def running() -> list[str]:
    """Which category endpoints currently hold a session manager."""
    return sorted(_managers)


# ── the ASGI entry point ─────────────────────────────────────────────────────

async def _send_error(send, status: int, message: str) -> None:
    body = message.encode()
    headers = [(b"content-type", b"text/plain; charset=utf-8"),
               (b"content-length", str(len(body)).encode())]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _authorised(scope) -> bool:
    """The same gate the runner puts in front of an instance.

    Not a formality: this endpoint reaches the tools of a whole category, so
    leaving it open while every instance behind it is closed would be a door
    around the wall. An agent's own token opens it as well as the shared one —
    *which* of them arrived is decided again upstream, per call.
    """
    token = mcp_bearer_token()
    if not token:
        return True
    raw = ""
    for key, value in scope.get("headers", []):
        if key.lower() == b"authorization":
            raw = value.decode("utf-8", errors="replace")
            break
    provided = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    import hmac
    if hmac.compare_digest(provided, token):
        return True
    return agent_identity.identify(provided) is not None


async def handle(scope, receive, send) -> None:
    """Serve `/mcp/<segment>/<name>` — the ASGI side of this module.

    The on/off switch is checked by the dispatcher in `admin_server.asgi`, not
    here: switched off, the path is not intercepted at all and falls through to
    the ordinary 404 of a URL this manager does not serve. Answering `403` here
    instead would tell an unauthenticated caller that the feature exists and is
    merely closed, which is a different sentence than "there is nothing here".
    """
    path = scope.get("path", "")
    here = prefix()
    wanted = path[len(here):].split("/", 1)[0]
    if not wanted:
        await _send_error(send, 404, f"Not Found — use {here}<category-name>")
        return

    if not _authorised(scope):
        await _send_error(send, 401, "Unauthorized")
        return

    category = resolve(wanted)
    if category is None:
        known = ", ".join(categories()) or "none"
        await _send_error(send, 404,
                          f"Unknown category '{unquote(wanted)}'. Categories: {known}")
        return

    session_manager = await _session_manager(category)
    if session_manager is None:
        await _send_error(send, 503, f"Category endpoint '{category}' is not available")
        return
    await session_manager.handle_request(scope, receive, send)
