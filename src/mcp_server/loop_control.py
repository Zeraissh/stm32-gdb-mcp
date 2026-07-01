"""The bounded-loop *brain* for the spec-to-silicon closed loop (Pillar C).

Pure control logic — no I/O, no hardware, fully unit-testable. It records the verdict of each
iteration (from the Pillar B1 acceptance evaluator, or a mechanical build/flash failure),
tracks the trajectory, and decides whether the loop should continue, has **converged**, is
**exhausted** (hit ``max_iterations``), or has **stalled** (the same checks keep failing, so the
current approach cannot converge — change strategy). Enforcing these bounds is what makes an
otherwise open-ended "keep trying" loop safe to run unattended.

The orchestrator (``loop_orchestrator``) supplies verdicts; the agent supplies code fixes
between iterations. This module never guesses — the verdict is Pillar B1's, deterministic.
"""

DEFAULT_MAX_ITERATIONS = 10
DEFAULT_STALL_PATIENCE = 3


def new_loop_state(plan: dict | None = None) -> dict:
    """Create fresh loop state from a (partial) plan, filling bound defaults."""
    plan = dict(plan or {})
    plan.setdefault("max_iterations", DEFAULT_MAX_ITERATIONS)
    plan.setdefault("stall_patience", DEFAULT_STALL_PATIENCE)
    plan.setdefault("has_build", False)
    plan.setdefault("has_flash", False)
    return {"plan": plan, "status": "active", "iterations": []}


def _verdict_ids(verdict: dict) -> tuple[list, int, int, int]:
    """From an acceptance report → (unsatisfied_ids, passed, failed, errored).

    ``unsatisfied_ids`` is failed ∪ errored: an errored check (unreadable target) is *not*
    satisfied, so it keeps the loop going rather than silently passing.
    """
    results = verdict.get("results", [])
    unsatisfied = [r["id"] for r in results if r.get("status") in ("fail", "error")]
    stats = verdict.get("stats") or {}
    return unsatisfied, stats.get("passed", 0), stats.get("failed", 0), stats.get("errored", 0)


def _previous_unsatisfied(state: dict) -> set:
    for entry in reversed(state["iterations"]):
        return set(entry.get("unsatisfied_ids") or [])
    return set()


def record_iteration(state: dict, *, verdict: dict | None = None, phase_error: dict | None = None) -> dict:
    """Append one iteration (an acceptance *verdict* or a mechanical *phase_error*) and
    recompute loop status. Returns the new iteration entry."""
    previous = _previous_unsatisfied(state)
    index = len(state["iterations"])

    if phase_error is not None:
        # A build / flash-run failure never reached acceptance; record it as a non-ok
        # iteration with no check-level detail so the trajectory still advances (and counts
        # toward max_iterations), but it is not treated as a check "stall".
        entry = {
            "index": index,
            "ok": False,
            "phase": phase_error.get("phase", "mechanical"),
            "phase_error": phase_error.get("detail", ""),
            "passed": 0,
            "failed": 0,
            "errored": 0,
            "unsatisfied_ids": [],
            "newly_satisfied": [],
            "newly_broken": [],
        }
    else:
        verdict = verdict or {}
        unsatisfied, passed, failed, errored = _verdict_ids(verdict)
        unsatisfied_set = set(unsatisfied)
        entry = {
            "index": index,
            "ok": bool(verdict.get("ok")),
            "phase": "acceptance",
            "phase_error": None,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "unsatisfied_ids": unsatisfied,
            "newly_satisfied": sorted(previous - unsatisfied_set),
            "newly_broken": sorted(unsatisfied_set - previous),
        }

    state["iterations"].append(entry)
    state["status"] = _recompute_status(state)
    return entry


def _is_stalled(state: dict) -> bool:
    patience = state["plan"]["stall_patience"]
    iterations = state["iterations"]
    if patience <= 0 or len(iterations) < patience:
        return False
    window = iterations[-patience:]
    if any(entry["ok"] for entry in window):
        return False
    sets = [tuple(entry["unsatisfied_ids"]) for entry in window]
    # A stall means the *same non-empty* set of checks failed every time in the window.
    return sets[0] != () and all(s == sets[0] for s in sets)


def _recompute_status(state: dict) -> str:
    iterations = state["iterations"]
    if not iterations:
        return "active"
    if iterations[-1]["ok"]:
        return "converged"
    if len(iterations) >= state["plan"]["max_iterations"]:
        return "exhausted"
    if _is_stalled(state):
        return "stalled"
    return "active"


def _next_actions(status: str, last: dict | None) -> list:
    if status == "converged":
        return ["describe_acceptance (what=last_result)", "plan_framework"]
    if status == "exhausted":
        return ["acceptance_loop_status", "raise max_iterations or rethink the framework design"]
    if status == "stalled":
        ids = ", ".join(last["unsatisfied_ids"]) if last else ""
        return [f"the same checks keep failing ({ids}); change approach, not just parameters"]
    # active
    if last and last.get("phase_error"):
        return [f"fix the {last['phase']} failure, then run_acceptance_iteration"]
    ids = ", ".join(last["unsatisfied_ids"]) if last and last.get("unsatisfied_ids") else ""
    hint = f"fix failing checks: {ids}" if ids else "inspect failures"
    return [hint, "run_acceptance_iteration"]


def loop_decision(state: dict) -> dict:
    """Summarize whether/why the loop should continue."""
    status = state["status"]
    iterations = state["iterations"]
    last = iterations[-1] if iterations else None

    if status == "converged":
        reason = "all acceptance checks passed"
    elif status == "exhausted":
        reason = f"reached max_iterations ({state['plan']['max_iterations']})"
    elif status == "stalled":
        reason = "the same checks failed repeatedly; the current approach cannot converge"
    elif last is None:
        reason = "no iterations yet"
    elif last.get("phase_error"):
        reason = f"{last['phase']} failure: {last['phase_error'][:200]}"
    else:
        reason = f"{last['failed'] + last['errored']} checks unsatisfied"

    return {
        "status": status,
        "converged": status == "converged",
        "exhausted": status == "exhausted",
        "stalled": status == "stalled",
        "should_continue": status == "active",
        "iteration_count": len(iterations),
        "reason": reason,
        "next_actions": _next_actions(status, last),
    }


def summarize_loop(state: dict) -> dict:
    """Compact trajectory view for acceptance_loop_status."""
    plan = state["plan"]
    trajectory = [
        {
            "index": entry["index"],
            "ok": entry["ok"],
            "phase": entry["phase"],
            "passed": entry["passed"],
            "failed": entry["failed"],
            "errored": entry["errored"],
            "phase_error": entry["phase_error"],
        }
        for entry in state["iterations"]
    ]
    last = state["iterations"][-1] if state["iterations"] else None
    return {
        "status": state["status"],
        "iteration_count": len(state["iterations"]),
        "max_iterations": plan["max_iterations"],
        "stall_patience": plan["stall_patience"],
        "acceptance_name": plan.get("acceptance_name"),
        "trajectory": trajectory,
        "last_unsatisfied_ids": last["unsatisfied_ids"] if last else [],
    }
