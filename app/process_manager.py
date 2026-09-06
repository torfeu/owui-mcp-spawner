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
from .settings_store import atomic_write_text
from .venv_manager import ensure_venv, python_path

logger = get_manager_logger()

BASE_DIR = Path(__file__).parent.parent
PIDS_FILE = BASE_DIR / "runtime" / "pids.json"
RUNNER_SCRIPT = Path(__file__).parent / "mcp_runner.py"

# Serializes read-modify-write cycles on pids.json (endpoints run in worker threads)
_pids_lock = threading.Lock()

# One lock per instance around the check-and-spawn phase of start_instance.
# Without it two concurrent starts (double click, agent + human) both pass the
# "already running" check and spawn two runners — the loser's bind failure then
# marks the healthy instance failed and drops its pids entry, leaving the
# winner running but untracked. Only the spawn phase is locked; the health-check
# wait stays outside so a concurrent stop_instance can still interrupt it.
_start_locks: dict[str, threading.Lock] = {}
_start_locks_guard = threading.Lock()

# The second, *short* lock — the one stop_instance can afford to wait for.
# The start lock above is held across ensure_venv(), which builds a venv and
# runs pip: minutes, sometimes. A stop that waited for it would be a stop that
# hangs, so stop deliberately does not take it — and that is exactly how a
# confirmed stop used to leave a runner behind. It returned "stopped" while the
# start was still inside ensure_venv; the start then spawned anyway, saw the
# stop afterwards and returned an error without killing what it had just
# created. Status stopped, pid set, port held, and the watchdog only looks at
# instances marked running.
#
# So the two sides meet on this lock instead, held only for moments: start
# takes it to make the last cancellation check, spawn, and publish the pid as
# one indivisible step; stop takes it to mark the instance stopping and read
# the pid it must kill, then releases it and does the killing outside. Whoever
# gets there first, the other one sees it.
_spawn_locks: dict[str, threading.Lock] = {}
_spawn_locks_guard = threading.Lock()


def _lock_from(registry: dict, guard: threading.Lock, instance_id: str) -> threading.Lock:
    with guard:
        lock = registry.get(instance_id)
        if lock is None:
            lock = threading.Lock()
            registry[instance_id] = lock
        return lock


def _start_lock_for(instance_id: str) -> threading.Lock:
    return _lock_from(_start_locks, _start_locks_guard, instance_id)


def _spawn_lock_for(instance_id: str) -> threading.Lock:
    return _lock_from(_spawn_locks, _spawn_locks_guard, instance_id)


# Which start currently speaks for an instance. Read and written only under
# that instance's spawn lock.
#
# The lock alone was not enough. It made each individual step indivisible, but
# a start recognised a stop only by the shared status — and a *later* start
# sets that status back to starting and then running. So: A spawns and waits
# for its port; a stop kills A; B spawns and registers itself; A wakes up,
# finds the status "running" again, decides it was never stopped, and writes
# its own long-dead pid over B's. The manager and the watchdog then follow a
# pid that is gone while B holds the port, which is the orphan from the other
# direction.
#
# A number settles it. Every start takes the next one; every stop takes one
# too, which makes whatever start was holding the previous number stale. Stale
# is permanent: a later start raises the number further, it can never hand an
# earlier one back its authority.
_generations: dict[str, int] = {}


def _claim_generation(instance_id: str) -> int:
    """Take the next number for this instance. Call under its spawn lock."""
    number = _generations.get(instance_id, 0) + 1
    _generations[instance_id] = number
    return number


def _is_current(instance_id: str, generation: int) -> bool:
    """Whether *generation* still speaks for the instance."""
    return _generations.get(instance_id, 0) == generation


def _abandon(proc, instance_id: str) -> None:
    """Kill a runner this start spawned but must not keep.

    Anything that spawns and then gives up has to come through here. A process
    nobody publishes a pid for cannot be stopped from the dashboard and cannot
    be seen by the watchdog; it would simply sit on the port until someone
    finds it with `ps`.
    """
    try:
        proc.terminate()
        if not _wait_pid_gone(proc.pid, 5.0):
            proc.kill()
            _wait_pid_gone(proc.pid, 2.0)
    except Exception as e:
        logger.warning(f"Could not clean up abandoned runner for '{instance_id}': {e}")
    else:
        logger.info(f"Cleaned up runner for '{instance_id}' (pid={proc.pid}) — stopped during startup")

