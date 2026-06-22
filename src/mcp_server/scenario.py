"""Declarative, replayable debug scenarios.

Phase 2 priority #1 (determinism) and design law #2 (minimal steps): a scenario
is a saved list of ``{"tool": ..., "args": {...}}`` steps that can be replayed
deterministically with one call. Define a complex repro once, re-run it on demand
and get a single pass/fail report — instead of re-issuing the whole sequence by
hand each time.
"""

import json


def step_summary(payload: dict):
    """Extract a one-line summary from a tool's JSON envelope payload."""
    if not payload.get("ok"):
        error = payload.get("error")
        if isinstance(error, dict):
            return error.get("message")
        return error
    data = payload.get("data")
    if isinstance(data, dict):
        return data.get("summary") or data.get("message")
    return None


async def replay_scenario(steps, run_step, stop_on_failure=True) -> dict:
    """Replay ``steps`` via ``run_step(tool, args) -> payload`` (an async callable).

    Returns a report with per-step outcomes and overall pass/fail.
    """
    results = []
    passed = 0
    for index, step in enumerate(steps):
        tool = step["tool"]
        args = step.get("args", {})
        payload = await run_step(tool, args)
        ok = bool(payload.get("ok"))
        results.append({
            "step": index,
            "tool": tool,
            "ok": ok,
            "summary": step_summary(payload),
            "error": payload.get("error") if not ok else None,
        })
        if ok:
            passed += 1
        elif stop_on_failure:
            break

    return {
        "steps": results,
        "passed": passed,
        "total": len(steps),
        "completed": len(results),
        "ok": passed == len(steps),
    }


def load_scenario(path: str) -> list:
    """Load a scenario step list from a JSON file ({"steps": [...]} or a bare list)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("steps", [])
    return data
