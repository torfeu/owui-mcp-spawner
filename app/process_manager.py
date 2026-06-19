import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from .logger import get_manager_logger, get_runtime_log_path
from .schema import MCPInstance, MCPStatus
from .config_store import get_instance_state, set_instance_state, load_config, get_all_states
from .venv_manager import ensure_venv, python_path

logger = get_manager_logger()

BASE_DIR = Path(__file__).parent.parent
PIDS_FILE = BASE_DIR / "runtime" / "pids.json"
RUNNER_SCRIPT = Path(__file__).parent / "mcp_runner.py"

# Serializes read-modify-write cycles on pids.json (endpoints run in worker threads)
_pids_lock = threading.Lock()

# How long start_instance waits for the runner to open its port
START_TIMEOUT = 15.0

# Rotate runtime logs bigger than this when the instance starts
MAX_RUNTIME_LOG_BYTES = 5 * 1024 * 1024


def _load_pids() -> dict:
    if PIDS_FILE.exists():
        try:
            return json.loads(PIDS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_pids(pids: dict) -> None:
    PIDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PIDS_FILE.write_text(json.dumps(pids, indent=2))


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _port_answering(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to the instance port succeeds."""
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.create_connection((connect_host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _log_tail(path: Path, lines: int = 5) -> str:
    """Last few log lines, used as error detail when a runner dies."""
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except Exception:
        return ""


def _rotate_runtime_log(path: Path) -> None:
    """Keep runtime logs bounded: move an oversized log to <name>.1 (one backup)."""
    try:
        if path.exists() and path.stat().st_size > MAX_RUNTIME_LOG_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            path.replace(backup)
    except Exception as e:
        logger.warning(f"Could not rotate log {path.name}: {e}")


def _pid_is_our_runner(pid: int) -> bool:
    """Return True only if the PID belongs to our mcp_runner subprocess.

    Checks /proc on Linux, falls back to `ps` on macOS/other Unix.
    Returns False (safe) when the PID is confirmed to belong to something else,
    or when the process is not found. Returns True only on positive confirmation
    or when the check mechanism itself is completely unavailable.
    """
    # Linux: read /proc directly
    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.exists():
        try:
            cmdline = proc_path.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            return "mcp_runner" in cmdline
        except Exception:
            return False

    # macOS / other Unix: ask ps
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False  # PID not found → not our process
        return "mcp_runner" in result.stdout
    except Exception:
        pass

    # Last resort: we have no way to verify — log and refuse to kill
    logger.warning(f"Cannot verify PID {pid} ownership — skipping kill for safety")
    return False


def sync_state_from_pids() -> None:
    """Reconcile in-memory state with pids.json on startup.

    Port/host/url always come from the current config file — never from the
    (potentially stale) pids.json — to avoid showing wrong URLs after a
    config change.
    """
    pids = _load_pids()
    for instance_id, info in pids.items():
        pid = info.get("pid")
        inst = get_instance_state(instance_id)
        if not inst:
            continue
        # Always sync connection details from the live config
        cfg = load_config(instance_id)
        if cfg:
            inst.port = cfg.server.port
            inst.host = cfg.server.host
            inst.endpoint = cfg.server.endpoint
            inst.url = f"http://{cfg.server.host}:{cfg.server.port}{cfg.server.endpoint}"
        if pid and _is_pid_alive(pid) and _pid_is_our_runner(pid):
            inst.status = MCPStatus.running
            inst.pid = pid
        else:
            inst.status = MCPStatus.stopped
            inst.pid = None
        set_instance_state(inst)

    # Rewrite pids.json: remove dead/foreign entries, update ports from live config
    with _pids_lock:
        cleaned = {}
        for k, v in pids.items():
            pid = v.get("pid")
            if not (pid and _is_pid_alive(pid) and _pid_is_our_runner(pid)):
                continue
            cfg = load_config(k)
            if cfg:
                v["port"] = cfg.server.port
            cleaned[k] = v
        _save_pids(cleaned)


def check_running_instances() -> None:
    """Watchdog pass: flag instances whose runner process has died.

    Called periodically by the manager. Only demotes running → failed;
    it never starts or stops anything itself.
    """
    for inst in get_all_states():
        if inst.status != MCPStatus.running or not inst.pid:
            continue
        if _is_pid_alive(inst.pid) and _pid_is_our_runner(inst.pid):
            continue
        tail = _log_tail(get_runtime_log_path(inst.id))
        logger.warning(f"Watchdog: '{inst.id}' (pid={inst.pid}) died unexpectedly")
        inst.status = MCPStatus.failed
        inst.error = f"Process died unexpectedly. Log tail:\n{tail}" if tail \
            else "Process died unexpectedly"
        inst.pid = None
        set_instance_state(inst)
        with _pids_lock:
            pids = _load_pids()
            pids.pop(inst.id, None)
            _save_pids(pids)


def start_instance(instance_id: str) -> tuple[bool, str]:
    cfg = load_config(instance_id)
    if not cfg:
        return False, "Config not found"

    inst = get_instance_state(instance_id)
    if inst and inst.status == MCPStatus.running:
        pid = inst.pid
        if pid and _is_pid_alive(pid) and _pid_is_our_runner(pid):
            return False, "Already running"

    # Sync instance state with fresh config (port/host may have changed)
    inst.port = cfg.server.port
    inst.host = cfg.server.host
    inst.endpoint = cfg.server.endpoint
    inst.url = f"http://{cfg.server.host}:{cfg.server.port}{cfg.server.endpoint}"
    inst.status = MCPStatus.starting
    inst.error = ""
    set_instance_state(inst)

    # The runner must use the instance's venv so the tool's deps are importable.
    venv_ok, venv_err = ensure_venv(cfg.venv)
    if not venv_ok:
        inst.status = MCPStatus.dependency_error
        inst.error = venv_err
        set_instance_state(inst)
        return False, venv_err
    runner_python = str(python_path(cfg.venv))

    config_path = BASE_DIR / "configs" / f"{instance_id}.json"
    log_path = get_runtime_log_path(instance_id)
    _rotate_runtime_log(log_path)
    log_file = open(log_path, "a")

    try:
        cmd = [runner_python, str(RUNNER_SCRIPT), "--config", str(config_path)]
        runner_host = os.environ.get("MCP_RUNNER_HOST")
        if runner_host:
            cmd += ["--host", runner_host]

        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            cwd=str(BASE_DIR),
        )
    except Exception as e:
        inst.status = MCPStatus.failed
        inst.error = str(e)
        set_instance_state(inst)
        return False, str(e)

    # Health check: wait until the runner answers on its port instead of
    # blindly assuming success after a fixed delay.
    check_host = runner_host or cfg.server.host
    deadline = time.monotonic() + START_TIMEOUT
    port_open = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = _log_tail(log_path)
            inst.status = MCPStatus.failed
            inst.error = f"Process exited during startup. Log tail:\n{tail}" if tail \
                else "Process exited during startup"
            set_instance_state(inst)
            with _pids_lock:
                pids = _load_pids()
                pids.pop(instance_id, None)
                _save_pids(pids)
            return False, inst.error
        if _port_answering(check_host, cfg.server.port):
            port_open = True
            break
        time.sleep(0.25)

    if not port_open:
        # Process is alive but slow to bind — keep it, but leave a trace in the log
        logger.warning(
            f"'{instance_id}' (pid={proc.pid}) did not open port {cfg.server.port} "
            f"within {START_TIMEOUT:.0f}s — marking running, watchdog will monitor it"
        )

    inst.status = MCPStatus.running
    inst.pid = proc.pid
    set_instance_state(inst)

    with _pids_lock:
        pids = _load_pids()
        pids[instance_id] = {"pid": proc.pid, "status": "running", "port": cfg.server.port}
        _save_pids(pids)

    logger.info(f"Started {instance_id} (pid={proc.pid}, port={cfg.server.port})")
    return True, ""


def stop_instance(instance_id: str) -> tuple[bool, str]:
    inst = get_instance_state(instance_id)
    if not inst:
        return False, "Instance not found"

    pids = _load_pids()
    pid = inst.pid or (pids.get(instance_id, {}).get("pid"))

    inst.status = MCPStatus.stopping
    set_instance_state(inst)

    if pid and _is_pid_alive(pid):
        if not _pid_is_our_runner(pid):
            logger.warning(f"PID {pid} for '{instance_id}' is not our runner — skipping kill")
        else:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(10):
                    time.sleep(0.5)
                    if not _is_pid_alive(pid):
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except Exception as e:
                logger.warning(f"Error killing {instance_id} (pid={pid}): {e}")

    inst.status = MCPStatus.stopped
    inst.pid = None
    set_instance_state(inst)

    with _pids_lock:
        pids = _load_pids()
        pids.pop(instance_id, None)
        _save_pids(pids)

    logger.info(f"Stopped {instance_id}")
    return True, ""


def restart_instance(instance_id: str) -> tuple[bool, str]:
    stop_instance(instance_id)
    time.sleep(0.5)
    return start_instance(instance_id)
