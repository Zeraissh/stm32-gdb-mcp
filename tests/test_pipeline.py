"""Tests for the pure design-pipeline orchestration logic (Pillar G)."""

from mcp_server import pipeline

# --- stage gating ------------------------------------------------------------


def test_stage_order_and_required():
    assert pipeline.STAGE_ORDER == (
        "import_netlist", "import_spec", "design_framework", "solve_clock_tree",
        "solve_timer", "render_framework", "synthesize_acceptance")
    assert pipeline.REQUIRED_STAGES == {
        "design_framework", "render_framework", "synthesize_acceptance"}


def test_wants_stage_optional_inputs():
    empty = {}
    assert pipeline.wants_stage("import_netlist", empty) is False
    assert pipeline.wants_stage("import_netlist", {"netlist": {"path": "b.net"}}) is True
    assert pipeline.wants_stage("import_spec", empty) is False
    assert pipeline.wants_stage("import_spec", {"spec": {"USART1": {}}}) is True
    assert pipeline.wants_stage("solve_clock_tree", empty) is False
    assert pipeline.wants_stage("solve_clock_tree", {"sysclk_hz": 80_000_000}) is True
    # Required stages always run.
    assert pipeline.wants_stage("design_framework", empty) is True
    assert pipeline.wants_stage("render_framework", empty) is True
    assert pipeline.wants_stage("synthesize_acceptance", empty) is True
    # Acceptance can be opted out.
    assert pipeline.wants_stage("synthesize_acceptance", {"synthesize": False}) is False


def test_solve_timer_gated_on_plan_targets():
    no_targets = {"peripherals": [{"kind": "timer", "name": "TIM3"}]}
    with_target = {"peripherals": [{"kind": "timer", "name": "TIM3", "timer_target_hz": 1000}]}
    assert pipeline.wants_stage("solve_timer", {}, plan=None) is False
    assert pipeline.wants_stage("solve_timer", {}, plan=no_targets) is False
    assert pipeline.wants_stage("solve_timer", {}, plan=with_target) is True


# --- arg projection ----------------------------------------------------------


def test_stage_args_projection():
    request = {
        "netlist": {"path": "b.net", "format": "kicad"},
        "spec": {"USART1": {"baud": 115200}},
        "design": {"USART1": {"nvic": True}},
        "af_map": {"STM32L4": {}},
        "db_path": "pins.json",
        "sysclk_hz": 80_000_000,
        "source": "hse", "source_hz": 8_000_000, "need_48mhz": True,
        "register_map": {"a": 1}, "irq_map": {"b": 2}, "gpio_map": {"c": 3},
        "acceptance_name": "bringup", "stopped_at": "main",
        "style": "hal",
    }
    assert pipeline.stage_args("import_netlist", request) == {"path": "b.net", "format": "kicad"}
    assert pipeline.stage_args("import_spec", request) == {"spec": {"USART1": {"baud": 115200}}}
    design = pipeline.stage_args("design_framework", request)
    assert design["from_spec"] is True
    assert design["design"] == {"USART1": {"nvic": True}}
    assert design["af_map"] == {"STM32L4": {}} and design["db_path"] == "pins.json"
    clock = pipeline.stage_args("solve_clock_tree", request)
    assert clock == {"sysclk_hz": 80_000_000, "source": "hse",
                     "source_hz": 8_000_000, "need_48mhz": True}
    assert pipeline.stage_args("solve_timer", request) == {}
    assert pipeline.stage_args("render_framework", request) == {"style": "hal"}
    synth = pipeline.stage_args("synthesize_acceptance", request)
    assert synth == {"register_map": {"a": 1}, "irq_map": {"b": 2}, "gpio_map": {"c": 3},
                     "stopped_at": "main", "name": "bringup"}


def test_stage_args_minimal_request():
    assert pipeline.stage_args("design_framework", {}) == {}
    assert pipeline.stage_args("solve_clock_tree", {}) == {"sysclk_hz": None}
    assert pipeline.stage_args("render_framework", {}) == {}
    assert pipeline.stage_args("synthesize_acceptance", {}) == {}


# --- gap extraction ----------------------------------------------------------


def test_extract_gaps_import_spec():
    data = {"conflicts": [{"peripheral": "SPI2", "detail": "not on board"}],
            "unresolved": [{"type": "unknown_key", "peripheral": "USART1", "detail": "bad key"}]}
    gaps = pipeline.extract_gaps("import_spec", data)
    assert {g["type"] for g in gaps} == {"spec_conflict", "unknown_key"}
    assert all(g["stage"] == "import_spec" for g in gaps)
    assert gaps[0]["peripheral"] == "SPI2"


