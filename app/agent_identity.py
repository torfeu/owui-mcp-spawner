"""
Named agent identities — one token per calling agent.

The MCP Bearer token (app/mcp_runner.py) answers "may this client talk to this
instance at all". One token serves every agent, so behind it Claude Code, Codex
and a cron job are the same caller: the access rules cannot tell them apart,
the usage statistics cannot either, and the log says nothing about who it was.

This module gives each of them a token of its own. A token maps 1:1 to an
identity — the same `Identity` the user-JWT path produces — so everything
downstream keeps working unchanged: policy rules, the rights dialog, the
identity roster, the usage numbers.

**Assigned, not proven.** Whoever holds the token *is* that agent. That is no
weaker than the shared Bearer token it sits next to (which was already the only
gate), and it buys the thing that was missing: two agents with the same rights
still get two tokens, revocable one at a time.

Precedence, decided together with the runner (see `_resolve_identity`):

    signed user JWT  >  agent identity  >  machine identity

A *broken* user token stays a rejection at the first step — it never falls
through to an agent token, or a forged JWT would be quietly downgraded into a
working identity.

Two ways to deliver the identities to a runner, and a set environment variable
wins — the same order `auth.configure_api_tokens()` already uses for the API
tokens, so it is one rule for the installation rather than two:

  MCP_AGENT_IDENTITIES       JSON list, read once at start. For containers and
                             systemd units that want no extra state file.
                             Changes need an instance restart.
  runtime/agent_identities.json
                             Written by the manager, only ever *read* by the
                             runners — the same split as runtime/content.key,
                             and for the same reason: a runner doing
                             read-modify-write on a manager file would sooner
                             or later overwrite a manager write. Changes take
                             effect on the next call, no restart.

Tokens are stored **hashed**. That is a deliberate break with the API tokens in
app/auth.py, which sit in runtime/settings.json in clear text: this file is
meant to be safe to put in a backup, and a hash is not a secret. It costs the
"show me the token again" button — a token is displayed once, at creation, and
after that the only way back is a new one.

A plain SHA-256 is enough here, unlike for the password: these tokens are 256
bits of `secrets` output, so there is no dictionary to run against the hash.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from .identity import Identity, SOURCE_AGENT
from .logger import get_manager_logger
from .settings_store import atomic_write_text

logger = get_manager_logger()

BASE_DIR = Path(__file__).parent.parent
DEFAULT_STORE = BASE_DIR / "runtime" / "agent_identities.json"

ENV_IDENTITIES = "MCP_AGENT_IDENTITIES"
ENV_STORE_FILE = "MCP_AGENT_IDENTITIES_FILE"

# Recognisable at a glance in a config file or a shell history, and it lets a
# reader tell an agent token from the shared Bearer token without trying it.
TOKEN_PREFIX = "mcpa_"

# The id *is* the policy key ("sub"), so there is no second identifier to keep
# in sync — and it travels in a URL, so it stays boring on purpose.
SUB_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

SOURCE_ENV = "env"
SOURCE_FILE = "file"


class AgentIdentityError(Exception):
    """A refused write. The message is meant for the user, never a token."""


# ── hashing ───────────────────────────────────────────────────────────────────

def hash_token(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


# ── loading ───────────────────────────────────────────────────────────────────

def store_path() -> Path:
    override = os.environ.get(ENV_STORE_FILE, "").strip()
    return Path(override) if override else DEFAULT_STORE


def _normalise(raw, where: str) -> list[dict]:
    """One record list out of whatever the file or the env var carried.

    Entries that make no sense are dropped with a log line rather than raising:
    a single malformed entry must not take the working ones down with it, and
    an identity that quietly grants nothing is the safe direction.
    """
    if isinstance(raw, dict):
        raw = raw.get("agent_identities", [])
    if not isinstance(raw, list):
        logger.warning(f"Agent identities in {where}: expected a list — ignoring all of them")
        return []

    records, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sub = str(entry.get("sub", "")).strip()
        if not sub or sub in seen:
            continue
        # Accept a plain token here as well: writing one into an env var is
        # the natural thing to do, and refusing it would push people to hash
        # by hand. The manager's own file only ever contains hashes.
        token_hash = str(entry.get("token_hash", "")).strip()
        if not token_hash and entry.get("token"):
            token_hash = hash_token(str(entry["token"]))
        if not token_hash:
            logger.warning(f"Agent identity '{sub}' in {where} has no token — ignored")
            continue
        seen.add(sub)
        records.append({
            "sub": sub,
            "name": str(entry.get("name", "")).strip(),
            "role": str(entry.get("role", "")).strip(),
            "token_hash": token_hash,
            "created_at": int(entry.get("created_at") or 0),
        })
    return records


def _env_records() -> Optional[list[dict]]:
    """The identities from the environment, or None when the var is unset.

    A set but unreadable variable returns an empty list, not None: the operator
    chose the environment as the source, so it stays the source and grants
    nobody. Falling back to the file there would re-open a door the broken
    variable was meant to define.
    """
    raw = os.environ.get(ENV_IDENTITIES)
    if raw is None or not raw.strip():
        return None
    try:
        return _normalise(json.loads(raw), ENV_IDENTITIES)
    except (ValueError, TypeError) as e:
        logger.error(f"{ENV_IDENTITIES} is not readable JSON ({e}) — no agent identity is active")
        return []


# (path, mtime, size) -> records. Re-read only when the file actually changed,
# because this sits in the path of every single tool call.
_file_cache: dict[str, tuple] = {}


def _file_records() -> list[dict]:
    path = store_path()
    try:
        stat = path.stat()
    except OSError:
        _file_cache.pop(str(path), None)
        return []
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _file_cache.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        records = _normalise(json.loads(path.read_text()), str(path))
    except (OSError, ValueError, TypeError) as e:
        logger.error(f"Could not read {path} ({e}) — no agent identity is active")
        records = []
    _file_cache[str(path)] = (stamp, records)
    return records


def source() -> str:
    """Which of the two delivery paths is in force right now."""
    return SOURCE_ENV if _env_records() is not None else SOURCE_FILE


def env_active() -> bool:
    return _env_records() is not None


def load() -> list[dict]:
    """Every configured identity, hashes included. Env wins over file."""
    from_env = _env_records()
    return from_env if from_env is not None else _file_records()


def configured() -> bool:
    """True when this server can identify anybody by an agent token at all."""
    return bool(load())


def public_list() -> list[dict]:
    """What the API and the dialog may see — everything except the hash."""
    return [
        {"sub": r["sub"], "name": r["name"], "role": r["role"], "created_at": r["created_at"]}
        for r in load()
    ]


# ── lookup (runner side) ──────────────────────────────────────────────────────

def identify(token: str) -> Optional[Identity]:
    """The identity behind *token*, or None if it belongs to nobody.

    Compared against every record with `compare_digest` and without an early
    exit, so the answer takes the same time whether the first or the last entry
    matched — the hashes are not secret, but the shape of the store need not be
    handed out either.
    """
    token = (token or "").strip()
    if not token:
        return None
    offered = hash_token(token)
    found = None
    for record in load():
        if hmac.compare_digest(offered, record["token_hash"]):
            found = record
    if found is None:
        return None
    return Identity(sub=found["sub"], name=found["name"], role=found["role"],
                    source=SOURCE_AGENT)


def bearer_token(headers: dict) -> str:
    """The Bearer credential of a request, or "" — case-insensitive per RFC 7235."""
    for key, value in (headers or {}).items():
        if str(key).lower() != "authorization":
            continue
        value = str(value)
        if value.lower().startswith("bearer "):
            return value[7:].strip()
    return ""


def identify_request(headers: dict) -> Optional[Identity]:
    """The agent identity a request carries, if any."""
    return identify(bearer_token(headers))


# ── writing (manager side) ────────────────────────────────────────────────────

def _require_file_source() -> None:
    """Refuse a write that could not possibly take effect.

    The lesson from the invented `lifecycle` valve on 30.08.: an API that
    answers `ok` to a write nobody will ever read is worse than an error,
    because the symptom shows up somewhere else entirely.
    """
    if env_active():
        raise AgentIdentityError(
            f"Agent identities come from {ENV_IDENTITIES} on this server — "
            "the environment wins over the file, so a change here would have no "
            f"effect. Unset {ENV_IDENTITIES} to manage them from the UI."
        )


def _save(records: list[dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"agent_identities": records}, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _file_cache.pop(str(path), None)


def create(sub: str, name: str = "", role: str = "") -> tuple[dict, str]:
    """Add an identity and return (public record, the token — shown once)."""
    _require_file_source()
    sub = str(sub or "").strip()
    if not SUB_PATTERN.match(sub):
        raise AgentIdentityError(
            "The id may hold letters, digits, dot, dash and underscore, up to 64 "
            "characters — it is the key the access rules use."
        )
    records = _file_records()
    if any(r["sub"] == sub for r in records):
        raise AgentIdentityError(f"An agent identity '{sub}' already exists")
    token = new_token()
    record = {"sub": sub, "name": str(name or "").strip(), "role": str(role or "").strip(),
              "token_hash": hash_token(token), "created_at": int(time.time())}
    _save(records + [record])
    logger.info(f"Agent identity '{sub}' created")
    return {k: v for k, v in record.items() if k != "token_hash"}, token


def regenerate(sub: str) -> str:
    """A new token for an existing identity; the old one stops working at once."""
    _require_file_source()
    records = _file_records()
    for record in records:
        if record["sub"] == sub:
            token = new_token()
            record["token_hash"] = hash_token(token)
            _save(records)
            logger.info(f"Agent identity '{sub}': token regenerated")
            return token
    raise AgentIdentityError(f"No agent identity '{sub}'")


def update(sub: str, name: Optional[str] = None, role: Optional[str] = None) -> dict:
    """Rename or re-role. The id itself never changes — rules hang off it."""
    _require_file_source()
    records = _file_records()
    for record in records:
        if record["sub"] == sub:
            if name is not None:
                record["name"] = str(name).strip()
            if role is not None:
                record["role"] = str(role).strip()
            _save(records)
            return {k: v for k, v in record.items() if k != "token_hash"}
    raise AgentIdentityError(f"No agent identity '{sub}'")


def delete(sub: str) -> bool:
    """Revoke. True when something was removed.

    The access rules for that id stay in the policy on purpose: revoking a
    token and forgetting who it was are two different intentions, and a
    re-issued identity of the same name should come back to its own rules.
    """
    _require_file_source()
    records = _file_records()
    remaining = [r for r in records if r["sub"] != sub]
    if len(remaining) == len(records):
        return False
    _save(remaining)
    logger.info(f"Agent identity '{sub}' revoked")
    return True
