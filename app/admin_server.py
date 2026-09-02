import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import category_endpoint, shared_proxy
from .activity import prune as prune_usage
from .content_store import ensure_secret, prune as prune_content
from .api_helpers import APP_VERSION
from .config_store import (BASE_DIR, find_free_port, get_instance_state, is_port_free, load_all_configs, resolve_tool_path, save_config, set_instance_state)
from .dependency_manager import install_dependencies
from .health import health_loop
from .logger import get_manager_logger
from .process_manager import check_running_instances, start_instance, sync_state_from_pids
from .schema import MCPStatus
from .settings_store import atomic_write_text
from .tool_editor import validate_tool_code
from .update_check import update_check_loop
from .venv_manager import python_path
from .routes import (agent_identities, auth, backup, categories, content, instances,
                     logs, permissions, settings, system, tools, usage, venvs)

logger = get_manager_logger()
WATCHDOG_INTERVAL = 10

async def _watchdog_loop() -> None:
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        try:
            await asyncio.to_thread(check_running_instances)
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        # Self-heal the shared-port proxy: a failed bind at boot (e.g. TIME_WAIT
        # after a self-restart) or a crashed listener gets retried here.
        try:
            sp = shared_proxy.configured_port()
            if sp and not shared_proxy.proxy_running():
                logger.warning(f"Shared MCP port {sp} is configured but down — retrying bind")
                await shared_proxy.start_proxy(sp, os.environ.get("MCP_RUNNER_HOST", "127.0.0.1"))
        except Exception as e:
            logger.error(f"Shared-port watchdog error: {e}")

PRUNE_INTERVAL = 24 * 60 * 60

async def _prune_usage_loop() -> None:
    """Daily housekeeping: usage events and stored files past their window.

    Deliberately not in the runners: ten processes attempting the same
    housekeeping would only fight over the write lock — and over each other's
    files. `totals` is never pruned, so "ever used" survives any retention
    setting; content retention is off by default and deletes nothing until
    somebody sets a window.
    """
    # The old JSON counters were superseded by runtime/usage.db before anyone
    # collected real data with them.
    legacy = BASE_DIR / "runtime" / "activity"
    if legacy.is_dir():
        try:
            for stale in legacy.glob("*.json"):
                stale.unlink(missing_ok=True)
            legacy.rmdir()
            logger.info("Removed the superseded JSON usage counters")
        except Exception as e:
            logger.debug(f"Could not remove legacy usage counters: {e}")

    while True:
        try:
            removed = await asyncio.to_thread(prune_usage)
            if removed:
                logger.info(f"Pruned {removed} usage event(s) past the retention window")
        except Exception as e:
            logger.debug(f"Usage pruning failed: {e}")
        try:
            removed = await asyncio.to_thread(prune_content)
            if removed:
                logger.info(f"Deleted {removed} stored file(s) past the retention window")
        except Exception as e:
            logger.debug(f"Content pruning failed: {e}")
        await asyncio.sleep(PRUNE_INTERVAL)

_SPECS_MIGRATION_MARKER = BASE_DIR / "runtime" / ".specs_migrated"

