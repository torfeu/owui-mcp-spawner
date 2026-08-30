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


# (path, mtime_ns, size) → parsed dict. Settings are read on every tool call
# (content quota, retention, base URL) and on every poll; parsing the same
# unchanged file each time is pure waste. The key includes the path so tests
# that repoint SETTINGS_FILE are never served another file's cache, and any
# write — ours or an editor's — changes mtime_ns and invalidates it.
_cache: tuple[tuple, dict] | None = None


def load_settings() -> dict:
    """Return the persisted settings dict (empty dict if file missing or unreadable)."""
    global _cache
    try:
        st = SETTINGS_FILE.stat()
        key = (str(SETTINGS_FILE), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _cache is not None and _cache[0] == key:
        return dict(_cache[1])
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    _cache = (key, data)
    return dict(data)


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
