import asyncio
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from .. import agent_identity, health, identity_registry, shared_proxy, tool_call
from ..activity import forget as forget_usage, read_usage
from ..api_helpers import (TOOLS_DIR, _bundled_version_info, _instance_to_dict, _request_host, _specs_from_tool_file, _valve_names_from_tool_file, _version_from_tool_file, require_not_locked, require_upload_or_edit)
from ..auth import is_request_authenticated, mcp_bearer_token, require_admin_auth, require_auth
from ..content_store import forget_instance as forget_content
from ..config_store import (config_exists, delete_config, find_free_port, get_all_states, get_instance_state, is_port_free, load_all_configs, load_config, resolve_tool_path, save_config, set_instance_state)
from ..dependency_manager import install_dependencies
from ..logger import get_manager_logger
from ..policy import PolicyError, load_policy
from ..process_manager import _is_pid_alive, restart_instance, start_instance, stop_instance
from ..schema import ContentConfig, IdentityMode, MCPStatus, ServerConfig, InstallConfig
from ..security import is_secret_field, keep_masked_values, mask_secrets
from ..venv_manager import DEFAULT_VENV
router = APIRouter()
logger = get_manager_logger()
_GUEST_FIELDS = ("id", "name", "description", "category", "status", "version")
def _guest_view(d: dict) -> dict:
    return {k: d[k] for k in _GUEST_FIELDS if k in d}

def _config_fields(cfg) -> dict:
    """Config-derived display fields shared by the list and detail endpoints."""
    # Version of the copy in examples/ when it is newer, "" otherwise. Cheap
    # enough for the polled list: the examples index and both version reads are
    # mtime-cached, and the installed version is read for this row anyway.
    bundled = _bundled_version_info(cfg) if cfg else None
    return {
        "locked": cfg.locked if cfg else False,
        "version": _version_from_tool_file(cfg) if cfg else "",
        "venv": cfg.venv if cfg else DEFAULT_VENV,
        "bundled_update": bundled["version"] if bundled and bundled["update_available"] else "",
        "identity_mode": cfg.identity_mode.value if cfg else IdentityMode.off.value,
        "forward_agent_token": bool(cfg.forward_agent_token) if cfg else False,
        "content_enabled": cfg.content.enabled if cfg else False,
    }

@router.get("/api/instances")
async def list_instances(request: Request, include: str = "") -> list[dict]:
    display_host = _request_host(request)
    guest = not await is_request_authenticated(request)
    # Opt-in only: the default payload is the hot path (polled every few seconds
    # per open tab) and specs are fat — one control tool alone carries 23 full
    # JSON schemas. Guests never get them; _guest_view is an allowlist anyway.
    want_specs = not guest and "specs" in {p.strip() for p in include.split(",")}

    def _build() -> list[dict]:
        # One directory scan feeds both the states and the enrichment, and the
        # whole disk walk stays off the event loop — the UI polls this endpoint
        # every few seconds per open tab.
        configs = load_all_configs()
        shared = shared_proxy.advertised_port()  # one settings read for the whole list
        result = []
        for s in get_all_states(configs):
            cfg = configs.get(s.id)
            d = _instance_to_dict(s, display_host, shared)
            d.update(_config_fields(cfg))
            # None until the first pass has probed it — the UI shows nothing
            # rather than guessing at a colour.
            d["health"] = health.for_instance(s.id)
            if want_specs:
                d["specs"] = _specs_from_tool_file(cfg)["specs"] if cfg else []
            result.append(_guest_view(d) if guest else d)
        return result

    return await asyncio.to_thread(_build)

@router.get("/api/instances/{instance_id}")
async def get_instance(instance_id: str, request: Request) -> dict:
    inst = get_instance_state(instance_id)
    if not inst:
        raise HTTPException(404, f"Instance '{instance_id}' not found")
    cfg = load_config(instance_id)
    d = _instance_to_dict(inst, _request_host(request), shared_proxy.advertised_port())
    d.update(_config_fields(cfg))
    d["health"] = health.for_instance(instance_id)
    if not await is_request_authenticated(request):
        return _guest_view(d)
    return d

