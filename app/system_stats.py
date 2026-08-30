"""What the machine is doing right now — the numbers behind the dashboard tiles.

Deliberately flat: the load of the machine the spawner runs on, plus what each
instance's own process tree costs in memory and CPU. No foreign processes, no
tree to unfold, and no GPU — every GPU reading is vendor-specific (amdgpu sysfs
here, `nvidia-smi` there) and this framework has to run on whatever machine
someone puts it on.

`psutil` is an optional import on purpose. An installation updated by rsync
gets this code before anyone has run pip, and the manager must not fall over in
that window: `available()` is false, the endpoint says so, and the UI leaves
the tiles out. Everything else keeps working exactly as before.

Two rates need a previous sample to mean anything — the machine's network
counters and CPU percentages. Those samples live in module variables, which is
sound here because the manager is a single process; each is guarded so that the
very first reading reports *nothing* rather than a fabricated zero.
"""
import threading
import time
from pathlib import Path

try:
    import psutil
except Exception:                                          # pragma: no cover - depends on the venv
    psutil = None

BASE_DIR = Path(__file__).parent.parent

# A rate computed over less than this is noise: two polls that land almost
# together divide a tiny byte count by a tiny interval and swing wildly.
MIN_SAMPLE_SECONDS = 0.2

# Below this a mount is boot machinery, not storage anyone stores things on:
# on the reference server it drops /boot (1.9 GB) and /boot/efi (1.0 GB) and
# keeps /, /data and /mnt/backup. A size rule rather than a list of paths —
# "/boot" is a Linux name, and this has to run wherever it is installed.
MIN_DISK_BYTES = 4 * 1024 ** 3

# Never a disk somebody keeps data on, whatever their size says.
SKIP_FSTYPES = {"squashfs", "overlay", "tmpfs", "devtmpfs", "vfat", "msdos"}

# The reason handed to the UI when there is nothing to measure with. It names
# the venv rather than just the package: on the server the manager has its own,
# and `pip install psutil` in the wrong shell is the obvious way to miss.
NO_PSUTIL = ("psutil is not installed in the manager's environment — "
             "install it there (.venv/bin/python -m pip install psutil) and restart the manager")

_lock = threading.Lock()
_last_net: tuple[float, int, int] | None = None            # (monotonic, sent, recv)
_last_net_rates: dict = {"up_bps": None, "down_bps": None}
_last_cpu_at = 0.0
_last_cpu_value: float | None = None
_procs: dict[int, "psutil.Process"] = {}                   # kept so cpu_percent has a baseline
_proc_cpu: dict[int, tuple[float, float | None]] = {}      # pid -> (measured at, percent)


def available() -> bool:
    return psutil is not None


def _cpu_percent() -> float | None:
    """Machine-wide CPU load, or the most recent reading if it is still fresh.

    psutil measures against its own previous call, which makes every reading
    destructive: a second reader arriving right behind the first gets a value
    covering microseconds *and* resets the baseline for everyone. With a
    dashboard polling every four seconds, an MCP call landing beside a poll
    used to come back "unknown" while a perfectly good measurement existed.
    So a reader inside the window takes no sample at all and is handed the last
    one. None survives only for the genuine case: nothing measured yet.
    """
    global _last_cpu_at, _last_cpu_value
    now = time.monotonic()
    if now - _last_cpu_at < MIN_SAMPLE_SECONDS:
        return _last_cpu_value
    _last_cpu_value = psutil.cpu_percent(interval=None)
    _last_cpu_at = now
    return _last_cpu_value


def _net_rates() -> dict:
    """Bytes per second in each direction, derived from the machine's counters."""
    global _last_net, _last_net_rates
    now = time.monotonic()
    # Same reasoning as the CPU above, plus one of its own: overwriting the
    # baseline here would move the start of the interval the *next* reader
    # measures over, so the sample is left exactly where it is.
    if _last_net is not None and now - _last_net[0] < MIN_SAMPLE_SECONDS:
        return dict(_last_net_rates)

    counters = psutil.net_io_counters()
    sent, recv = counters.bytes_sent, counters.bytes_recv
    previous, _last_net = _last_net, (now, sent, recv)
    if previous is None:
        _last_net_rates = {"up_bps": None, "down_bps": None}
        return dict(_last_net_rates)
    elapsed = now - previous[0]
    # Counters reset when an interface goes down or the machine reboots; a
    # negative delta is that, not traffic, so it is reported as unknown.
    up, down = sent - previous[1], recv - previous[2]
    if up < 0 or down < 0:
        _last_net_rates = {"up_bps": None, "down_bps": None}
    else:
        _last_net_rates = {"up_bps": up / elapsed, "down_bps": down / elapsed}
    return dict(_last_net_rates)


def _install_mount() -> str:
    """The mount point the installation sits on, or "" if it cannot be told."""
    base = str(BASE_DIR)
    best = ""
    for part in _partitions():
        mount = part.mountpoint
        if base == mount or base.startswith(mount.rstrip("/") + "/"):
            # Longest match wins: with / and /data both mounted, an install
            # under /data belongs to /data, not to /.
            if len(mount) > len(best):
                best = mount
    return best


def _partitions() -> list:
    try:
        return psutil.disk_partitions(all=False)
    except Exception:                                      # pragma: no cover - platform dependent
        return []


