import asyncio
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager

from fastapi import HTTPException, Request

from .auth import edit_mode, token_edit_enabled
from .config_store import (
    BASE_DIR, config_exists, find_free_port, get_all_states, is_port_free,
    load_config, resolve_tool_path, save_config, set_instance_state,
)
from .dependency_manager import install_dependencies
from .logger import get_manager_logger
from .process_manager import restart_instance
from .schema import MCPConfig, MCPInstance, MCPStatus, ServerConfig, InstallConfig, ToolSourceConfig
from .tool_editor import generate_openwebui_json, parse_requirements, validate_tool_code
from .venv_manager import DEFAULT_VENV, python_path

logger = get_manager_logger()
APP_VERSION = "0.1.2"
TOOLS_DIR = BASE_DIR / "tools"
TOOLS_DIR.mkdir(exist_ok=True)
HISTORY_DIR = BASE_DIR / "runtime" / "history"
HISTORY_KEEP = 10
LOG_TAIL_LINES = 500
LOG_TAIL_MAX_BYTES = 256 * 1024
# configs/example.json is shipped documentation and skipped by load_all_configs;
# an instance with this ID would overwrite it and never show up in the list.
RESERVED_IDS = {"example"}
_version_cache: dict[str, tuple[float, str]] = {}
_background_tasks: set = set()

# Ports handed out to an install that is still running (pip can take minutes and
# the config is only saved at the end). Without this, two concurrent installs
# can be assigned the same port because neither sees the other's config yet.
_inflight_ports: set[int] = set()
_inflight_ports_lock = threading.Lock()


def str_field(value) -> str:
    """Coerce an arbitrary JSON value to a stripped string ('' for non-strings).

    Route bodies are plain dicts, so optional string fields must tolerate any
    JSON type without 500ing; non-strings count as "not provided".
    """
    return value.strip() if isinstance(value, str) else ""


def require_available_id(tool_id: str) -> None:
    """Raises 400/409 when *tool_id* is reserved or already taken."""
    if tool_id in RESERVED_IDS:
        raise HTTPException(400, f"ID '{tool_id}' is reserved — choose a different ID")
    if config_exists(tool_id):
        raise HTTPException(
            409,
            f"ID '{tool_id}' already exists — use the code editor / save_tool_code "
            "to modify the existing tool, or choose a different ID",
        )


@contextmanager
def reserve_port(requested: int | None):
    """Yield a port that is free and held for the duration of the block.

    Pass *requested* to claim a specific port (409 if taken or mid-install
    elsewhere); pass None to auto-assign one.
    """
    with _inflight_ports_lock:
        if requested is not None:
            if requested in _inflight_ports or not is_port_free(requested):
                raise HTTPException(409, f"Port {requested} is already in use")
            port = requested
        else:
            port = find_free_port()
            while port in _inflight_ports:
                port = find_free_port(port + 1)
        _inflight_ports.add(port)
    try:
        yield port
    finally:
        with _inflight_ports_lock:
            _inflight_ports.discard(port)

def _version_from_tool_file(cfg) -> str:
    """Extract version from the tool code docstring or meta.manifest.version."""
    try:
        tool_path = resolve_tool_path(cfg)
        if not tool_path.exists():
            return ""
        mtime = tool_path.stat().st_mtime
        cached = _version_cache.get(str(tool_path))
        if cached and cached[0] == mtime:
            return cached[1]
        raw = json.loads(tool_path.read_text())
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        # Try Python docstring first: version: x.y.z
        code = raw.get("content", "")
        # Anchor to line start so "version: ..." inside a description line doesn't match
        m = re.search(r'^\s*version:\s*([0-9][^\s\n]*)', code[:800], re.MULTILINE)
        if m:
            version = m.group(1).strip()
        else:
            # Fallback: meta.manifest.version
            version = raw.get("meta", {}).get("manifest", {}).get("version", "")
        _version_cache[str(tool_path)] = (mtime, version)
        return version
    except Exception:
        return ""

def require_upload_or_edit() -> None:
    """Raises 403 in readonly mode (--no-edit). Upload, config edit and delete are blocked."""
    if edit_mode() == "readonly":
        raise HTTPException(403, "Disabled: server is running in read-only mode (--no-edit)")

def require_code_edit() -> None:
    """Raises 403 in upload mode and readonly mode (--no-code-edit / --no-edit)."""
    if edit_mode() in ("upload", "readonly"):
        raise HTTPException(403, "Disabled: code editing is turned off on this server")

def require_not_locked(instance_id: str) -> None:
    """Raises 403 if the instance has been locked via the web UI."""
    cfg = load_config(instance_id)
    if cfg and cfg.locked:
        raise HTTPException(403, f"Instance '{instance_id}' is locked — unlock it in the web UI before making changes")

def _backup_tool_file(tool_path, instance_id: str) -> None:
    """Snapshot the current tool JSON before overwriting; keep the last HISTORY_KEEP."""
    if not tool_path.exists():
        return
    try:
        dest_dir = HISTORY_DIR / instance_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (dest_dir / f"{stamp}.json").write_text(tool_path.read_text())
        backups = sorted(dest_dir.glob("*.json"))
        for old in backups[:-HISTORY_KEEP]:
            old.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Could not write tool-code backup for '{instance_id}': {e}")

