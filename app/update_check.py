"""Opt-in check for a newer release on GitHub.

Off by default and deliberately so: the check is an outgoing request that tells
GitHub this installation exists (IP + timestamp). For a tool that is meant to
run on your own machine, "on unless you turn it off" would break that promise.

The check runs server-side and its result is cached in runtime/settings.json —
the UI only ever reads that cache. If every browser tab asked GitHub directly,
every viewer's IP would leak instead of the server's, plus CORS and CSP trouble.

Reports only. A tool that spawns processes and runs Python does not update
itself.
"""
import asyncio
import time

import httpx
from packaging.version import InvalidVersion, Version

from . import __version__
from .logger import get_manager_logger
from .settings_store import load_settings, save_settings

logger = get_manager_logger()

RELEASES_URL = "https://api.github.com/repos/torfeu/owui-mcp-spawner/releases/latest"
CHECK_INTERVAL = 24 * 60 * 60  # hard-wired: nobody ever tunes a check frequency
REQUEST_TIMEOUT = 3.0


def update_check_enabled() -> bool:
    return bool(load_settings().get("update_check", False))


def cached_result() -> dict:
    """The last check's outcome, as the API serves it. Never triggers a request."""
    settings = load_settings()
    enabled = bool(settings.get("update_check", False))
    latest = str(settings.get("update_latest_version") or "")
    return {
        "enabled": enabled,
        "current_version": __version__,
        "latest_version": latest,
        "html_url": str(settings.get("update_html_url") or ""),
        "last_checked": settings.get("update_last_checked") or 0,
        # Gated on the switch: with the check off, a leftover version in the
        # settings file must not produce a badge nobody asked for.
        "update_available": enabled and is_newer(latest, __version__),
    }


def is_newer(latest: str, current: str) -> bool:
    """True when *latest* is a strictly newer release than *current*.

    Parsed, not compared as strings — "0.1.10" sorts before "0.1.9"
    lexicographically. Unparsable tags (a renamed release, a moved tag) count as
    "no update" rather than nagging about a version that may not exist.
    """
    if not latest or not current:
        return False
    try:
        return Version(_strip_v(latest)) > Version(_strip_v(current))
    except InvalidVersion:
        return False


def _strip_v(tag: str) -> str:
    tag = tag.strip()
    return tag[1:] if tag[:1] in ("v", "V") else tag


async def _fetch_latest() -> tuple[str, str]:
    """Ask GitHub for the latest release. Raises on any failure."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(
            RELEASES_URL,
            headers={"Accept": "application/vnd.github+json"},
        )
    response.raise_for_status()
    data = response.json()
    return str(data.get("tag_name") or ""), str(data.get("html_url") or "")


async def check_now(force: bool = False) -> dict:
    """Query GitHub if enabled and the cache is stale; return `cached_result()`.

    Failures (offline, DNS gone, GitHub down, rate limit) keep the previous
    cache and log at debug level: a server without internet must not fill its
    log with something the user never asked for.
    """
    if not update_check_enabled():
        return cached_result()
    settings = load_settings()
    last = float(settings.get("update_last_checked") or 0)
    if not force and time.time() - last < CHECK_INTERVAL:
        return cached_result()

    try:
        tag, html_url = await _fetch_latest()
    except Exception as e:
        logger.debug(f"Update check failed: {e}")
        return cached_result()

    save_settings({
        "update_latest_version": tag,
        "update_html_url": html_url,
        "update_last_checked": int(time.time()),
    })
    if is_newer(tag, __version__):
        logger.info(f"Update available: {tag} (running {__version__})")
    return cached_result()


async def check_manually() -> dict:
    """One check on explicit request ("Check now"), cache and switch ignored.

    Runs even with the switch off: the reason the automatic check is opt-in is
    unrequested background traffic, and a click is exactly the request that was
    missing. The result is only persisted while the switch is on, so a one-off
    look leaves no trace — same promise as switching the check off.

    Unlike the background check this one reports failures: someone waiting for
    an answer must not be told "up to date" when nothing was reached.
    """
    enabled = update_check_enabled()
    try:
        tag, html_url = await _fetch_latest()
    except Exception as e:
        logger.debug(f"Manual update check failed: {e}")
        return {
            "ok": False, "error": str(e), "enabled": enabled,
            "current_version": __version__, "latest_version": "",
            "html_url": "", "update_available": False,
        }

    if enabled:
        save_settings({
            "update_latest_version": tag,
            "update_html_url": html_url,
            "update_last_checked": int(time.time()),
        })
    return {
        "ok": True, "enabled": enabled,
        "current_version": __version__,
        "latest_version": tag,
        "html_url": html_url,
        # Not gated on the switch: this answer was asked for directly.
        "update_available": is_newer(tag, __version__),
    }


async def update_check_loop() -> None:
    """Background task: one check at startup, then once a day."""
    while True:
        try:
            await check_now()
        except Exception as e:  # never let this task kill itself
            logger.debug(f"Update check loop error: {e}")
        await asyncio.sleep(CHECK_INTERVAL)
