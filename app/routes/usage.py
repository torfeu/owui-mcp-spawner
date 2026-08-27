import asyncio

from fastapi import APIRouter, Depends

from ..activity import bucket_labels, bucket_matrix, bucket_unit, retention_days, usage_summary
from ..auth import require_auth
from ..config_store import get_all_states, load_all_configs

router = APIRouter()

MAX_DAYS = 3650


@router.get("/api/usage", dependencies=[Depends(require_auth)])
async def get_usage(days: int = 7) -> dict:
    """Usage per instance and per function, for the statistics view.

    Every known instance is listed, including those never used — that is the
    answer the view exists for. Sorted by the calls within the window, not by
    the total: the total favours whatever has been installed longest and
    answers "what was once important", not "what do I use".
    """
    # Clamped rather than defaulted: an explicit days=0 means "as short as
    # possible", not "give me the default I didn't ask for".
    days = max(1, min(int(days), MAX_DAYS))

    def _build() -> dict:
        configs = load_all_configs()
        states = {s.id: s for s in get_all_states(configs)}
        summary = usage_summary(days)
        labels = bucket_labels(days)
        series = bucket_matrix(days)
        empty = [0] * len(labels)

        rows = []
        for instance_id, cfg in configs.items():
            entry = summary.get(instance_id) or {}
            state = states.get(instance_id)
            tools = [
                {"name": name, **values}
                for name, values in sorted(
                    (entry.get("tools") or {}).items(),
                    key=lambda item: (-item[1]["recent"], -item[1]["calls"], item[0]),
                )
            ]
            rows.append({
                "id": instance_id,
                "name": cfg.name or instance_id,
                "category": cfg.category or "",
                "status": state.status.value if state else "unknown",
                "calls": entry.get("calls", 0),
                "recent": entry.get("recent", 0),
                "first_call": entry.get("first_call") or 0,
                "last_call": entry.get("last_call", 0),
                "last_tool": entry.get("last_tool", ""),
                "daily": series.get(instance_id, empty),
                "tools": tools,
            })

        rows.sort(key=lambda r: (-r["recent"], -r["calls"], r["id"]))
        return {
            "days": days,
            # What the buckets are, and what each one stands for: the shortest
            # window is cut by hour, the rest by date. Labels travel with the
            # payload so the chart never counts backwards from the browser's
            # clock — which is not necessarily the clock they were cut with.
            "bucket": bucket_unit(days),
            "bucket_labels": labels,
            "retention_days": retention_days(),
            "instances": rows,
        }

    return await asyncio.to_thread(_build)
