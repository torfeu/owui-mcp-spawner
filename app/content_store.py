"""Content store: the files instances produce, and the only two ways out.

An instance that is switched on for content gets `content/<instance_id>/` and
three ways to be told about it (valve autofill, environment, and simply writing
relatively — the runner's cwd is BASE_DIR). What it writes there belongs to us:
quota, retention and deletion are ours, not the tool's.

Two ways in, and no third (decision 2 of the 0.2.1 plan):

* the link in the chat, carrying a token for exactly one file
* the control tool, over the authenticated API

The token is *derived*, not stored: HMAC(server secret, "<instance>/<file>"),
truncated. So there is no sidecar index that can drift out of step with the
directory, the link survives a restart, and revoking access is just deleting
the file. It is worth being clear about what this token is and is not: it
proves that whoever holds the link was given it by us for this one file. It is
not a user identity — anyone the link is forwarded to can fetch that file, and
nothing else.
"""
import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

BASE_DIR = Path(__file__).parent.parent
CONTENT_DIR = BASE_DIR / "content"
SECRET_FILE = BASE_DIR / "runtime" / "content.key"

# Per instance, not for the store as a whole: the warning ends up in front of a
# tool result that a small local model has to act on, and "your folder is full"
# is something it can do something about, whereas "some other instance filled
# the disk" is not.
DEFAULT_MAX_MB = 200
DEFAULT_WARN_PERCENT = 80
DEFAULT_RETENTION_DAYS = 0        # 0 = keep forever
DEFAULT_RETENTION_MODE = "file_age"
RETENTION_MODES = ("file_age", "whole_folder")

# The OpenWebUI convention. Tools written for it return links below this path,
# which resolve against OpenWebUI in the browser — and hit nothing of ours.
DEFAULT_URL_PREFIX = "/cache/files/"

TOKEN_LENGTH = 16
_ID_RE = re.compile(r"[a-zA-Z0-9_\-]+")


# ── Secret ────────────────────────────────────────────────────────────────────

# Cached per file path: the secret never changes once created (rotation means
# deleting the file and restarting), and without the cache every token — one
# per link in a listing — costs a disk read. Keyed by path, not a bare global,
# so tests that repoint SECRET_FILE never see a stale value.
_secret_cache: dict[str, str] = {}


