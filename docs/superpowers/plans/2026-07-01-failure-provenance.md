# Failure -> source provenance (Pillar E, design synthesis)

## Why / 目标

The spec-to-silicon loop can now say *what* failed: `synthesize_acceptance`
derives a machine judge and `run_acceptance` reports, per check, `pass` / `fail`
/ `error`. But when a check fails the agent is told the **symptom** (`[0xE000E100]
& 0x20 = 0x0 bits_set 0x20`) with no pointer to the **cause** -- which init
function, which line, which construct was supposed to set that bit. The agent
then greps the generated `bsp_init.c` by hand. That hand-off is the weak point:
the closed loop ("verify fails -> fix code -> re-verify") jumps context on every
iteration.

This pillar closes the gap: every failing check carries the exact **source site**
that should satisfy it, so the fix is precision-guided instead of a blind search.

## The determinism boundary (why this stays honest)

Provenance is **not** inference. Every derived check already comes from one
concrete plan element, and `framework_render` renders that element
deterministically into one construct on one line. So the mapping

    check  ->  plan element  ->  rendered construct (file, init_fn, line)

is a deterministic join of facts the machine already produced -- analogous to a
compiler emitting debug line info. Nothing is guessed:

- Each check gets a `provenance` object naming its `origin` and a **join key**
  taken verbatim from the plan: the RCC clock **macro** (`__HAL_RCC_USART1_CLK_ENABLE`),
  the **IRQ name** (`USART1_IRQn`), or the **port-pin** (`PA9`).
- `build_source_map` scans the rendered init text into an index of init functions
  (line spans) and tagged constructs `{tag, key, line, text, init_fn}` -- a pure
  text scan of exactly what was emitted.
- `annotate_spec_sources` joins each check's provenance key to the source-map
  construct. A hit yields `provenance.source = {file, init_fn, line, text}`.

**Honest misses are surfaced, never faked:**

- If the construct was **not emitted** (the NVIC/GPIO/clock was `unresolved`, so
  the renderer wrote a `TODO` instead of a `HAL_..._EnableIRQ` line), the join
  finds nothing -> `provenance.located = false` with a reason that tells the agent
  *"make the code emit it"*, not *"the value is wrong"*.
- `no_fault` is a whole-init invariant with no single line -> it points at the
  `BSP_Init` span with a note, not a fabricated line.
- If the plan changed since the spec was synthesized, unmatched keys are reported
  as `located = false` -- never mapped to the wrong line.

## What the machine does / 机器职责

- `acceptance_synth.derive_acceptance_spec` -- every derived check now carries a
  `provenance` dict (`origin` + join key + human context). Pure; no render
  dependency, so a direct caller gets provenance-without-source and the server
  layer resolves the source.
- `provenance.build_source_map(content, path)` -- scan a rendered init file into
  `{path, functions[], constructs[]}` with 1-based line numbers.
- `provenance.annotate_spec_sources(spec, source_maps)` -- fill each check's
  `provenance.source` (or `located=false` + `reason`).
- `framework_render.render_framework` -- additionally returns `source_map`
  (one per file) so the map is inspectable and reused by the synth handler.
- `acceptance_eval.evaluate_acceptance` -- copies a check's `provenance` into its
  result for every non-`pass` status. Because the Pillar C loop reuses the same
  evaluator, the located source flows into the loop verdict for free.

## Tier 2 / 工具集成

`synthesize_acceptance` renders the plan, builds the per-file source maps, and
annotates the derived spec's checks with their resolved source **before storing**
-- so the stored spec is self-contained. No new tool: `run_acceptance` and every
`run_acceptance_iteration` verdict now carry `result.provenance.source` on each
failing/errored check, pointing straight at the init function + line to fix.
Tool count stays 77.

## Boundary / 边界

- **Agent (creative):** consumes the source pointer to fix the right line; still
  writes the actual fix.
- **Machine (deterministic):** check -> source construct join, honest
  `located=false` when a construct was not emitted or the plan drifted. Never a
  fabricated file/line, never a wrong mapping.

## Status / 状态
- [x] `acceptance_synth` -- provenance on every check kind.
- [x] `provenance.py` -- `build_source_map` + `annotate_spec_sources`.
- [x] `render_framework` -- `source_map` in the response.
- [x] `acceptance_eval` -- provenance passthrough on non-pass results.
- [x] `synthesize_acceptance` -- annotate source before store.
- [x] Tests + docs + suite green / ruff clean.