def _disks() -> list[dict]:
    """Every mount worth a tile, largest first, with the install one marked.

    All of them, not just the one the installation is on: a machine with a data
    disk and a backup disk was showing only its system disk, which is the least
    interesting of the three.
    """
    install = _install_mount()
    found = []
    for part in _partitions():
        if part.fstype in SKIP_FSTYPES:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue                                       # unreadable mount is not an error
        is_install = part.mountpoint == install
        if usage.total < MIN_DISK_BYTES and not is_install:
            continue
        found.append({"mount": part.mountpoint, "free": usage.free, "total": usage.total,
                      "percent": usage.percent, "install": is_install})

    # Two mounts reporting the same size *and* the same free bytes are the same
    # storage seen twice — a bind mount, or the volumes of one APFS container,
    # which would otherwise fill the tile row with five copies of one disk.
    # Separate drives never agree on free space for long. The install mount
    # wins a tie so its tile keeps the marker.
    found.sort(key=lambda d: (not d["install"], d["mount"]))
    disks, seen = [], set()
    for disk in found:
        key = (disk["total"], disk["free"])
        if key in seen:
            continue
        seen.add(key)
        disks.append(disk)
    disks.sort(key=lambda d: (not d["install"], -d["total"]))
    return disks


def _install_disk(disks: list[dict]) -> dict:
    """The disk the installation lives on — venvs, tool code and the content
    store are all there, so it is the one that decides whether the next
    document can be written. Measured directly if it is not among the mounts.
    """
    for disk in disks:
        if disk["install"]:
            return dict(disk, path=str(BASE_DIR))
    usage = psutil.disk_usage(str(BASE_DIR))
    return {"mount": "", "free": usage.free, "total": usage.total,
            "percent": usage.percent, "install": True, "path": str(BASE_DIR)}


def _machine() -> dict:
    memory = psutil.virtual_memory()
    disks = _disks()
    return {
        "cpu_percent": _cpu_percent(),
        "cpu_count": psutil.cpu_count(logical=True),
        "memory": {"used": memory.total - memory.available, "total": memory.total,
                   "percent": memory.percent},
        # `disk` is the install disk and stays as it was; `disks` is every
        # mount, so a second or third drive is no longer invisible.
        "disk": _install_disk(disks),
        "disks": disks,
        "network": _net_rates(),
    }


def _process_tree(pid: int) -> dict | None:
    """RSS and CPU of one runner plus its children, or None if it is gone.

    Children count: an instance that shells out (an agent CLI, a converter)
    does its real work in a subprocess, and charging the parent alone would
    show the busiest instance as the cheapest one.
    """
    proc = _procs.get(pid)
    if proc is None or not proc.is_running():
        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            _procs.pop(pid, None)
            _proc_cpu.pop(pid, None)
            return None
        _procs[pid] = proc
        _proc_cpu.pop(pid, None)

    now = time.monotonic()
    measured_at, cached = _proc_cpu.get(pid, (0.0, None))
    # Memory is a gauge and can be read as often as anyone likes. CPU is not:
    # it is measured against the last reading of this very Process object, so
    # two readers arriving together would hand each other a 0 % that reads as
    # "idle". Inside the window the previous figure stands.
    fresh_cpu = now - measured_at >= MIN_SAMPLE_SECONDS

    try:
        members = [proc, *proc.children(recursive=True)]
        rss = 0
        cpu = 0.0
        for member in members:
            try:
                rss += member.memory_info().rss
                if fresh_cpu:
                    cpu += member.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue   # a child that exited mid-measurement is not an error
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        _procs.pop(pid, None)
        _proc_cpu.pop(pid, None)
        return None

    if fresh_cpu:
        # The first cpu_percent() a Process object ever answers is 0.0 by
        # definition — report nothing rather than "idle".
        value = None if measured_at == 0.0 else cpu
        _proc_cpu[pid] = (now, value)
    else:
        value = cached

    return {"rss": rss, "processes": len(members), "cpu_percent": value}


def instance_stats(pids: dict[str, int]) -> dict[str, dict]:
    """Per instance id, for those that are actually running.

    An id whose process has since died is simply absent — the caller already
    knows the status of every instance and does not need a second, slightly
    older opinion on it from here.
    """
    if psutil is None:
        return {}
    with _lock:
        alive = {pid for pid in pids.values() if pid}
        for gone in [pid for pid in _procs if pid not in alive]:
            _procs.pop(gone, None)
            _proc_cpu.pop(gone, None)
        result = {}
        for instance_id, pid in pids.items():
            if not pid:
                continue
            stats = _process_tree(pid)
            if stats is not None:
                result[instance_id] = stats
        return result


def machine_stats() -> dict | None:
    if psutil is None:
        return None
    with _lock:
        return _machine()


# Primed at import so the first request already has a baseline to measure
# against — the manager starts long before anyone opens the dashboard.
if psutil is not None:                                     # pragma: no cover - import-time priming
    try:
        psutil.cpu_percent(interval=None)
        _last_cpu_at = time.monotonic()
        _last_net = (time.monotonic(), psutil.net_io_counters().bytes_sent,
                     psutil.net_io_counters().bytes_recv)
    except Exception:
        pass
