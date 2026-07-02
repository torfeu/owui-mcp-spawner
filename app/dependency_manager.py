import subprocess

from .logger import get_install_log_path, get_manager_logger
from .security import validate_package_spec
from .venv_manager import DEFAULT_VENV, ensure_venv, python_path

logger = get_manager_logger()

# Per-package pip timeout — large wheels (torch, scipy, ...) can take minutes.
PIP_TIMEOUT = 600


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

    for dep in dependencies:
        cmd = [str(py), "-m", "pip", "install", dep]
        if upgrade:
            cmd.append("--upgrade")

        _log(instance_id, f"$ {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=PIP_TIMEOUT,
            )
            if result.stdout:
                for line in result.stdout.strip().splitlines():
                    _log(instance_id, line)
            if result.returncode != 0:
                err = result.stderr.strip()
                _log(instance_id, f"[ERROR] Failed: {err}")
                return False, f"Failed to install {dep}: {err}"
            else:
                _log(instance_id, f"[OK] {dep}")
        except subprocess.TimeoutExpired:
            msg = f"Timeout installing {dep}"
            _log(instance_id, f"[ERROR] {msg}")
            return False, msg
        except Exception as e:
            msg = f"Exception installing {dep}: {e}"
            _log(instance_id, f"[ERROR] {msg}")
            return False, msg

    _log(instance_id, "All dependencies installed successfully.")
    return True, ""
