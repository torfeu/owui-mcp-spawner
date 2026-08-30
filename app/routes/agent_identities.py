"""Named tokens for calling agents — issue, rename, revoke.

The rights themselves live in the policy (`/api/policy`, the same dialog people
get). This is only the roster of agents and their credentials: an entry here
says *who* a token is, never what it may do.

Every write sits behind `require_admin_auth`, like the token routes in
settings.py and for the same reason — a credential that could mint another
credential is the password. `require_token_edit` on top, so `--no-token-edit`
closes this door as well as the others.
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import agent_identity
from ..agent_identity import AgentIdentityError
from ..api_helpers import require_token_edit
from ..auth import require_admin_auth, require_auth

router = APIRouter()

# The write routes need all three; spelled once so they cannot drift apart.
_WRITE = [Depends(require_auth), Depends(require_admin_auth), Depends(require_token_edit)]


def _refused(e: AgentIdentityError) -> HTTPException:
    """409 for the environment-wins case, 422 for a bad request.

    The distinction matters to the dialog: one is "you cannot manage them
    here", the other is "fix your input".
    """
    return HTTPException(409 if agent_identity.env_active() else 422, str(e))


@router.get("/api/agent-identities", dependencies=[Depends(require_auth)])
async def list_agent_identities() -> dict:
    """The configured agents — never a token, not even a hash.

    *source* is the delivery path in force. It is in the response because "I
    changed the token and nothing happened" is otherwise the first support
    question: with the environment variable set, the file is ignored entirely.
    """
    return {
        "identities": agent_identity.public_list(),
        "source": agent_identity.source(),
        "env_var": agent_identity.ENV_IDENTITIES,
        "path": str(agent_identity.store_path()),
        "editable": not agent_identity.env_active(),
    }


@router.post("/api/agent-identities", dependencies=_WRITE)
async def create_agent_identity(body: dict) -> dict:
    """Issue a token. It is in this response and nowhere else, ever.

    Only the hash is stored (see the module docstring), so there is no route
    that could hand it out a second time — the way back is a new token.
    """
    try:
        record, token = agent_identity.create(
            str(body.get("sub", "")), str(body.get("name", "")), str(body.get("role", "")),
        )
    except AgentIdentityError as e:
        raise _refused(e)
    return {"ok": True, "identity": record, "token": token,
            "note": "Copy this token now — it is stored hashed and cannot be shown again."}


@router.post("/api/agent-identities/{sub}/token", dependencies=_WRITE)
async def regenerate_agent_token(sub: str) -> dict:
    """Roll the token. The previous one stops working on the next call."""
    try:
        token = agent_identity.regenerate(sub)
    except AgentIdentityError as e:
        raise _refused(e)
    return {"ok": True, "sub": sub, "token": token,
            "note": "Copy this token now — it is stored hashed and cannot be shown again."}


@router.put("/api/agent-identities/{sub}", dependencies=_WRITE)
async def update_agent_identity(sub: str, body: dict) -> dict:
    """Change the display name or the role. The id stays — rules hang off it."""
    try:
        record = agent_identity.update(
            sub,
            name=None if "name" not in body else str(body.get("name", "")),
            role=None if "role" not in body else str(body.get("role", "")),
        )
    except AgentIdentityError as e:
        raise _refused(e)
    return {"ok": True, "identity": record}


@router.delete("/api/agent-identities/{sub}", dependencies=_WRITE)
async def delete_agent_identity(sub: str) -> dict:
    """Revoke the token. Any rules for this id stay in the policy."""
    try:
        removed = agent_identity.delete(sub)
    except AgentIdentityError as e:
        raise _refused(e)
    if not removed:
        raise HTTPException(404, f"No agent identity '{sub}'")
    return {"ok": True, "revoked": sub}
