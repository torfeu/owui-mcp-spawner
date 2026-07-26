import asyncio
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from ..api_helpers import _tail_file
from ..auth import require_auth
from ..logger import get_install_log_path, get_runtime_log_path
router = APIRouter()

@router.get("/api/instances/{instance_id}/logs/install", dependencies=[Depends(require_auth)])
async def logs_install(instance_id: str) -> PlainTextResponse:
    path = get_install_log_path(instance_id)
    # Reads up to 256KB — keep the disk I/O off the event loop.
    text = await asyncio.to_thread(_tail_file, path) if path.exists() else "(no install log)"
    return PlainTextResponse(text)

@router.get("/api/instances/{instance_id}/logs/runtime", dependencies=[Depends(require_auth)])
async def logs_runtime(instance_id: str) -> PlainTextResponse:
    path = get_runtime_log_path(instance_id)
    text = await asyncio.to_thread(_tail_file, path) if path.exists() else "(no runtime log)"
    return PlainTextResponse(text)

