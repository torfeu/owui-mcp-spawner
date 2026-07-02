"""
On-demand virtual-environment pool.

Each MCP instance runs in its own venv under runtime/venvs/<name>/ so a tool's
third-party dependencies never collide with another tool's — and never pollute
the manager process itself. Validation and the runtime both use the instance's
venv Python, so import-time checks see exactly the packages the tool will have
at runtime.

Backward compatibility: configs without a `venv` field use "default", which maps
to a real venv (runtime/venvs/default) created lazily on first use with the base
packages the MCP runner needs (mcp, uvicorn, starlette, pydantic, httpx).
"""
import os
import re
import shutil
import subprocess
import threading
import venv
from pathlib import Path
from typing import Callable, Optional

from .logger import get_manager_logger

logger = get_manager_logger()

BASE_DIR = Path(__file__).parent.parent
VENVS_DIR = BASE_DIR / "runtime" / "venvs"

DEFAULT_VENV = "default"

# Packages every instance venv needs so the MCP runner can import and serve.
BASE_PACKAGES = ["mcp", "uvicorn", "starlette", "pydantic", "httpx"]
READY_MARKER = ".mcp-manager-ready"

# venv creation + base-package install can be slow on first use.
ENSURE_TIMEOUT = 600

_VALID_NAME = re.compile(r"[a-zA-Z0-9_\-]+")

# One lock per venv name so concurrent starts/installs of the *same* venv don't
# race on venv.create / pip (different venvs proceed in parallel). ensure_venv
# runs in a thread pool (asyncio.to_thread), so this is a thread lock.
_venv_locks: dict[str, threading.Lock] = {}
_venv_locks_guard = threading.Lock()


def _lock_for(name: str) -> threading.Lock:
    with _venv_locks_guard:
        lock = _venv_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _venv_locks[name] = lock
        return lock


def _validate_name(name: str) -> None:
    if not name or not _VALID_NAME.fullmatch(name):
        raise ValueError(f"Invalid venv name: {name!r} (letters, digits, _ and - only)")


def venv_dir(name: str = DEFAULT_VENV) -> Path:
    _validate_name(name)
    return VENVS_DIR / name


def python_path(name: str = DEFAULT_VENV) -> Path:
    """Path to the venv's Python interpreter (may not exist yet)."""
    d = venv_dir(name)
    if os.name == "nt":
        return d / "Scripts" / "python.exe"
    return d / "bin" / "python"


def venv_exists(name: str = DEFAULT_VENV) -> bool:
    return python_path(name).exists()


def venv_ready(name: str = DEFAULT_VENV) -> bool:
    """True only when the venv is fully set up (interpreter + ready marker).

    A bare interpreter without the marker is a half-built venv that ensure_venv
    can still repair, so callers that want to *repair* should not treat it as
    done — unlike venv_exists(), which only checks the interpreter.
    """
    return python_path(name).exists() and (venv_dir(name) / READY_MARKER).exists()


def list_venvs() -> list[str]:
    """Names of all created venvs (directories that contain a Python)."""
    if not VENVS_DIR.exists():
        return []
    return sorted(
        p.name for p in VENVS_DIR.iterdir()
        if p.is_dir() and python_path(p.name).exists()
    )


def _base_packages_available(py: Path) -> bool:
    """Return True when the runner's base imports work in this venv."""
    try:
        result = subprocess.run(
            [str(py), "-c", "import mcp, uvicorn, starlette, pydantic, httpx"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def ensure_venv(name: str = DEFAULT_VENV, log: Optional[Callable[[str], None]] = None) -> tuple[bool, str]:
    """Create the venv with base packages if it does not exist yet.

    Idempotent and cheap once the venv exists. Returns (ok, error_message).
    Pass *log* to mirror progress into an instance install log.
    """
    _validate_name(name)
    py = python_path(name)
    d = venv_dir(name)
    marker = d / READY_MARKER
    # Hot path: already ready, no need to serialize on the lock.
    if py.exists() and marker.exists():
        return True, ""

    def _emit(msg: str) -> None:
        logger.info(f"[venv:{name}] {msg}")
        if log:
            log(msg)

    # Serialize creation/install of this venv so concurrent starts of the same
    # venv don't both run venv.create / pip. Re-check inside the lock: a caller
    # that waited may find the venv already built by the one that held it.
    with _lock_for(name):
        if py.exists() and marker.exists():
            return True, ""
        if py.exists() and _base_packages_available(py):
            try:
                marker.write_text("ready\n")
            except Exception as e:
                logger.warning(f"[venv:{name}] Could not write ready marker: {e}")
            return True, ""

        created_this_call = not py.exists()
        if created_this_call:
            _emit(f"Creating venv at {d} ...")
            try:
                VENVS_DIR.mkdir(parents=True, exist_ok=True)
                venv.create(d, with_pip=True, clear=False)
            except Exception as e:
                msg = f"Failed to create venv '{name}': {e}"
                _emit(f"[ERROR] {msg}")
                shutil.rmtree(d, ignore_errors=True)
                return False, msg
        else:
            _emit(f"Completing base package setup in existing venv at {d} ...")

        _emit(f"Installing base packages: {', '.join(BASE_PACKAGES)} ...")
        cmd = [str(py), "-m", "pip", "install", *BASE_PACKAGES]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=ENSURE_TIMEOUT)
        except subprocess.TimeoutExpired:
            msg = f"Timeout installing base packages into venv '{name}'"
            _emit(f"[ERROR] {msg}")
            # Remove the half-built venv so a later start doesn't accept the bare
            # interpreter as valid and skip the base-package install.
            if created_this_call:
                shutil.rmtree(d, ignore_errors=True)
            return False, msg
        except Exception as e:
            msg = f"Failed to install base packages into venv '{name}': {e}"
            _emit(f"[ERROR] {msg}")
            if created_this_call:
                shutil.rmtree(d, ignore_errors=True)
            return False, msg
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[-500:]
            msg = f"Failed to install base packages into venv '{name}': {err}"
            _emit(f"[ERROR] {msg}")
            if created_this_call:
                shutil.rmtree(d, ignore_errors=True)
            return False, msg

        try:
            marker.write_text("ready\n")
        except Exception as e:
            logger.warning(f"[venv:{name}] Could not write ready marker: {e}")
        _emit("Base packages installed.")
        return True, ""


def delete_venv(name: str) -> tuple[bool, str]:
    """Remove a venv directory. The default venv is protected."""
    _validate_name(name)
    if name == DEFAULT_VENV:
        return False, "Cannot delete the default venv"
    d = venv_dir(name)
    if not d.exists():
        return True, ""
    try:
        shutil.rmtree(d)
        return True, ""
    except Exception as e:
        return False, str(e)
