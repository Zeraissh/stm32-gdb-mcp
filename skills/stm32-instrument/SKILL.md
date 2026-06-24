---
name: stm32-instrument
description: Instrument STM32 firmware with SWO/ITM trace at write-time so it is observable when later debugged with stm32-gdb-mcp. Use when writing or editing STM32 C/C++ firmware that an AI (or engineer) will need to debug — to add gated, low-overhead trace points at decision points, error paths, ISR boundaries, and state transitions. Pairs with the stm32-debug skill (capture side).
---

# Instrumenting STM32 firmware for debuggability

Add observability **while writing the code**, so when something breaks later the trace is
already there — no re-instrument/re-flash round-trip. Read it back over the single SWO pin
with `stm32-gdb-mcp` (`setup_swo` → `logging(channel="swo")`).

Use the drop-in `swo_trace.h` / `swo_trace.c` in this skill folder. They give three macros:

| Macro | Cost | Where |
|---|---|---|
| `SWO_EVENT(code)` | 1 byte, **non-blocking (drop-on-full)** | ISRs, hot loops, anywhere timing matters |
| `SWO_MARK(port, val)` | 1 word, non-blocking | structured values (counters, IDs, states) |
| `SWO_LOG("fmt", …)` | printf, may block briefly | **non-hot paths only**: init, state changes, error/fault paths |

## The discipline (this is the whole point — do not skip)

1. **Gate everything.** All three macros vanish when built without `-DSWO_TRACE_ENABLED=1`.
   Never write bare `printf`/`ITM_SendChar`. Result: **zero cost and zero code in release.**
2. **Never `SWO_LOG` in an ISR or hot loop.** Formatted text blocks while the FIFO drains —
   that perturbs timing and *creates* Heisenbugs. Use `SWO_EVENT`/`SWO_MARK` there (they drop
   the sample instead of blocking).
3. **Instrument decisions, not lines.** Sparse and structured beats a wall of logs. Good spots:
   - **State transitions** in a state machine (`SWO_LOG("ST %s->%s\n", …)`).
   - **Error / early-return-fail paths** — the `if (err) { … }` you'd otherwise never see.
   - **Assert / fault handlers** (`HardFault_Handler`, `configASSERT`, `assert_failed`).
   - **ISR entry/exit** with `SWO_EVENT` (e.g. `EVT_UART_RX`, `EVT_DMA_DONE`).
   - **Peripheral init results** (which `HAL_*_Init` returned non-OK).
   - **RTOS task switch / blocking points** if you own that code.
4. **Define a small event enum** so codes are readable on the host:
   ```c
   enum { EVT_BOOT=1, EVT_ISR_UART=2, EVT_DMA_DONE=3, EVT_STATE_ERR=0xEE };
   ```
5. **Keep messages short** (the SWO link is finite — ST-Link V2 ≈ 2 Mbit/s). Prefer codes over
   prose in anything that runs often.

## Wiring it in (once per project)

1. Copy `swo_trace.h` + `swo_trace.c` into the project; add `swo_trace.c` to the build.
2. Build the debug config with:
   `-DSWO_TRACE_ENABLED=1 -DSWO_TRACE_INCLUDE="\"stm32l4xx.h\""` (your CMSIS device header).
3. Ensure the **SWO pin** (commonly PB3 / TRACESWO) is wired to the probe's SWO. On a Nucleo/
   Discovery it already is; on a custom board it must be routed.
4. The firmware needs **no ITM init** — the debugger configures TPIU+ITM via `setup_swo`. (If you
   run without a debugger and still want SWO, call `ITM` setup yourself; usually not needed.)

## Capturing it (debug side — see the stm32-debug skill)

```
setup_swo(hclk_hz=<core clock, e.g. 80000000>)          # configures TPIU+ITM from the debugger
logging(action=start, channel="swo", file="swo_itm.log") # tail OpenOCD's ITM decode, no extra tool
# … reproduce …
get_logs(channel="swo")                                  # port-0 text shows up here
```
For "where is it spending time / stuck" you usually don't even need trace points — use
`sample_pc` (non-intrusive PC profiler over SWD, returns a symbolized hot-spot histogram).

## When NOT to instrument

- Don't add trace to prove something a breakpoint + `capture_state` answers in one call.
- Don't instrument tight DSP/control loops with `SWO_LOG` — use a single `SWO_MARK` per cycle
  at most, or sample externally.
- Don't leave un-gated debug output. If it isn't behind `SWO_TRACE_ENABLED`, it doesn't ship.

## Optional: make it a repo rule

To have an agent apply this automatically when editing this project's firmware, add to the
firmware project's `CLAUDE.md`:

> When writing or modifying firmware that will be AI-debugged, use the `swo_trace.h` macros
> (`SWO_EVENT`/`SWO_MARK`/`SWO_LOG`) at state transitions, error paths, and ISR boundaries —
> gated behind `SWO_TRACE_ENABLED`, never bare `printf`, never `SWO_LOG` in an ISR.
