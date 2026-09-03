"""
Shared-port reverse proxy: expose every MCP instance under one port.

When a shared port is configured (settings key "shared_port"), a second
uvicorn listener is started inside the manager process that forwards

    http://<host>:<shared_port>/mcp/<instance_id>[/<tail>]

to the instance's internal server at

    http://127.0.0.1:<instance_port><endpoint>[/<tail>]

The same forwarding is also served on the manager's own port by the
dispatcher in `admin_server.asgi` (`handle()` below), where it sits next to
`/mcp/category/<name>` — no second listener, no second port. That path is off
by default and has its own switch; the listeners here are unaffected by it.

Listeners are kept **by port**, not one per feature: `shared_port` says where
instances get a port of their own, `category_port` says the same for the
category endpoints, and the two may be the same number — then one listener
serves both. What a listener serves is decided per request from the settings,
so moving a role between ports never needs a restart; only adding or removing
a *port* starts or stops a listener (`sync_listeners`).

Instances keep running as separate venv subprocesses on their own internal
ports — the proxy only makes those ports invisible from the outside.
Responses are streamed through (streamable HTTP / SSE safe).
"""
import asyncio
import os
import socket
from typing import Optional

import httpx
import uvicorn

from . import category_endpoint
from .config_store import get_instance_state, load_all_configs
from .schema import MCPStatus
from .settings_store import load_settings
from .logger import get_manager_logger

logger = get_manager_logger()

# port -> {"server": uvicorn.Server, "task": asyncio.Task}
_listeners: dict[int, dict] = {}
_client: Optional[httpx.AsyncClient] = None
# The client for the manager-port path. Separate on purpose: that path outlives
# every start_proxy()/stop_proxy() cycle, and must not go 503 because somebody
# switched the shared port off.
_dispatch_client: Optional[httpx.AsyncClient] = None

# What the dispatcher in admin_server hands to `handle()`. `/mcp/category/` is
# checked before it, so a category endpoint is never shadowed by an instance —
# and "category" is a reserved instance ID (api_helpers.RESERVED_IDS).
PREFIX = "/mcp/"

# Headers that must not be forwarded verbatim in either direction
_SKIP_REQUEST_HEADERS = {b"host", b"content-length", b"transfer-encoding", b"connection"}
_SKIP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "date", "server"}


def _port_setting(key: str) -> Optional[int]:
    port = load_settings().get(key)
    if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
        return port
    return None


def configured_port() -> Optional[int]:
    """The shared port from the settings file, or None when disabled."""
    return _port_setting("shared_port")


def category_port() -> Optional[int]:
    """The port the category endpoints get for themselves, or None.

    Read here as well as in `category_endpoint` because this module decides
    which listeners exist, and that is a question about ports, not features.
    """
    return _port_setting("category_port")


def wanted_ports() -> set[int]:
    """Every port that should have a listener right now."""
    return {p for p in (configured_port(), category_port()) if p}


def listener_running(port: Optional[int]) -> bool:
    entry = _listeners.get(port) if port else None
    return bool(entry) and not entry["task"].done()


def proxy_running() -> bool:
    """Whether the shared instance port is up. Kept for the settings route."""
    return listener_running(configured_port())


def instances_localhost_only() -> bool:
    """Whether the runners are force-bound to loopback.

    True as soon as there is a way in that does not need their own port. Both
    switches promise "reachable through one port", and that promise is only
    kept if the instance ports stop answering from outside — otherwise the
    second door stays open and nobody asked for it to.

    `process_manager` reads this when starting a runner, and the settings route
    restarts what is running when the answer changes.
    """
    return configured_port() is not None or manager_port_enabled()


def advertised_port() -> Optional[int]:
    """The port to hand out for `/mcp/<id>`, or None for the instance's own.

    What the dashboard shows and the OpenWebUI export writes. A port of its own
    wins over the manager port: it is the more deliberate of the two and keeps
    answering if the manager port is later switched off. None means neither way
    in is on, so the instance's own address is all there is — and handing out an
    address that does not answer would be worse than handing out the direct one.
    """
    own = configured_port()
    if own:
        return own
    if manager_port_enabled():
        return int(os.environ.get("MCP_MANAGER_PORT", "7860"))
    return None


def bind_host() -> str:
    """Where a listener of ours binds.

    The manager's own host first. `manager.py` sets both variables to `--host`,
    so on a normally started manager they agree; the separate name exists
    because a listener meant to be the public way in should not hang on
    `MCP_RUNNER_HOST`, which is about where *instances* bind. Loopback is the
    last resort — and what a manager started by hand with uvicorn gets, since
    that sets neither.
    """
    return (os.environ.get("MCP_MANAGER_HOST")
            or os.environ.get("MCP_RUNNER_HOST")
            or "127.0.0.1")


def manager_port_enabled() -> bool:
    """Whether `/mcp/<id>` is served on the manager's own port. **Off by default.**

    Off for the reason the category endpoints are off: an instance that binds
    to localhost is reachable from outside the moment this is on, and a new way
    in is opened by a person, not by a version number. It opens no port and
    needs no credential of its own — the instance authenticates the caller
    exactly as it does on a direct connection.

    Read on every request rather than cached: switching takes effect now.
    """
    return bool(load_settings().get("instance_endpoints_enabled", False))


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        # read=None: SSE streams stay open indefinitely
        timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
        limits=httpx.Limits(max_connections=100),
    )