def test_extract_gaps_design_reads_plan():
    plan = {"unresolved": [{"type": "af_unknown", "port_pin": "PA9", "signal": "USART1_TX"}]}
    gaps = pipeline.extract_gaps("design_framework", {}, plan=plan)
    assert len(gaps) == 1
    assert gaps[0] == {"stage": "design_framework", "type": "af_unknown",
                       "port_pin": "PA9", "signal": "USART1_TX"}


def test_extract_gaps_clock_only_when_infeasible():
    assert pipeline.extract_gaps("solve_clock_tree", {"feasible": True}) == []
    data = {"feasible": False, "unresolved": [{"type": "device_unmodelled", "family": "STM32G0"}]}
    gaps = pipeline.extract_gaps("solve_clock_tree", data)
    assert gaps == [{"stage": "solve_clock_tree", "type": "device_unmodelled", "family": "STM32G0"}]


def test_extract_gaps_timer_per_result():
    data = {"results": [
        {"timer": "TIM2", "feasible": True, "unresolved": []},
        {"timer": "TIM3", "feasible": False,
         "unresolved": [{"type": "bus_unknown", "detail": "APB bus unknown"}]},
    ]}
    gaps = pipeline.extract_gaps("solve_timer", data)
    assert len(gaps) == 1
    assert gaps[0]["type"] == "bus_unknown"
    assert gaps[0]["timer"] == "TIM3"


def test_extract_gaps_acceptance():
    data = {"unresolved": [{"type": "irq_number_unknown", "irq": "USART1_IRQn"}]}
    gaps = pipeline.extract_gaps("synthesize_acceptance", data)
    assert gaps == [{"stage": "synthesize_acceptance", "type": "irq_number_unknown",
                     "irq": "USART1_IRQn"}]


# --- consolidation -----------------------------------------------------------


def test_consolidate_complete():
    outcomes = [
        {"stage": "design_framework", "ok": True, "summary": "3 periph", "gaps": []},
        {"stage": "render_framework", "ok": True, "summary": "2 files", "gaps": []},
        {"stage": "synthesize_acceptance", "ok": True, "summary": "5 checks", "gaps": []},
    ]
    report = pipeline.consolidate(outcomes, skipped=[])
    assert report["pipeline_status"] == "complete"
    assert report["ran"] == ["design_framework", "render_framework", "synthesize_acceptance"]
    assert report["blocked"] is None
    assert report["unresolved_count"] == 0


def test_consolidate_complete_with_unresolved_aggregates():
    outcomes = [
        {"stage": "design_framework", "ok": True, "gaps": [
            {"stage": "design_framework", "type": "af_unknown", "port_pin": "PA9"}]},
        {"stage": "render_framework", "ok": True, "gaps": []},
        {"stage": "synthesize_acceptance", "ok": True, "gaps": [
            {"stage": "synthesize_acceptance", "type": "irq_number_unknown"}]},
    ]
    report = pipeline.consolidate(outcomes, skipped=[{"stage": "solve_clock_tree", "reason": "no sysclk_hz"}])
    assert report["pipeline_status"] == "complete_with_unresolved"
    assert report["unresolved_count"] == 2
    assert {g["stage"] for g in report["unresolved"]} == {"design_framework", "synthesize_acceptance"}
    assert report["skipped"] == [{"stage": "solve_clock_tree", "reason": "no sysclk_hz"}]


def test_consolidate_blocked_on_required_stage_error():
    outcomes = [
        {"stage": "design_framework", "ok": False, "code": "no_board",
         "message": "No board imported.", "gaps": []},
    ]
    report = pipeline.consolidate(outcomes, skipped=[])
    assert report["pipeline_status"] == "blocked"
    assert report["blocked"] == {"stage": "design_framework", "code": "no_board",
                                 "message": "No board imported."}
    assert report["stages"][0]["code"] == "no_board"


def test_consolidate_optional_stage_failure_is_not_blocked():
    # An optional stage returning ok=False (e.g. solve_timer no targets) must not block.
    outcomes = [
        {"stage": "design_framework", "ok": True, "gaps": []},
        {"stage": "solve_clock_tree", "ok": False, "code": "missing_argument", "gaps": []},
        {"stage": "render_framework", "ok": True, "gaps": []},
        {"stage": "synthesize_acceptance", "ok": True, "gaps": []},
    ]
    report = pipeline.consolidate(outcomes, skipped=[])
    assert report["pipeline_status"] == "complete"
    assert report["blocked"] is None
