import json
import subprocess
from pathlib import Path

from .logger import get_install_log_path, get_manager_logger
from .security import validate_package_spec
from .venv_manager import DEFAULT_VENV, ensure_venv, python_path

logger = get_manager_logger()

# Per-package pip timeout — large wheels (torch, scipy, ...) can take minutes.
PIP_TIMEOUT = 600
# `pip check` reads installed metadata and does not go to the network.
PIP_CHECK_TIMEOUT = 120
# Conflicts an install left behind, per venv. A file rather than memory: the
# damage outlives the manager process, so the note has to as well.
CONFLICTS_DIR = Path(__file__).parent.parent / "runtime" / "venv-conflicts"


def _log(instance_id: str, msg: str) -> None:
    log_path = get_install_log_path(instance_id)
    with open(log_path, "a") as f:
        f.write(msg + "\n")
    logger.info(f"[{instance_id}] {msg}")


def install_dependencies(
    instance_id: str,
    dependencies: list[str],
    upgrade: bool = False,
    venv: str = DEFAULT_VENV,
) -> tuple[bool, str]:
    """Install dependencies into the instance's venv. Returns (success, error_message)."""
    log_path = get_install_log_path(instance_id)
    log_path.write_text("")  # clear log

    # Make sure the target venv exists (creates it + base packages on first use).
    ok, err = ensure_venv(venv, log=lambda m: _log(instance_id, m))
    if not ok:
        return False, err

    py = python_path(venv)

    if not dependencies:
        _log(instance_id, "No dependencies to install.")
        return True, ""

    invalid = [d for d in dependencies if not validate_package_spec(d)]
    if invalid:
        msg = f"Invalid/unsafe package specs: {invalid}"
        _log(instance_id, f"[ERROR] {msg}")
        return False, msg

    _log(instance_id, f"Installing {len(dependencies)} dependencies into venv '{venv}'...")

    # What pip complains about *before* we touch anything. A venv that is
    # already inconsistent — shared with another instance, or broken by hand —
    # must not make every later install fail for a problem it did not cause.
    #
    # With one exception, and it is the whole reason the marker below exists:
    # a complaint *we* left behind is not somebody else's old problem. An
    # install that ends inconsistent leaves the packages where they are, so on
    # the very next attempt its own damage looks pre-existing and gets the
    # exemption — the same call, repeated, reported success over a venv that
    # was still broken. Lines we already know are ours get no pass.
    unresolved = _read_unresolved(venv)
    before = [line for line in _conflicts(py, instance_id) if line not in unresolved]

    # One call for all of them, so pip resolves them against each other. Run
    # one at a time, a later package could replace a version an earlier one
    # required: both calls exit 0, the function reported success, and the venv
    # was left with an import that fails at runtime — "review-a 1.0 has
    # requirement review-shared==1.0, but you have review-shared 2.0".
    cmd = [str(py), "-m", "pip", "install", *dependencies]
    if upgrade:
        cmd.append("--upgrade")

    _log(instance_id, f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PIP_TIMEOUT)
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                _log(instance_id, line)
        if result.returncode != 0:
            err = result.stderr.strip()
            _log(instance_id, f"[ERROR] Failed: {err}")
            return False, f"Failed to install {', '.join(dependencies)}: {err}"
    except subprocess.TimeoutExpired:
        msg = f"Timeout installing {', '.join(dependencies)}"
        _log(instance_id, f"[ERROR] {msg}")
        return False, msg
    except Exception as e:
        msg = f"Exception installing {', '.join(dependencies)}: {e}"
        _log(instance_id, f"[ERROR] {msg}")
        return False, msg

    # Exit code 0 is not the same as a usable environment: pip installs what it
    # was asked for and reports what that broke only if it is asked. Anything
    # new since the check above is ours.
    after = _conflicts(py, instance_id)
    introduced = [line for line in after if line not in before]
    if introduced:
        msg = "Installed, but the environment is inconsistent: " + " ".join(introduced)
        _log(instance_id, f"[ERROR] {msg}")
        # The packages are on disk. Saying so is the point: an instance whose
        # venv contradicts itself fails at import time, in the runtime log,
        # far away from the install that caused it. Written down so a repeat of
        # this call cannot inherit the damage as somebody else's.
        _write_unresolved(venv, after)
        return False, msg

    # Whatever was on the list and is gone is genuinely resolved.
    _write_unresolved(venv, [line for line in unresolved if line in after])
    _log(instance_id, "All dependencies installed successfully.")
    return True, ""


def _unresolved_path(venv: str):
    """Where the unfinished business of one venv is noted."""
    return CONFLICTS_DIR / f"{venv}.json"


def _read_unresolved(venv: str) -> list:
    try:
        data = json.loads(_unresolved_path(venv).read_text())
    except (OSError, ValueError):
        return []
    return [line for line in data if isinstance(line, str)] if isinstance(data, list) else []


def _write_unresolved(venv: str, lines: list) -> None:
    path = _unresolved_path(venv)
    try:
        if not lines:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(set(lines)), indent=2))
    except OSError as e:
        # A note that cannot be written must not break an install; the worst
        # case is the exemption this note exists to withhold.
        logger.warning(f"Could not record venv conflicts for '{venv}': {e}")


def _conflicts(py, instance_id: str) -> list:
    """What `pip check` complains about in this venv, line by line.

    An unusable `pip check` (missing, timing out) yields nothing: this is a
    guard, and a guard that cannot run must not turn every install into a
    failure.
    """
    try:
        result = subprocess.run([str(py), "-m", "pip", "check"],
                                capture_output=True, text=True, timeout=PIP_CHECK_TIMEOUT)
    except Exception as e:
        _log(instance_id, f"[WARN] Could not check the environment: {e}")
        return []
    if result.returncode == 0:
        return []
    return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