def port_conflicts(port: int) -> list[str]:
    """Return IDs of instances whose internal listener uses *port*.

    The proxy and an instance cannot share a TCP port, even when the instance
    is currently stopped. Checking persisted configs prevents enabling the
    proxy successfully only to make that instance fail on its next start.
    """
    return sorted(
        cfg.id for cfg in load_all_configs().values()
        if cfg.server.port == port
    )


async def _send_error(send, status: int, message: str) -> None:
    body = message.encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


def _target_host(inst) -> str:
    """Where this manager reaches the instance's own listener.

    In shared-port mode `process_manager` force-binds every runner to
    127.0.0.1 regardless of the host in its config — `inst.host` still holds
    the config value (e.g. "::1") and must not be trusted then. Without a
    shared port the runner bound what `MCP_RUNNER_HOST` said at start time, or
    else its own config host, and that is the address to dial. A wildcard is
    not an address: 0.0.0.0 and :: are reached on their loopback.
    """
    if instances_localhost_only():
        return "127.0.0.1"
    host = os.environ.get("MCP_RUNNER_HOST") or inst.host or "127.0.0.1"
    host = {"0.0.0.0": "127.0.0.1", "::": "::1", "::0": "::1"}.get(host, host)
    return f"[{host}]" if ":" in host else host


async def _forward(scope, receive, send, client: httpx.AsyncClient) -> None:
    """`/mcp/<instance_id>[/<tail>]` → the instance, streamed through.

    Both ways in share this: the shared-port listener below and the
    manager-port dispatcher in `handle()`. Neither checks any rights — the
    caller's headers travel unchanged and the instance decides, as it would on
    a direct connection.
    """
    # Expected path: /mcp/<instance_id>[/<tail>]
    parts = scope["path"].split("/", 3)
    if len(parts) < 3 or parts[1] != "mcp" or not parts[2]:
        await _send_error(send, 404, "Not Found — use /mcp/<instance-id>")
        return
    instance_id = parts[2]
    tail = "/" + parts[3] if len(parts) > 3 and parts[3] else ""

    inst = get_instance_state(instance_id)
    if not inst:
        await _send_error(send, 404, f"Unknown MCP instance '{instance_id}'")
        return
    if inst.status != MCPStatus.running:
        await _send_error(send, 503, f"MCP instance '{instance_id}' is not running")
        return

    # Instances not yet restarted after a mode switch are mid-rebind and
    # briefly unreachable either way.
    target = f"http://{_target_host(inst)}:{inst.port}{inst.endpoint.rstrip('/')}{tail}"
    query = scope.get("query_string", b"")
    if query:
        target += "?" + query.decode("latin-1")

    headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() not in _SKIP_REQUEST_HEADERS]

    async def request_body():
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                if msg.get("body"):
                    yield msg["body"]
                if not msg.get("more_body"):
                    return
            elif msg["type"] == "http.disconnect":
                return

    try:
        req = client.build_request(
            scope["method"], target,
            headers=[(k.decode("latin-1"), v.decode("latin-1")) for k, v in headers],
            content=request_body(),
        )
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        await _send_error(send, 502, f"Upstream MCP instance unreachable: {e}")
        return
    except RuntimeError:
        # The client was closed under us — the shared port was switched off, or
        # the manager-port path was, while this request was being built.
        await _send_error(send, 503, "MCP proxy is shutting down")
        return

    try:
        await send({
            "type": "http.response.start",
            "status": resp.status_code,
            "headers": [
                (k.encode("latin-1"), v.encode("latin-1"))
                for k, v in resp.headers.multi_items()
                if k.lower() not in _SKIP_RESPONSE_HEADERS
            ],
        })
        async for chunk in resp.aiter_raw():
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    except Exception:
        pass  # client disconnected mid-stream — nothing to salvage
    finally:
        await resp.aclose()


