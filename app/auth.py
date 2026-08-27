"""
Optional Bearer-token auth for the MCP Manager API.

Configuration (environment variables, evaluated at startup):
  MCP_MANAGER_PASSWORD       Plain-text password — hashed with SHA-256 at startup.
  MCP_MANAGER_PASSWORD_HASH  Pre-hashed SHA-256 hex digest (takes precedence).
  MCP_MANAGER_READ_TOKEN     Optional API token, valid for GET requests only.
  MCP_MANAGER_AGENT_TOKEN    Optional API token for agents that also write.

If neither variable is set:
  - Binding to 127.0.0.1  → auth disabled, access unrestricted (local-only).
  - Binding to 0.0.0.0    → startup warning; access still allowed but strongly
                             discouraged without a password.
"""
import hashlib
import hmac
import os
from typing import Optional

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_password_hash: Optional[str] = None   # SHA-256 hex digest, or None = no auth

_bearer = HTTPBearer(auto_error=False)


def configure_auth() -> bool:
    """Read env vars and set up the password hash. Returns True if auth is active."""
    global _password_hash
    raw_hash = os.environ.get("MCP_MANAGER_PASSWORD_HASH", "").strip()
    raw_pw   = os.environ.get("MCP_MANAGER_PASSWORD", "").strip()

    if raw_hash:
        _password_hash = raw_hash.lower()
        return True
    if raw_pw:
        _password_hash = hashlib.sha256(raw_pw.encode()).hexdigest()
        return True

    # Fall back to persisted settings (runtime/settings.json)
    try:
        from .settings_store import load_settings
        stored_hash = load_settings().get("password_hash")
        if stored_hash:
            _password_hash = stored_hash
            return True
    except Exception:
        pass

    _password_hash = None
    return False


def verify_password(plain: str) -> bool:
    """Return True if *plain* matches the active password hash."""
    if _password_hash is None:
        return True  # no auth set — nothing to verify
    return hmac.compare_digest(hashlib.sha256(plain.encode()).hexdigest(), _password_hash)


def set_password(plain: str) -> None:
    """Update the active password hash in memory and persist it to the settings file."""
    global _password_hash
    from .settings_store import save_settings
    new_hash = hashlib.sha256(plain.encode()).hexdigest() if plain else None
    _password_hash = new_hash
    save_settings({"password_hash": new_hash})


def set_edit_mode_setting(mode: str) -> None:
    """Update edit mode at runtime and persist to the settings file."""
    from .settings_store import save_settings
    if mode == "full":
        os.environ.pop("MCP_EDIT_MODE", None)
        save_settings({"edit_mode": None})
    else:
        os.environ["MCP_EDIT_MODE"] = mode
        save_settings({"edit_mode": mode})


# ── MCP Bearer Token ──────────────────────────────────────────────────────────

def mcp_bearer_token() -> Optional[str]:
    """Return the active MCP Bearer token, or None if MCP auth is disabled."""
    return os.environ.get("MCP_BEARER_TOKEN") or None


def token_edit_enabled() -> bool:
    """Return False when --no-token-edit was passed at startup."""
    return os.environ.get("MCP_NO_TOKEN_EDIT") != "1"


def set_mcp_bearer_token(token: Optional[str]) -> None:
    """Update the MCP Bearer token at runtime and persist it."""
    from .settings_store import save_settings
    if token:
        os.environ["MCP_BEARER_TOKEN"] = token
    else:
        os.environ.pop("MCP_BEARER_TOKEN", None)
    save_settings({"mcp_bearer_token": token})


# ── User-JWT secret ───────────────────────────────────────────────────────────
#
# Shared with OpenWebUI (its FORWARD_USER_INFO_HEADER_JWT_SECRET) so the runner
# can verify who is calling; see app/identity.py. Kept next to the MCP Bearer
# token because it travels the same way: manager environment, inherited by
# every runner subprocess at spawn time.
#
# Consequence worth knowing before rotating it: the runners read the value once
# at startup and survive a manager restart. A new secret takes effect per
# instance, on that instance's next start.

def user_jwt_secret() -> Optional[str]:
    """The active user-JWT secret, or None when identity verification is off."""
    return os.environ.get("MCP_USER_JWT_SECRET") or None


def set_user_jwt_secret(secret: Optional[str]) -> None:
    """Update the user-JWT secret at runtime and persist it."""
    from .settings_store import save_settings
    if secret:
        os.environ["MCP_USER_JWT_SECRET"] = secret
    else:
        os.environ.pop("MCP_USER_JWT_SECRET", None)
    save_settings({"user_jwt_secret": secret})


def user_trust_headers() -> bool:
    """Whether OpenWebUI's unsigned user headers are accepted as an identity."""
    from .identity import trust_plain_headers
    return trust_plain_headers()


def set_user_trust_headers(enabled: bool) -> None:
    """Switch the unsigned-header fallback on or off, and persist it."""
    from .settings_store import save_settings
    if enabled:
        os.environ["MCP_USER_TRUST_HEADERS"] = "1"
    else:
        os.environ.pop("MCP_USER_TRUST_HEADERS", None)
    save_settings({"user_trust_headers": True if enabled else None})


