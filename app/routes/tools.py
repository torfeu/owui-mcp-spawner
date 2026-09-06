import asyncio
import json
import re
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from ..api_helpers import (_backup_tool_file, _bundled_version_info, _example_code, _import_mcp_config, _import_openwebui_tool, _provision_new_tool, require_available_id, require_code_edit, require_not_locked, require_upload_or_edit, str_field)
from ..auth import require_auth
from ..config_store import InstanceBusy, get_instance_state, instance_lock, load_config, resolve_tool_path, save_config, set_instance_state
from ..dependency_manager import install_dependencies
from ..logger import get_manager_logger
from ..process_manager import restart_instance
from ..schema import InstallConfig, MCPStatus
from ..tool_editor import STARTER_TEMPLATE, generate_openwebui_json, merge_openwebui_json, parse_requirements, validate_tool_code
from ..tool_loader import load_openwebui_json
from ..venv_manager import DEFAULT_VENV, ensure_venv, python_path, venv_exists
router = APIRouter()
logger = get_manager_logger()

@router.post("/api/instances/upload", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def upload_json(
    file: UploadFile = File(...),
    port: str | None = Form(None),
    venv: str | None = Form(None),
    category: str | None = Form(None),
) -> dict:
    content = await file.read()
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")

    # Optional install-time overrides from the upload dialog
    port_int: int | None = None
    if port not in (None, ""):
        try:
            port_int = int(port)
        except ValueError:
            raise HTTPException(400, "port must be an integer")
        if not 1024 <= port_int <= 65535:
            raise HTTPException(400, "port must be between 1024 and 65535")
    # Only override venv when the upload dialog actually supplied one; otherwise
    # leave it unset so a venv declared in the JSON stays the default.
    venv_name: str | None = None
    if venv not in (None, ""):
        venv_name = venv.strip()
        if not re.fullmatch(r"[a-zA-Z0-9_\-]+", venv_name):
            raise HTTPException(400, f"Invalid venv name '{venv_name}': letters, digits, _ and - only")

    # Category is manager metadata. It is stored in the MCP config only and is
    # never injected into or otherwise used to modify the uploaded tool JSON.
    # An empty form field means "keep the category from an uploaded MCP
    # config", just like an omitted venv. Only a non-empty value is an
    # explicit manager-side override.
    category_name = (category.strip() or None) if category is not None else None

    # Detect: OpenWebUI export (array or has 'content'+'specs') vs MCP config
    if isinstance(raw, list):
        if not raw:
            raise HTTPException(400, "Uploaded JSON array is empty")
        raw = raw[0]
    if not isinstance(raw, dict):
        raise HTTPException(400, "Uploaded JSON must be an object or an array of objects")

    if "content" in raw and "specs" in raw:
        # OpenWebUI tools have no venv field of their own, so fall back to default.
        return await _import_openwebui_tool(
            raw, port=port_int, venv=venv_name or DEFAULT_VENV,
            category=category_name or "",
        )
    else:
        return await _import_mcp_config(
            raw, port=port_int, venv=venv_name, category=category_name,
        )

@router.get("/api/instances/{instance_id}/tool-code", dependencies=[Depends(require_auth), Depends(require_code_edit)])
async def get_tool_code(instance_id: str) -> dict:
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, "Config not found")
    tool_path = resolve_tool_path(cfg)
    tool = load_openwebui_json(tool_path)
    if not tool:
        raise HTTPException(404, "Tool source not found or unreadable")
    return {
        "id": cfg.id,
        "name": cfg.name,
        "description": cfg.description,
        "code": tool.content,
    }

@router.put("/api/instances/{instance_id}/tool-code", dependencies=[Depends(require_auth), Depends(require_code_edit)])
async def save_tool_code(instance_id: str, body: dict) -> dict:
    try:
        with instance_lock(instance_id):
            return await _save_tool_code(instance_id, body)
    except InstanceBusy:
        raise HTTPException(409, (
            f"Another change to '{instance_id}' is running — an install can take "
            "minutes. Wait for it to finish and try again."
        ))


