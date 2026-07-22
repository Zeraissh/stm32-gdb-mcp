"""Aggregate per-tool metrics from the session journal.

Phase 2 priority #3 (observability): turn the raw journal into success/failure
rates and timing per tool, so a long autonomous run is measurable — which tools
fail, where the time goes — without an external system.
"""


def compute_metrics(entries) -> dict:
    by_tool: dict[str, dict[str, float]] = {}
    for entry in entries or []:
        tool = entry.get("tool")
        stats = by_tool.setdefault(tool, {"calls": 0, "ok": 0, "failed": 0, "total_ms": 0.0})
        stats["calls"] += 1
        if entry.get("ok"):
            stats["ok"] += 1
        else:
            stats["failed"] += 1
        duration = entry.get("duration_ms")
        if duration:
            stats["total_ms"] += duration

    for stats in by_tool.values():
        stats["total_ms"] = round(stats["total_ms"], 1)
        stats["avg_ms"] = round(stats["total_ms"] / stats["calls"], 1) if stats["calls"] else 0

    totals = {
        "calls": sum(s["calls"] for s in by_tool.values()),
        "ok": sum(s["ok"] for s in by_tool.values()),
        "failed": sum(s["failed"] for s in by_tool.values()),
        "tools": len(by_tool),
    }
    return {"totals": totals, "by_tool": by_tool}