async def _migrate_tool_specs() -> None:
    """One-time: rebuild the `specs` of installed tools from their own code.

    Tools uploaded as a finished OpenWebUI JSON kept whatever specs that export
    carried — frequently just the first line of each docstring, cut off
    mid-sentence. Those descriptions are exactly what the info dialog, the
    specs API and a tool router hand to a model, so a truncated one is worse
    than none: it reads like a complete sentence and isn't. The generator that
    every framework-created tool already uses derives them in full from the
    same code.

    New uploads are fixed at the source (`_provision_new_tool`); this repairs
    what is already installed. Runs in the background so a slow validation
    never delays the boot, rewrites a file only when the specs actually differ,
    and ignores the instance lock on purpose: `content` stays untouched, only a
    derived field is recomputed — "don't modify this tool" must not mean "keep
    describing it wrongly forever".
    """
    if _SPECS_MIGRATION_MARKER.exists():
        return
    configs = load_all_configs()
    if not configs:
        return

    repaired, failed = [], []
    for cfg in configs.values():
        try:
            tool_path = resolve_tool_path(cfg)
            if not tool_path.exists():
                continue
            raw = json.loads(tool_path.read_text())
            entry = raw[0] if isinstance(raw, list) and raw else raw
            if not isinstance(entry, dict):
                continue
            code = entry.get("content") or ""
            if not code.strip():
                continue  # MCP-config style instance without embedded code

            validation = await asyncio.to_thread(
                validate_tool_code, code, str(python_path(cfg.venv))
            )
            specs = validation.get("tools") if validation.get("valid") else None
            if not specs:
                failed.append(cfg.id)
                continue
            if specs == entry.get("specs"):
                continue  # already current — don't churn the mtime or the cache

            entry["specs"] = specs
            atomic_write_text(tool_path, json.dumps(raw, indent=2, ensure_ascii=False))
            repaired.append(cfg.id)
        except Exception as e:
            logger.warning(f"Specs migration failed for '{cfg.id}': {e}")
            failed.append(cfg.id)

    if repaired:
        logger.info(
            f"Rebuilt tool specs from code for {len(repaired)} instance(s): "
            f"{', '.join(sorted(repaired))}"
        )
    if failed:
        # Retried on the next start: a tool whose imports are momentarily
        # broken must not be written off permanently.
        logger.warning(
            f"Specs migration incomplete — could not validate: {', '.join(sorted(failed))}"
        )
        return
    try:
        _SPECS_MIGRATION_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _SPECS_MIGRATION_MARKER.write_text("done\n")
    except Exception as e:
        logger.warning(f"Could not write specs migration marker: {e}")

_VENV_MIGRATION_MARKER = BASE_DIR / "runtime" / ".venv_migrated"

async def _migrate_existing_venv_deps() -> None:
    """One-time: install existing instances' deps into their venv.

    Before 0.1 every instance ran in the manager interpreter. On the first 0.1
    start each instance gets its own venv, so its declared dependencies must be
    (re)installed there once. Guarded by a marker file so it never repeats.
    """
    if _VENV_MIGRATION_MARKER.exists():
        return
    configs = load_all_configs()
    with_deps = [c for c in configs.values() if c.install.dependencies]
    all_ok = True
    if with_deps:
        logger.info(
            f"First 0.1 start: installing dependencies for {len(with_deps)} "
            "existing instance(s) into their venvs ..."
        )
        for cfg in with_deps:
            logger.info(f"  migrating deps for '{cfg.id}' → venv '{cfg.venv}'")
            ok, err = await asyncio.to_thread(
                install_dependencies, cfg.id, cfg.install.dependencies,
                cfg.install.upgrade, cfg.venv,
            )
            if not ok:
                all_ok = False
                logger.error(
                    f"Migration of deps for '{cfg.id}' into venv '{cfg.venv}' failed: {err}"
                )

    # Only mark the migration done if every install succeeded; otherwise it is
    # retried on the next start so tools don't end up in venvs without their deps.
    if not all_ok:
        logger.warning(
            "Venv dependency migration incomplete — will retry on next start"
        )
        return
    try:
        _VENV_MIGRATION_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _VENV_MIGRATION_MARKER.write_text("done\n")
    except Exception as e:
        logger.warning(f"Could not write venv migration marker: {e}")