async def _save_tool_code(instance_id: str, body: dict) -> dict:
    """The body of the route, with the instance held for its whole length."""
    require_not_locked(instance_id)
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, "Config not found")
    code = body.get("code", "")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(400, "No code provided")

    # Install any newly declared dependencies into the instance venv before
    # validating, so adding a new import works in one save (B1). Existing deps
    # are already installed, so we only re-run pip when the set grows.
    new_reqs = parse_requirements(code)
    merged_reqs = list(dict.fromkeys([*cfg.install.dependencies, *new_reqs]))
    # Which environment everything below is prepared *for*. Checked again at
    # the commit: installing and validating in `default` and then saving the
    # result onto a config that meanwhile points at `alternate` would leave the
    # code sitting in an environment nobody prepared for it.
    venv_used = cfg.venv
    deps_installed = False
    if merged_reqs != cfg.install.dependencies:
        ok, err = await asyncio.to_thread(
            install_dependencies, instance_id, merged_reqs, False, cfg.venv
        )
        if not ok:
            raise HTTPException(422, {
                "message": "Dependency installation failed — fix 'requirements' and try again",
                "errors": [err],
            })
        deps_installed = True
    else:
        venv_ok, venv_err = await asyncio.to_thread(ensure_venv, cfg.venv)
        if not venv_ok:
            raise HTTPException(422, {"message": "venv unavailable", "errors": [venv_err]})

    # Validate inside the instance venv so third-party imports resolve
    result = await asyncio.to_thread(validate_tool_code, code, str(python_path(cfg.venv)))
    if not result["valid"]:
        raise HTTPException(422, {"errors": result["errors"]})

    # Everything above ran on a config read minutes ago — installing packages
    # and validating code take that long. Writing that snapshot back is how a
    # value somebody saved in the meantime disappeared, and `locked: true` with
    # it: a flag set to protect this instance, cleared by a save that began
    # before it was set. So: read it again, under a lock, and change only what
    # this route decides — the code, the schemas from it, the valve values it
    # brings and the dependencies it declared. Everything else on that config
    # belongs to whoever wrote it last.
    valves_introspected = not any(
        "Could not instantiate Valves" in w for w in result.get("warnings", [])
    )
    new_defaults = result.get("valves", {}) or {}
    # The instance has been held since the top of the route, so nothing
    # below can be overtaken. The re-read and the checks stay all the same:
    # they are cheap, and they are what makes a path that forgets the lock
    # fail loudly instead of quietly.
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, "Config not found")
    # Checked again, not only at the door: locking is exactly the thing
    # somebody does *while* a long save is running.
    if cfg.locked:
        raise HTTPException(409, (
            f"'{instance_id}' was locked while this save was running — nothing "
            "was written. Unlock it and save again."
        ))
    if cfg.venv != venv_used:
        raise HTTPException(409, (
            f"'{instance_id}' was moved from venv '{venv_used}' to '{cfg.venv}' while "
            "this save was being prepared — the code was installed and validated in "
            "the old one, so nothing was written. Save again."
        ))
    # Merged onto the list as it stands *now*, never written over it: the
    # list computed before the install is older than whatever else was
    # saved meanwhile, and replacing it dropped a dependency somebody had
    # added and installed in between.
    if deps_installed:
        current = list(cfg.install.dependencies)
        merged_now = list(dict.fromkeys([*current, *new_reqs]))
        if merged_now != current:
            cfg.install = InstallConfig(dependencies=merged_now,
                                        upgrade=cfg.install.upgrade)

    # Sync config.values with the code's Valve defaults so new/changed valves are
    # editable in the UI; keep user-set values, drop valves no longer in the code (B2).
    for k, v in new_defaults.items():
        cfg.values.setdefault(k, v)
    if valves_introspected:
        cfg.values = {k: cfg.values[k] for k in cfg.values if k in new_defaults}

    # Update the tool JSON file: id/name/description come from the config, the
    # code and its schemas from this save — and everything else in the file
    # stays. Regenerating it wholesale is what used to empty an imported tool's
    # manifest on the first save. Inside the lock and after the refusal above,
    # so a rejected save leaves no half-written state behind.
    tool_path = resolve_tool_path(cfg)
    _backup_tool_file(tool_path, instance_id)
    try:
        existing = json.loads(tool_path.read_text())
    except (OSError, ValueError):
        existing = None
    updated = merge_openwebui_json(existing, code, cfg.id, cfg.name, cfg.description,
                                   validation=result)
    tool_path.write_text(json.dumps(updated, indent=2, ensure_ascii=False))
    save_config(cfg)

    # Restart if running and restart_on_change. Saved and *running the saved
    # code* are two different things: the file is written either way, but a
    # restart can fail — a port taken meanwhile, a venv that no longer builds —
    # and reporting it as done would leave the old runner serving the old code
    # while the dashboard says the change is live.
    inst = get_instance_state(instance_id)
    restarted, restart_error = False, ""
    if inst and inst.status == MCPStatus.running and cfg.lifecycle.restart_on_change:
        restarted, restart_error = await asyncio.to_thread(restart_instance, instance_id)
        if not restarted:
            logger.warning(f"'{instance_id}': code saved, restart failed — {restart_error}")

    return {"ok": True, "restarted": restarted, "restart_error": restart_error,
            "warnings": result["warnings"]}

