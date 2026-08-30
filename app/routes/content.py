"""The content store's two doors: the authenticated API, and one file at a time.

`/api/content*` is the admin side — listing, usage, deletion — and sits behind
the same auth as everything else. `/content/<instance>/<file>` is the door the
chat link uses, and it opens for exactly one file, for whoever holds that
file's token. There is deliberately no listing there and no guest view: a file
nobody was given a link to cannot be found (decision 2).
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from ..api_helpers import require_upload_or_edit
from ..auth import require_auth
from ..content_store import (block_when_full, clear_all, clear_instance, delete_file,
                             instance_usage, list_files, max_bytes, overview,
                             resolve_file, retention_days, retention_mode,
                             token_valid, warn_percent, base_url)
from ..logger import get_manager_logger

router = APIRouter()
logger = get_manager_logger()


@router.get("/api/content", dependencies=[Depends(require_auth)])
async def get_content_overview() -> dict:
    """Usage per instance plus the settings the numbers are measured against."""
    def _build() -> dict:
        instances = overview()
        return {
            "instances": instances,
            "total_bytes": sum(i["bytes"] for i in instances),
            "total_files": sum(i["files"] for i in instances),
            "settings": {
                "content_max_mb": max_bytes() // (1024 * 1024),
                "content_warn_percent": warn_percent(),
                "content_block_when_full": block_when_full(),
                "content_retention_days": retention_days(),
                "content_retention_mode": retention_mode(),
                "content_base_url": base_url(),
            },
        }

    return await asyncio.to_thread(_build)


@router.get("/api/content/{instance_id}", dependencies=[Depends(require_auth)])
async def get_instance_content(instance_id: str) -> dict:
    def _build() -> dict:
        usage = instance_usage(instance_id)
        usage["items"] = list_files(instance_id)
        return usage

    return await asyncio.to_thread(_build)


@router.delete("/api/content/{instance_id}/{filename}",
               dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def delete_content_file(instance_id: str, filename: str) -> dict:
    # "Not there" and "could not be removed" are different answers: after a
    # 404 the admin believes the link is dead, but a file whose unlink failed
    # (permissions, immutable flag) is still there and still downloadable.
    path = await asyncio.to_thread(resolve_file, instance_id, filename)
    if path is None or not path.is_file():
        raise HTTPException(404, f"No file '{filename}' in the storage of '{instance_id}'")
    if not await asyncio.to_thread(delete_file, instance_id, filename):
        raise HTTPException(500, f"Could not delete '{filename}' — check permissions on the content folder")
    logger.info(f"Deleted content file {instance_id}/{filename}")
    return {"ok": True}


@router.delete("/api/content/{instance_id}",
               dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def delete_instance_content(instance_id: str) -> dict:
    removed = await asyncio.to_thread(clear_instance, instance_id)
    logger.info(f"Emptied content storage of '{instance_id}': {removed} file(s)")
    return {"ok": True, "removed": removed}


@router.delete("/api/content",
               dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def delete_all_content() -> dict:
    removed = await asyncio.to_thread(clear_all)
    logger.info(f"Emptied the whole content storage: {removed} file(s)")
    return {"ok": True, "removed": removed}


# Registered on the router that admin_server includes *before* it mounts
# StaticFiles on "/" — that mount is a catch-all and would otherwise swallow
# this path and answer 404 for every download.
@router.get("/content/{instance_id}/{filename}")
async def download_content(instance_id: str, filename: str,
                           t: str = Query("", description="per-file token")) -> FileResponse:
    """One file, for whoever holds its token. No auth, no listing, no guessing.

    The same 404 for a wrong token and for a missing file, on purpose: the
    difference is the one thing worth hiding here.
    """
    path = await asyncio.to_thread(resolve_file, instance_id, filename)
    if path is None or not path.is_file() or not token_valid(instance_id, filename, t):
        raise HTTPException(404, "Not found")
    return FileResponse(
        path,
        # filename= lets Starlette build the Content-Disposition header itself:
        # it RFC-5987-encodes names that are not latin-1 (umlauts are, CJK and
        # en-dashes are not) and escapes quotes — a hand-built header here 500s
        # on exactly those names.
        filename=filename,
        content_disposition_type="attachment",
        headers={
            # Never rendered in place: these files come from a tool, and this
            # server has no business being the origin that executes them.
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0, no-store",
        },
    )
