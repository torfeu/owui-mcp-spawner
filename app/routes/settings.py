import os
import threading
from fastapi import APIRouter, Depends, HTTPException
from .. import category_endpoint, health, shared_proxy
from ..api_helpers import _rebind_running_instances, _restart_after_delay, require_token_edit, str_field
from ..auth import (agent_token, auth_enabled, edit_mode, edit_mode_locked, mcp_bearer_token,
                    read_token, require_admin_auth, require_auth, token_edit_enabled,
                    user_jwt_secret, user_trust_headers)
from ..activity import retention_days
from ..content_store import (RETENTION_MODES, base_url as content_base_url,
                             block_when_full as content_block_when_full,
                             max_bytes as content_max_bytes,
                             retention_days as content_retention_days,
                             retention_mode as content_retention_mode,
                             warn_percent as content_warn_percent)
from ..update_check import cached_result, check_manually, check_now
router = APIRouter()

@router.get("/api/settings", dependencies=[Depends(require_auth)])
async def get_settings() -> dict:
    return {
        "auth_enabled": auth_enabled(),
        "edit_mode": edit_mode(),
        "edit_mode_locked": edit_mode_locked(),
        "host": os.environ.get("MCP_RUNNER_HOST", "127.0.0.1"),
        "port": int(os.environ.get("MCP_MANAGER_PORT", "7860")),
        "mcp_token_set": mcp_bearer_token() is not None,
        "read_token_set": read_token() is not None,
        "agent_token_set": agent_token() is not None,
        # Only whether it exists. The secret itself has no read route at all —
        # unlike the tokens, nobody needs to copy it out of here: it is set once
        # to match what OpenWebUI already has.
        "user_jwt_secret_set": user_jwt_secret() is not None,
        "user_trust_headers": user_trust_headers(),
        "token_edit_enabled": token_edit_enabled(),
        "shared_port": shared_proxy.configured_port(),
        "shared_proxy_running": shared_proxy.proxy_running(),
        # One category served as one MCP server on the manager port. Off by
        # default: one endpoint reaches a whole category at once, and an
        # upgrade must not open that door on its own.
        "category_endpoints_enabled": category_endpoint.enabled(),
        # The same forwarding the shared port does, on the manager's own port.
        # Off by default: it makes localhost-bound instances reachable from
        # outside, which is a door a person opens, not an upgrade.
        "instance_endpoints_enabled": shared_proxy.manager_port_enabled(),
        "usage_retention_days": retention_days(),
        # The health check. Auto-restart is off by default: restarting a tool
        # nobody asked to restart is a decision, not a convenience.
        "health_check_enabled": health.enabled(),
        "health_autorestart": health.autorestart(),
        "health_failures_before_restart": health.failures_before_restart(),
        "health_max_restarts": health.MAX_RESTARTS,
        # The content store. Quota and warning threshold apply per instance
        # folder — the warning ends up in front of a tool result, and only a
        # number about its own folder is something the model can act on.
        "content_max_mb": content_max_bytes() // (1024 * 1024),
        "content_warn_percent": content_warn_percent(),
        "content_block_when_full": content_block_when_full(),
        "content_retention_days": content_retention_days(),
        "content_retention_mode": content_retention_mode(),
        "content_base_url": content_base_url(),
        # Authenticated on purpose: /api/auth-status already leaks the bare
        # version to anyone, but "this instance is outdated" is the more useful
        # sentence for an unauthenticated visitor and stays behind the login.
        "update": cached_result(),
    }

