"""
Per-user access rules and per-user backend credentials.

Answers two questions about the verified caller (see app/identity.py):

  1. May this user run this tool on this instance?      is_tool_allowed()
  2. Under which account does a tool act for them?      credentials_for_current_user()

Both are answered from one file, runtime/identity_policy.json (override with
MCP_IDENTITY_POLICY). It maps the stable OpenWebUI user id — the JWT's "sub" —
to what that person may reach:

    {
      "default": { "deny": true },
      "users": {
        "8f2c…": {
          "account": "anna",
          "credentials_file": "secrets/accounts/anna",
          "instances": {
            "example_instance": ["search_items", "get_item"],
            "another_instance": "*"
          }
        }
      }
    }

Deny by default: a user who is not in the file reaches nothing, and neither
does anyone when the file is missing. Rules are keyed by "sub" because display
names and e-mail addresses change; matching on e-mail is possible but has to
be turned on deliberately ("match_email": true), and a mapping is then only
used when it is unambiguous.

*account* and *credentials_file* are opaque to this framework. It reads the
file and hands the content to the tool that asked for it; what the value means
— an app password, an API key, a token — is the tool's business and the
installation's. Nothing about a specific backend belongs in here.

Two things this module deliberately does not do. It does not cache secrets in
memory (a rotated credentials file must take effect on the next call), and it
never writes a credential, an account name's secret, or a policy value into a
log line.
"""
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .identity import Identity, get_current_identity
from .logger import get_manager_logger
from .settings_store import atomic_write_text

logger = get_manager_logger()

BASE_DIR = Path(__file__).parent.parent
DEFAULT_POLICY_PATH = BASE_DIR / "runtime" / "identity_policy.json"

ALL_TOOLS = "*"

_cache: Optional[dict] = None
_cache_key: Optional[tuple] = None
_missing_file_warned = False


class PolicyError(Exception):
    """The policy file exists but cannot be used. Fails closed at the call site."""


def policy_path() -> Path:
    configured = os.environ.get("MCP_IDENTITY_POLICY")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else BASE_DIR / path
    return DEFAULT_POLICY_PATH


def load_policy(force: bool = False) -> dict:
    """Read the policy file. Cached until the file's mtime or size changes.

    A missing file is not an error — it is the state of every installation that
    does not use per-user rules. It yields an empty policy, which denies
    everything, which is what "no rules configured" has to mean.
    """
    global _cache, _cache_key, _missing_file_warned
    path = policy_path()

    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        if not _missing_file_warned:
            logger.info(f"No identity policy at {path} — per-user rules deny everything until it exists")
            _missing_file_warned = True
        _cache, _cache_key = {}, None
        return {}
    except OSError as e:
        raise PolicyError(f"cannot read policy file: {e}") from e

    if not force and _cache is not None and key == _cache_key:
        return _cache

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        # Do not fall back to the last good version: a policy that silently
        # keeps applying after someone broke the file is worse than a loud stop.
        raise PolicyError(f"policy file is unusable: {e}") from e
    if not isinstance(data, dict):
        raise PolicyError("policy file must contain a JSON object")

    _cache, _cache_key, _missing_file_warned = data, key, False
    logger.info(f"Loaded identity policy: {len(data.get('users') or {})} user(s) from {path}")
    return data


def save_policy(data: dict) -> None:
    """Validate and write the policy file, then drop the cache.

    Validation is deliberately about *shape*, not about content: an instance id
    that does not exist yet is fine (rules may precede the instance), and so is
    a credentials file that is not there yet. What is refused is a structure
    that would silently mean something other than intended — a tool list that
    is a string, a user entry that is not an object.
    """
    if not isinstance(data, dict):
        raise PolicyError("policy must be an object")

    users = data.get("users", {})
    if not isinstance(users, dict):
        raise PolicyError("'users' must be an object keyed by user id")
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        raise PolicyError("'roles' must be an object keyed by role name")

    for section, entries in (("users", users), ("roles", roles)):
        for key, entry in entries.items():
            where = f"{section}.{key}"
            if not isinstance(entry, dict):
                raise PolicyError(f"{where} must be an object")
            for field in ("account", "credentials_file", "email"):
                if field in entry and not isinstance(entry[field], str):
                    raise PolicyError(f"{where}.{field} must be a string")
            # A secret belongs in a file with mode 600, never in a config that
            # is read by the web UI and copied into backups.
            for suspicious in ("password", "secret", "app_password", "token"):
                if suspicious in entry:
                    raise PolicyError(
                        f"{where}.{suspicious}: secrets do not belong in the policy — "
                        "point 'credentials_file' at a file instead"
                    )
            instances = entry.get("instances")
            if instances in (None, ALL_TOOLS, True):
                continue
            if not isinstance(instances, dict):
                raise PolicyError(f"{where}.instances must be an object or \"*\"")
            for instance_id, tools in instances.items():
                if tools in (ALL_TOOLS, True):
                    continue
                if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
                    raise PolicyError(
                        f"{where}.instances.{instance_id} must be a list of tool names or \"*\""
                    )

    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    load_policy(force=True)
    logger.info(f"Identity policy saved: {len(users)} user(s), {len(roles)} role(s)")