@router.get("/api/instances/{instance_id}/specs", dependencies=[Depends(require_auth)])
async def get_specs(instance_id: str) -> dict:
    """Function catalog of an instance, read straight from its tool JSON.

    Deliberately only behind require_auth and not require_code_edit: these are
    metadata, not source, so the info dialog keeps working under --no-code-edit
    and --no-edit.
    """
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, f"Config '{instance_id}' not found")
    info = await asyncio.to_thread(_specs_from_tool_file, cfg)
    return {
        "id": instance_id,
        "description": info["description"] or cfg.description or "",
        "specs": info["specs"],
        # Served here rather than in the instance list: the list is polled every
        # few seconds per tab, and this is one file read per instance.
        "usage": read_usage(instance_id),
        # None unless a tool of the same id ships in examples/ — see
        # _bundled_version_info. Reported, never applied.
        "bundled": await asyncio.to_thread(_bundled_version_info, cfg),
        # What a test call could do here, so the panel can say why a button is
        # missing instead of failing once per click. Metadata, like the rest of
        # this payload — the call itself is admin-only.
        "test_call": {
            "identity_mode": cfg.identity_mode.value,
            "can_identify": tool_call.identity_possible(),
            "locked": bool(cfg.locked),
        },
    }

@router.get("/api/instances/{instance_id}/config", dependencies=[Depends(require_auth)])
async def get_config(instance_id: str) -> dict:
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, f"Config '{instance_id}' not found")
    d = cfg.model_dump()
    d["values"] = mask_secrets(d.get("values", {}))
    # The edit dialog must not guess which fields are credentials — ship the
    # server's classification with the payload so client and server can't drift.
    d["secret_fields"] = sorted(k for k in cfg.values if is_secret_field(k))
    return d

