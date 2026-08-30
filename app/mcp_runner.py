"""
Runs a single MCP instance. Started as a subprocess by the process manager.
Usage: python app/mcp_runner.py --config configs/mcp1.json
"""
import argparse
import asyncio
import hmac
import inspect
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import content_store
from app.activity import flush as flush_usage, record_call
from app.identity import (PLAIN_HEADERS, Identity, IdentityError, configured_secret,
                          header_name, identity_configured, identity_from_headers,
                          identity_scope, trust_plain_headers)
from app.identity_registry import record as record_identity
from app.policy import is_tool_allowed, visible_tools
from app.schema import IdentityMode, MCPConfig
from app.tool_loader import load_openwebui_json, create_tools_instance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcp_runner")


def load_config(config_path: str) -> MCPConfig:
    raw = json.loads(Path(config_path).read_text())
    return MCPConfig.model_validate(raw)


def resolve_path(p: str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def _request_headers(ctx) -> dict:
    """Headers of the HTTP request that carried the current MCP message.

    The SDK hands the Starlette request down with every JSON-RPC message
    (streamable_http → ServerMessageMetadata → ServerRequestContext.request),
    and each message is dispatched in its own task. So this is per call, not
    per session — which matters, because one MCP session can carry calls made
    seconds apart by a client that re-authenticates in between.

    Since mcp 2.x the context arrives as the handler's first argument instead
    of being fetched off the server, so there is no ContextVar to miss: outside
    an HTTP context (stdio, tests) `request` is simply None.

    Empty dict when there is no request to read.
    """
    request = getattr(ctx, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return {}
    try:
        return dict(headers.items())
    except Exception:
        return {}


def _claims_an_identity(headers: dict) -> bool:
    """Did the caller claim to be somebody, even unsuccessfully?

    The difference between "no login, as configured" and "a token that did not
    hold up". Only the first may fall back to a machine identity — otherwise a
    forged or expired token would be quietly upgraded into a working one.

    Compared lowercased: ASGI, Starlette and httpx each hand headers over with
    their own capitalisation, and getting this wrong would open exactly the
    hole the distinction exists to close.
    """
    lowered = {str(k).lower() for k, v in (headers or {}).items() if str(v).strip()}
    if header_name().lower() in lowered:
        return True
    return trust_plain_headers() and PLAIN_HEADERS["sub"].lower() in lowered


def _resolve_identity(ctx, mode: IdentityMode, instance_id: str = "",
                      machine: Optional[Identity] = None) -> tuple[Optional[Identity], str]:
    """Establish who is calling. Returns (identity, rejection reason).

    An identity of None with an empty reason means "none was offered and none
    was needed". A reason without an identity is a refusal the caller should
    hear about — with the *reason*, never the token.

    *machine* stands in when no user token arrives: a configured identity for
    callers without a login. A real token always wins over it, and a *broken*
    token is still a refusal — falling back to the machine identity there would
    turn a forged token into a working one.
    """
    if mode == IdentityMode.off:
        return None, ""
    if not identity_configured() and machine is None:
        # required with no way to establish an identity is a misconfiguration,
        # not an open door.
        if mode == IdentityMode.required:
            return None, ("this instance requires an identified user, but the server "
                          "has neither a user-JWT secret nor trusted user headers "
                          "configured")
        return None, ""
    headers = _request_headers(ctx)
    try:
        identity = identity_from_headers(headers)
    except IdentityError as e:
        if machine is not None and not _claims_an_identity(headers):
            # Nobody claimed to be anybody — this is the agent-CLI case.
            record_identity(machine, instance_id)
            return machine, ""
        if mode == IdentityMode.required:
            return None, str(e)
        # optional: an absent or broken token is not fatal, but silence here
        # would make a wrong secret look like a working setup.
        logger.info(f"No verified user identity ({e}) — continuing, identity_mode=optional")
        return None, ""
    # Noted for the roster the dashboard offers when assigning rights —
    # bookkeeping only, and it never fails a call.
    record_identity(identity, instance_id)
    return identity, ""


def require_mcp_2() -> None:
    """Stop with an instruction instead of a stack trace when mcp is too old.

    The runner speaks the 2.x low-level API (handlers as constructor arguments
    of Server). Against the 1.x line the first thing that happens is a
    TypeError from inside the SDK, which says nothing about the actual problem.
    Every venv built before this port is in exactly that state: ensure_venv()
    never re-installs into a venv it has once marked ready, so the old `mcp<2`
    stays there until somebody upgrades it.

    This is the same lesson as the 2.0 breakage in the other direction — four
    weeks of broken fresh installs because a version mismatch surfaced as an
    AttributeError nobody connected to pip.
    """
    try:
        from importlib.metadata import version
        installed = version("mcp")
        major = int(installed.split(".")[0])
    except Exception:
        return  # cannot tell: not a reason to refuse to start
    if major < 2:
        logger.error(
            f"mcp {installed} in this venv, but the runner needs mcp >= 2. "
            f"Upgrade it: {sys.executable} -m pip install -U 'mcp>=2' "
            f"(venv: {Path(sys.executable).parent.parent})"
        )
        sys.exit(1)


def build_server(cfg: MCPConfig, mcp_tool_defs: list[dict], tools_instance):
    """Wire the tool methods up as MCP handlers.

    Separate from run_server so the access decisions below can be tested
    without a socket: they are the gate the whole per-user setup rests on.

    Since mcp 2.x the handlers are constructor arguments rather than decorated
    functions, and each one is handed the request context of the call it is
    serving — which is where the forwarded user token is read from.
    """
    from mcp.server import Server
    from mcp import types

    allowed_tools = {t["name"] for t in mcp_tool_defs}
    identity_mode = cfg.identity_mode
    machine_identity = cfg.machine_identity.as_identity() if cfg.machine_identity else None
    content_enabled = cfg.content.enabled
    content_prefix = cfg.content.url_prefix

    def with_content_notes(text: str) -> str:
        """Rewrite the tool's own file links, and say so when storage runs out.

        The warning goes *in front of* the result and never after it. A tool
        result is often an instruction block addressed to the model ("copy
        exactly the text between the lines") — anything appended below that
        gets swallowed by the instruction and is never seen.
        """
        if not content_enabled:
            return text
        text = content_store.rewrite_links(text, cfg.id, content_prefix)
        warning = content_store.quota_warning(cfg.id)
        return f"{warning}\n\n{text}" if warning else text

    def answer(text: str) -> "types.CallToolResult":
        """A refusal or a result — both travel as plain content.

        Deliberately not is_error: on the other end of this is usually a small
        model that has to *read* why it was turned away, and an error result is
        what clients hand to their own error handling instead of to the model.
        """
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    async def handle_list_tools(ctx, params) -> "types.ListToolsResult":
        defs = mcp_tool_defs
        if identity_mode != IdentityMode.off:
            identity, refusal = _resolve_identity(ctx, identity_mode, cfg.id, machine_identity)
            if refusal:
                # An empty catalog rather than an error: a client that cannot
                # identify its user should see nothing to call, and the refusal
                # it gets from call_tool explains why.
                logger.warning(f"list_tools refused: {refusal}")
                return types.ListToolsResult(tools=[])
            # Rules apply in required mode only. In optional mode an identity
            # is passed on but nothing is enforced — otherwise switching an
            # instance to optional before writing a policy would leave
            # identified users with less access than anonymous ones, which is
            # the opposite of what the mode is for.
            if identity_mode == IdentityMode.required:
                permitted = set(visible_tools(identity, cfg.id, sorted(allowed_tools)))
                defs = [t for t in defs if t["name"] in permitted]
        return types.ListToolsResult(tools=[
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in defs
        ])

    async def handle_call_tool(ctx, params) -> "types.CallToolResult":
        name = params.name
        arguments = params.arguments or {}
        # Only methods advertised via list_tools are callable — getattr alone
        # would also expose private helpers and inherited methods.
        if name not in allowed_tools:
            return answer(f"Tool '{name}' not found")

        identity = None
        if identity_mode != IdentityMode.off:
            identity, refusal = _resolve_identity(ctx, identity_mode, cfg.id, machine_identity)
            if refusal:
                logger.warning(f"Denied call to '{name}': {refusal}")
                return answer(f"Access denied: {refusal}.")
            # Checked here and not only in list_tools: a hidden tool is still
            # callable by name, so the listing is a courtesy and this is the
            # actual gate. The router in front of this enforces nothing.
            if identity_mode == IdentityMode.required and not is_tool_allowed(identity, cfg.id, name):
                who = identity.sub if identity else "unidentified caller"
                logger.warning(f"Denied call to '{name}' for {who}: not permitted by policy")
                return answer(
                    f"Access denied: you are not permitted to use '{name}' on this server."
                )

        # Refusing before the call, not after: the only enforcement that is
        # honest here. The tool's own open() calls cannot be intercepted
        # without touching its code, so once it runs it writes what it wants —
        # the call is the only place with a gate. Off by default (decision 6):
        # a warning the model can act on beats a refusal it cannot.
        if content_enabled and content_store.block_when_full() and content_store.is_full(cfg.id):
            logger.warning(f"Refused '{name}': content storage full")
            return answer(content_store.full_refusal(cfg.id))

        # Counted here and not in the proxy: with one port per instance the
        # manager is not in the data path at all.
        record_call(cfg.id, name)
        method = getattr(tools_instance, name, None)
        if method is None:
            return answer(f"Tool '{name}' not found")
        try:
            # The identity is published for exactly this call and taken down
            # again in the ContextVar's own finally. It must not be attached to
            # tools_instance: that object is shared by every concurrent caller.
            # asyncio.to_thread copies the context, so synchronous tools see it too.
            with identity_scope(identity):
                if inspect.iscoroutinefunction(method):
                    result = await method(**arguments)
                else:
                    result = await asyncio.to_thread(method, **arguments)
            return answer(with_content_notes(str(result)))
        except Exception as e:
            logger.error(f"Error calling {name}: {e}")
            # Through the same treatment: when a write fails because the folder
            # is full, the quota line is exactly the missing half of the story.
            return answer(with_content_notes(f"Error: {e}"))

    return Server(
        cfg.id,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


async def run_server(config_path: str, host_override: str | None = None) -> None:
    require_mcp_2()
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    import uvicorn

    cfg = load_config(config_path)
    if host_override:
        cfg.server.host = host_override
    logger.info(f"Starting MCP '{cfg.name}' on {cfg.server.host}:{cfg.server.port}")

    tool_path = resolve_path(cfg.tool_source.path)
    tool = load_openwebui_json(tool_path)
    if tool is None:
        logger.error(f"Failed to load tool source: {tool_path}")
        sys.exit(1)

    content_dir = None
    if cfg.content.enabled:
        folder = content_store.ensure_instance_dir(cfg.id)
        content_dir = str(folder) if folder is not None else None
        if content_dir:
            logger.info(
                f"Content store: {content_dir} — links starting with "
                f"'{cfg.content.url_prefix}' are rewritten to {content_store.instance_url(cfg.id)}/"
            )
    tools_instance = create_tools_instance(tool, cfg.values, content_dir)
    if tools_instance is None:
        logger.error("Failed to instantiate Tools class")
        sys.exit(1)

    mcp_tool_defs = tool.get_mcp_tool_defs()
    logger.info(f"Loaded {len(mcp_tool_defs)} tools: {sorted(t['name'] for t in mcp_tool_defs)}")

    if cfg.identity_mode != IdentityMode.off:
        proof = "signed token" if configured_secret() else "none"
        if trust_plain_headers():
            proof += " + trusted plain headers (unsigned)"
        logger.info(
            f"User identity: {cfg.identity_mode.value} (header {header_name()}, "
            f"accepted proof: {proof})"
        )

    server = build_server(cfg, mcp_tool_defs, tools_instance)

    endpoint = cfg.server.endpoint.rstrip("/")
    session_manager = StreamableHTTPSessionManager(server)
    mcp_auth_token = os.environ.get("MCP_BEARER_TOKEN") or None
    if mcp_auth_token:
        logger.info("MCP Bearer token authentication enabled")

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            async with session_manager.run():
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                event = await receive()
                if event["type"] == "lifespan.shutdown":
                    # Write out what is still queued — otherwise every stop
                    # would silently drop the last calls, and stopping is what
                    # happens ten times an evening.
                    await flush_usage()
                    await send({"type": "lifespan.shutdown.complete"})
            return

        # Bearer token check
        if mcp_auth_token:
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
            # RFC 7235: the auth scheme is case-insensitive
            provided = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(provided, mcp_auth_token):
                from starlette.responses import Response
                await Response(
                    "Unauthorized", status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return

        if scope.get("path", "").rstrip("/") == endpoint:
            await session_manager.handle_request(scope, receive, send)
            return
        from starlette.responses import Response
        await Response("Not Found", status_code=404)(scope, receive, send)

    config = uvicorn.Config(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )
    server_instance = uvicorn.Server(config)
    await server_instance.serve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default=None, help="Override bind host from config")
    args = parser.parse_args()
    asyncio.run(run_server(args.config, host_override=args.host))


if __name__ == "__main__":
    main()