# Treated as an upload, not as code editing: the code comes from a file this
# spawner ships, not from a text box, so it stays available under
# --no-code-edit exactly like re-uploading the same JSON by hand would.
@router.post("/api/instances/{instance_id}/update-from-example", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def update_from_example(instance_id: str) -> dict:
    """Replace an instance's code with the copy shipped in examples/.

    Forward only: refused unless the shipped version is strictly newer, so a
    stray click cannot silently downgrade a tool someone updated by hand.

    What makes this safe enough to offer at all is the snapshot below in
    save_tool_code — the previous tool JSON goes to runtime/history/<id>/
    before anything is written, so an adapted copy can be recovered. It is
    still an overwrite, which is why the UI asks first.
    """
    require_not_locked(instance_id)
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, f"Config '{instance_id}' not found")

    bundled = _bundled_version_info(cfg)
    if not bundled:
        raise HTTPException(404, f"No tool ships with this spawner under the id '{instance_id}'")
    if not bundled["update_available"]:
        raise HTTPException(
            409,
            f"Nothing to update — the shipped copy is {bundled['version']}, "
            f"which is not newer than what is installed",
        )
    code = _example_code(instance_id)
    if not code.strip():
        raise HTTPException(422, f"The shipped tool file {bundled['path']} carries no code")

    # Same path as a manual save: dependency install, validation in the
    # instance venv, valve sync, backup, restart. Nothing about this update
    # deserves a second implementation of that.
    result = await save_tool_code(instance_id, {"code": code})
    logger.info(f"Updated '{instance_id}' from {bundled['path']} to {bundled['version']}")
    return {**result, "version": bundled["version"], "source": bundled["path"]}

# Deliberately available in readonly mode (see README "Edit modes"): reinstall
# only re-runs pip for the dependencies already pinned in the config — it cannot
# introduce new code or packages, those changes are blocked by --no-edit.
@router.post("/api/instances/{instance_id}/reinstall", dependencies=[Depends(require_auth)])
async def reinstall(instance_id: str) -> dict:
    require_not_locked(instance_id)
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, "Config not found")
    inst = get_instance_state(instance_id)
    was_running = inst and inst.status == MCPStatus.running
    if inst:
        inst.status = MCPStatus.installing
        set_instance_state(inst)
    ok, err = await asyncio.to_thread(
        install_dependencies, instance_id, cfg.install.dependencies, cfg.install.upgrade, cfg.venv
    )
    if inst:
        if was_running:
            inst.status = MCPStatus.running  # process is still running — restore status
        else:
            inst.status = MCPStatus.installed if ok else MCPStatus.dependency_error
        inst.error = err
        set_instance_state(inst)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True}