# require_admin_auth: this route sets the password and both API tokens. An
# agent token that reached it could promote itself to admin, which would make
# the whole separation decorative.
@router.put("/api/settings", dependencies=[Depends(require_auth), Depends(require_admin_auth)])
async def update_settings(body: dict) -> dict:
    from ..auth import (set_agent_token, set_password, set_edit_mode_setting,
                        set_mcp_bearer_token, set_read_token, verify_password)
    changed = []

    def _str_field(key: str) -> str:
        return str_field(body.get(key))

    # ── Validate everything before applying anything: a rejected request must
    # not leave earlier fields (e.g. the password) already persisted while the
    # client treats the whole save as failed and keeps its old session token.
    pw = _str_field("password")
    if pw:
        if len(pw) < 4:
            raise HTTPException(400, "Password must be at least 4 characters")
        confirm = _str_field("password_confirm")
        if pw != confirm:
            raise HTTPException(400, "Passwords do not match")
        if auth_enabled():
            current_pw = _str_field("current_password")
            if not current_pw:
                raise HTTPException(400, "Current password required")
            if not verify_password(current_pw):
                raise HTTPException(401, "Current password is wrong")

    new_mode = None
    if "edit_mode" in body:
        mode = body["edit_mode"]
        if mode not in ("full", "upload", "readonly"):
            raise HTTPException(400, f"Invalid edit_mode: {mode}")
        # Only act on an actual change: the settings dialog always sends the
        # field, and an unchanged mode must not be persisted (it would freeze
        # a CLI-flag default into the settings file) or reported as changed.
        if mode != edit_mode():
            if edit_mode_locked():
                raise HTTPException(
                    403,
                    "Edit mode is fixed by a CLI flag (--no-edit / --no-code-edit) on this server",
                )
            new_mode = mode

    new_token = None  # ("clear", None) or ("set", token)
    if "mcp_token" in body or body.get("mcp_token_clear"):
        require_token_edit()
        if body.get("mcp_token_clear"):
            if mcp_bearer_token() is not None:
                new_token = ("clear", None)
        else:
            token = _str_field("mcp_token")
            if len(token) < 8:
                raise HTTPException(400, "MCP token must be at least 8 characters")
            # The dialog pre-fills the field with the current value and always
            # sends it back — re-saving the same token is not a change, and
            # reporting one fires "restart your instances" advice for nothing.
            if token != mcp_bearer_token():
                new_token = ("set", token)

    # The two API tokens have the same shape and the same rules — one loop, so
    # they cannot drift apart.
    api_token_changes = {}  # kind -> ("clear", None) | ("set", token)
    for kind, label in (("read", "Read token"), ("agent", "Agent token")):
        if f"{kind}_token" not in body and not body.get(f"{kind}_token_clear"):
            continue
        require_token_edit()
        current = read_token() if kind == "read" else agent_token()
        if body.get(f"{kind}_token_clear"):
            if current is not None:
                api_token_changes[kind] = ("clear", None)
            continue
        token = _str_field(f"{kind}_token")
        if len(token) < 8:
            raise HTTPException(400, f"{label} must be at least 8 characters")
        # Reusing the password would defeat the whole split: the token is
        # stored in clear text in every config that carries it.
        if token == pw or (auth_enabled() and verify_password(token)):
            raise HTTPException(400, f"{label} must differ from the password")
        # Same reason as the MCP token above: the dialog echoes the current
        # value back on every save, and that is not a change.
        if token != current:
            api_token_changes[kind] = ("set", token)

    # …and they must differ from each other, or the GET-only token silently
    # inherits the agent token's write access.
    def _other_token(kind: str):
        other = "agent" if kind == "read" else "read"
        if other in api_token_changes:
            return api_token_changes[other][1]
        return agent_token() if other == "agent" else read_token()

    for kind, (action, value) in api_token_changes.items():
        if action == "set" and value == _other_token(kind):
            raise HTTPException(400, "Read token and agent token must differ")

    # The user-JWT secret is not a token this server hands out — it is a copy of
    # what OpenWebUI signs with, so it is write-only here and has no minimum
    # length of our choosing beyond "not trivially short".
    jwt_secret_change = None  # ("clear", None) or ("set", secret)
    if "user_jwt_secret" in body or body.get("user_jwt_secret_clear"):
        require_token_edit()
        if body.get("user_jwt_secret_clear"):
            jwt_secret_change = ("clear", None)
        else:
            secret = _str_field("user_jwt_secret")
            if len(secret) < 16:
                raise HTTPException(400, "User-JWT secret must be at least 16 characters")
            jwt_secret_change = ("set", secret)

    trust_change = None
    if "user_trust_headers" in body:
        raw = body["user_trust_headers"]
        if not isinstance(raw, bool):
            raise HTTPException(400, "user_trust_headers must be true or false")
        if raw != user_trust_headers():
            trust_change = raw

    update_change = None
    if "update_check" in body:
        raw = body["update_check"]
        if not isinstance(raw, bool):
            raise HTTPException(400, "update_check must be true or false")
        from ..update_check import update_check_enabled
        if raw != update_check_enabled():
            update_change = raw

    retention_change = None
    if "usage_retention_days" in body:
        raw = body["usage_retention_days"]
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise HTTPException(400, "usage_retention_days must be a number")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, "usage_retention_days must be a number")
        if not 0 <= value <= 3650:
            raise HTTPException(400, "usage_retention_days must be between 0 (keep forever) and 3650")
        if value != retention_days():
            retention_change = value

    # ── Content store ─────────────────────────────────────────────────────
    # Collected, not applied: like everything above, a rejected request must
    # leave nothing half-saved.
    store_changes: dict = {}

    def _bounded_int(key: str, low: int, high: int, current: int) -> None:
        if key not in body:
            return
        raw = body[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise HTTPException(400, f"{key} must be a number")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be a number")
        if not low <= value <= high:
            raise HTTPException(400, f"{key} must be between {low} and {high}")
        if value != current:
            store_changes[key] = value

    _bounded_int("content_max_mb", 0, 1_000_000, content_max_bytes() // (1024 * 1024))
    _bounded_int("content_warn_percent", 1, 100, content_warn_percent())
    _bounded_int("content_retention_days", 0, 3650, content_retention_days())

    if "content_block_when_full" in body:
        raw = body["content_block_when_full"]
        if not isinstance(raw, bool):
            raise HTTPException(400, "content_block_when_full must be true or false")
        if raw != content_block_when_full():
            store_changes["content_block_when_full"] = raw

    if "content_retention_mode" in body:
        mode = str(body["content_retention_mode"]).strip()
        if mode not in RETENTION_MODES:
            raise HTTPException(
                400, f"content_retention_mode must be one of: {', '.join(RETENTION_MODES)}"
            )
        if mode != content_retention_mode():
            store_changes["content_retention_mode"] = mode

    if "content_base_url" in body:
        # Absolute on purpose: the links this builds end up in an OpenWebUI
        # chat, and a relative one would be resolved against OpenWebUI — which
        # serves nothing of ours under /content.
        url = str(body["content_base_url"]).strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(400, "content_base_url must start with http:// or https://")
        if len(url) > 300:
            raise HTTPException(400, "content_base_url is too long")
        if url != content_base_url():
            store_changes["content_base_url"] = url or None

    for key, current in (("health_check_enabled", health.enabled()),
                         ("health_autorestart", health.autorestart()),
                         ("category_endpoints_enabled", category_endpoint.enabled()),
                         ("instance_endpoints_enabled", shared_proxy.manager_port_enabled())):
        if key in body:
            raw = body[key]
            if not isinstance(raw, bool):
                raise HTTPException(400, f"{key} must be true or false")
            if raw != current:
                store_changes[key] = raw

    _bounded_int("health_failures_before_restart", 1, 20, health.failures_before_restart())

    shared_change = None  # ("disable", None) or ("enable", port)
    current_shared = shared_proxy.configured_port()
    if "shared_port" in body:
        raw = body["shared_port"]
        if raw in (None, "", 0):
            if current_shared is not None:
                shared_change = ("disable", None)
        else:
            try:
                port = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(400, "shared_port must be an integer")
            if not 1024 <= port <= 65535:
                raise HTTPException(400, "shared_port must be between 1024 and 65535")
            if port == int(os.environ.get("MCP_MANAGER_PORT", "7860")):
                raise HTTPException(409, "shared_port must differ from the manager port")
            if port != current_shared:
                shared_change = ("enable", port)

    # ── Apply. The proxy swap goes first: it is the only step that can still
    # fail (bind error) and must abort before any other setting is persisted.
    if shared_change:
        from ..settings_store import save_settings
        action, port = shared_change
        if action == "disable":
            await shared_proxy.stop_proxy()
            save_settings({"shared_port": None})
            changed.append("shared_port_disabled")
        else:
            ok, err = await shared_proxy.start_proxy(
                port, os.environ.get("MCP_RUNNER_HOST", "127.0.0.1")
            )
            if not ok:
                # Roll back to the previous listener if there was one
                if current_shared is not None:
                    await shared_proxy.start_proxy(
                        current_shared, os.environ.get("MCP_RUNNER_HOST", "127.0.0.1")
                    )
                raise HTTPException(409, err)
            save_settings({"shared_port": port})
            changed.append("shared_port")
        if action == "disable" or current_shared is None:
            # Mode switched (shared ↔ per-port): move instances between
            # localhost-only and the configured host.
            _rebind_running_instances()
            changed.append("instances_restarting")

    if pw:
        set_password(pw)
        changed.append("password")

    if new_mode is not None:
        set_edit_mode_setting(new_mode)
        changed.append("edit_mode")

    if retention_change is not None:
        from ..settings_store import save_settings
        save_settings({"usage_retention_days": retention_change})
        # Applied by the daily pruning task; shortening the window does not
        # delete anything before the next pass, and `totals` never at all.
        changed.append("usage_retention_days")

    if store_changes:
        from ..settings_store import save_settings
        save_settings(store_changes)
        changed.extend(sorted(store_changes))
        # Switching the category endpoints off takes their session managers
        # down with it. Without this a client that is already connected keeps
        # its session — the dispatcher would stop routing to it, so it could
        # not be used, but it would sit there until the manager restarts.
        if store_changes.get("category_endpoints_enabled") is False:
            await category_endpoint.stop_all()
        # Same for the instance endpoints: switching them off closes the client
        # that carries them, so a stream already running does not outlive the
        # switch. The shared-port listener has its own client and is untouched.
        if store_changes.get("instance_endpoints_enabled") is False:
            await shared_proxy.stop_dispatch_client()
        # Read fresh on every call, in both the manager and the runners — no
        # restart, unlike the identity settings above.

    if update_change is not None:
        from ..settings_store import save_settings
        if update_change:
            save_settings({"update_check": True})
            # Check straight away so switching it on gives an answer instead of
            # an empty badge until the next daily pass.
            await check_now(force=True)
        else:
            # Drop the cached result too: nothing stale should be served, and
            # switching off should leave no trace of the request behind.
            save_settings({
                "update_check": False, "update_latest_version": None,
                "update_html_url": None, "update_last_checked": None,
            })
        changed.append("update_check")

    if new_token is not None:
        if new_token[0] == "clear":
            set_mcp_bearer_token(None)
            changed.append("mcp_token_cleared")
        else:
            set_mcp_bearer_token(new_token[1])
            changed.append("mcp_token")

    for kind, (action, value) in api_token_changes.items():
        (set_read_token if kind == "read" else set_agent_token)(value)
        changed.append(f"{kind}_token" if action == "set" else f"{kind}_token_cleared")

    if trust_change is not None:
        from ..auth import set_user_trust_headers
        set_user_trust_headers(trust_change)
        changed.append("user_trust_headers")
        changed.append("restart_instances_to_apply")

    if jwt_secret_change is not None:
        from ..auth import set_user_jwt_secret
        set_user_jwt_secret(jwt_secret_change[1])
        # Running runners keep the value they inherited at spawn, so say so:
        # otherwise the next identity failure looks like a wrong secret.
        changed.append("user_jwt_secret" if jwt_secret_change[0] == "set" else "user_jwt_secret_cleared")
        changed.append("restart_instances_to_apply")

    return {"ok": True, "changed": changed}

@router.post("/api/settings/update-check", dependencies=[Depends(require_auth)])
async def run_update_check() -> dict:
    """"Check now" button: one immediate check, cache and 24 h interval ignored."""
    return await check_manually()

# require_admin_auth on both token routes: they return a credential verbatim,
# so the read-only token must not reach them.
@router.get("/api/settings/mcp-token", dependencies=[Depends(require_auth), Depends(require_admin_auth), Depends(require_token_edit)])
async def get_mcp_token_value() -> dict:
    return {"token": mcp_bearer_token() or ""}

@router.get("/api/settings/read-token", dependencies=[Depends(require_auth), Depends(require_admin_auth), Depends(require_token_edit)])
async def get_read_token_value() -> dict:
    return {"token": read_token() or ""}

@router.get("/api/settings/agent-token", dependencies=[Depends(require_auth), Depends(require_admin_auth), Depends(require_token_edit)])
async def get_agent_token_value() -> dict:
    return {"token": agent_token() or ""}

@router.post("/api/server/restart", dependencies=[Depends(require_auth)])
async def restart_server_endpoint() -> dict:
    threading.Thread(target=_restart_after_delay, daemon=True).start()
    return {"ok": True}