def _deny_by_default(policy: dict) -> bool:
    default = policy.get("default")
    if isinstance(default, dict) and default.get("deny") is False:
        return False
    return True


def rules_for(identity: Optional[Identity]) -> Optional[dict]:
    """The effective rules for *identity*, or None when nothing applies.

    A role is the base equipment, the personal entry sits on top — merged per
    instance, so a personal entry adds to what the role grants instead of
    replacing it. "Every admin may search the law database, but Nextcloud only
    for whoever is named."

    The role is the closest thing to a group we have: OpenWebUI's forwarded
    token carries no groups, but it does carry the role, and that one is
    signed. An explicit `"deny": true` on the personal entry wins over
    everything.
    """
    if identity is None:
        return None
    policy = load_policy()

    role_entry = None
    roles = policy.get("roles")
    if isinstance(roles, dict) and identity.role:
        candidate = roles.get(identity.role)
        if isinstance(candidate, dict):
            role_entry = candidate

    users = policy.get("users")
    entry = _user_entry(policy, users, identity)
    if entry is not None and entry.get("deny") is True:
        return None
    if entry is None:
        return role_entry
    if role_entry is None:
        return entry

    merged = {**role_entry, **entry}
    base, extra = role_entry.get("instances"), entry.get("instances")
    if isinstance(base, dict) and isinstance(extra, dict):
        merged["instances"] = {**base, **extra}
    return merged


def is_explicitly_denied(identity: Optional[Identity]) -> bool:
    """True when the policy names this person and refuses them outright.

    A ban has to outrank everything that could otherwise grant: the role, the
    relaxed default, and the e-mail or name a rule may be matched by. It is
    the one answer in this file that nothing overrules.
    """
    if identity is None:
        return False
    policy = load_policy()
    entry = _user_entry(policy, policy.get("users"), identity)
    return isinstance(entry, dict) and entry.get("deny") is True


def _user_entry(policy: dict, users, identity: Identity) -> Optional[dict]:
    """The personal entry, matched by whatever this policy allows to identify."""
    if not isinstance(users, dict):
        return None

    entry = users.get(identity.sub)
    if isinstance(entry, dict):
        return entry

    # Fallbacks for humans who find UUIDs unreadable — each one off unless
    # asked for, and each one weaker than the "sub" it replaces. Ambiguity
    # means no match rather than an arbitrary one.
    #
    #   e-mail: an address can be reassigned to another person by an admin.
    #   name:   the user can change it themselves in OpenWebUI. Signed only
    #           means "OpenWebUI sent this", not "this is true" — so anyone
    #           could rename themselves into someone else's rule. Offered
    #           because installations exist where that is nobody's concern;
    #           never on by default, and never without this warning.
    for flag, field, value in (
        ("match_email", "email", identity.email),
        ("match_name", "name", identity.name),
    ):
        if not policy.get(flag) or not value:
            continue
        wanted = value.strip().lower()
        hits = [
            candidate for candidate in users.values()
            if isinstance(candidate, dict)
            and str(candidate.get(field, "")).strip().lower() == wanted
        ]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            logger.warning(f"Policy: {field} matches more than one user entry — refusing to guess")
    return None


