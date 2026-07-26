from fastapi import APIRouter, Depends
from ..api_helpers import APP_VERSION
from ..auth import auth_enabled, edit_mode, require_auth
router = APIRouter()

@router.get("/api/auth-status")
async def auth_status() -> dict:
    return {"auth_enabled": auth_enabled(), "edit_mode": edit_mode(), "version": APP_VERSION}

@router.get("/api/auth-check", dependencies=[Depends(require_auth)])
async def auth_check() -> dict:
    return {"ok": True}