@router.put("/api/instances/{instance_id}", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def update_config(instance_id: str, body: dict) -> dict:
    require_not_locked(instance_id)
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, f"Config '{instance_id}' not found")

    old_server = (cfg.server.host, cfg.server.port, cfg.server.endpoint)

    # Sub-objects come from arbitrary JSON — reject wrong shapes with a 422
    # instead of crashing on .get()/.items() below.
    for key in ("server", "values", "install", "lifecycle", "content"):
        if key in body and not isinstance(body[key], dict):
            raise HTTPException(422, f"'{key}' must be an object")

    # Only allow updating safe fields
    if "name" in body:
        cfg.name = str(body["name"])
    if "description" in body:
        cfg.description = str(body["description"])
    if "category" in body:
        cfg.category = str(body["category"]).strip()
    if "server" in body:
        s = body["server"]
        new_port = s.get("port", cfg.server.port)
        try:
            new_server = ServerConfig(
                host=s.get("host", cfg.server.host),
                port=new_port,
                endpoint=s.get("endpoint", cfg.server.endpoint),
            )
        except Exception as e:
            raise HTTPException(422, f"Invalid server config: {e}")
        if new_server.port != cfg.server.port and not is_port_free(new_server.port, exclude_id=instance_id):
            raise HTTPException(409, f"Port {new_server.port} is already in use")
        cfg.server = new_server
    values_changed = False
    if "values" in body:
        # A name that is not a valve of this tool cannot take effect: the loader
        # skips anything the Valves class does not declare (tool_loader, the
        # hasattr check), so accepting it would persist a setting that does
        # nothing and answer "ok". That is how `{"lifecycle": {...}}` — which
        # belongs at the top level of this body, not inside `values` — was
        # stored as a valve and reported as saved.
        #
        # Asked of the tool code, not of `cfg.values`: the two agree only while
        # nothing wrong has ever been written into the config, and the very key
        # this guard exists to catch was already sitting in `values` — vouching
        # for itself. None means the valves could not be determined (tool file
        # missing, no Valves class, unparsable code); that is "cannot tell",
        # not "has none", so nothing is refused.
        declared = _valve_names_from_tool_file(cfg)
        if declared is not None:
            unknown = [k for k in body["values"] if k not in declared]
            if unknown:
                raise HTTPException(422, (
                    f"'{instance_id}' has no valve named {', '.join(repr(k) for k in unknown)}. "
                    f"Its valves are: {', '.join(sorted(declared))}. "
                    "auto_start and restart_on_change are not valves — they belong in "
                    "the 'lifecycle' field of this request, beside 'values'."
                ))
        # GET /config masks secrets — at any depth — and a client echoing the
        # config back must not overwrite the real values with the mask. Masks
        # are put back before the update, so a nested credential survives a
        # read-and-save that never touched it.
        old_values = dict(cfg.values)
        cfg.values.update(keep_masked_values(body["values"], old_values))
        values_changed = cfg.values != old_values
    deps_changed = False
    if "install" in body:
        i = body["install"]
        old_deps = list(cfg.install.dependencies)
        old_upgrade = cfg.install.upgrade
        cfg.install = InstallConfig(
            dependencies=i.get("dependencies", cfg.install.dependencies),
            upgrade=i.get("upgrade", cfg.install.upgrade),
        )
        deps_changed = (
            list(cfg.install.dependencies) != old_deps
            or cfg.install.upgrade != old_upgrade
        )
    if "lifecycle" in body:
        lc = body["lifecycle"]
        cfg.lifecycle.auto_start = lc.get("auto_start", cfg.lifecycle.auto_start)
        cfg.lifecycle.restart_on_change = lc.get("restart_on_change", cfg.lifecycle.restart_on_change)

    content_changed = False
    if "content" in body:
        c = body["content"]
        enabled = c.get("enabled", cfg.content.enabled)
        if not isinstance(enabled, bool):
            raise HTTPException(422, "content.enabled must be true or false")
        prefix = str(c.get("url_prefix", cfg.content.url_prefix)).strip()
        if prefix and not prefix.startswith("/"):
            raise HTTPException(422, "content.url_prefix must start with '/'")
        new_content = ContentConfig(enabled=enabled, url_prefix=prefix)
        if new_content != cfg.content:
            cfg.content = new_content
            content_changed = True

    identity_changed = False
    if "identity_mode" in body:
        raw_mode = str(body["identity_mode"]).strip().lower()
        try:
            new_identity_mode = IdentityMode(raw_mode)
        except ValueError:
            raise HTTPException(422, f"Invalid identity_mode '{raw_mode}': off, optional or required")
        if new_identity_mode != cfg.identity_mode:
            cfg.identity_mode = new_identity_mode
            identity_changed = True

    if "forward_agent_token" in body:
        raw_forward = body["forward_agent_token"]
        if not isinstance(raw_forward, bool):
            raise HTTPException(422, "forward_agent_token must be true or false")
        if raw_forward != cfg.forward_agent_token:
            cfg.forward_agent_token = raw_forward
            identity_changed = True

    venv_changed = False
    if "venv" in body:
        new_venv = str(body["venv"]).strip()
        if not re.fullmatch(r"[a-zA-Z0-9_\-]+", new_venv):
            raise HTTPException(400, f"Invalid venv name '{new_venv}': letters, digits, _ and - only")
        if new_venv != cfg.venv:
            cfg.venv = new_venv
            venv_changed = True

    # Install deps before persisting when the venv changed (deps must exist in
    # the new venv) or the dependency list changed (otherwise the instance would
    # restart into a venv missing the new packages). A failed install must not
    # leave the config pointing at an unprepared venv.
    if venv_changed or deps_changed:
        ok, err = await asyncio.to_thread(
            install_dependencies, instance_id, cfg.install.dependencies, cfg.install.upgrade, cfg.venv
        )
        if not ok:
            raise HTTPException(422, {"message": f"Could not prepare venv '{cfg.venv}'", "errors": [err]})

    save_config(cfg)

    # Restart so the runner picks up the new interpreter / address / deps (below).
    inst = get_instance_state(instance_id)
    server_changed = (cfg.server.host, cfg.server.port, cfg.server.endpoint) != old_server

    # Values only take effect at runner startup, so a changed value needs a
    # restart just like a changed address — gated by restart_on_change below.
    needs_restart = (server_changed or venv_changed or deps_changed or values_changed
                     or identity_changed or content_changed)
    restarted, restart_error = False, ""
    if inst:
        inst.name = cfg.name
        inst.category = cfg.category
        if needs_restart and inst.status == MCPStatus.running:
            if cfg.lifecycle.restart_on_change or venv_changed or deps_changed:
                # Restart so the subprocess binds the new address / uses the new
                # venv / picks up freshly installed dependencies.
                reason = "venv" if venv_changed else "dependencies" if deps_changed \
                    else "server config" if server_changed \
                    else "identity mode" if identity_changed \
                    else "content storage" if content_changed else "values"
                logger.info(f"{reason} changed for '{instance_id}', restarting")
                # The config is already written. Whether the runner picked it
                # up is a separate answer, and a failed restart that reports
                # success leaves the old process serving the old settings.
                restarted, restart_error = await asyncio.to_thread(restart_instance, instance_id)
                if not restarted:
                    logger.warning(
                        f"'{instance_id}': config saved, restart failed — {restart_error}")
            else:
                # Keep UI pointing at what is actually running until user restarts manually
                pass
        else:
            # Not running or nothing that needs a restart: safe to update displayed URL now
            inst.port = cfg.server.port
            inst.host = cfg.server.host
            inst.endpoint = cfg.server.endpoint
            inst.url = f"http://{cfg.server.host}:{cfg.server.port}{cfg.server.endpoint}"
            set_instance_state(inst)

    return {"ok": True, "restarted": restarted, "restart_error": restart_error}