@asynccontextmanager
async def _lifespan(app: FastAPI):
    sync_state_from_pids()
    logger.info("OWUI MCP Spawner started")

    # Create the download-token secret here rather than letting the first
    # runner do it: two runners starting at once would race, and the loser's
    # already-handed-out links would stop verifying.
    await asyncio.to_thread(ensure_secret)

    await _migrate_existing_venv_deps()

    # Shared MCP port: start the reverse proxy before auto-starting instances
    sp = shared_proxy.configured_port()
    if sp:
        ok, err = await shared_proxy.start_proxy(sp, os.environ.get("MCP_RUNNER_HOST", "127.0.0.1"))
        if not ok:
            logger.error(f"Shared MCP port disabled for this run: {err}")

    # Auto-start instances with lifecycle.auto_start = True that aren't already
    # running. Port checks stay sequential (they read and rewrite configs), the
    # starts themselves run concurrently: serial starts put the whole boot —
    # and every MCP client — on hold for the *sum* of the startup times, up to
    # 15 s per instance that never opens its port.
    to_start: list[str] = []
    for cfg in load_all_configs().values():
        if not cfg.lifecycle.auto_start:
            continue
        inst = get_instance_state(cfg.id)
        if inst and inst.status == MCPStatus.running:
            continue
        if not is_port_free(cfg.server.port, exclude_id=cfg.id):
            new_port = find_free_port(cfg.server.port + 1)
            logger.warning(
                f"Auto-start: port {cfg.server.port} busy for '{cfg.id}', reassigning to {new_port}"
            )
            cfg.server = cfg.server.model_copy(update={"port": new_port})
            save_config(cfg)
            if inst:
                inst.port = new_port
                inst.url = f"http://{inst.host}:{new_port}{inst.endpoint}"
                set_instance_state(inst)
        logger.info(f"Auto-starting '{cfg.id}'")
        to_start.append(cfg.id)
    if to_start:
        # Safe concurrently: start_instance holds a per-instance lock around
        # its spawn phase, venv creation has its own per-venv lock, and
        # pids.json writes go through _pids_lock.
        await asyncio.gather(
            *(asyncio.to_thread(start_instance, instance_id) for instance_id in to_start),
            return_exceptions=True,
        )

    watchdog = asyncio.create_task(_watchdog_loop())
    # No-op while the update check is switched off (the default) — it reads the
    # setting on every pass, so toggling it needs no restart.
    updates = asyncio.create_task(update_check_loop())
    # One-time repair, in the background: validating every tool costs a
    # subprocess each and must not hold up the boot or the auto-start above.
    specs = asyncio.create_task(_migrate_tool_specs())
    pruner = asyncio.create_task(_prune_usage_loop())
    # Its own clock: the watchdog runs every ten seconds and only reads a pid,
    # while a health pass opens an MCP session per instance.
    health = asyncio.create_task(health_loop())
    yield
    watchdog.cancel()
    updates.cancel()
    specs.cancel()
    pruner.cancel()
    health.cancel()
    await category_endpoint.stop_all()
    await shared_proxy.stop_dispatch_client()
    await shared_proxy.stop_proxy()

app = FastAPI(title="OWUI MCP Spawner", version=APP_VERSION, lifespan=_lifespan)
for router in (auth.router, instances.router, tools.router, logs.router, venvs.router,
               settings.router, usage.router, permissions.router, content.router,
               system.router, agent_identities.router, backup.router,
               categories.router):
    app.include_router(router)

@app.middleware("http")
async def _revalidate_static(request, call_next):
    response = await call_next(request)
    # The UI is split into ES modules: a cached stale common.js mixed with a
    # fresh instances.js breaks the whole import graph after an upgrade.
    # no-cache keeps files cached but forces revalidation (cheap 304s).
    if request.url.path == "/" or request.url.path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response

WEB_DIR = BASE_DIR / "web"

@app.get("/")
async def root() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")


async def asgi(scope, receive, send):
    """What uvicorn serves: the MCP endpoints, and the manager behind them.

    A dispatcher rather than a mount. `_revalidate_static` above is an
    `@app.middleware("http")`, and Starlette middleware wraps the response of
    every request that reaches the FastAPI app — including a streamable-HTTP
    session that is meant to stay open. Sitting *in front of* the app instead
    means MCP traffic never enters that chain at all.

    Two paths are taken here, each only while its own switch is on: a whole
    category under `/mcp/<segment>/<name>` (`category` unless configured
    otherwise), and a single instance under
    `/mcp/<id>` — the latter the same forwarding the shared-port listener does,
    on the port that is already open. Switched off, a path is not intercepted
    at all, so it ends in the ordinary 404 of a URL this manager does not serve
    rather than in a 403 that announces a feature is there and closed.

    Order matters: the category prefix is longer and is checked first, so a
    category endpoint can never be shadowed by an instance. Whatever the
    segment is set to is a reserved instance ID, so the collision cannot be
    created from the other side either.
    """
    if scope["type"] == "http":
        path = scope.get("path", "")
        if path.startswith(category_endpoint.prefix()) and category_endpoint.enabled():
            await category_endpoint.handle(scope, receive, send)
            return
        if path.startswith(shared_proxy.PREFIX) and shared_proxy.manager_port_enabled():
            await shared_proxy.handle(scope, receive, send)
            return
    # Everything else, lifespan included: the manager's own startup and
    # shutdown run in the FastAPI app and must keep doing so.
    await app(scope, receive, send)
