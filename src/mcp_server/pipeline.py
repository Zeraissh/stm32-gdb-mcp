"""Deterministic design-pipeline orchestration (Pillar G).

Chains the spec-to-silicon *design half* -- import_netlist -> import_spec ->
design_framework -> solve_clock_tree -> solve_timer -> render_framework ->
synthesize_acceptance -- in dependency order, so a single call turns a netlist
plus a product spec into a flashable HAL skeleton and a machine-checked
acceptance spec. Pure orchestration: every stage is an existing, individually
verified tool, so **nothing new is guessed here**. The value added is (1) running
the DAG in the right order with the right args and (2) aggregating every stage's
honest ``unresolved`` / ``conflict`` gaps into one "human decisions / data still
needed" list.

Honest by construction: a gating hard-error (no board, invalid input) stops the
pipeline and is reported as ``blocked`` at that stage; expected gaps (an
alternate-function number that needs a pin DB, an unmodelled clock device) are
**not** failures -- the pipeline runs through and they surface aggregated with
``pipeline_status = "complete_with_unresolved"``. A clean run is ``complete``.
Deterministic design half only: the pipeline stops before build/flash/verify
(the hardware half, already orchestrated by the Pillar C loop) and hands off.

Pure module: no imports from the rest of the package; everything is plain dicts
so a report serializes straight through the JSON envelope.
"""

# Canonical stage order. Each entry: (tool name, required?). Optional stages run
# only when their input is present (see ``wants_stage``).
_ORDER = (
    ("import_netlist", False),
    ("import_spec", False),
    ("design_framework", True),
    ("solve_clock_tree", False),
    ("solve_timer", False),
    ("render_framework", True),
    ("synthesize_acceptance", True),
)

STAGE_ORDER = tuple(name for name, _ in _ORDER)
REQUIRED_STAGES = frozenset(name for name, required in _ORDER if required)

# Locator fields worth preserving on an aggregated gap so the engineer knows
# exactly what to supply and where. Copied verbatim from the sub-tool's item.
_GAP_FIELDS = ("detail", "peripheral", "port_pin", "signal", "role", "timer",
               "line", "family", "irq", "macro", "name", "suggestion")


def _has_timer_targets(plan):
    """True when the plan has any TIM block carrying a recorded update target."""
    return any(block.get("kind") == "timer" and block.get("timer_target_hz") is not None
               for block in (plan or {}).get("peripherals", []))


def wants_stage(stage, request, plan=None):
    """Whether a stage should run, given the request and (for solve_timer) the plan."""
    if stage == "import_netlist":
        return bool(request.get("netlist"))
    if stage == "import_spec":
        return bool(request.get("spec"))
    if stage == "design_framework":
        return True
    if stage == "solve_clock_tree":
        return request.get("sysclk_hz") is not None
    if stage == "solve_timer":
        return _has_timer_targets(plan)
    if stage == "render_framework":
        return True
    if stage == "synthesize_acceptance":
        return request.get("synthesize", True) is not False
    return False


def skip_reason(stage, request, plan=None):
    """Human-readable reason a wanted-less stage was skipped (for the report)."""
    if stage == "import_netlist":
        return "no netlist supplied (using the board already in this session)"
    if stage == "import_spec":
        return "no product spec supplied"
    if stage == "solve_clock_tree":
        return "no sysclk_hz target supplied"
    if stage == "solve_timer":
        return "no timer had a recorded update target (design update_hz)"
    if stage == "synthesize_acceptance":
        return "synthesize=false"
    return "not applicable"