@router.post("/api/instances/{instance_id}/start", dependencies=[Depends(require_auth)])
async def start(instance_id: str) -> dict:
    if not config_exists(instance_id):
        raise HTTPException(404, "Config not found")
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(422, f"Config file for '{instance_id}' exists but could not be parsed")
    # A running instance holds its own port open — the OS-level check below
    # would see that listener as "busy" and reassign the port of a healthy
    # instance. Bail out first instead (only when the process really is alive;
    # a stale 'running' state with a dead pid must still be startable).
    inst = get_instance_state(instance_id)
    if inst and inst.status == MCPStatus.running and inst.pid and _is_pid_alive(inst.pid):
        raise HTTPException(409, f"Instance '{instance_id}' is already running")
    if not is_port_free(cfg.server.port, exclude_id=instance_id):
        new_port = find_free_port(cfg.server.port + 1)
        logger.warning(f"Port {cfg.server.port} busy for '{instance_id}', reassigning to {new_port}")
        cfg.server = cfg.server.model_copy(update={"port": new_port})
        save_config(cfg)
        inst = get_instance_state(instance_id)
        if inst:
            inst.port = new_port
            inst.url = f"http://{inst.host}:{new_port}{inst.endpoint}"
            set_instance_state(inst)
    ok, err = await asyncio.to_thread(start_instance, instance_id)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True}

@router.post("/api/instances/{instance_id}/stop", dependencies=[Depends(require_auth)])
async def stop(instance_id: str) -> dict:
    # Deliberately allowed on locked instances: lock means "don't modify",
    # but a misbehaving instance must always be stoppable.
    ok, err = await asyncio.to_thread(stop_instance, instance_id)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True}

@router.post("/api/instances/{instance_id}/restart", dependencies=[Depends(require_auth)])
async def restart(instance_id: str) -> dict:
    require_not_locked(instance_id)
    ok, err = await asyncio.to_thread(restart_instance, instance_id)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True}

@router.post("/api/instances/{instance_id}/lock", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def lock_instance(instance_id: str) -> dict:
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, f"Config '{instance_id}' not found")
    cfg.locked = True
    save_config(cfg)
    return {"ok": True, "locked": True}

@router.post("/api/instances/{instance_id}/unlock", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def unlock_instance(instance_id: str) -> dict:
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, f"Config '{instance_id}' not found")
    cfg.locked = False
    save_config(cfg)
    return {"ok": True, "locked": False}

# require_admin_auth: the payload embeds the MCP Bearer token, so this GET is
# closed to the read-only token.
@router.get("/api/instances/{instance_id}/export", dependencies=[Depends(require_auth), Depends(require_admin_auth)])
async def export_instance(instance_id: str, request: Request) -> JSONResponse:
    inst = get_instance_state(instance_id)
    if not inst:
        raise HTTPException(404, f"Instance '{instance_id}' not found")
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, f"Config '{instance_id}' not found")

    display_host = _request_host(request) or cfg.server.host
    host_in_url = f"[{display_host}]" if ":" in display_host else display_host
    # The same choice the dashboard makes: a port of its own, else the manager
    # port, else the instance's own address. An export is pasted into OpenWebUI
    # and has to name the address that answers there.
    via_port = shared_proxy.advertised_port()
    if via_port:
        url = f"http://{host_in_url}:{via_port}/mcp/{instance_id}"
    else:
        url = f"http://{host_in_url}:{inst.port}{inst.endpoint}"

    token = mcp_bearer_token()
    result = [{
        "type": "mcp",
        "url": url,
        "spec_type": "url",
        "spec": "",
        "path": "openapi.json",
        "auth_type": "bearer" if token else "none",
        "key": token or "",
        "info": {
            "id": instance_id,
            "name": cfg.name,
            "description": cfg.description or cfg.name,
        }
    }]
    return JSONResponse(
        content=result,
        headers={"Content-Disposition": f'attachment; filename="{instance_id}-mcp-server.json"'},
    )

