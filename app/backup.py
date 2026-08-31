"""The whole manager state as one file — and the way back.

There is a per-instance export (`GET /api/instances/{id}/export`), but it hands
OpenWebUI a connection entry, not a backup: it carries a URL and a token, no
configuration and no code. And what is worth backing up is exactly what is
*not* in git and not in the rsync — `configs/`, `tools/`, `runtime/`. If the
disk of the machine this runs on dies, the code is safe on GitHub and every
instance definition is gone.

**What travels, and why:**

  configs/            the instance definitions
  tools/              their code — the plan's collect-list did not name these,
                      and without them a restored config points at a file that
                      is not there. They are the bulk of the archive (~1.2 MB
                      against 68 KB of configs on the live installation)
  runtime/settings.json         the server's own settings
  runtime/identity_policy.json  who may call what — the rights assignment
  runtime/agent_identities.json the agent roster. Hashed, and the module says
                      so itself: it stores hashes precisely so that this file
                      can go into a backup
  runtime/content.key optional, and the sharpest thing here: with it every
                      file download link of this server can be forged

**What deliberately does not travel:** the identity roster
(`runtime/identities.db`) rebuilds itself from the next call of each user, the
usage numbers (`runtime/usage.db`) are history rather than configuration, the
venvs are rebuilt through the normal install path, and the stored files under
`content/` are not backed up at all — that would be cloning a machine, which is
a different project.

**Secrets are a choice** (`include_secrets`). With them the archive restores a
working server and *is* a collection of credentials. Without them it is safe to
put anywhere, and the price is that no instance holding an API key will run
until the keys are typed in again. Both are honest; neither is right for every
situation, so the caller says which one this is and the archive states it.

**One rule governs the whole restore: write only what is not there.** Not per
category, not per switch — everywhere. An instance whose id exists is skipped
and reported. A setting already set stays. `content.key` is replaced only when
there is none, because a new key silently invalidates every download link that
has ever been handed out. On an empty machine — the case this exists for —
nothing is there, so everything lands. On a running one nothing can be lost by
a mistaken click, which matters more here than anywhere else in this project:
a restore is the most destructive endpoint it has.
"""
import json
import time
from pathlib import Path
from typing import Optional

from . import agent_identity, policy
from .api_helpers import APP_VERSION, TOOLS_DIR
from .config_store import (CONFIGS_DIR, config_exists, find_free_port, is_port_free,
                           load_all_configs, save_config)
from .content_store import SECRET_FILE as CONTENT_KEY_FILE
from .logger import get_manager_logger
from .schema import MCPConfig
from .security import is_secret_field, SECRET_MASK
from .settings_store import SETTINGS_FILE, atomic_write_text, load_settings

logger = get_manager_logger()

FORMAT = "owui-mcp-spawner-backup"
FORMAT_VERSION = 1

# The settings that are credentials. Listed rather than detected: `is_secret_field`
# matches on substrings, and a silent miss here would put a token into a file
# somebody was told is safe to keep in a cloud folder.
SECRET_SETTINGS = ("mcp_bearer_token", "read_token", "agent_token",
                   "user_jwt_secret", "password_hash")


class BackupError(Exception):
    """The payload is not a backup this server can read."""


# ── export ───────────────────────────────────────────────────────────────────

def _tool_payload(cfg: MCPConfig) -> Optional[dict]:
    """The tool's own JSON, or None when the file is gone.

    None rather than an exception: one instance whose tool file was moved must
    not cost the backup of the other fifteen. The gap is reported instead.
    """
    from .config_store import resolve_tool_path
    try:
        return json.loads(resolve_tool_path(cfg).read_text())
    except (OSError, ValueError) as e:
        logger.warning(f"Backup: no readable tool file for '{cfg.id}' ({e})")
        return None


def _is_tool(value) -> bool:
    """Does this look like tool code?

    An OpenWebUI export is a **list holding one tool object**, and that is what
    the files under `tools/` actually are; `tool_loader` takes the first entry
    of a list and a bare object alike. A backup has to accept both or it would
    refuse every real installation while passing every invented fixture —
    which is exactly how this was found: green suite, first live archive
    rejected.
    """
    if isinstance(value, dict):
        return True
    return isinstance(value, list) and bool(value) and isinstance(value[0], dict)


