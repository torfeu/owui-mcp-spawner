import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import shared_proxy
from .api_helpers import APP_VERSION
from .config_store import (BASE_DIR, find_free_port, get_instance_state, is_port_free, load_all_configs, save_config, set_instance_state)
from .dependency_manager import install_dependencies
from .logger import get_manager_logger
from .process_manager import check_running_instances, start_instance, sync_state_from_pids
from .schema import MCPStatus
from .routes import auth, instances, logs, settings, tools, venvs

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

    await _migrate_existing_venv_deps()

    # Shared MCP port: start the reverse proxy before auto-starting instances
    sp = shared_proxy.configured_port()
    if sp:
        ok, err = await shared_proxy.start_proxy(sp, os.environ.get("MCP_RUNNER_HOST", "127.0.0.1"))
        if not ok:
            logger.error(f"Shared MCP port disabled for this run: {err}")

    # Auto-start instances with lifecycle.auto_start = True that aren't already running
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
        await asyncio.to_thread(start_instance, cfg.id)

    watchdog = asyncio.create_task(_watchdog_loop())
    yield
    watchdog.cancel()
    await shared_proxy.stop_proxy()

app = FastAPI(title="OWUI MCP Spawner", version=APP_VERSION, lifespan=_lifespan)
for router in (auth.router, instances.router, tools.router, logs.router, venvs.router, settings.router):
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
