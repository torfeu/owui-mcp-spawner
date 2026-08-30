"""The machine behind the instances: `GET /api/system/stats`.

One route, read-only, polled by the dashboard. It answers with whatever it can
measure and says so plainly when it can measure nothing — an installation that
has the new code but not yet `psutil` gets `available: false` and a sentence to
act on, never a 500 that looks like a broken manager.
"""
import asyncio

from fastapi import APIRouter, Depends

from ..auth import require_auth
from ..config_store import get_all_states
from ..system_stats import NO_PSUTIL, available, instance_stats, machine_stats

router = APIRouter()


@router.get("/api/system/stats", dependencies=[Depends(require_auth)])
async def get_system_stats() -> dict:
    """Machine load plus what each running instance costs.

    The pids come from the in-memory instance state — the same source the
    dashboard's status column already believes — so the numbers cannot
    contradict the row they are rendered next to.
    """
    def _build() -> dict:
        if not available():
            return {"available": False, "reason": NO_PSUTIL, "machine": None, "instances": {}}
        pids = {inst.id: inst.pid for inst in get_all_states() if inst.pid}
        return {
            "available": True,
            "reason": "",
            "machine": machine_stats(),
            "instances": instance_stats(pids),
        }

    # psutil reads /proc (and walks the children of every runner): blocking work
    # that has no business sitting on the event loop of a server that is also
    # proxying MCP traffic.
    return await asyncio.to_thread(_build)
