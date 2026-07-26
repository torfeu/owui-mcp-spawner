import os
import threading
from fastapi import APIRouter, Depends, HTTPException
from .. import shared_proxy
from ..api_helpers import _rebind_running_instances, _restart_after_delay, require_token_edit, str_field
from ..auth import auth_enabled, edit_mode, edit_mode_locked, mcp_bearer_token, require_auth, token_edit_enabled
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
        "token_edit_enabled": token_edit_enabled(),
        "shared_port": shared_proxy.configured_port(),
        "shared_proxy_running": shared_proxy.proxy_running(),
    }

@router.put("/api/settings", dependencies=[Depends(require_auth)])
async def update_settings(body: dict) -> dict:
    from ..auth import set_password, set_edit_mode_setting, set_mcp_bearer_token, verify_password
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
            new_token = ("clear", None)
        else:
            token = _str_field("mcp_token")
            if len(token) < 8:
                raise HTTPException(400, "MCP token must be at least 8 characters")
            new_token = ("set", token)

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

    if new_token is not None:
        if new_token[0] == "clear":
            set_mcp_bearer_token(None)
            changed.append("mcp_token_cleared")
        else:
            set_mcp_bearer_token(new_token[1])
            changed.append("mcp_token")

    return {"ok": True, "changed": changed}

@router.get("/api/settings/mcp-token", dependencies=[Depends(require_auth), Depends(require_token_edit)])
async def get_mcp_token_value() -> dict:
    return {"token": mcp_bearer_token() or ""}

@router.post("/api/server/restart", dependencies=[Depends(require_auth)])
async def restart_server_endpoint() -> dict:
    threading.Thread(target=_restart_after_delay, daemon=True).start()
    return {"ok": True}
