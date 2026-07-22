"""Acceptance tools: spec loading/evaluation and the bounded acceptance loop (Pillar C)."""

import json

from mcp.types import TextContent, Tool

from ..acceptance_eval import GdbAcceptanceReader, evaluate_acceptance
from ..acceptance_model import summarize_acceptance, validate_acceptance_spec
from ..loop_control import loop_decision, new_loop_state, summarize_loop
from ..loop_orchestrator import GdbLoopSteps, run_iteration
from ..tool_response import content_error, content_success
from .context import ToolContext
from .registry import register


@register(Tool(
    name="load_acceptance",
    description="Load a machine-checked AcceptanceSpec (product-spec → deterministic checks) "
                "from an inline 'spec' object or a JSON 'path'. Checks: memory_u32 (any "
                "memory-mapped register), variable (C global), core_register, no_fault, "
                "stopped_at (PC in a symbol). Stored on the session; evaluate with run_acceptance.",
    inputSchema={
        "type": "object",
        "properties": {
            "spec": {"type": "object", "description": "Inline AcceptanceSpec {name, description, checks:[...]}."},
            "path": {"type": "string", "description": "Path to a JSON AcceptanceSpec (alternative to 'spec')."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def load_acceptance(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    raw = arguments.get("spec")
    path = arguments.get("path")
    if raw is None and not path:
        return [content_error(
            "Provide 'spec' (inline object) or 'path' (JSON file).", code="missing_argument",
            suggested_next_actions=["load_acceptance(spec={'checks': [...]})"])]
    if raw is None:
        try:
            with open(path or "", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as e:
            return [content_error(
                f"Failed to read acceptance spec: {e}", code="spec_load_error",
                suggested_next_actions=["load_acceptance(spec={...})"])]
    try:
        normalized = validate_acceptance_spec(raw)
    except ValueError as e:
        return [content_error(
            str(e), code="invalid_spec",
            suggested_next_actions=["load_acceptance with a corrected spec"])]
    ctx.acceptance["current"] = normalized
    ctx.acceptance["last_result"] = None
    return [content_success(
        summarize_acceptance(normalized),
        suggested_next_actions=["run_acceptance", "describe_acceptance (what=checks)"])]


@register(Tool(
    name="run_acceptance",
    description="Evaluate the loaded AcceptanceSpec against live silicon state and return a "
                "deterministic pass/fail/error verdict per check (the closed-loop judge). An "
                "unreadable target is reported as 'error', never a silent pass. A failing/errored "
                "check derived by synthesize_acceptance carries provenance.source — the init "
                "function + line that should satisfy it — so fixes are precision-guided. Run "
                "load_acceptance first; halt the target at the state you want to assert.",
    inputSchema={
        "type": "object",
        "properties": {
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def run_acceptance(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    spec = ctx.acceptance.get("current")
    if not spec:
        return [content_error(
            "No acceptance spec loaded for this session. Run load_acceptance first.", code="no_spec",
            suggested_next_actions=["load_acceptance(spec={...})"])]
    reader = GdbAcceptanceReader(ctx.gdb_client)
    report = evaluate_acceptance(spec, reader)
    ctx.acceptance["last_result"] = report
    if report["ok"]:
        actions = ["describe_acceptance (what=last_result)", "plan_framework"]
    else:
        actions = ["describe_acceptance (what=last_result)", "read_call_stack",
                   "reconstruct_fault_context"]
    return [content_success(report, suggested_next_actions=actions)]


@register(Tool(
    name="describe_acceptance",
    description="Read the loaded AcceptanceSpec or the last verdict. what=summary (name + "
                "check counts), checks (full check list), or last_result (the most recent "
                "run_acceptance report).",
    inputSchema={
        "type": "object",
        "properties": {
            "what": {"type": "string", "description": "summary|checks|last_result (default summary)."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def describe_acceptance(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    spec = ctx.acceptance.get("current")
    if not spec:
        return [content_error(
            "No acceptance spec loaded for this session. Run load_acceptance first.", code="no_spec",
            suggested_next_actions=["load_acceptance(spec={...})"])]
    what = arguments.get("what") or "summary"
    if what == "summary":
        return [content_success(summarize_acceptance(spec),
                                suggested_next_actions=["run_acceptance"])]
    if what == "checks":
        return [content_success({"checks": spec["checks"]},
                                suggested_next_actions=["run_acceptance"])]
    if what == "last_result":
        result = ctx.acceptance.get("last_result")
        if result is None:
            return [content_error(
                "No verdict yet. Run run_acceptance first.", code="no_result",
                suggested_next_actions=["run_acceptance"])]
        return [content_success(result)]
    return [content_error(
        "what must be summary|checks|last_result", code="invalid_argument",
        suggested_next_actions=["describe_acceptance (what=summary|checks|last_result)"])]


@register(Tool(
    name="start_acceptance_loop",
    description="Start a bounded spec-to-silicon acceptance loop (Pillar C) for the loaded "
                "AcceptanceSpec. Each run_acceptance_iteration does one build → flash → "
                "run-to-state → run_acceptance pass; the loop stops on convergence, on "
                "max_iterations, or on a stall (same checks failing repeatedly). Omit build/"
                "flash to run evaluate-only iterations (you flash manually). Load a spec first.",
    inputSchema={
        "type": "object",
        "properties": {
            "max_iterations": {"type": "integer", "description": "Hard bound on iterations (default 10)."},
            "stall_patience": {"type": "integer", "description": "Stop if the same checks fail this many times in a row (default 3)."},
            "build": {"type": "object", "description": "Optional build_firmware config {kind, project|build_dir|directory, target, config, rebuild, command, cwd}."},
            "flash": {"type": "object", "description": "Optional flash+run config {file_path, run_to (default 'main'), timeout_sec}."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def start_acceptance_loop(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    spec = ctx.acceptance.get("current")
    if not spec:
        return [content_error(
            "No acceptance spec loaded for this session. Run load_acceptance first.", code="no_spec",
            suggested_next_actions=["load_acceptance(spec={...})"])]
    max_iterations = arguments.get("max_iterations", 10)
    if not isinstance(max_iterations, int) or max_iterations < 1:
        return [content_error(
            "max_iterations must be an integer >= 1.", code="invalid_argument",
            suggested_next_actions=["start_acceptance_loop(max_iterations=10)"])]
    build_cfg = arguments.get("build")
    flash_cfg = arguments.get("flash")
    plan = {
        "max_iterations": max_iterations,
        "stall_patience": arguments.get("stall_patience", 3),
        "has_build": bool(build_cfg),
        "has_flash": bool(flash_cfg),
        "build": build_cfg,
        "flash": flash_cfg,
        "acceptance_name": spec.get("name"),
    }
    state = new_loop_state(plan)
    ctx.loop["current"] = state
    return [content_success(
        summarize_loop(state), suggested_next_actions=["run_acceptance_iteration"])]


@register(Tool(
    name="run_acceptance_iteration",
    description="Run one bounded loop iteration: (optional build) → (optional flash+run-to) → "
                "evaluate the AcceptanceSpec against live silicon, then record the verdict and "
                "return a decision (converged / should_continue / exhausted / stalled) plus the "
                "checks to fix. A build or run-to failure is recorded as a phase_error, not a "
                "crash. Refuses to run a terminal loop unless force=true. start_acceptance_loop first.",
    inputSchema={
        "type": "object",
        "properties": {
            "force": {"type": "boolean", "description": "Run even if the loop already converged/exhausted/stalled."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def run_acceptance_iteration(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    state = ctx.loop.get("current")
    if not state:
        return [content_error(
            "No acceptance loop started for this session. Run start_acceptance_loop first.", code="no_loop",
            suggested_next_actions=["start_acceptance_loop"])]
    spec = ctx.acceptance.get("current")
    if not spec:
        return [content_error(
            "No acceptance spec loaded for this session. Run load_acceptance first.", code="no_spec",
            suggested_next_actions=["load_acceptance(spec={...})"])]
    if state["status"] != "active" and not arguments.get("force"):
        decision = loop_decision(state)
        return [content_success(
            {"iteration": None, "decision": decision, "summary": summarize_loop(state)},
            suggested_next_actions=decision["next_actions"])]
    plan = state["plan"]
    steps = GdbLoopSteps(ctx.gdb_client, spec, build_cfg=plan.get("build"), flash_cfg=plan.get("flash"))
    outcome = run_iteration(state, steps)
    ctx.loop["current"] = state
    decision = outcome["decision"]
    return [content_success(
        {"iteration": outcome["iteration"], "decision": decision, "summary": summarize_loop(state)},
        suggested_next_actions=decision["next_actions"])]


@register(Tool(
    name="acceptance_loop_status",
    description="Read the acceptance loop's trajectory (per-iteration pass/fail counts, status) "
                "and the current decision without running an iteration.",
    inputSchema={
        "type": "object",
        "properties": {
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def acceptance_loop_status(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    state = ctx.loop.get("current")
    if not state:
        return [content_error(
            "No acceptance loop started for this session. Run start_acceptance_loop first.", code="no_loop",
            suggested_next_actions=["start_acceptance_loop"])]
    return [content_success({"summary": summarize_loop(state), "decision": loop_decision(state)})]
