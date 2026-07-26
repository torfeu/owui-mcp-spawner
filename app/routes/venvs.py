import asyncio
import re
from fastapi import APIRouter, Depends, HTTPException
from ..api_helpers import require_upload_or_edit
from ..auth import require_auth
from ..config_store import load_all_configs
from ..venv_manager import DEFAULT_VENV, delete_venv, ensure_venv, list_venvs, venv_exists, venv_ready
router = APIRouter()

@router.get("/api/venvs", dependencies=[Depends(require_auth)])
async def list_venvs_endpoint() -> list[dict]:
    """All venvs with how many instances use each — drives the UI dropdowns."""
    counts: dict[str, int] = {}
    for c in load_all_configs().values():
        counts[c.venv] = counts.get(c.venv, 0) + 1
    names = set(list_venvs()) | {DEFAULT_VENV} | set(counts)
    return sorted(
        (
            {
                "name": n,
                "instances": counts.get(n, 0),
                "exists": venv_exists(n),
                "is_default": n == DEFAULT_VENV,
            }
            for n in names
        ),
        key=lambda d: (not d["is_default"], d["name"]),
    )

@router.post("/api/venvs", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def create_venv_endpoint(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", name):
        raise HTTPException(400, "Invalid venv name: letters, digits, underscores and hyphens only")
    # Only block when the venv is fully set up; a half-built one (interpreter but
    # no ready marker) is repaired by ensure_venv below instead of 409'ing.
    if venv_ready(name):
        raise HTTPException(409, f"Venv '{name}' already exists")
    ok, err = await asyncio.to_thread(ensure_venv, name)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True, "name": name}

@router.delete("/api/venvs/{name}", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def delete_venv_endpoint(name: str) -> dict:
    if name == DEFAULT_VENV:
        raise HTTPException(400, "Cannot delete the default venv")
    in_use = [c.id for c in load_all_configs().values() if c.venv == name]
    if in_use:
        raise HTTPException(409, f"Venv '{name}' is in use by: {', '.join(in_use)}")
    ok, err = await asyncio.to_thread(delete_venv, name)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True}

