"""
Shared-port reverse proxy: expose every MCP instance under one port.

When a shared port is configured (settings key "shared_port"), a second
uvicorn listener is started inside the manager process that forwards

    http://<host>:<shared_port>/mcp/<instance_id>[/<tail>]

to the instance's internal server at

    http://127.0.0.1:<instance_port><endpoint>[/<tail>]

Instances keep running as separate venv subprocesses on their own internal
ports — the proxy only makes those ports invisible from the outside.
Responses are streamed through (streamable HTTP / SSE safe).
"""
import asyncio
import socket
from typing import Optional

import httpx
import uvicorn

from .config_store import get_instance_state, load_all_configs
from .schema import MCPStatus
from .settings_store import load_settings
from .logger import get_manager_logger

logger = get_manager_logger()

_server: Optional[uvicorn.Server] = None
_task: Optional[asyncio.Task] = None
_client: Optional[httpx.AsyncClient] = None

# Headers that must not be forwarded verbatim in either direction
_SKIP_REQUEST_HEADERS = {b"host", b"content-length", b"transfer-encoding", b"connection"}
_SKIP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "date", "server"}


def configured_port() -> Optional[int]:
    """The shared port from the settings file, or None when disabled."""
    port = load_settings().get("shared_port")
    if isinstance(port, int) and 1024 <= port <= 65535:
        return port
    return None


def proxy_running() -> bool:
    return _task is not None and not _task.done()


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


async def _proxy_app(scope, receive, send) -> None:
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

    # Instances started in shared mode are force-bound to 127.0.0.1 by
    # process_manager regardless of their configured host — inst.host still
    # holds the config value (e.g. "::1") and must not be trusted here.
    # Instances not yet restarted after the mode switch are mid-rebind and
    # briefly unreachable either way.
    target = f"http://127.0.0.1:{inst.port}{inst.endpoint.rstrip('/')}{tail}"
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

    # Snapshot the client: stop_proxy() sets the global to None while
    # in-flight requests may still be running.
    client = _client
    if client is None:
        await _send_error(send, 503, "Shared MCP proxy is shutting down")
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


async def start_proxy(port: int, host: str) -> tuple[bool, str]:
    """Start (or restart) the shared-port listener. Returns (ok, error)."""
    global _server, _task, _client

    conflicts = port_conflicts(port)
    if conflicts:
        instances = ", ".join(conflicts)
        err = (
            f"Shared MCP port {port} conflicts with the internal port of: "
            f"{instances}. Choose a different shared port."
        )
        logger.error(err)
        return False, err

    await stop_proxy()

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
        err = f"Could not bind shared MCP port {port}: {e.strerror or e}"
        logger.error(err)
        return False, err

    _client = httpx.AsyncClient(
        # read=None: SSE streams stay open indefinitely
        timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
        limits=httpx.Limits(max_connections=100),
    )
    config = uvicorn.Config(_proxy_app, host=host, port=port, log_level="warning")
    _server = uvicorn.Server(config)
    _task = asyncio.create_task(_serve_guarded(_server, sock))

    for _ in range(40):
        if _server.started:
            logger.info(f"Shared MCP port active on {host}:{port} (/mcp/<id>)")
            return True, ""
        if _task.done():
            break
        await asyncio.sleep(0.05)

    err = f"Shared MCP port listener on {port} failed to start"
    logger.error(err)
    await stop_proxy()
    return False, err


async def stop_proxy() -> None:
    global _server, _task, _client
    if _server is not None:
        _server.should_exit = True
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            _task.cancel()
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
    _server = None
    _task = None
    _client = None