def allowed_tools(identity: Optional[Identity], instance_id: str) -> Optional[set[str] | str]:
    """Tool names this user may run on *instance_id*.

    Returns a set of names, the string "*" for "all tools of that instance", or
    None when the user may not reach the instance at all.
    """
    policy = load_policy()
    if is_explicitly_denied(identity):
        # "Named and refused" is not the same answer as "not named at all", and
        # only the second one may fall through to a relaxed default. Telling
        # them apart by `rules_for()` returning None alone turned an explicit
        # ban into full access on any installation that had opened its default.
        return None
    rules = rules_for(identity)

    if rules is None:
        # No entry. Only an explicitly relaxed default lets this through, which
        # exists for installations that want identity in their tools without
        # running an allow-list.
        return ALL_TOOLS if not _deny_by_default(policy) else None

    instances = rules.get("instances")
    if instances in (ALL_TOOLS, True):
        return ALL_TOOLS
    if not isinstance(instances, dict):
        return None

    entry = instances.get(instance_id)
    if entry is None:
        return None
    if entry in (ALL_TOOLS, True):
        return ALL_TOOLS
    if isinstance(entry, str):
        return {entry}
    if isinstance(entry, (list, tuple, set)):
        names = {str(name) for name in entry}
        return ALL_TOOLS if ALL_TOOLS in names else names
    return None


def is_tool_allowed(identity: Optional[Identity], instance_id: str, tool_name: str) -> bool:
    """The single yes/no. Called for every tool call, not just for listings.

    Hiding a tool from a listing is presentation; a model that knows the name
    from an earlier conversation can still ask for it by hand.
    """
    try:
        allowed = allowed_tools(identity, instance_id)
    except PolicyError as e:
        logger.error(f"Denying access — {e}")
        return False
    if allowed is None:
        return False
    if allowed == ALL_TOOLS:
        return True
    return tool_name in allowed


def visible_tools(identity: Optional[Identity], instance_id: str, names: list[str]) -> list[str]:
    """*names* filtered down to what this user may see. Order is preserved."""
    try:
        allowed = allowed_tools(identity, instance_id)
    except PolicyError as e:
        logger.error(f"Hiding all tools — {e}")
        return []
    if allowed is None:
        return []
    if allowed == ALL_TOOLS:
        return list(names)
    return [name for name in names if name in allowed]


# ── per-user credentials ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Credentials:
    """An account name and the file holding its secret.

    The secret is read on access, not on construction, so it exists in memory
    for as short a time as possible and a rotated file takes effect at once.
    """

    account: str
    path: Path

    @property
    def secret(self) -> str:
        """The credential: the **last non-empty line** of the file.

        Not the whole content. Credential files grow a history — the previous
        password stays above the new one during a rotation, and people write
        a comment line at the top. Taking everything would send that along and
        fail as "wrong password", which is the least helpful place to look.
        A single line with a trailing newline behaves exactly as expected.
        """
        try:
            mode = self.path.stat().st_mode
        except OSError as e:
            raise PolicyError(f"credentials file for '{self.account}' is unreadable: {e}") from e
        # Group- or world-readable defeats the point of a separate file; say so
        # once per call rather than failing, because tightening the mode is the
        # operator's job and refusing service over it helps nobody at 23:00.
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            logger.warning(
                f"Credentials file for '{self.account}' is accessible beyond its owner — chmod 600 it"
            )
        try:
            lines = [line.strip() for line in self.path.read_text().splitlines() if line.strip()]
        except OSError as e:
            raise PolicyError(f"credentials file for '{self.account}' is unreadable: {e}") from e
        if not lines:
            raise PolicyError(f"credentials file for '{self.account}' is empty")
        return lines[-1]


def credentials_for(identity: Optional[Identity]) -> Optional[Credentials]:
    """The account this user acts as, or None when none is configured."""
    rules = rules_for(identity)
    if not rules:
        return None
    account = str(rules.get("account") or "").strip()
    raw_path = str(rules.get("credentials_file") or "").strip()
    if not account or not raw_path:
        return None
    path = Path(raw_path)
    return Credentials(account=account, path=path if path.is_absolute() else BASE_DIR / path)


def credentials_for_current_user() -> Optional[Credentials]:
    """Credentials for the user of the tool call running right now.

    This is the call a tool makes instead of reading a credential out of its
    valves. Resolve it *per call* and keep nothing on the Tools instance — one
    instance serves every caller, and a cached client is how two users end up
    sharing one account.
    """
    return credentials_for(get_current_identity())