def _read_json(path: Path) -> Optional[dict | list]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def build(include_secrets: bool = False) -> dict:
    """Collect everything worth restoring into one document."""
    settings = load_settings()
    if not include_secrets:
        settings = {k: v for k, v in settings.items() if k not in SECRET_SETTINGS}

    instances, missing_tools = [], []
    for instance_id, cfg in sorted(load_all_configs().items()):
        record = cfg.model_dump(mode="json")
        if not include_secrets:
            # The masked value is a marker, not a password: the restore refuses
            # to write it back, so a redacted backup cannot quietly install
            # "********" as somebody's API key.
            record["values"] = {
                k: (SECRET_MASK if is_secret_field(k) and isinstance(v, str) and v else v)
                for k, v in record.get("values", {}).items()
            }
        tool = _tool_payload(cfg)
        if tool is None:
            missing_tools.append(instance_id)
        instances.append({"id": instance_id, "config": record, "tool": tool})

    payload = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": int(time.time()),
        "spawner_version": APP_VERSION,
        "contains_secrets": bool(include_secrets),
        "settings": settings,
        "instances": instances,
        # Hashes, not tokens — app/agent_identity.py stores them that way so
        # this file can exist. They travel in either mode. Read through the
        # module rather than off the disk: the file wraps the list in an
        # object, and the environment variant is a list, and `load()` is the
        # one place that already knows both.
        "agent_identities": agent_identity.load(),
        "policy": _read_json(policy.policy_path()) or {},
        # Named so a reader of the file knows what it cannot do for them.
        "not_included": ["venvs (rebuilt through the install path)",
                         "content/ (the stored files themselves)",
                         "runtime/identities.db (rebuilds itself)",
                         "runtime/usage.db (history, not configuration)"],
    }
    if missing_tools:
        payload["tool_files_missing"] = missing_tools
    if include_secrets:
        key = _read_key()
        if key:
            payload["content_key"] = key
    return payload


def _read_key() -> str:
    try:
        return CONTENT_KEY_FILE.read_text().strip()
    except OSError:
        return ""


def filename(include_secrets: bool) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = "-with-secrets" if include_secrets else ""
    return f"mcp-spawner-backup-{stamp}{suffix}.json"


# ── restore ──────────────────────────────────────────────────────────────────

def _check(payload) -> None:
    if not isinstance(payload, dict):
        raise BackupError("not a backup file (expected a JSON object)")
    if payload.get("format") != FORMAT:
        raise BackupError(f"not a spawner backup (format is {payload.get('format')!r})")
    version = payload.get("format_version")
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise BackupError(
            f"backup format {version} is newer than this server understands "
            f"({FORMAT_VERSION}) — update the spawner first")


def restore(payload: dict, dry_run: bool = False) -> dict:
    """Write back everything the target does not already have.

    Returns a report, always with the same shape, so the dialog can show the
    same table for a dry run and for the real thing. Nothing here raises for a
    single bad entry: a restore that stops halfway would leave the target in a
    state nobody asked for.
    """
    _check(payload)
    report = {
        "dry_run": bool(dry_run),
        "contains_secrets": bool(payload.get("contains_secrets")),
        "instances_restored": [], "instances_skipped": [], "instances_failed": [],
        "ports_reassigned": [], "settings_restored": [], "settings_skipped": [],
        "agents_restored": [], "agents_skipped": [],
        "policy_users_restored": [], "policy_users_skipped": [],
        "content_key": "",
    }

    for entry in payload.get("instances") or []:
        _restore_instance(entry, report, dry_run)
    _restore_settings(payload.get("settings"), report, dry_run)
    _restore_agents(payload.get("agent_identities"), report, dry_run)
    _restore_policy(payload.get("policy"), report, dry_run)
    _restore_content_key(payload.get("content_key"), report, dry_run)
    return report