def stage_args(stage, request):
    """Project the flat pipeline request onto one stage's tool arguments."""
    if stage == "import_netlist":
        return dict(request.get("netlist") or {})
    if stage == "import_spec":
        return {"spec": request.get("spec")}
    if stage == "design_framework":
        args = {}
        if request.get("spec") is not None:
            args["from_spec"] = True
        for key in ("design", "af_map", "db_path"):
            if request.get(key) is not None:
                args[key] = request[key]
        return args
    if stage == "solve_clock_tree":
        args = {"sysclk_hz": request.get("sysclk_hz")}
        for key in ("source", "source_hz", "need_48mhz"):
            if request.get(key) is not None:
                args[key] = request[key]
        return args
    if stage == "solve_timer":
        return {}  # solve every timer with a recorded target
    if stage == "render_framework":
        return {"style": request["style"]} if request.get("style") is not None else {}
    if stage == "synthesize_acceptance":
        args = {}
        for key in ("register_map", "irq_map", "gpio_map", "stopped_at"):
            if request.get(key) is not None:
                args[key] = request[key]
        if request.get("acceptance_name") is not None:
            args["name"] = request["acceptance_name"]
        return args
    return {}


def _gap(stage, kind, item):
    """Normalize one sub-tool gap into a stage-tagged, locator-preserving dict."""
    gap = {"stage": stage, "type": kind}
    if isinstance(item, dict):
        for key in _GAP_FIELDS:
            if item.get(key) is not None:
                gap[key] = item[key]
    else:
        gap["detail"] = str(item)
    return gap


def extract_gaps(stage, data, plan=None):
    """Project one stage's honest unresolved/conflict items into tagged gaps.

    ``data`` is the stage envelope's ``data`` block. ``design_framework`` gaps are
    read from the session ``plan`` because its summary carries only a count.
    """
    data = data or {}
    gaps = []
    if stage == "import_spec":
        for conflict in data.get("conflicts") or []:
            gaps.append(_gap(stage, "spec_conflict", conflict))
        for item in data.get("unresolved") or []:
            gaps.append(_gap(stage, _kind(item, "spec_unresolved"), item))
    elif stage == "design_framework":
        for item in (plan or {}).get("unresolved") or []:
            gaps.append(_gap(stage, _kind(item, "unresolved"), item))
    elif stage == "solve_clock_tree":
        if not data.get("feasible", True):
            for item in data.get("unresolved") or []:
                gaps.append(_gap(stage, _kind(item, "clock_unresolved"), item))
    elif stage == "solve_timer":
        for result in data.get("results") or []:
            if not result.get("feasible", True):
                timer = result.get("timer")
                for item in result.get("unresolved") or []:
                    tagged = {**item, "timer": timer} if isinstance(item, dict) else item
                    gaps.append(_gap(stage, _kind(item, "timer_unresolved"), tagged))
    elif stage == "synthesize_acceptance":
        for item in data.get("unresolved") or []:
            gaps.append(_gap(stage, _kind(item, "acceptance_unresolved"), item))
    return gaps


def _kind(item, default):
    return item.get("type", default) if isinstance(item, dict) else default


def consolidate(outcomes, skipped):
    """Reduce per-stage outcomes into the capstone pipeline report.

    ``outcomes``: ordered list of ``{stage, ok, code?, message?, summary?, gaps:[...]}``
    for the stages that actually ran. ``skipped``: ``[{stage, reason}]``.
    """
    blocked = None
    for outcome in outcomes:
        if not outcome.get("ok") and outcome["stage"] in REQUIRED_STAGES:
            blocked = {"stage": outcome["stage"], "code": outcome.get("code"),
                       "message": outcome.get("message")}
            break

    all_gaps = [gap for outcome in outcomes for gap in outcome.get("gaps", [])]
    if blocked:
        status = "blocked"
    elif all_gaps:
        status = "complete_with_unresolved"
    else:
        status = "complete"

    stages = []
    for outcome in outcomes:
        entry = {"stage": outcome["stage"], "ok": bool(outcome.get("ok")),
                 "summary": outcome.get("summary"),
                 "unresolved_count": len(outcome.get("gaps", []))}
        if outcome.get("code"):
            entry["code"] = outcome["code"]
        stages.append(entry)

    return {
        "pipeline_status": status,
        "ran": [outcome["stage"] for outcome in outcomes],
        "skipped": skipped,
        "stages": stages,
        "blocked": blocked,
        "unresolved": all_gaps,
        "unresolved_count": len(all_gaps),
    }
