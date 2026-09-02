"""
OWUI MCP Spawner entry point.
Usage: python app/manager.py [--host HOST] [--port PORT]
"""
import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="OWUI MCP Spawner")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--no-code-edit", action="store_true",
        help="Disable inline code editor. Upload, config edit and delete remain available.",
    )
    parser.add_argument(
        "--no-edit", action="store_true",
        help="Full read-only mode: disables upload, code editor, config edit and delete.",
    )
    parser.add_argument(
        "--mcp-token", default=None, metavar="TOKEN",
        help="Require this Bearer token on all MCP endpoints (overrides settings file).",
    )
    parser.add_argument(
        "--no-token-edit", action="store_true",
        help="Disable changing the MCP Bearer token and the API read token via the web UI.",
    )
    args = parser.parse_args()

    # Propagate bind host and port so the settings page can read them
    os.environ["MCP_RUNNER_HOST"] = args.host
    os.environ["MCP_MANAGER_PORT"] = str(args.port)

    # Edit mode: CLI flags > settings file > default "full". A CLI-set mode is
    # locked so the web UI / API cannot lift it at runtime.
    from app.settings_store import load_settings
    _file_settings = load_settings()
    if args.no_edit:
        os.environ["MCP_EDIT_MODE"] = "readonly"
        os.environ["MCP_EDIT_MODE_LOCKED"] = "1"
    elif args.no_code_edit:
        os.environ["MCP_EDIT_MODE"] = "upload"
        os.environ["MCP_EDIT_MODE_LOCKED"] = "1"
    elif _file_settings.get("edit_mode") and _file_settings["edit_mode"] != "full":
        os.environ["MCP_EDIT_MODE"] = _file_settings["edit_mode"]
        os.environ.pop("MCP_EDIT_MODE_LOCKED", None)
    else:
        os.environ.pop("MCP_EDIT_MODE", None)
        os.environ.pop("MCP_EDIT_MODE_LOCKED", None)

    # MCP Bearer Token: CLI > settings file > off
    if args.mcp_token:
        os.environ["MCP_BEARER_TOKEN"] = args.mcp_token
    elif _file_settings.get("mcp_bearer_token"):
        os.environ["MCP_BEARER_TOKEN"] = _file_settings["mcp_bearer_token"]
    else:
        os.environ.pop("MCP_BEARER_TOKEN", None)

    # User-JWT secret: env > settings file > off. Unlike the tokens above there
    # is no CLI flag — it is shared with another system's configuration, so it
    # belongs in a file or the unit's environment, not in a shell history.
    if not os.environ.get("MCP_USER_JWT_SECRET") and _file_settings.get("user_jwt_secret"):
        os.environ["MCP_USER_JWT_SECRET"] = _file_settings["user_jwt_secret"]
    if not os.environ.get("MCP_USER_TRUST_HEADERS") and _file_settings.get("user_trust_headers"):
        os.environ["MCP_USER_TRUST_HEADERS"] = "1"

    if args.no_token_edit:
        os.environ["MCP_NO_TOKEN_EDIT"] = "1"
    else:
        os.environ.pop("MCP_NO_TOKEN_EDIT", None)

    from app.auth import agent_token, configure_auth, configure_api_tokens, read_token
    auth_active = configure_auth()
    configure_api_tokens()

    if args.host not in ("127.0.0.1", "localhost", "::1") and not auth_active:
        print(
            "\n⚠  WARNING: Binding to a public/network interface without a password.\n"
            "   Set MCP_MANAGER_PASSWORD=<secret> to enable authentication.\n"
        )

    edit_label = {"readonly": "readonly", "upload": "upload-only"}.get(
        os.environ.get("MCP_EDIT_MODE", ""), "full"
    )
    mcp_token = os.environ.get("MCP_BEARER_TOKEN")
    mcp_label = "bearer-token" if mcp_token else "open"
    if mcp_token and args.no_token_edit:
        mcp_label += ", locked"
    auth_label = "enabled" if auth_active else "disabled"
    if auth_active:
        extra = [name for name, active in
                 (("read", read_token()), ("agent", agent_token())) if active]
        if extra:
            auth_label += f" + {'/'.join(extra)}-token"
    identity_label = "signed" if os.environ.get("MCP_USER_JWT_SECRET") else "off"
    if os.environ.get("MCP_USER_TRUST_HEADERS"):
        identity_label = "unsigned-headers" if identity_label == "off" else identity_label + "+headers"
    print(f"\nOWUI MCP Spawner starting on http://{args.host}:{args.port}"
          f"  [auth: {auth_label}, edit: {edit_label}, mcp-auth: {mcp_label},"
          f" user-identity: {identity_label}]\n")

    uvicorn.run(
        "app.admin_server:asgi",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
