"""
Persistent settings for OWUI MCP Spawner.
Stored in runtime/settings.json (gitignored).
Priority: env vars / CLI flags > this file > built-in defaults.
"""
import json
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
SETTINGS_FILE = BASE_DIR / "runtime" / "settings.json"


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* via a temp file + rename, so a crash mid-write
    never leaves a truncated/corrupt file behind."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_settings() -> dict:
    """Return the persisted settings dict (empty dict if file missing or unreadable)."""
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        pass
    return {}


def save_settings(updates: dict) -> None:
    """Merge *updates* into the persisted settings file. None values remove the key."""
    current = load_settings()
    for k, v in updates.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    SETTINGS_FILE.parent.mkdir(exist_ok=True)
    atomic_write_text(SETTINGS_FILE, json.dumps(current, indent=2))
