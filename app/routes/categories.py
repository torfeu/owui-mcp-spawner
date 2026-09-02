"""The category endpoints, as the dashboard sees them.

Two routes. A listing, so the UI can show which categories exist and what each
one currently offers, and an export in the shape OpenWebUI imports — the same
shape the per-instance export has, so registering a whole category is the same
gesture as registering one instance.

Categories are not configured anywhere: a category exists as long as an
instance carries it. There is therefore nothing here that creates or deletes
one, and no state that can disagree with the dashboard.
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import category_endpoint, health
from ..auth import mcp_bearer_token, require_admin_auth, require_auth
from ..logger import get_manager_logger
from ..schema import MCPStatus

router = APIRouter()
logger = get_manager_logger()


def _base(request: Request) -> str:
    """The address this manager was reached on, port included.

    Taken from the request rather than from the bind arguments on purpose: the
    URL is going to be pasted into OpenWebUI or an agent config, so it has to
    be the address that actually works from where the browser is sitting —
    which is the one the browser just used.
    """
    host = request.headers.get("host")
    if not host:
        bind = os.environ.get("MCP_RUNNER_HOST", "127.0.0.1")
        if ":" in bind:
            bind = f"[{bind}]"
        host = f"{bind}:{os.environ.get('MCP_MANAGER_PORT', '7860')}"
    own = category_endpoint.port()
    if own is not None:
        # A port of their own is the address to hand out: it is the one that
        # keeps answering even when the manager port stops serving categories,
        # and the one the person just configured for exactly this purpose. The
        # hostname still comes from the request — only the port is replaced.
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        host = f"{name}:{own}"
    return f"{request.url.scheme}://{host}"


def _tool_count(instance_id: str):
    """How many tools that instance last answered with, or None.

    Read off the health check, which asks every running instance once a minute
    anyway. Counting them here would mean opening an MCP session per instance
    on every poll of the dashboard.
    """
    entry = health.for_instance(instance_id)
    return entry["tools"] if entry else None


@router.get("/api/categories", dependencies=[Depends(require_auth)])
async def list_categories(request: Request) -> dict:
    """Whether category endpoints are on, and every category with its URL.

    The categories are listed either way, and `enabled` says whether those URLs
    currently answer. Returning nothing while the feature is off would be the
    simpler contract but a worse one: the settings dialog has to fill its list
    the moment the switch is ticked, before anything is saved, and a caller
    that sees an empty list cannot tell "no categories" from "switched off".

    Nothing is given away by listing them: every category is already on its
    instance's row in the dashboard, and this route needs a login.
    """
    base = _base(request)
    result = []
    for name in category_endpoint.categories():
        instances = category_endpoint.members(name, running_only=False)
        running = [i for i in instances if i.status == MCPStatus.running]
        counted = [_tool_count(i.id) for i in running]
        known = [c for c in counted if isinstance(c, int)]
        result.append({
            "name": name,
            "url": base + category_endpoint.endpoint_path(name),
            "instances": [
                {"id": i.id, "name": i.name, "status": i.status.value,
                 "tools": _tool_count(i.id)}
                for i in instances
            ],
            "running": len(running),
            "total": len(instances),
            # None rather than 0 while nothing has been probed yet: "no tools"
            # and "not measured" must not look the same in the UI.
            "tools": sum(known) if known else None,
            # Whether the endpoint has been asked for at least once since the
            # manager started. Not a health verdict — it is created on demand.
            "active": name in category_endpoint.running(),
        })
    return {"enabled": category_endpoint.enabled(), "categories": result}


# require_admin_auth: the payload embeds the MCP Bearer token, exactly as the
# per-instance export does, so this GET is closed to the read-only token.
@router.get("/api/categories/{name}/export",
            dependencies=[Depends(require_auth), Depends(require_admin_auth)])
async def export_category(name: str, request: Request) -> JSONResponse:
    # Switched off there is nothing to register, and an export that hands out a
    # URL answering 404 would be worse than no export at all.
    category = category_endpoint.resolve(name) if category_endpoint.enabled() else None
    if category is None:
        raise HTTPException(404, f"No category '{name}'")

    members = category_endpoint.members(category, running_only=False)
    token = mcp_bearer_token()
    described = (f"All tools of the '{category}' category "
                 f"({len(members)} instance(s)), named <instance>"
                 f"{category_endpoint.SEPARATOR}<tool>.")
    result = [{
        "type": "mcp",
        "url": _base(request) + category_endpoint.endpoint_path(category),
        "spec_type": "url",
        "spec": "",
        "path": "openapi.json",
        "auth_type": "bearer" if token else "none",
        "key": token or "",
        "info": {
            # Prefixed so it cannot collide with an instance id of the same
            # name in the importing system's own list.
            "id": f"category-{category}",
            "name": category,
            "description": described,
        }
    }]
    filename = "".join(c if c.isalnum() or c in "-_" else "-" for c in category)
    return JSONResponse(
        content=result,
        headers={"Content-Disposition": f'attachment; filename="{filename}-category-mcp.json"'},
    )