@router.get("/api/tools/template")
async def get_tool_template() -> PlainTextResponse:
    return PlainTextResponse(STARTER_TEMPLATE)

@router.post("/api/tools/validate", dependencies=[Depends(require_auth), Depends(require_code_edit)])
async def tool_validate(body: dict) -> dict:
    code = body.get("code", "")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(400, "No code provided")
    # Validate inside the instance's venv when one is given, so third-party
    # imports of already-installed dependencies resolve instead of false-failing.
    python_exe = None
    instance_id = body.get("instance_id")
    if instance_id:
        cfg = load_config(instance_id)
        if cfg and venv_exists(cfg.venv):
            python_exe = str(python_path(cfg.venv))
    return await asyncio.to_thread(validate_tool_code, code, python_exe)

@router.post("/api/tools/export", dependencies=[Depends(require_auth), Depends(require_code_edit)])
async def tool_export(body: dict) -> JSONResponse:
    code = body.get("code", "")
    tool_id = str(body.get("id", "")).strip().replace(" ", "_")
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(400, "No code provided")
    if not tool_id:
        raise HTTPException(400, "id is required")
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", tool_id):
        raise HTTPException(400, "id must contain only letters, digits, underscores and hyphens")
    if not name:
        raise HTTPException(400, "name is required")
    try:
        result = await asyncio.to_thread(generate_openwebui_json, code, tool_id, name, description)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return JSONResponse(
        content=result,
        headers={"Content-Disposition": f'attachment; filename="{tool_id}.json"'},
    )

@router.post("/api/tools/create", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def create_tool(body: dict) -> dict:
    """Create a new instance from raw Python code in a single step.

    Installs dependencies (from the `requirements:` frontmatter or an explicit
    list), validates the code in the instance venv, fills config.values from the
    Valve defaults and saves the config. No placeholder upload, no double JSON
    escaping. The instance does not auto-start — call start afterwards.
    """
    tool_id = str(body.get("id", "")).strip().replace(" ", "_")
    if not tool_id or not re.fullmatch(r"[a-zA-Z0-9_\-]+", tool_id):
        raise HTTPException(400, f"Invalid tool ID '{tool_id}': only letters, digits, underscores and hyphens allowed")
    require_available_id(tool_id)
    code = body.get("code", "")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(400, "No code provided")
    # Optional metadata comes from arbitrary JSON — non-strings mean "not set"
    # and fall back to the defaults instead of crashing on .strip().
    name = str_field(body.get("name")) or tool_id
    description = str_field(body.get("description"))
    category = str_field(body.get("category"))
    venv = str_field(body.get("venv")) or DEFAULT_VENV
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", venv):
        raise HTTPException(400, f"Invalid venv name '{venv}': only letters, digits, underscores and hyphens allowed")

    port = body.get("port")
    if port not in (None, ""):
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise HTTPException(400, "port must be an integer")
        if not 1024 <= port <= 65535:
            raise HTTPException(400, "port must be between 1024 and 65535")
    else:
        port = None

    # Requirements: an explicit list/string wins, otherwise parse the frontmatter
    reqs = body.get("requirements")
    if isinstance(reqs, str):
        requirements = [p.strip() for p in reqs.replace(";", ",").split(",") if p.strip()]
    elif isinstance(reqs, list):
        requirements = [str(p).strip() for p in reqs if str(p).strip()]
    else:
        requirements = parse_requirements(code)

    result = await _provision_new_tool(
        tool_id=tool_id,
        name=name,
        description=description,
        category=category,
        code=code,
        requirements=requirements,
        venv=venv,
        persist_json=None,
        port=port,
    )
    logger.info(f"Created tool via create_tool: {tool_id}")
    return result
