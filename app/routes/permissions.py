"""Who may do what: the user roster and the access rules behind it.

Two halves of one dialog. The roster (`/api/identities`) is what the runners
have seen — it exists so a rule can be attached to a person you pick from a
list instead of a UUID you copy out of a log. The policy (`/api/policy`) is the
rules themselves, the same file the runners read on every call.

The policy is served and written as-is, with one guarantee: it never carries a
secret. `credentials_file` points at a file; the file's content never passes
through this API in either direction, so the dialog can show who acts as which
account without ever displaying a password.
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import agent_identity, identity_registry
from ..auth import require_admin_auth, require_auth
from ..api_helpers import require_upload_or_edit
from ..identity import PLAIN_HEADERS, header_name, identity_configured, trust_plain_headers
from ..logger import get_manager_logger
from ..policy import PolicyError, load_policy, policy_path, rules_for, save_policy

router = APIRouter()
logger = get_manager_logger()


@router.get("/api/identities", dependencies=[Depends(require_auth)])
async def list_identities() -> dict:
    """Everyone the runners have seen, plus everyone a rule or a token names.

    Three halves, really. A person who has called but has no rule is the one
    you want to grant something to; a rule for someone who has never called is
    the one you want to notice — a typo in a user id looks exactly like that;
    and an agent identity exists the moment its token is issued, so it has to
    be assignable before its first call rather than after it.
    """
    try:
        policy = load_policy()
        policy_error = ""
    except PolicyError as e:
        policy, policy_error = {}, str(e)
    users = policy.get("users") if isinstance(policy.get("users"), dict) else {}

    agents = {record["sub"]: record for record in agent_identity.public_list()}

    seen = identity_registry.known()
    entries = [{**row, "has_rules": row["sub"] in users, "agent": row["sub"] in agents}
               for row in seen]
    listed = {row["sub"] for row in seen}
    # Rules whose user has never appeared: shown last, marked, never hidden.
    for sub, entry in users.items():
        if sub in listed:
            continue
        listed.add(sub)
        entries.append({
            "sub": sub, "email": str(entry.get("email", "")), "name": "", "role": "",
            "source": "", "first_seen": 0, "last_seen": 0, "last_instance": "",
            "has_rules": True, "never_seen": True, "agent": sub in agents,
        })
    # Agents that have been issued a token but have not called yet — the
    # normal state right after creating one, and exactly when you want to give
    # it its rules.
    for sub, record in agents.items():
        if sub in listed:
            continue
        entries.append({
            "sub": sub, "email": "", "name": record["name"], "role": record["role"],
            "source": "", "first_seen": record["created_at"], "last_seen": 0,
            "last_instance": "", "has_rules": sub in users, "never_seen": True,
            "agent": True,
        })

    return {
        "identities": entries,
        "identity_configured": identity_configured() or agent_identity.configured(),
        "trust_headers": trust_plain_headers(),
        "header": header_name(),
        "plain_header": PLAIN_HEADERS["sub"],
        "policy_error": policy_error,
    }


@router.delete("/api/identities/{sub}", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def forget_identity(sub: str) -> dict:
    """Drop one person from the roster.

    Not a revocation: the rules live in the policy and stay untouched, and the
    person reappears on their next call. It only tidies the list.
    """
    return {"ok": True, "forgotten": identity_registry.forget(sub)}


@router.get("/api/policy", dependencies=[Depends(require_auth)])
async def get_policy() -> dict:
    try:
        policy = load_policy(force=True)
        error = ""
    except PolicyError as e:
        # A broken file must be visible in the dialog, not just in a log —
        # while it is broken, every rule is denying.
        policy, error = {}, str(e)
    return {"policy": policy, "path": str(policy_path()), "error": error}


# require_admin_auth: assigning an account to a person hands that account's
# data to them. That is credential business, so the agent and read tokens stop
# at this door like they do at the token routes.
@router.put("/api/policy", dependencies=[Depends(require_auth), Depends(require_admin_auth), Depends(require_upload_or_edit)])
async def put_policy(body: dict) -> dict:
    policy = body.get("policy")
    if not isinstance(policy, dict):
        raise HTTPException(422, "Body must be {\"policy\": { … }}")
    try:
        save_policy(policy)
    except PolicyError as e:
        raise HTTPException(422, str(e))
    # Rules are read per call, so this is live everywhere at once — no restart,
    # unlike almost everything else in this framework.
    return {"ok": True}


@router.post("/api/policy/preview", dependencies=[Depends(require_auth)])
async def preview_policy(body: dict) -> dict:
    """What would this user be allowed, as the rules stand right now?

    A rules file has enough moving parts — role, personal entry, deny, the
    matching switches — that "read it and see" is not an answer. This applies
    the real lookup to a hypothetical caller.
    """
    from ..identity import Identity

    sub = str(body.get("sub", "")).strip()
    if not sub:
        raise HTTPException(422, "sub is required")
    identity = Identity(
        sub=sub,
        email=str(body.get("email", "")),
        name=str(body.get("name", "")),
        role=str(body.get("role", "")),
    )
    try:
        rules = rules_for(identity)
    except PolicyError as e:
        raise HTTPException(422, str(e))
    instances = (rules or {}).get("instances")
    return {
        "sub": sub,
        "matched": rules is not None,
        "account": str((rules or {}).get("account", "")),
        "instances": instances if isinstance(instances, (dict, str)) else {},
    }
