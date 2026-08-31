"""Download the whole manager state, and put it back.

Both routes are **password only**. The export can carry every credential this
server holds, so a read-only or agent token must not be able to fetch it — the
same reason the per-instance export is closed to them. And the restore writes
configuration: a token that could seed instances into a server would be an
admin by another name.

`require_token_edit` on top, so `--no-token-edit` closes this door too: with
secrets included the archive contains the very tokens that switch exists to
protect.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import backup
from ..api_helpers import require_token_edit, require_upload_or_edit
from ..auth import require_admin_auth, require_auth
from ..logger import get_manager_logger

router = APIRouter()
logger = get_manager_logger()

# Spelled once so the two cannot drift apart.
_GUARDS = [Depends(require_auth), Depends(require_admin_auth), Depends(require_token_edit)]


@router.get("/api/backup", dependencies=_GUARDS)
async def export_backup(secrets: bool = False) -> JSONResponse:
    """Everything worth restoring, as one file.

    `secrets=true` makes the file a collection of credentials — and the only
    version that restores a working server without retyping every API key. The
    archive says which one it is, in a field and in its filename, because a
    backup whose contents nobody can tell by looking is one that ends up in the
    wrong folder.
    """
    payload = backup.build(include_secrets=secrets)
    logger.info(f"Backup exported: {len(payload['instances'])} instances, "
                f"secrets={'yes' if secrets else 'no'}")
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{backup.filename(secrets)}"'},
    )


@router.post("/api/backup/restore", dependencies=[*_GUARDS, Depends(require_upload_or_edit)])
async def restore_backup(request: Request) -> dict:
    """Write back everything this server does not already have.

    Never overwrites: an instance whose id is taken is skipped and named, a
    setting already set stays, and `content.key` is only written where there is
    none — replacing it would invalidate every download link ever handed out.
    So a mistaken click here costs nothing, which is the point.

    `dry_run` returns the same report without writing anything, so the dialog
    can show what would happen before it happens.
    """
    body = await request.body()
    try:
        payload = json.loads(body or b"{}")
    except ValueError as e:
        raise HTTPException(422, f"Not readable as JSON: {e}")
    if not isinstance(payload, dict):
        raise HTTPException(422, "Not a backup file (expected a JSON object)")

    dry_run = bool(payload.pop("dry_run", False))
    try:
        report = backup.restore(payload, dry_run=dry_run)
    except backup.BackupError as e:
        raise HTTPException(422, str(e))

    if not dry_run:
        logger.warning(
            f"Backup restored: {len(report['instances_restored'])} instances added, "
            f"{len(report['instances_skipped'])} skipped, "
            f"{len(report['instances_failed'])} failed")
    return report