def ensure_secret() -> str:
    """The server secret the file tokens are derived from.

    In its own file rather than in settings.json: runners read this too, and a
    runner doing a read-modify-write on the settings file would sooner or later
    clobber a concurrent write from the manager. O_EXCL makes the creation
    race-free — whoever loses the race reads what the winner wrote.
    """
    cache_key = str(SECRET_FILE)
    cached = _secret_cache.get(cache_key)
    if cached:
        return cached
    try:
        existing = SECRET_FILE.read_text().strip()
        if len(existing) >= 32:
            _secret_cache[cache_key] = existing
            return existing
    except OSError:
        pass
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    candidate = secrets.token_hex(32)
    try:
        fd = os.open(str(SECRET_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(candidate + "\n")
        _secret_cache[cache_key] = candidate
        return candidate
    except FileExistsError:
        current = SECRET_FILE.read_text().strip()
        if len(current) >= 32:
            _secret_cache[cache_key] = current
            return current
        # The file exists but is unusably short (truncated by a full disk, a
        # botched restore) — without this branch the length guard above would
        # be decorative: the O_EXCL create always fails and the corrupt value
        # would be used forever. Replace it atomically; links derived from the
        # corrupt secret die, which is better than weak tokens for good.
        fd, tmp = None, f"{SECRET_FILE}.tmp-{secrets.token_hex(4)}"
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(candidate + "\n")
            os.replace(tmp, SECRET_FILE)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return current or candidate  # cannot repair — serve what there is
        _secret_cache[cache_key] = candidate
        return candidate


def file_token(instance_id: str, filename: str) -> str:
    """Token for exactly this one file — see the module docstring."""
    mac = hmac.new(
        ensure_secret().encode(),
        f"{instance_id}/{filename}".encode(),
        hashlib.sha256,
    )
    return mac.hexdigest()[:TOKEN_LENGTH]


def token_valid(instance_id: str, filename: str, token: str) -> bool:
    return hmac.compare_digest(file_token(instance_id, filename), token or "")


# ── Paths ─────────────────────────────────────────────────────────────────────

def _valid_id(instance_id: str) -> bool:
    return bool(instance_id) and bool(_ID_RE.fullmatch(instance_id))


def instance_dir(instance_id: str) -> Optional[Path]:
    """The folder of *instance_id*, without creating it. None for a bad id."""
    if not _valid_id(instance_id):
        return None
    return CONTENT_DIR / instance_id


def ensure_instance_dir(instance_id: str) -> Optional[Path]:
    d = instance_dir(instance_id)
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_file(instance_id: str, filename: str) -> Optional[Path]:
    """Resolve *filename* inside the instance folder, or None if it escapes.

    Two gates, because either alone has a hole. The name check rejects the
    obvious `../` and any separator, so nothing can address a sibling folder.
    The resolved-path check catches what the name cannot show: a symlink inside
    the folder pointing anywhere on the disk — resolve() follows it, and the
    containment test then fails.
    """
    d = instance_dir(instance_id)
    if d is None or not filename:
        return None
    if filename in (".", "..") or "/" in filename or "\\" in filename:
        return None
    if filename.startswith("."):
        return None  # no dotfiles: nothing legitimate writes one here
    if "\x00" in filename:
        return None
    candidate = (d / filename).resolve()
    try:
        if not candidate.is_relative_to(d.resolve()):
            return None
    except (OSError, ValueError):
        return None
    return candidate


# ── Settings ──────────────────────────────────────────────────────────────────

def _setting(key: str, default):
    try:
        from .settings_store import load_settings
        value = load_settings().get(key, default)
        return default if value is None else value
    except Exception:
        return default


def max_bytes() -> int:
    """Quota per instance folder in bytes; 0 means no limit."""
    try:
        mb = int(_setting("content_max_mb", DEFAULT_MAX_MB))
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_MB
    return max(0, mb) * 1024 * 1024


def warn_percent() -> int:
    try:
        value = int(_setting("content_warn_percent", DEFAULT_WARN_PERCENT))
    except (TypeError, ValueError):
        value = DEFAULT_WARN_PERCENT
    return min(100, max(1, value))


def block_when_full() -> bool:
    return bool(_setting("content_block_when_full", False))


def retention_days() -> int:
    try:
        return max(0, int(_setting("content_retention_days", DEFAULT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def retention_mode() -> str:
    mode = str(_setting("content_retention_mode", DEFAULT_RETENTION_MODE))
    return mode if mode in RETENTION_MODES else DEFAULT_RETENTION_MODE


def base_url() -> str:
    """Absolute prefix the download links are built with, without trailing slash.

    Empty means "relative", which is right for a browser already on the manager
    and wrong for a link that ends up in an OpenWebUI chat — that browser would
    resolve it against OpenWebUI. Hence the setting.
    """
    return str(_setting("content_base_url", "")).strip().rstrip("/")


# ── Listing and usage ─────────────────────────────────────────────────────────

def _files_of(d: Path) -> list[Path]:
    """Regular files directly in *d*. No recursion, no symlinks followed.

    This is the *listing* view — only these files have a download URL, because
    the route serves exactly one path segment. Accounting uses _all_files().
    """
    try:
        return sorted(
            (p for p in d.iterdir() if p.is_file() and not p.is_symlink()),
            key=lambda p: p.name,
        )
    except OSError:
        return []


def _all_files(d: Path) -> list[Path]:
    """Every regular file under *d*, subdirectories included, symlinks skipped.

    Quota, retention and deletion count with this one: a tool (or its library)
    that writes into a subfolder must not slip past the quota, sit outside the
    retention window forever, or survive "empty this folder".
    """
    try:
        return sorted(
            (p for p in d.rglob("*") if p.is_file() and not p.is_symlink()),
            key=lambda p: str(p),
        )
    except OSError:
        return []


def list_files(instance_id: str) -> list[dict]:
    d = instance_dir(instance_id)
    if d is None or not d.is_dir():
        return []
    result = []
    for p in _files_of(d):
        try:
            st = p.stat()
        except OSError:
            continue
        result.append({
            "name": p.name,
            "size": st.st_size,
            "modified": int(st.st_mtime),
            "url": file_url(instance_id, p.name),
        })
    result.sort(key=lambda f: f["modified"], reverse=True)
    return result


def instance_usage(instance_id: str) -> dict:
    """Bytes, file count and the age of the oldest file — one stat per file."""
    d = instance_dir(instance_id)
    total, count, oldest = 0, 0, None
    if d is not None and d.is_dir():
        for p in _all_files(d):
            try:
                st = p.stat()
            except OSError:
                continue
            total += st.st_size
            count += 1
            if oldest is None or st.st_mtime < oldest:
                oldest = st.st_mtime
    limit = max_bytes()
    return {
        "instance": instance_id,
        "bytes": total,
        "files": count,
        "oldest": int(oldest) if oldest else None,
        "limit_bytes": limit,
        "percent": int(total * 100 / limit) if limit else 0,
    }


def overview() -> list[dict]:
    """One entry per instance folder that exists — including folders whose
    instance is gone, because those are exactly the ones nobody would clean up
    otherwise."""
    if not CONTENT_DIR.is_dir():
        return []
    result = []
    for d in sorted(CONTENT_DIR.iterdir()):
        if not d.is_dir() or not _valid_id(d.name):
            continue
        result.append(instance_usage(d.name))
    return result


def instance_url(instance_id: str) -> str:
    """Base URL of one instance's folder — handed to the tool as MCP_CONTENT_URL.

    Without a token, because there is nothing to fetch here: the download route
    serves single files only and never lists a directory.
    """
    return f"{base_url()}/content/{quote(instance_id)}"


def file_url(instance_id: str, filename: str) -> str:
    return (
        f"{base_url()}/content/{quote(instance_id)}/{quote(filename)}"
        f"?t={file_token(instance_id, filename)}"
    )


# ── Quota ─────────────────────────────────────────────────────────────────────

def _mb(value: int) -> str:
    return f"{value / (1024 * 1024):.1f}"


def is_full(instance_id: str) -> bool:
    limit = max_bytes()
    if not limit:
        return False
    return instance_usage(instance_id)["bytes"] >= limit


def quota_warning(instance_id: str) -> str:
    """One short, literal line, or "" while there is room.

    Kept to a single sentence on purpose: on the other end is a small model
    that will invent a cause for anything it does not understand, and a
    paragraph about retention policy is an invitation to do exactly that.
    """
    limit = max_bytes()
    if not limit:
        return ""
    usage = instance_usage(instance_id)
    if usage["percent"] < warn_percent():
        return ""
    state = "full" if usage["bytes"] >= limit else "nearly full"
    return (
        f"Note: the file storage of this server is {state} "
        f"({_mb(usage['bytes'])} of {_mb(limit)} MB). "
        f"Tell the user to delete old files or contact the administrator."
    )


def full_refusal(instance_id: str) -> str:
    usage = instance_usage(instance_id)
    return (
        f"File storage is full ({_mb(usage['bytes'])} of {_mb(max_bytes())} MB) "
        f"and this server is configured to refuse new files. "
        f"Tell the user to delete old files or contact the administrator."
    )


# ── Link rewriting ────────────────────────────────────────────────────────────

# Everything up to a character that cannot be part of a filename in a Markdown
# link or a sentence. `)` ends `[text](url)`, the quotes end an HTML attribute.
_LINK_TAIL = r'([^\s)\]}"\'<>]+)'


def rewrite_links(text: str, instance_id: str, prefix: str) -> str:
    """Point the tool's own links at our download route.

    The tool stays untouched — what gets rewritten is its result, not its code.
    Only names that actually exist in the instance folder are replaced: a tool
    mentioning the prefix in its documentation, or returning a link to a file
    it wrote somewhere else entirely, must come through unchanged rather than
    gain a token for a file that is not there.
    """
    if not text or not prefix:
        return text
    pattern = re.compile(re.escape(prefix) + _LINK_TAIL)

    def _replace(match: "re.Match") -> str:
        filename = match.group(1)
        path = resolve_file(instance_id, filename)
        if path is None or not path.is_file():
            # A tool that URL-encodes its own link ("Q3%20Report.docx") names
            # the same file on disk as the decoded form — try that before
            # giving up, or every name with a space stays a dead link. The
            # decoded name goes through the same resolve_file gate, so %2F
            # buys no traversal.
            decoded = unquote(filename)
            if decoded != filename:
                path = resolve_file(instance_id, decoded)
                if path is not None and path.is_file():
                    return file_url(instance_id, decoded)
            return match.group(0)
        return file_url(instance_id, filename)

    return pattern.sub(_replace, text)


# ── Retention ─────────────────────────────────────────────────────────────────

def prune(days: Optional[int] = None, mode: Optional[str] = None,
          now: Optional[float] = None) -> int:
    """Delete files past the retention window; returns how many went.

    Two modes, because "obsolete" means different things for different tools.
    *file_age* drops each file on its own birthday. *whole_folder* empties the
    folder as soon as its oldest file is obsolete — for tools that write a set
    of files belonging together, where keeping half of it is worse than
    keeping none (decision 5).
    """
    days = retention_days() if days is None else days
    if days <= 0:
        return 0
    mode = retention_mode() if mode is None else mode
    cutoff = (time.time() if now is None else now) - days * 86400
    if not CONTENT_DIR.is_dir():
        return 0

    removed = 0
    for d in sorted(CONTENT_DIR.iterdir()):
        if not d.is_dir() or not _valid_id(d.name):
            continue
        files = _all_files(d)
        if not files:
            continue
        try:
            ages = [(p, p.stat().st_mtime) for p in files]
        except OSError:
            continue
        if mode == "whole_folder":
            if min(ts for _, ts in ages) >= cutoff:
                continue
            doomed = [p for p, _ in ages]
        else:
            doomed = [p for p, ts in ages if ts < cutoff]
        for p in doomed:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ── Deletion ──────────────────────────────────────────────────────────────────

def delete_file(instance_id: str, filename: str) -> bool:
    path = resolve_file(instance_id, filename)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def clear_instance(instance_id: str) -> int:
    """Empty one folder; the folder itself stays so the instance keeps writing."""
    d = instance_dir(instance_id)
    if d is None or not d.is_dir():
        return 0
    removed = 0
    for p in _all_files(d):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    # Now-empty subfolders go too — deepest first, and only genuinely empty
    # ones: rmdir refuses anything still holding a file whose unlink failed.
    for sub in sorted((p for p in d.rglob("*") if p.is_dir()), reverse=True):
        try:
            sub.rmdir()
        except OSError:
            pass
    return removed


def clear_all() -> int:
    if not CONTENT_DIR.is_dir():
        return 0
    return sum(
        clear_instance(d.name)
        for d in sorted(CONTENT_DIR.iterdir())
        if d.is_dir() and _valid_id(d.name)
    )


def forget_instance(instance_id: str) -> None:
    """Remove the folder of a deleted instance, files and all."""
    d = instance_dir(instance_id)
    if d is None or not d.is_dir():
        return
    clear_instance(instance_id)
    try:
        d.rmdir()
    except OSError:
        pass  # something unexpected is in there — leave it for a human