def _restore_instance(entry, report: dict, dry_run: bool) -> None:
    if not isinstance(entry, dict) or not isinstance(entry.get("config"), dict):
        report["instances_failed"].append({"id": "?", "reason": "unreadable entry"})
        return
    raw = dict(entry["config"])
    instance_id = str(raw.get("id") or entry.get("id") or "").strip()
    if not instance_id:
        report["instances_failed"].append({"id": "?", "reason": "entry has no id"})
        return
    if config_exists(instance_id):
        report["instances_skipped"].append({"id": instance_id, "reason": "already exists"})
        return
    if not _is_tool(entry.get("tool")):
        # Without the code there is nothing to run, and a config pointing at a
        # missing file is worse than no instance: it looks installed.
        report["instances_failed"].append({"id": instance_id, "reason": "no tool code in the backup"})
        return

    # A redacted backup cannot bring credentials back. Dropping the marker is
    # the honest move — an instance with a missing key fails loudly on its first
    # call, one holding "********" fails in a way that reads like a broken tool.
    dropped = [k for k, v in (raw.get("values") or {}).items() if v == SECRET_MASK]
    if dropped:
        raw["values"] = {k: v for k, v in raw["values"].items() if v != SECRET_MASK}

    tool_path = TOOLS_DIR / f"{instance_id}.json"
    raw.setdefault("tool_source", {})
    raw["tool_source"] = {**raw["tool_source"], "path": str(tool_path)}

    port_note = None
    server = raw.get("server") or {}
    port = server.get("port")
    if isinstance(port, int) and not is_port_free(port, exclude_id=instance_id):
        # The port stays in the config, so an OpenWebUI registration keeps
        # working — unless something else on this machine already has it. Then
        # a new one, and it is *said*, never quietly assigned.
        new_port = find_free_port()
        raw["server"] = {**server, "port": new_port}
        port_note = {"id": instance_id, "was": port, "now": new_port}

    try:
        cfg = MCPConfig(**raw)
    except Exception as e:
        report["instances_failed"].append({"id": instance_id, "reason": f"invalid config: {e}"})
        return

    if not dry_run:
        try:
            TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_text(tool_path, json.dumps(entry["tool"], indent=2))
            save_config(cfg)
        except Exception as e:
            report["instances_failed"].append({"id": instance_id, "reason": str(e)})
            return

    restored = {"id": instance_id, "venv": cfg.venv}
    if dropped:
        restored["credentials_missing"] = sorted(dropped)
    report["instances_restored"].append(restored)
    if port_note:
        report["ports_reassigned"].append(port_note)


def _restore_settings(settings, report: dict, dry_run: bool) -> None:
    if not isinstance(settings, dict):
        return
    current = load_settings()
    incoming = {}
    for key, value in sorted(settings.items()):
        if key in current:
            report["settings_skipped"].append(key)
        else:
            incoming[key] = value
            report["settings_restored"].append(key)
    if incoming and not dry_run:
        from .settings_store import save_settings
        save_settings(incoming)


def _restore_agents(records, report: dict, dry_run: bool) -> None:
    if not isinstance(records, list) or not records:
        return
    if agent_identity.env_active():
        # The environment variable wins over the file, so writing the file here
        # would produce a restore that reports success and changes nothing.
        report["agents_skipped"].append("all (MCP_AGENT_IDENTITIES is set — the file is ignored)")
        return
    existing = {r["sub"] for r in agent_identity.load()}
    keep = list(agent_identity.load())
    for record in records:
        if not isinstance(record, dict) or not record.get("sub"):
            continue
        if record["sub"] in existing:
            report["agents_skipped"].append(record["sub"])
            continue
        keep.append(record)
        report["agents_restored"].append(record["sub"])
    if report["agents_restored"] and not dry_run:
        # The module's own writer: it knows the file's shape, sets 0600 and
        # drops the cache. A second writer here would be a second thing to keep
        # in step with the first.
        agent_identity._save(keep)


def _restore_policy(incoming, report: dict, dry_run: bool) -> None:
    if not isinstance(incoming, dict):
        return
    users = incoming.get("users")
    if not isinstance(users, dict) or not users:
        return
    try:
        current = policy.load_policy(force=True)
    except Exception:
        current = {}
    merged = dict(current)
    existing_users = dict(merged.get("users") or {})
    for sub, entry in sorted(users.items()):
        if sub in existing_users:
            report["policy_users_skipped"].append(sub)
            continue
        existing_users[sub] = entry
        report["policy_users_restored"].append(sub)
    if report["policy_users_restored"] and not dry_run:
        merged["users"] = existing_users
        policy.save_policy(merged)


def _restore_content_key(key, report: dict, dry_run: bool) -> None:
    if not isinstance(key, str) or len(key.strip()) < 32:
        return
    if _read_key():
        # Overwriting it would invalidate every download link this server has
        # ever handed out, and nothing on screen would say so.
        report["content_key"] = "skipped (this server already has one)"
        return
    report["content_key"] = "restored"
    if not dry_run:
        CONTENT_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(CONTENT_KEY_FILE, key.strip())
        CONTENT_KEY_FILE.chmod(0o600)
        from .content_store import _secret_cache
        _secret_cache.clear()
