"""Pipeline tool (Pillar G): the spec-to-silicon capstone that re-dispatches verified tools."""

import json

from mcp.types import TextContent, Tool

from .. import pipeline
from ..acceptance_model import summarize_acceptance
from ..tool_response import content_success
from .context import ToolContext
from .registry import register


def _pipeline_stage_summary(stage: str, data: dict) -> dict:
    """A compact, display-only highlight of one pipeline stage's envelope."""
    data = data or {}
    if stage == "import_netlist":
        mcu = data.get("mcu") or {}
        return {"mcu": mcu.get("part_normalized") or mcu.get("part"),
                "peripherals": len(data.get("peripherals") or [])}
    if stage == "import_spec":
        return {"design_peripherals": len(data.get("design") or {}),
                "conflicts": len(data.get("conflicts") or []),
                "cross_checked": data.get("cross_checked")}
    if stage == "design_framework":
        return {"peripherals": len(data.get("peripherals") or []),
                "clocks": len(data.get("clocks") or []),
                "unresolved": data.get("unresolved_count")}
    if stage == "solve_clock_tree":
        if data.get("feasible"):
            return {"feasible": True, "sysclk_mhz": (data.get("clock") or {}).get("sysclk_mhz")}
        return {"feasible": False}
    if stage == "solve_timer":
        return {"solved": data.get("solved_count"), "timers": data.get("timer_count")}
    if stage == "render_framework":
        return {"files": [f.get("path") for f in data.get("files") or []],
                "todo_count": data.get("todo_count")}
    if stage == "synthesize_acceptance":
        spec = data.get("spec") or {}
        return {"checks": spec.get("check_count"), "kinds": spec.get("kinds")}
    return {}


def _pipeline_next(report: dict) -> list[str]:
    """Suggested next actions tailored to the pipeline outcome."""
    status = report["pipeline_status"]
    if status == "blocked":
        blocked = report.get("blocked") or {}
        return [f"fix the blocked stage: {blocked.get('stage')} ({blocked.get('code')})"]
    if status == "complete_with_unresolved":
        return ["review 'unresolved' (supply af_map/db_path, a device pack, or a clock profile)",
                "build_firmware", "start_acceptance_loop"]
    return ["build_firmware", "start_acceptance_loop"]


@register(Tool(
    name="run_pipeline",
    description="Spec-to-silicon capstone (Pillar G): run the whole deterministic DESIGN half in one call. "
                "Chains the existing, individually verified tools in dependency order -- import_netlist? -> "
                "import_spec? -> design_framework -> solve_clock_tree? -> solve_timer? -> render_framework -> "
                "synthesize_acceptance -- turning a netlist plus a product spec into a flashable HAL skeleton "
                "and a machine-checked acceptance spec, then hands off to the build/verify loop. Optional "
                "stages (marked ?) run only when their input is present. Honest by construction: it GUESSES "
                "NOTHING -- it sequences verified tools and aggregates every stage's own unresolved/conflict "
                "gaps into one 'human decisions / data still needed' list. A required-stage hard error (no "
                "board, invalid input) stops with pipeline_status=blocked at that stage; expected gaps (an AF "
                "needing a pin DB, an unmodelled clock) surface as complete_with_unresolved; a clean run is "
                "complete. Operates on the session's board unless netlist= is supplied.",
    inputSchema={
        "type": "object",
        "properties": {
            "netlist": {"type": "object", "description": "Netlist to import first: {path|text, format?}. Omit to use the board already in this session."},
            "spec": {"type": "object", "description": "Product spec object to import and design from (import_spec)."},
            "design": {"type": "object", "description": "Peripheral config overrides merged into the design {PERIPH: {..}}."},
            "af_map": {"type": "object", "description": "Alternate-function map for GPIO AF resolution {line_or_family: {port_pin: {'PERIPH_SIG': af}}}."},
            "db_path": {"type": "string", "description": "Path to a pin-capability DB for AF resolution (else STM32_GDB_MCP_PIN_DB)."},
            "sysclk_hz": {"type": "integer", "description": "Target SYSCLK in Hz; presence enables the solve_clock_tree stage."},
            "source": {"type": "string", "description": "Clock source for solve_clock_tree (e.g. hse/hsi)."},
            "source_hz": {"type": "integer", "description": "Clock source frequency in Hz for solve_clock_tree."},
            "need_48mhz": {"type": "boolean", "description": "Require a 48 MHz domain (USB/SDMMC) in the clock solve."},
            "register_map": {"type": "object", "description": "Clock-enable register map for acceptance placement {line_or_family: {clock: {address, bit}}}."},
            "irq_map": {"type": "object", "description": "IRQ-number map for NVIC acceptance checks {line_or_family: {irq_name: number}}."},
            "gpio_map": {"type": "object", "description": "GPIO base-address map for pin acceptance checks {line_or_family: {port_letter: base_address}}."},
            "acceptance_name": {"type": "string", "description": "Name for the derived acceptance spec."},
            "stopped_at": {"type": "string", "description": "Symbol the acceptance spec should assert the core halts at."},
            "style": {"type": "string", "description": "Render style for the framework skeleton (default hal)."},
            "synthesize": {"type": "boolean", "description": "Set false to stop after render_framework and skip acceptance synthesis (default true)."}
        }
    }
))
def run_pipeline(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    # Capstone: run the deterministic design DAG (Pillar G) by re-dispatching the
    # existing verified tools in order, aggregating every stage's honest gaps. It
    # never guesses and never hard-errors: a required stage failing (e.g. no board)
    # is reported honestly as pipeline_status=blocked at that stage.
    request = dict(arguments)
    outcomes: list = []
    skipped: list = []
    for stage in pipeline.STAGE_ORDER:
        plan = ctx.design.get("current")
        if not pipeline.wants_stage(stage, request, plan):
            skipped.append({"stage": stage,
                            "reason": pipeline.skip_reason(stage, request, plan)})
            continue
        sub_args = dict(pipeline.stage_args(stage, request))
        sub_args["session"] = ctx.sess.id  # keep every hop on this session
        env = json.loads(ctx.dispatch(stage, sub_args)[0].text)
        ok = bool(env.get("ok"))
        data = env.get("data") or {}
        error = env.get("error") or {}
        gaps = pipeline.extract_gaps(stage, data, ctx.design.get("current")) if ok else []
        outcomes.append({
            "stage": stage, "ok": ok,
            "code": error.get("code"), "message": error.get("message"),
            "summary": _pipeline_stage_summary(stage, data),
            "gaps": gaps,
        })
        if not ok and stage in pipeline.REQUIRED_STAGES:
            break  # a required stage hard-failed -> blocked, stop here
    report = pipeline.consolidate(outcomes, skipped)
    # Enrich with the pipeline "products" the engineer actually wants in hand.
    plan = ctx.design.get("current")
    if plan:
        report["mcu"] = plan.get("mcu")
    render = ctx.design.get("last_render")
    if render and report["pipeline_status"] != "blocked":
        report["files"] = [{"path": f["path"], "content": f["content"]}
                           for f in render.get("files", [])]
    accepted = ctx.acceptance.get("current")
    if accepted and any(o["stage"] == "synthesize_acceptance" and o["ok"] for o in outcomes):
        report["acceptance"] = summarize_acceptance(accepted)
    return [content_success(report, suggested_next_actions=_pipeline_next(report))]
