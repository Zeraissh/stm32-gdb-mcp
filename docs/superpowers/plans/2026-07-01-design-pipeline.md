# Design pipeline — end-to-end orchestration (Pillar G)

## Why / 目标

The spec-to-silicon **design half** is now feature-complete — a netlist becomes a
board model (Pillar A), a product spec becomes design params (spec_model), a plan
becomes a HAL skeleton (Pillar D) with clock/timer values solved, and a
machine-checked acceptance spec is synthesized (Pillars B1/C/E). But the engineer
(or the agent) still has to **hand-chain** those tools in the right order, wire
each output into the next input, and manually gather the `unresolved` gaps that
every stage reports independently. That orchestration was the last of the five
weak points from the pipeline review (#4 — end-to-end orchestration manual).

Pillar G adds a single capstone tool that runs the deterministic design DAG in
dependency order and returns one consolidated report.

## Core principle / 核心原则

**Pure orchestration — nothing new is guessed here.** Every stage is an existing,
individually verified tool, re-dispatched in order. The only new logic is (1)
choosing which stages to run and with what args, and (2) aggregating every
stage's honest `unresolved`/`conflict` items into a single "human decisions /
data still needed" list. A gating hard-error (no board, invalid input) stops the
pipeline and is reported as `blocked` at that stage; expected gaps (an AF number
needing a pin DB, an unmodelled clock device) are **not** failures — the pipeline
runs through and surfaces them aggregated. Deterministic design half only: it
stops before build/flash/verify (the hardware half, already run by the Pillar C
loop) and hands off.

## Pipeline DAG

```
import_netlist?  ->  import_spec?  ->  design_framework  ->  solve_clock_tree?
   (if netlist)       (if spec)         (required)            (if sysclk_hz)
   ->  solve_timer?  ->  render_framework  ->  synthesize_acceptance
       (if plan has        (required)            (required)
        timer targets)
```

Optional stages run only when their input is present (`?`). `solve_timer` is
gated on the *plan* (any TIM block with a recorded `timer_target_hz`), decided
after `design_framework`.

## run_pipeline request

```jsonc
{
  "netlist": {"path": "board.net"},        // or {"text","format"}; omit if board already imported
  "spec": {"USART1": {"baud": 115200, "framing": "8N1"}},   // -> import_spec + design_framework(from_spec)
  "design": {"USART1": {"nvic": true}},    // explicit design overrides (merged over spec)
  "af_map": {...}, "db_path": "pins.json", // GPIO AF resolution
  "sysclk_hz": 80000000,                   // -> solve_clock_tree; omit to skip
  "source": "hse", "source_hz": 8000000, "need_48mhz": false,
  "register_map": {...}, "irq_map": {...}, "gpio_map": {...},  // acceptance placement
  "acceptance_name": "bringup", "stopped_at": "main",
  "style": "hal"
}
```

## Report

```jsonc
{
  "pipeline_status": "complete" | "complete_with_unresolved" | "blocked",
  "ran": ["import_netlist","import_spec","design_framework",...],
  "skipped": [{"stage":"solve_timer","reason":"no timer targets"}],
  "stages": [{"stage","ok","summary","unresolved_count","code?"}],
  "blocked": null | {"stage","code","message"},
  "unresolved": [{"stage","type","detail",...}],   // aggregated across all stages
  "unresolved_count": 3,
  "mcu": {...}, "files": [{path,content}], "acceptance": {name,check_count,kinds}
}
```

## Tier 1 — pure orchestration logic (unit-tested)

- `pipeline.py` (NEW, pure): `STAGE_ORDER`, `REQUIRED_STAGES`, `wants_stage(stage,
  request, plan=None)`, `stage_args(stage, request)`, `_has_timer_targets(plan)`,
  `extract_gaps(stage, data, plan=None)` (per-stage honest gap projection),
  `consolidate(outcomes, skipped)` (status + aggregation).
- Tests: `tests/test_pipeline.py`.

## Tier 2 — MCP surface

- `run_pipeline` tool + handler: drives `STAGE_ORDER`, re-dispatching each wanted
  stage via `_dispatch_tool(stage, {**stage_args, "session": _sess.id})`, parsing
  each envelope, extracting gaps (design gaps read from the session plan's
  `unresolved`), stopping on a required-stage hard error. Enriches the
  consolidated report with `mcu` / `files` / `acceptance` highlights. Tool count
  78 -> 79.

## Not fabricating / 不臆造

The pipeline invents no device facts or design values — it only sequences the
existing deterministic tools and reports their honest output verbatim. A blocked
stage keeps the sub-tool's own error; every gap keeps its originating stage +
type so the engineer knows exactly what to supply and where.

## Status / 状态
- [x] `pipeline.py` — DAG order + gating + gap extraction + consolidation.
- [x] `run_pipeline` tool + handler (re-dispatch, aggregate, highlights).
- [x] Tests (unit + server) + docs + suite green / ruff clean.
