# Acceptance-Loop Orchestrator Implementation Plan / 验收闭环编排器实现计划

> **For agentic workers:** Implement this plan task-by-task using TDD (test-first).
> Pure control logic in `loop_control.py`, a thin injectable mechanical driver in
> `loop_orchestrator.py`, then server tools. Plain-dict state, no dataclasses.

**Goal / 目标:** Close the spec-to-silicon loop. Given a machine-readable board (Pillar A) and
a machine-checked AcceptanceSpec (Pillar B1), the orchestrator runs a **bounded** iterate loop:
`build → flash → run-to-state → run_acceptance → verdict`; on failure it hands the agent a
precise "these checks failed, fix and iterate" report; on success it stops. This is **Pillar C**
(网表+规格 → 框架设计 → 代码 → **闭环验证**).

**What is / isn't automated / 自动化边界:** The orchestrator owns the *deterministic mechanics
and the bounds* — sequencing build/flash/run/evaluate, tracking the verdict trajectory,
detecting convergence / exhaustion / stall, and refusing to loop forever. It does **not** write
firmware fixes — that is the agent's job between iterations. So the loop is *agent-driven but
machine-bounded*: one `run_acceptance_iteration` call performs a whole mechanical pass and
returns an objective decision, keeping the LLM in the loop only for the creative step (editing
code) and never for the judgement (Pillar B1 is the judge).

**Why bounded matters / 为什么"有界":** An unbounded "keep trying" loop wastes hardware time and
can oscillate forever. The control core enforces `max_iterations`, detects a **stall** (the same
checks failing N times in a row → the current approach cannot converge), and reports a
trajectory so a human or the agent can see progress. Determinism + bounds make the loop safe to
run unattended.

**Architecture / 架构:**
- `loop_control.py` — **pure brain** (fully unit-tested, no I/O): loop state, per-iteration
  recording + trajectory diff, status recomputation (active/converged/exhausted/stalled),
  and the decision + next-action synthesis.
- `loop_orchestrator.py` — **thin hands**: `run_iteration(state, steps)` performs one mechanical
  pass through an injected `steps` object (`build`/`flash`/`evaluate`), capturing a build or
  run-to failure as a `phase_error` iteration instead of crashing. `GdbLoopSteps` is the real
  adapter, reusing `build.py`, `composites.flash_and_run`, and `acceptance_eval`. Tests inject a
  fake `steps`.
- `server.py` — thin MCP adapter: 3 tools + per-session loop state, mirroring the board /
  acceptance persistence.

**Tech Stack:** Python 3.10+, stdlib, pytest, ruff (E/F/I/UP/B @120). No new hardware needed to
test the control logic (fakes); real runs need a probe + toolchain.

---

## Loop state (persisted per session)

```jsonc
{
  "plan": {
    "max_iterations": 10,
    "stall_patience": 3,          // same failing set this many times => stalled
    "has_build": true, "build": {"kind": "cmake", "build_dir": "build", ...},
    "has_flash": true, "flash": {"file_path": "build/fw.elf", "run_to": "main"},
    "acceptance_name": "blinky-accept"
  },
  "status": "active",             // active | converged | exhausted | stalled
  "iterations": [
    {"index": 0, "ok": false, "phase": "acceptance",
     "passed": 3, "failed": 2, "errored": 0,
     "unsatisfied_ids": ["usart1-on", "sysclk"],
     "newly_satisfied": [], "newly_broken": [],
     "phase_error": null}
    // ... or a mechanical failure:
    // {"index": 1, "ok": false, "phase": "build", "phase_error": "cmake: 3 errors ...",
    //  "unsatisfied_ids": [], ...}
  ]
}
```

## Decision (returned every iteration)
```jsonc
{"status": "active", "converged": false, "exhausted": false, "stalled": false,
 "should_continue": true, "iteration_count": 1, "reason": "2 checks failing",
 "next_actions": ["fix failing checks: usart1-on, sysclk", "run_acceptance_iteration"]}
```

---

## Tier 1 — Control core + orchestrator (pure, unit-tested)
### T1.1 `loop_control.py`
- `new_loop_state(plan)` → normalized state (defaults: max_iterations=10, stall_patience=3).
- `record_iteration(state, *, verdict=None, phase_error=None)` → append an iteration entry.
  From an acceptance `verdict`: `unsatisfied_ids = failed ∪ errored`; diff vs the previous
  iteration's set → `newly_satisfied`, `newly_broken`. A `phase_error` (build/flash) → `ok=False`,
  empty unsatisfied set, phase recorded.
- `_recompute_status(state)`: `converged` if last ok; else `exhausted` if count ≥ max_iterations;
  else `stalled` if the last `stall_patience` iterations are all non-ok with an identical,
  non-empty `unsatisfied_ids`; else `active`.
- `loop_decision(state)` → status flags + `should_continue` (only when `active`) + `next_actions`.
- `summarize_loop(state)` → compact trajectory.

### T1.2 `loop_orchestrator.py`
- `run_iteration(state, steps)`: `steps.has_build`→`steps.build()` (fail → phase_error build);
  `steps.has_flash`→`steps.flash()` (not stopped → phase_error flash_run); else `steps.evaluate()`
  → verdict; record + decide. Returns `{"iteration", "decision"}`.
- `GdbLoopSteps(gdb_client, spec, build_cfg, flash_cfg)` real adapter (built in Tier 2 wiring).
- Tests inject a `FakeSteps`.

---

## Tier 2 — Tools + real steps + per-session state
- `_loop = {"current": None}` global; add `"loop"` to `_SESSION_ATTRS`/`_DEFAULT_SESSION_GLOBALS`;
  `DebugSession.loop`; bind `session_loop` in `_dispatch_tool`.
- `start_acceptance_loop` — requires a loaded AcceptanceSpec; args `max_iterations?`,
  `stall_patience?`, `build?`, `flash?`. Builds the plan, resets loop state, returns summary.
- `run_acceptance_iteration` — runs one mechanical pass via `GdbLoopSteps`; persists state; if the
  loop is already terminal it refuses unless `force=true`. Returns `{iteration, decision, summary}`.
- `acceptance_loop_status` — `summarize_loop` + `loop_decision`.
- Not in `_CORE_TOOLS` (reachable via `call` in compact mode).

---

## Cross-cutting
- Plain-dict state (JSON-native) — no dataclasses.
- Keep `python -m ruff check .` and `python -m pytest -q` green after every task.
- Every tool populates `suggested_next_actions`.
- Update `CHANGELOG.md` (bilingual) once Tier 2 exposes the tools.

## Execution order
T1.1 → T1.2 → T2. Committed as one Pillar C change (or split if large).

## Status / 状态
- [x] **Tier 1** — `loop_control.py` + `loop_orchestrator.py` + tests. (9 + 5 tests)
- [x] **Tier 2** — `start/run/status` loop tools + real steps + per-session state + tests. (6 server tests; suite 307 passed / 1 skipped)

## Deferred / 延后
- Auto-editing firmware between iterations (stays with the agent — by design).
- Multi-board / parallel loop fan-out for a CI rack.
- External-instrument acceptance (scope/logic-analyzer/PSU) — Pillar B2.
- Auto-bisect / hypothesis ranking across the failure trajectory.