def _tail_file(path, max_lines: int = LOG_TAIL_LINES) -> str:
    """Return the last *max_lines* lines without loading the whole file."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > LOG_TAIL_MAX_BYTES:
            f.seek(-LOG_TAIL_MAX_BYTES, os.SEEK_END)
        lines = f.read().decode(errors="replace").splitlines()
    if size > LOG_TAIL_MAX_BYTES or len(lines) > max_lines:
        shown = lines[-max_lines:]
        return f"… (truncated, showing last {len(shown)} lines)\n" + "\n".join(shown)
    return "\n".join(lines)

def require_token_edit() -> None:
    """Raises 403 when --no-token-edit was passed at startup."""
    if not token_edit_enabled():
        raise HTTPException(403, "MCP token editing is disabled on this server (--no-token-edit)")

def _rebind_running_instances() -> None:
    """Restart all running instances in the background so their bind address
    matches the current shared-port mode (localhost-only vs. configured host).
    Without this, toggling the mode leaves instances listening on the old
    address while the displayed URLs already point at the new one."""
    running = [
        inst.id for inst in get_all_states() if inst.status == MCPStatus.running
    ]
    if not running:
        return

    async def _do_restarts():
        for iid in running:
            try:
                logger.info(f"Shared-port mode changed — restarting '{iid}' to rebind")
                await asyncio.to_thread(restart_instance, iid)
            except Exception as e:
                logger.error(f"Rebind restart failed for '{iid}': {e}")

    # Keep a strong reference: the loop only holds a weak ref, so a bare
    # create_task could be garbage-collected mid-restart.
    task = asyncio.create_task(_do_restarts())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

def _restart_after_delay() -> None:
    time.sleep(0.8)
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def _provision_new_tool(
    *,
    tool_id: str,
    name: str,
    description: str,
    category: str,
    code: str,
    requirements: list[str],
    venv: str,
    persist_json: list | dict | None = None,
    port: int | None = None,
) -> dict:
    """Install deps → validate in the venv → write tool file → save config.

    Shared by the OpenWebUI-JSON upload and the create_tool flow. Dependencies
    are installed *before* validation so the import-time check sees them (B1).
    Pass *persist_json* to store the uploaded JSON verbatim (keeps meta/manifest);
    otherwise the tool JSON is generated from the code. Pass *port* to request a
    specific port (409 if taken) instead of auto-assigning. Raises HTTPException
    on dependency or validation failure; returns {ok, id, port, warnings}.
    """
    with reserve_port(port) as port:
        # 1. Install dependencies into the instance venv (creates it on first use)
        ok, err = await asyncio.to_thread(install_dependencies, tool_id, requirements, False, venv)
        if not ok:
            raise HTTPException(422, {
                "message": "Dependency installation failed — fix 'requirements' and try again",
                "errors": [err],
            })

        # 2. Validate the code inside that venv, so third-party imports resolve
        validation = await asyncio.to_thread(validate_tool_code, code, str(python_path(venv)))
        if not validation.get("valid"):
            raise HTTPException(422, {
                "message": "Tool code failed validation — fix these errors and try again",
                "errors": validation.get("errors", []),
            })

        # 3. Persist the tool JSON (tool_id is validated by callers — safe as filename)
        tool_file = TOOLS_DIR / f"{tool_id}.json"
        if persist_json is not None:
            payload = persist_json if isinstance(persist_json, list) else [persist_json]
            tool_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            generated = generate_openwebui_json(code, tool_id, name, description, validation=validation)
            tool_file.write_text(json.dumps(generated, indent=2, ensure_ascii=False))

        # 4. Save config with deps, venv and Valve defaults pre-filled
        cfg = MCPConfig(
            id=tool_id,
            name=name,
            description=description,
            category=category,
            server=ServerConfig(host="127.0.0.1", port=port, endpoint="/mcp"),
            install=InstallConfig(dependencies=requirements),
            tool_source=ToolSourceConfig(type="openwebui_json", path=f"./tools/{tool_id}.json"),
            values=validation.get("valves", {}) or {},
            venv=venv,
        )
        save_config(cfg)

        inst = MCPInstance(
            id=cfg.id,
            name=cfg.name,
            description=cfg.description,
            category=cfg.category,
            status=MCPStatus.installed,
            port=port,
            host="127.0.0.1",
            endpoint="/mcp",
        )
        set_instance_state(inst)
    logger.info(f"Provisioned tool '{tool_id}' (venv={venv}, deps={len(requirements)})")
    return {"ok": True, "id": tool_id, "port": port, "warnings": validation.get("warnings", [])}

async def _import_openwebui_tool(
    raw: dict,
    port: int | None = None,
    venv: str = DEFAULT_VENV,
    category: str = "",
) -> dict:
    raw_id = raw.get("id", "")
    tool_id = raw_id.strip().replace(" ", "_") if isinstance(raw_id, str) else ""
    if not tool_id or not re.fullmatch(r"[a-zA-Z0-9_\-]+", tool_id):
        raise HTTPException(400, f"Invalid tool ID '{tool_id}': only letters, digits, underscores and hyphens allowed")
    tool_name = str_field(raw.get("name")) or tool_id

    require_available_id(tool_id)

    code = raw.get("content", "")
    if not isinstance(code, str):
        raise HTTPException(400, "'content' must be a string of Python code")
    meta = raw.get("meta")
    description = str_field(meta.get("description")) if isinstance(meta, dict) else ""
    requirements = parse_requirements(code)
    result = await _provision_new_tool(
        tool_id=tool_id,
        name=tool_name,
        description=description,
        category=category,
        code=code,
        requirements=requirements,
        venv=venv,
        persist_json=raw,
        port=port,
    )
    logger.info(f"Imported OpenWebUI tool: {tool_id}")
    return result

async def _import_mcp_config(
    raw: dict,
    port: int | None = None,
    venv: str | None = None,
    category: str | None = None,
) -> dict:
    try:
        cfg = MCPConfig.model_validate(raw)
    except Exception as e:
        raise HTTPException(400, f"Invalid MCP config: {e}")

    require_available_id(cfg.id)

    # Install-time overrides from the upload dialog (JSON values are the default)
    if venv:
        cfg.venv = venv
    if category:
        cfg.category = category
    if port is not None:
        # Re-validate via ServerConfig so the port range check still applies
        # (model_copy would bypass Pydantic validation).
        try:
            cfg.server = ServerConfig.model_validate({**cfg.server.model_dump(), "port": port})
        except Exception as e:
            raise HTTPException(400, f"Invalid port: {e}")

    with reserve_port(cfg.server.port):
        src_path = resolve_tool_path(cfg)
        # Reject paths that escape the project directory
        try:
            src_path.resolve().relative_to(BASE_DIR.resolve())
        except ValueError:
            raise HTTPException(400, "tool_source.path must be inside the project directory")
        if not src_path.exists():
            raise HTTPException(400, f"Tool source not found: {src_path}")

        # Copy into an instance-owned file so two instances never share a tool file —
        # otherwise deleting one would delete the other's source (B8).
        own_path = TOOLS_DIR / f"{cfg.id}.json"
        if src_path.resolve() != own_path.resolve():
            own_path.write_text(src_path.read_text())
            cfg.tool_source = ToolSourceConfig(type=cfg.tool_source.type, path=f"./tools/{cfg.id}.json")

        inst = MCPInstance(
            id=cfg.id,
            name=cfg.name,
            description=cfg.description,
            category=cfg.category,
            status=MCPStatus.installing,
            port=cfg.server.port,
            host=cfg.server.host,
            endpoint=cfg.server.endpoint,
        )
        set_instance_state(inst)

        ok, err = await asyncio.to_thread(
            install_dependencies, cfg.id, cfg.install.dependencies, cfg.install.upgrade, cfg.venv
        )
        inst.status = MCPStatus.installed if ok else MCPStatus.dependency_error
        inst.error = err
        set_instance_state(inst)

        save_config(cfg)
    if not ok:
        # Config and instance are kept (status dependency_error) so the user can
        # fix the dependencies and hit Reinstall — but the upload must not claim success.
        raise HTTPException(
            422,
            f"Instance '{cfg.id}' was created, but dependency installation failed: {err} "
            "— fix the dependencies and use Reinstall.",
        )
    return {"ok": True, "id": cfg.id, "port": cfg.server.port}

def _request_host(request: Request) -> str | None:
    """Extract just the hostname from the request Host header (no port).
    Handles IPv6 bracket notation: [::1]:7860 → ::1
    """
    host_header = request.headers.get("host", "")
    if host_header.startswith("["):
        # IPv6: [::1]:7860 or [::1]
        end = host_header.find("]")
        hostname = host_header[1:end] if end != -1 else host_header[1:]
    else:
        hostname = host_header.split(":")[0]
    return hostname if hostname else None

def _instance_to_dict(
    inst: MCPInstance,
    display_host: str | None = None,
    shared: int | None = None,
) -> dict:
    host = inst.host
    # If binding on all interfaces and caller knows the real IP, show that
    if display_host and host in ("0.0.0.0", "127.0.0.1", "::1", "localhost"):
        host = display_host
    host_in_url = f"[{host}]" if ":" in host else host  # bracket IPv6 addresses
    if shared:
        url = f"http://{host_in_url}:{shared}/mcp/{inst.id}"
    else:
        url = f"http://{host_in_url}:{inst.port}{inst.endpoint}"
    return {
        "id": inst.id,
        "name": inst.name,
        "description": inst.description,
        "category": inst.category,
        "status": inst.status.value,
        "port": inst.port,
        "host": host,
        "endpoint": inst.endpoint,
        "url": url,
        "pid": inst.pid,
        "error": inst.error,
    }
