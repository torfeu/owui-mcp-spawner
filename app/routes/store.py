"""The store: the tools that ship in examples/, installable with one click.

Before this, putting the router or the control tool on a server meant
downloading its JSON from the repository and uploading it again through the
dialog — for a file that was already sitting next to the manager. The store
lists what `examples/` holds and installs it through exactly the pipeline an
upload uses (`_import_openwebui_tool`): dependencies, validation in the venv,
valve defaults, category from the tool's own header. Nothing here is a second
way to install.

What it deliberately is not: a marketplace. The list comes from this server's
own `examples/` directory and nowhere else — a remote source was discussed and
set aside until the local one proves it is used (plan-v0.2.3, 29.09.). The id
in the URL is only ever looked up in that index, never turned into a path.

Updating an installed copy is not repeated here either: the store points at
`POST /api/instances/{id}/update-from-example`, which already exists, only
goes forward, and snapshots the previous code first.
"""
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ..api_helpers import (_bundled_version_info, _category_from_code, _examples_by_id,
                           _import_openwebui_tool, _version_from_path, _version_from_tool_file,
                           require_upload_or_edit, str_field)
from ..auth import require_auth
from ..config_store import config_exists, load_config
from ..logger import get_manager_logger
from ..venv_manager import DEFAULT_VENV

router = APIRouter()
logger = get_manager_logger()


def _shipped(path: str) -> dict | None:
    """The tool object of one shipped JSON, or None when it cannot be read."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    return raw if isinstance(raw, dict) else None


def _description(tool: dict) -> str:
    meta = tool.get("meta")
    text = str_field(meta.get("description")) if isinstance(meta, dict) else ""
    if text:
        return text
    # The docstring header carries one too — `description:` next to `version:`.
    for line in str(tool.get("content") or "")[:800].splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("description:"):
            return stripped.split(":", 1)[1].strip()
    return ""


@router.get("/api/store", dependencies=[Depends(require_auth)])
async def list_store() -> list[dict]:
    """Every tool in examples/, and where this server stands with it."""
    entries = []
    for tool_id, path in sorted(_examples_by_id().items()):
        tool = _shipped(path)
        if tool is None:
            continue
        entry = {
            "id": tool_id,
            "name": str_field(tool.get("name")) or tool_id,
            "description": _description(tool),
            "version": _version_from_path(Path(path)),
            "category": _category_from_code(tool.get("content") or ""),
            "installed": config_exists(tool_id),
            "installed_version": "",
            "update_available": False,
        }
        if entry["installed"]:
            cfg = load_config(tool_id)
            bundled = _bundled_version_info(cfg) if cfg else None
            if bundled:
                entry["update_available"] = bool(bundled["update_available"])
            entry["installed_version"] = _version_from_tool_file(cfg) if cfg else ""
        entries.append(entry)
    return entries


@router.post("/api/store/{tool_id}/install",
             dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def install_from_store(tool_id: str, body: dict | None = None) -> dict:
    """Install one shipped tool as a new instance.

    Under the tool's own id by default. `{"id": "…"}` in the body installs a
    further copy under another id — the case the bridge exists for: one
    instance per external MCP server, each with its own target in its values.

    Same pipeline, same answers as an upload of that file: 409 when the id is
    taken, 422 with the pip or validation errors when it cannot be installed.
    The instance is not started — that stays a deliberate click, as after
    upload.

    A copy under another id is not matched to its shipped file afterwards:
    that matching goes by id (see _bundled_version_info), so the copy gets no
    update badge. Accepted rather than guessed at — the same trade-off as for
    any tool uploaded under a name of its own.
    """
    path = _examples_by_id().get(tool_id)
    if not path:
        raise HTTPException(404, f"No tool '{tool_id}' ships with this spawner")
    tool = _shipped(path)
    if tool is None:
        raise HTTPException(422, f"The shipped file for '{tool_id}' cannot be read")

    body = body or {}
    venv = str_field(body.get("venv")) or DEFAULT_VENV
    category = str_field(body.get("category"))
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", venv):
        raise HTTPException(400, f"Invalid venv name '{venv}': letters, digits, _ and - only")
    instance_id = str_field(body.get("id")) or tool_id
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", instance_id):
        raise HTTPException(400, f"Invalid id '{instance_id}': letters, digits, _ and - only")

    # A copy: the importer may add fields to what it is handed, and the index
    # cache must keep describing the file on disk.
    raw = dict(tool)
    raw["id"] = instance_id
    result = await _import_openwebui_tool(raw, venv=venv, category=category)
    logger.info(f"Installed '{instance_id}' from the store ({path})")
    return result