# ── API tokens ────────────────────────────────────────────────────────────────
#
# Two optional credentials for machines, so a tool does not have to carry the
# admin password — which grants everything and ends up in clear text inside an
# instance config:
#
#   read token   accepted on GET only  — the tool router, any read-only agent
#   agent token  accepted on any method — the control tool, which writes
#
# Neither is accepted on the routes behind require_admin_auth: those hand out
# other credentials, or can set the password. A token that can rewrite the
# password *is* the password, so the agent token stops at that door.
#
# Both are optional and independent; unset means "password only", as before.

_READ_TOKEN_ENV = "MCP_MANAGER_READ_TOKEN"
_AGENT_TOKEN_ENV = "MCP_MANAGER_AGENT_TOKEN"
_TOKEN_SETTINGS_KEY = {_READ_TOKEN_ENV: "read_token", _AGENT_TOKEN_ENV: "agent_token"}


def _api_token(env: str) -> Optional[str]:
    return os.environ.get(env) or None


def _set_api_token(env: str, token: Optional[str]) -> None:
    from .settings_store import save_settings
    if token:
        os.environ[env] = token
    else:
        os.environ.pop(env, None)
    save_settings({_TOKEN_SETTINGS_KEY[env]: token})


def _verify_api_token(env: str, plain: str) -> bool:
    """False when the token is unset — an absent credential must never turn
    into "everything matches"."""
    active = _api_token(env)
    if not active:
        return False
    return hmac.compare_digest(plain.encode(), active.encode())


def read_token() -> Optional[str]:
    """The active read-only API token, or None if none is configured."""
    return _api_token(_READ_TOKEN_ENV)


def agent_token() -> Optional[str]:
    """The active agent (read/write) API token, or None if none is configured."""
    return _api_token(_AGENT_TOKEN_ENV)


def set_read_token(token: Optional[str]) -> None:
    _set_api_token(_READ_TOKEN_ENV, token)


def set_agent_token(token: Optional[str]) -> None:
    _set_api_token(_AGENT_TOKEN_ENV, token)


def verify_read_token(plain: str) -> bool:
    return _verify_api_token(_READ_TOKEN_ENV, plain)


def verify_agent_token(plain: str) -> bool:
    return _verify_api_token(_AGENT_TOKEN_ENV, plain)


def configure_api_tokens() -> None:
    """Load both API tokens from the settings file; a set env var wins."""
    try:
        from .settings_store import load_settings
        stored = load_settings()
    except Exception:
        return
    for env, key in _TOKEN_SETTINGS_KEY.items():
        if not os.environ.get(env) and stored.get(key):
            os.environ[env] = stored[key]


def _token_grants_access(token: str, method: str) -> bool:
    """The single place that decides which credential opens which request.

    Keeping it here rather than per route is what makes the read token honest:
    a mutating route cannot forget to opt out, because it is not a GET.
    """
    if verify_password(token):
        return True
    if verify_agent_token(token):
        return True
    return method == "GET" and verify_read_token(token)


async def is_request_authenticated(request) -> bool:
    """True when auth is disabled or the request carries a valid Bearer token.

    Used by public routes that serve a reduced guest view to anonymous callers.
    Parses the header through the same HTTPBearer instance as require_auth and
    applies the same rule, so both paths accept exactly the same tokens — the
    read token included, which would otherwise get the guest view (and no
    specs) on the very routes it exists for.
    """
    if _password_hash is None:
        return True
    credentials = await _bearer(request)
    if credentials is None:
        return False
    return _token_grants_access(credentials.credentials, request.method)


def auth_enabled() -> bool:
    return _password_hash is not None


def edit_mode() -> str:
    """Returns 'full' (default), 'upload' (no code editing), or 'readonly' (no mutations)."""
    return os.environ.get("MCP_EDIT_MODE", "full")


def edit_mode_locked() -> bool:
    """True when the edit mode was fixed by a CLI flag (--no-edit / --no-code-edit).

    A CLI-set mode is an operator guarantee — the web UI / API must not be able
    to lift it at runtime.
    """
    return os.environ.get("MCP_EDIT_MODE_LOCKED") == "1"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> None:
    """FastAPI dependency — raises 401 when auth is active and token is wrong.

    Accepts the password, the agent token, and the read token on GET requests
    (see _token_grants_access). Routes that serve credentials or can change
    them add require_admin_auth on top.
    """
    if _password_hash is None:
        return  # auth disabled

    token = credentials.credentials if credentials else None
    if not token:
        raise _unauthorized("Authentication required")
    if _token_grants_access(token, request.method):
        return
    raise _unauthorized("Invalid password")


def require_admin_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> None:
    """Password-only dependency for routes that serve or change credentials.

    Two reasons a route lands here. It hands out a credential verbatim — the
    read token could otherwise fetch the MCP Bearer token, and read access
    would quietly become full MCP access. Or it can set the password, in which
    case an agent token that reached it could promote itself to admin, and the
    separation would be decorative.
    """
    if _password_hash is None:
        return  # auth disabled

    token = credentials.credentials if credentials else None
    if not token:
        raise _unauthorized("Authentication required")
    if verify_password(token):
        return
    if verify_agent_token(token) or verify_read_token(token):
        # Deliberately 403, not 401: the credential is valid, the door is not.
        raise HTTPException(
            status_code=403,
            detail="This route handles credentials and requires the admin password",
        )
    raise _unauthorized("Invalid password")


# Auto-configure at import time so auth is active even when admin_server is
# imported directly (e.g. uvicorn app.admin_server:app) without going through manager.py.
configure_auth()
configure_api_tokens()