def start_in_progress(instance_id: str) -> bool:
    """True while a start or restart holds this instance's spawn lock.

    The health check asks before it judges an instance: a runner that is still
    coming up has no open port yet, and marking it unhealthy would be a race
    with the very start that is about to succeed.
    """
    lock = _start_lock_for(instance_id)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


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
    atomic_write_text(PIDS_FILE, json.dumps(pids, indent=2))


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    """Wait until *pid* is gone, reaping it if it is our zombie child.

    A killed subprocess stays as a zombie until waited on, and os.kill(pid, 0)
    reports zombies as alive — without reaping, a successfully killed runner
    would look unkillable.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass  # not our child (e.g. adopted after a manager restart)
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.25)
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

    def _superseded() -> bool:
        """Whether this start still speaks for the instance.

        Asked instead of reading the shared status: a stop *or* a newer start
        takes the number away, and neither can give it back.
        """
        with _spawn_lock_for(instance_id):
            return not _is_current(instance_id, generation)

    with _start_lock_for(instance_id):
        inst = get_instance_state(instance_id)
        if inst and inst.status == MCPStatus.running:
            pid = inst.pid
            if pid and _is_pid_alive(pid) and _pid_is_our_runner(pid):
                return False, "Already running"
        # A start that got past this lock is either waiting for its port (pid
        # alive) or crashed mid-start (pid dead / never set) — only the first
        # is a reason to refuse.
        if inst and inst.status == MCPStatus.starting and inst.pid and _is_pid_alive(inst.pid):
            return False, "Already starting"

        # Sync instance state with fresh config (port/host may have changed)
        inst.port = cfg.server.port
        inst.host = cfg.server.host
        inst.endpoint = cfg.server.endpoint
        inst.url = f"http://{cfg.server.host}:{cfg.server.port}{cfg.server.endpoint}"
        # Announcing the start and claiming its number are one step, under the
        # lock a stop also takes. Split apart — the state published first, the
        # number taken a moment later — a stop that ran completely in between
        # was simply forgotten: it raised the counter, and the start then took
        # a *higher* number and looked perfectly current. Together, a stop is
        # either entirely before this (and the start is the later intent, which
        # is allowed to proceed) or after it (and takes the number away).
        with _spawn_lock_for(instance_id):
            inst.status = MCPStatus.starting
            inst.error = ""
            set_instance_state(inst)
            generation = _claim_generation(instance_id)

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
        # The child gets its own copy of this handle; the manager's copy is
        # closed in the finally below. Leaving it open cost one file descriptor
        # per start — invisible until an instance that restarts on every config
        # change had been running for weeks.
        log_file = open(log_path, "a")

        try:
            cmd = [runner_python, str(RUNNER_SCRIPT), "--config", str(config_path)]
            # Served through a port that is not this instance's own — the
            # manager port, a shared one, or both: then this instance stays on
            # localhost and only that way in is public. Otherwise it binds what
            # it was told to.
            from . import shared_proxy
            if shared_proxy.instances_localhost_only():
                runner_host = "127.0.0.1"
            else:
                runner_host = os.environ.get("MCP_RUNNER_HOST")
            if runner_host:
                cmd += ["--host", runner_host]

            # Content store: create the folder before the tool can try to write into
            # it, and pass the path explicitly. Popen otherwise inherits the
            # manager's environment unchanged, so env stays None when the store is
            # off — an instance that never opted in sees nothing new.
            env = None
            if cfg.content.enabled:
                from .content_store import ensure_instance_dir, instance_url
                content_dir = ensure_instance_dir(cfg.id)
                if content_dir is not None:
                    env = dict(os.environ)
                    env["MCP_CONTENT_DIR"] = str(content_dir)
                    env["MCP_CONTENT_URL"] = instance_url(cfg.id)

            # Ask once more whether we are still wanted, spawn, and publish the
            # pid — all three under the spawn lock, so a stop cannot slip
            # between the question and the answer. The pid is published while
            # the instance is still 'starting': that is what lets a stop
            # arriving a moment later kill this process instead of missing it.
            with _spawn_lock_for(instance_id):
                if not _is_current(instance_id, generation):
                    return False, "Instance was stopped during startup"
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=log_file,
                    cwd=str(BASE_DIR),
                    env=env,
                )
                inst.pid = proc.pid
                set_instance_state(inst)
        except Exception as e:
            inst.status = MCPStatus.failed
            inst.error = str(e)
            set_instance_state(inst)
            return False, str(e)
        finally:
            log_file.close()

    # Health check: wait until the runner answers on its port instead of
    # blindly assuming success after a fixed delay.
    check_host = runner_host or cfg.server.host
    deadline = time.monotonic() + START_TIMEOUT
    port_open = False
    while time.monotonic() < deadline:
        if _superseded():
            return _stopped_during_startup(proc, instance_id)
        if proc.poll() is not None:
            tail = _log_tail(log_path)
            reason = f"Process exited during startup. Log tail:\n{tail}" if tail \
                else "Process exited during startup"
            # Even a failure is only ours to record while we still speak for
            # the instance: marking it failed after a newer start took over
            # would bury a runner that is coming up fine.
            with _spawn_lock_for(instance_id):
                if not _is_current(instance_id, generation):
                    return False, "Instance was stopped during startup"
                inst.status = MCPStatus.failed
                inst.error = reason
                inst.pid = None
                set_instance_state(inst)
                with _pids_lock:
                    pids = _load_pids()
                    pids.pop(instance_id, None)
                    _save_pids(pids)
            return False, reason
        if _port_answering(check_host, cfg.server.port):
            port_open = True
            break
        time.sleep(0.25)

    if _superseded():
        return _stopped_during_startup(proc, instance_id)

    if not port_open:
        # Process is alive but slow to bind — keep it, but leave a trace in the log
        logger.warning(
            f"'{instance_id}' (pid={proc.pid}) did not open port {cfg.server.port} "
            f"within {START_TIMEOUT:.0f}s — marking running, watchdog will monitor it"
        )

    # Same question, same lock — and pids.json inside it, so a start that lost
    # its claim cannot leave its pid behind in the file either.
    with _spawn_lock_for(instance_id):
        if not _is_current(instance_id, generation):
            return _stopped_during_startup(proc, instance_id)
        inst.status = MCPStatus.running
        inst.pid = proc.pid
        set_instance_state(inst)
        with _pids_lock:
            pids = _load_pids()
            pids[instance_id] = {"pid": proc.pid, "status": "running", "port": cfg.server.port}
            _save_pids(pids)

    logger.info(f"Started {instance_id} (pid={proc.pid}, port={cfg.server.port})")
    return True, ""


def _stopped_during_startup(proc, instance_id: str) -> tuple[bool, str]:
    """Give up on a start a stop overtook — and take the runner with us.

    The stop may have killed this process already (it holds the pid from the
    moment we published it); _abandon() then finds nothing to do. What must not
    happen is returning from here with the process still alive.
    """
    if proc.poll() is None:
        _abandon(proc, instance_id)
    return False, "Instance was stopped during startup"


def stop_instance(instance_id: str) -> tuple[bool, str]:
    inst = get_instance_state(instance_id)
    if not inst:
        return False, "Instance not found"

    pids = _load_pids()

    # Under the spawn lock, so a start that is about to create a runner either
    # has already published its pid (we read it here and kill it) or has not
    # spawned yet (it finds 'stopping' at its own last check and gives up).
    # Only the reading and the marking are locked; the killing below can take
    # seven seconds and holds nothing.
    with _spawn_lock_for(instance_id):
        pid = inst.pid or (pids.get(instance_id, {}).get("pid"))
        inst.status = MCPStatus.stopping
        set_instance_state(inst)
        # Whatever start was holding the current number no longer speaks for
        # this instance — including one that is still waiting for its port and
        # would otherwise publish itself over whatever comes next. The stop
        # keeps the number it took: killing a process can run for seconds, and
        # a start arriving in that window is the later intent and may take
        # over. When it does, this stop no longer speaks for the instance
        # either, and everything below is no longer its to write.
        generation = _claim_generation(instance_id)

    if pid and _is_pid_alive(pid):
        if not _pid_is_our_runner(pid):
            logger.warning(f"PID {pid} for '{instance_id}' is not our runner — skipping kill")
        else:
            dead = False
            try:
                os.kill(pid, signal.SIGTERM)
                dead = _wait_pid_gone(pid, 5.0)
                if not dead:
                    os.kill(pid, signal.SIGKILL)
                    dead = _wait_pid_gone(pid, 2.0)
            except Exception as e:
                logger.warning(f"Error killing {instance_id} (pid={pid}): {e}")
                dead = not _is_pid_alive(pid)
            # Never report "stopped" while the process is still alive: the
            # orphan would keep the port and escape the watchdog (status
            # stopped + pid None is invisible to it).
            if not dead:
                error = f"Could not stop process {pid} — it survived SIGTERM and SIGKILL"
                logger.error(f"Failed to stop {instance_id}: pid {pid} still alive after SIGKILL")
                with _spawn_lock_for(instance_id):
                    if _is_current(instance_id, generation):
                        inst.status = MCPStatus.running
                        inst.pid = pid
                        inst.error = error
                        set_instance_state(inst)
                return False, error

    with _spawn_lock_for(instance_id):
        if not _is_current(instance_id, generation):
            # A start came along while we were killing and has registered its
            # own runner. The process we were asked to end is gone, which is
            # what was asked for — but the state and pids.json now describe
            # *its* runner, and clearing them would leave a live process with
            # no registration and no watchdog.
            logger.info(f"Stopped {instance_id} — a newer start has taken the instance over")
            return True, ""
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
    ok, err = stop_instance(instance_id)
    if not ok:
        return False, f"Restart aborted — stop failed: {err}"
    time.sleep(0.5)
    return start_instance(instance_id)