def _listener_app(port: int):
    """The ASGI app of the listener on *port*.

    What it serves is read from the settings on every request, not captured
    when the listener starts: switching a role between the manager port and
    this one, or between two ports that both already have a listener, then
    takes effect at once — the same rule the switches themselves follow.

    Both roles can land on the same port; the category prefix is longer and is
    checked first, exactly as in the dispatcher on the manager port.
    """
    async def app(scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return

        path = scope.get("path", "")
        serves_categories = category_port() == port
        serves_instances = configured_port() == port

        if serves_categories and path.startswith(category_endpoint.prefix()):
            await category_endpoint.handle(scope, receive, send)
            return
        if serves_instances:
            # Snapshot the client: a listener going down sets the global to
            # None while in-flight requests may still be running.
            client = _client
            if client is None:
                await _send_error(send, 503, "MCP proxy is shutting down")
                return
            await _forward(scope, receive, send, client)
            return

        # A port that only serves categories, asked for something else. Naming
        # the one thing this port does is more use than a bare 404 — at the
        # other end may be a model that has to decide what to try next.
        if serves_categories:
            await _send_error(send, 404,
                              f"Not Found — this port serves {category_endpoint.prefix()}<category-name>")
        else:
            await _send_error(send, 404, "Not Found")

    return app


async def handle(scope, receive, send) -> None:
    """Serve `/mcp/<id>` on the manager port — the dispatcher's second branch.

    The switch is checked by the dispatcher in `admin_server.asgi`, not here:
    switched off the path is not intercepted at all and ends in the ordinary
    404 of a URL this manager does not serve, rather than in a 403 that tells
    an unauthenticated caller the feature is there and merely closed. Same
    reasoning as the category endpoints next to it.

    The client is built on first use and lives as long as the manager: this
    path has no listener to hang its lifetime on, and building one per request
    would throw away every kept-alive connection to the instances.
    """
    if scope["type"] != "http":
        return
    global _dispatch_client
    if _dispatch_client is None:
        # No await between the check and the assignment — two concurrent
        # requests cannot both get past it.
        _dispatch_client = _new_client()
    await _forward(scope, receive, send, _dispatch_client)


async def stop_dispatch_client() -> None:
    """Drop the manager-port client: manager shutdown, or the switch going off.

    In-flight streams die with it, which is what "off" has to mean — the
    category endpoints take their sessions down the same way.
    """
    global _dispatch_client
    client, _dispatch_client = _dispatch_client, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass


async def _serve_guarded(server: uvicorn.Server, sock: socket.socket) -> None:
    """uvicorn calls sys.exit(1) on startup failure; a SystemExit escaping a
    task kills the whole event loop — and with it the manager. Contain it."""
    try:
        await server.serve(sockets=[sock])
    except (SystemExit, Exception) as e:
        logger.error(f"Shared-port listener stopped unexpectedly: {e!r}")
    finally:
        try:
            sock.close()
        except OSError:
            pass


async def start_listener(port: int, host: str) -> tuple[bool, str]:
    """Bring up the listener on *port*. Returns (ok, error).

    Idempotent: a port that already has a live listener is left alone, because
    what it serves is a settings question and not a property of the socket.
    """
    if listener_running(port):
        return True, ""
    await stop_listener(port)

    conflicts = port_conflicts(port)
    if conflicts:
        instances = ", ".join(conflicts)
        err = (f"MCP port {port} conflicts with the internal port of: {instances}. "
               "Choose a different port.")
        logger.error(err)
        return False, err

    # Bind the socket ourselves so a taken port is a clean, synchronous error
    # instead of a sys.exit(1) inside the serve task.
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(128)
    except OSError as e:
        sock.close()
        err = f"Could not bind MCP port {port}: {e.strerror or e}"
        logger.error(err)
        return False, err

    global _client
    if _client is None:
        _client = _new_client()

    config = uvicorn.Config(_listener_app(port), host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(_serve_guarded(server, sock))
    _listeners[port] = {"server": server, "task": task}

    for _ in range(40):
        if server.started:
            logger.info(f"MCP port active on {host}:{port}")
            return True, ""
        if task.done():
            break
        await asyncio.sleep(0.05)

    err = f"MCP port listener on {port} failed to start"
    logger.error(err)
    await stop_listener(port)
    return False, err


async def stop_listener(port: int) -> None:
    """Take the listener on *port* down, and the shared client with the last one."""
    entry = _listeners.pop(port, None)
    if entry is not None:
        entry["server"].should_exit = True
        try:
            await asyncio.wait_for(entry["task"], timeout=5)
        except asyncio.CancelledError:
            # Not an `Exception`, so it would walk straight out of here — and
            # out through whatever route asked for the shutdown.
            if not entry["task"].cancelled():
                raise
        except (asyncio.TimeoutError, TimeoutError):
            entry["task"].cancel()
        except Exception:
            pass
    await _close_client_if_idle()


async def _close_client_if_idle() -> None:
    global _client
    if _listeners or _client is None:
        return
    client, _client = _client, None
    try:
        await client.aclose()
    except Exception:
        pass


async def sync_listeners(host: Optional[str] = None) -> list[str]:
    """Make the running listeners match the settings. Returns what failed.

    The single entry point: startup, the watchdog and the settings route all
    say "make it so" rather than each working out which listener to start or
    stop. A port that keeps its listener is never touched, so changing *what*
    a port serves does not interrupt anything running on it.
    """
    host = host or bind_host()
    wanted = wanted_ports()
    errors = []
    for port in [p for p in _listeners if p not in wanted]:
        await stop_listener(port)
    for port in sorted(wanted):
        if not listener_running(port):
            ok, err = await start_listener(port, host)
            if not ok:
                errors.append(err)
    return errors


async def start_proxy(port: int, host: str) -> tuple[bool, str]:
    """The shared instance port, by its old name — the settings route's entry."""
    return await start_listener(port, host)


async def stop_proxy() -> None:
    """Every listener this module owns. Called from the manager's shutdown."""
    for port in list(_listeners):
        await stop_listener(port)
    await _close_client_if_idle()
