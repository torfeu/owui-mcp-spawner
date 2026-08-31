"""Call one tool of one instance, from the dashboard, and show the raw answer.

Until now a tool that misbehaved in a chat left two candidates and no way to
tell them apart: the tool is broken, or the small local model called it wrong.
This makes the first one checkable in seconds — pick a function, fill the
parameters in, see exactly what comes back, including how long the answer
actually is. That length is the recurring problem with the local model: a
default that returns too much gets truncated, and a truncated result makes that
model invent a cause.

The client is the health check's, one step further. `health.probe()` already
does initialize + list_tools over the instance's own MCP endpoint, with the
Bearer token, with the port-recycling guard and with `_probe_url()`, which
knows about the shared port; the only thing missing here was `call_tool`. Both
therefore share `_probe_url()` and `_reason()` rather than growing a second
copy that drifts.

Two things are deliberately *not* like the probe:

  * **Time.** HEALTH_TIMEOUT is 8 s because a runner that answers slower than
    that is not well. A real call is allowed to be slow — a document is
    generated, a remote API is asked — so it gets a budget of its own.
  * **Identity.** The probe carries none, which is why an instance in
    `identity_mode: required` answers it with an empty catalog. A test call
    that behaved the same way would be useless on exactly the instances one
    most wants to test, so it can be made *as* somebody: the manager signs a
    short-lived user token with the shared secret (app/identity.py
    `sign_user_jwt`). That doubles as the answer to "what does this user
    actually get to see?", which the rights dialog can otherwise only promise.

    An agent identity is signed the same way. Its own token cannot be used —
    those are stored hashed and cannot be reconstructed — but the runner
    resolves both paths to the same `Identity`, so the rules that apply are the
    same ones.
"""
import asyncio
import time
from typing import Optional

from .auth import mcp_bearer_token
from .health import _probe_url, _reason
from .identity import (IdentityError, PLAIN_HEADERS, configured_secret, header_name,
                       sign_user_jwt, trust_plain_headers)
from .logger import get_manager_logger

logger = get_manager_logger()

# Generous where the health check is strict, but not unbounded: the request
# holds a worker of the admin server, and a tool that needs longer than this
# needs a log, not a dialog.
CALL_TIMEOUT = 60.0

# The answer is rendered in a browser and is meant to be *read*. A tool that
# returns half a megabyte is itself the finding — the cut is reported, so the
# number stays true even when the text does not.
MAX_TEXT = 100_000


def identity_possible() -> bool:
    """Can this server call *as* somebody at all?

    False when neither a user-JWT secret nor trusted plain headers are
    configured — then the dropdown in the dialog has nothing to offer and says
    so, instead of failing once per attempt.
    """
    return bool(configured_secret()) or trust_plain_headers()


def _identity_headers(who: Optional[dict]) -> dict:
    """Headers that make the runner see *who*. Raises IdentityError.

    A signed token when there is a secret; otherwise the unsigned OpenWebUI
    headers, but only where the server has been told to trust them. Sending
    plain headers to an instance that ignores them would produce a silent
    "access denied" whose reason is nowhere on screen.
    """
    if not who or not str(who.get("sub", "")).strip():
        return {}
    if configured_secret():
        token = sign_user_jwt(
            who["sub"], email=who.get("email", ""), name=who.get("name", ""),
            role=who.get("role", ""),
        )
        return {header_name(): token}
    if trust_plain_headers():
        headers = {PLAIN_HEADERS["sub"]: str(who["sub"])}
        for key in ("email", "name", "role"):
            if who.get(key):
                headers[PLAIN_HEADERS[key]] = str(who[key])
        return headers
    raise IdentityError(
        "this server cannot call as a user: no user-JWT secret is configured and "
        "unsigned user headers are not trusted"
    )


def _text_of(content) -> str:
    """The readable part of one content block.

    Text is the overwhelming case. Everything else — images, embedded
    resources — is named rather than rendered: the panel exists to show what a
    chat would receive, and "1 image" is that answer honestly.
    """
    kind = getattr(content, "type", "") or ""
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    if kind:
        return f"<{kind}>"
    return str(content)


def _unpack(result) -> dict:
    blocks = list(getattr(result, "content", None) or [])
    parts = [_text_of(block) for block in blocks]
    text = "\n".join(parts)
    full_length = len(text)
    truncated = full_length > MAX_TEXT
    structured = getattr(result, "structured_content", None)
    return {
        "is_error": bool(getattr(result, "is_error", False)),
        "text": text[:MAX_TEXT],
        "chars": full_length,
        "truncated": truncated,
        "blocks": [{"type": getattr(block, "type", "") or "text"} for block in blocks],
        # Only when the tool actually returned one; None keeps the panel from
        # showing an empty "structured result" box for every ordinary tool.
        "structured": structured if isinstance(structured, (dict, list)) else None,
    }


async def call(inst, tool: str, arguments: dict, who: Optional[dict] = None,
               timeout: float = CALL_TIMEOUT) -> dict:
    """Run *tool* on *inst* and report what came back. Never raises.

    The return value always has the same shape: `ok` says whether the call
    reached the tool at all, `is_error` whether the tool itself reported a
    failure. The two are different findings — "the instance did not answer" and
    "the tool answered with an error" send you to different places.
    """
    from mcp import ClientSession
    from mcp.client import streamable_http as transport

    token = mcp_bearer_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        headers.update(_identity_headers(who))
    except IdentityError as e:
        return {"ok": False, "error": str(e), "duration_ms": 0}

    started = time.monotonic()
    try:
        async with transport.httpx2.AsyncClient(headers=headers, timeout=timeout) as client:
            async with transport.streamable_http_client(_probe_url(inst), http_client=client) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    served = getattr(getattr(init, "server_info", None), "name", "")
                    if served and served != inst.id:
                        # Same guard as the probe: ports get recycled, and a
                        # test call landing on a neighbour's tool would be the
                        # most confusing possible answer.
                        return {"ok": False, "duration_ms": _ms(started),
                                "error": f"Port {inst.port} now serves '{served}'"}
                    result = await session.call_tool(tool, arguments or {},
                                                     read_timeout_seconds=timeout)
                    if not hasattr(result, "content"):
                        # mcp 2.x can answer a call with something other than a
                        # CallToolResult (an input request, for instance). No
                        # tool here asks for input, so this is a finding, not a
                        # flow to support.
                        return {"ok": False, "duration_ms": _ms(started),
                                "error": f"unexpected result type {type(result).__name__}"}
                    return {"ok": True, "duration_ms": _ms(started), "error": "",
                            **_unpack(result)}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"ok": False, "duration_ms": _ms(started), "error": _reason(e)[:500]}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