def _caller(sub: str) -> dict:
    """Fill in email, name and role for *sub* from what this server knows.

    The claims are looked up, never taken from the request: the panel says
    *who* to call as, the server decides what that identity consists of.
    Otherwise the dialog could hand a tool a role its owner does not have, and
    the access rules would be testing a fiction.

    An unknown sub is allowed through bare. That is deliberate and matches the
    rights dialog, where a rule may be written for someone who has not called
    yet — "what would a new user see?" is a question worth being able to ask.
    """
    for row in identity_registry.known():
        if row["sub"] == sub:
            return {"sub": sub, "email": row.get("email", ""),
                    "name": row.get("name", ""), "role": row.get("role", "")}
    for row in agent_identity.public_list():
        if row["sub"] == sub:
            return {"sub": sub, "email": "", "name": row.get("name", ""),
                    "role": row.get("role", "")}
    try:
        users = load_policy().get("users")
    except PolicyError:
        users = None
    entry = users.get(sub) if isinstance(users, dict) else None
    if isinstance(entry, dict):
        return {"sub": sub, "email": str(entry.get("email", "")), "name": "", "role": ""}
    return {"sub": sub, "email": "", "name": "", "role": ""}


# require_admin_auth: this runs the instance's real code with the instance's
# real credentials. The read token may look at the dashboard; it may not make
# the server do things on its behalf. require_not_locked for the same reason a
# locked instance refuses an edit — locking it is the user saying "leave this
# one alone".
@router.post("/api/instances/{instance_id}/call", dependencies=[Depends(require_auth), Depends(require_admin_auth)])
async def call_tool(instance_id: str, body: dict) -> dict:
    """Call one tool and hand back the raw answer.

    Exists to separate two failures that look identical from a chat window: a
    broken tool, and a small model that called a working tool wrongly. What
    comes back here is what the model would have received — including the
    length, which is the usual culprit.
    """
    require_not_locked(instance_id)
    inst = get_instance_state(instance_id)
    if not inst:
        raise HTTPException(404, f"Instance '{instance_id}' not found")
    if inst.status != MCPStatus.running:
        raise HTTPException(409, f"Instance '{instance_id}' is not running — start it first")

    tool = str(body.get("tool") or "").strip()
    if not tool:
        raise HTTPException(422, "No tool named")
    arguments = body.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise HTTPException(422, "'arguments' must be an object")
    as_user = str(body.get("as_user") or "").strip()

    result = await tool_call.call(inst, tool, arguments,
                                  _caller(as_user) if as_user else None)
    # One line per test call, in the manager log rather than the instance's:
    # the argument values may be anybody's data, so only the shape is written.
    logger.info(f"Test call '{instance_id}.{tool}' "
                f"({', '.join(sorted(arguments)) or 'no arguments'})"
                f"{f' as {as_user}' if as_user else ''} → "
                f"{'error: ' + result['error'] if not result.get('ok') else ('tool error' if result.get('is_error') else 'ok')}"
                f" in {result.get('duration_ms', 0)} ms")
    return {"instance": instance_id, "tool": tool, "as_user": as_user, **result}


@router.delete("/api/instances/{instance_id}", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def delete_instance(instance_id: str) -> dict:
    require_not_locked(instance_id)
    inst = get_instance_state(instance_id)
    # 'starting' counts as alive: deleting inside the startup window would
    # otherwise skip the stop and orphan the runner that is just coming up —
    # config and state gone, process holding the port.
    if inst and inst.status in (MCPStatus.running, MCPStatus.starting):
        ok, err = await asyncio.to_thread(stop_instance, instance_id)
        if not ok:
            # Deleting the config while the process is still alive would orphan
            # it — nothing would know its PID or port anymore.
            raise HTTPException(500, f"Delete aborted — stop failed: {err}")
    cfg = load_config(instance_id)
    if not delete_config(instance_id):
        raise HTTPException(404, "Config not found")
    forget_usage(instance_id)
    # The files go with the instance. Nothing else can reach them afterwards:
    # the folder is addressed by instance id, and that id is now free to be
    # reused by something entirely unrelated.
    forget_content(instance_id)
    if cfg:
        tool_path = resolve_tool_path(cfg).resolve()
        # Only delete the tool file if no other instance still references it (B8).
        # delete_config already removed this instance, so load_all_configs() lists
        # only the survivors.
        shared = any(
            resolve_tool_path(c).resolve() == tool_path
            for c in load_all_configs().values()
        )
        if not shared and tool_path.exists() and tool_path.is_relative_to(TOOLS_DIR.resolve()):
            tool_path.unlink(missing_ok=True)
    return {"ok": True}
