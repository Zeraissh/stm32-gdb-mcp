---
name: stm32-debug
description: Debug STM32 firmware on real hardware via the stm32-gdb-mcp server (GDB + OpenOCD/ST-Link/J-Link). Use when the user wants to flash, run, breakpoint, inspect memory/registers/RTOS, diagnose a HardFault, find a hang, or reproduce a hardware bug on an STM32 target.
---

# STM32 hardware debugging

This skill teaches how to drive the `stm32-gdb-mcp` tools to debug an STM32 target
the way a senior embedded engineer would. The model is a loop:
**observe → orient (symbolize) → hypothesize → act safely → verify.**

## Golden rules

- **Always `self_check` right after `start_debug_session`.** It validates byte order,
  the Cortex-M core, and the device family — catching link/config faults before you
  waste steps on garbage data. (This caught a real byte-order bug on hardware.)
- **The core must be HALTED to read** registers, memory, or frames. If a read returns
  `target_unresponsive`, the core is running — call `halt_execution` first.
- **`run_and_wait` leaves the core running on timeout.** Halt before reading state.
- **Prefer composites over manual sequences** (fewest round-trips): `flash_and_run`,
  `debug_until`, `capture_state`.
- **Writes are guarded.** Option bytes, IWDG, WWDG are blocked by default. Use
  `set_write_policy` to allow specific regions, or `dry_run` to simulate. All writes
  are audited (`get_write_audit_log`).
- **On a wedged/dropped probe** (`probe_unavailable`, `connection_lost`) call
  `recover_session`. Never hard-kill the GDB server — it can wedge the probe's USB.
- **Follow `suggested_next_actions`** on every result — they encode the next loop step.

## Bring-up (get from ELF to a known state)

```
start_debug_session(server_type="openocd", server_args=[...])
self_check(expected_family="STM32L4...")
set_debug_profile(mcu=..., elf_path=..., svd_path=...)   # so symbols/peripherals resolve
flash_and_run(file_path="fw.elf", run_to="main")          # flash + reset + break at main, one call
```

See `scenarios/bringup.json` for a replayable template.

## Diagnose a HardFault / crash

1. Get to the fault handler: set a breakpoint on `HardFault_Handler` (or the relevant
   handler), then `run_and_wait`.
2. `reconstruct_fault_context` — it decodes the fault registers, unwinds the auto-stacked
   exception frame from MSP/PSP via EXC_RETURN, recovers the **true faulting PC**, and
   resolves it to `file:line`.
3. `list_source` and `read_frame_variables` around that PC to see the offending code/state.

See `scenarios/hardfault.json`.

## Find a hang / livelock

1. If the core is running, `halt_execution`, then `capture_state` to see where it stopped.
2. `read_call_stack` to see who is spinning; for RTOS, `capture_rtos_snapshot` /
   `read_freertos_tasks` to find the blocked/looping task and check queues/mutexes/heap.
3. For timing/hot-spots, `read_cycle_counter` and `sample_pc`.

## Diagnose a stack overflow (e.g. crash during flash read/write)

Stack overflow is a *technique-specific* hunt — don't single-step blindly. Signature:
a large local buffer or deep recursion drives SP below the stack limit, corrupting
whatever is beneath it, which later HardFaults (often a stacking fault).

1. **Orient:** `inspect_project` (get `.map`/`.elf`), `set_debug_profile(elf_path, mcu)`,
   `list_functions("Flash|Write|Read")` to find the exact function.
2. **Set the trap** (pick one):
   - Catch the fault: `set_breakpoint("HardFault_Handler")`, then `run_and_wait`.
   - Catch the overflow moment: `set_watchpoint(<stack_limit_addr>, "w")` — it triggers
     the instant the stack grows past the guard, so you catch the deepest call.
3. **Reproduce:** trigger the flash op (`debug_until(location="Flash_Write")`, then continue
   / drive it over UART).
4. **Diagnose:**
   - `analyze_stack(stack_size=<from .map>)` → `overflow: true` and how far past the limit.
     Note: a breakpoint at a function's *entry* lands before its `sub sp` (the large
     local isn't allocated yet, so SP looks fine). Break a line *inside* the function,
     or `step_into` once past the prologue, then `analyze_stack`.
   - `reconstruct_fault_context` → CFSR `STKERR`/`MSTKERR` confirms a stacking fault.
   - `read_call_stack` → the deep chain / the function with the huge local that ate the stack.
   - FreeRTOS: `read_freertos_tasks` → the offending task's stack high-water mark ≈ 0.
5. **Fix & verify:** move the big buffer off the stack (e.g. `static`/`.bss`) or grow the
   stack; then `build_firmware` → `flash_and_run` → re-run `scenarios/stack_overflow.json`
   → `analyze_stack` shows healthy headroom.

See `scenarios/stack_overflow.json`.

## A peripheral isn't working (UART/SPI/I2C/timer/GPIO)

Most "dead peripheral" bugs are one of: clock not enabled, wrong GPIO alternate
function/mode, or a misconfigured control register. Check those before anything else.

1. `load_svd` (from the profile's `svd_path`) so registers decode by name.
2. **Clock first:** read the relevant `RCC` enable register and confirm the peripheral's
   clock bit is set (`read_peripheral_register("RCC", ...)` / `decode_peripheral_register`).
   A clock-gated peripheral reads back as all-zero and never responds.
3. **Pins:** check the GPIO `MODER` (alternate-function mode) and `AFRL/AFRH` (the AF
   number) for the peripheral's pins.
4. **Config:** decode the peripheral's own control/status registers and compare against the
   expected setup (baud/prescaler, enable bit, etc.).

See `scenarios/peripheral_check.json`.

## Heap exhaustion / memory leak

1. **FreeRTOS:** `read_freertos_heap` → free bytes and minimum-ever-free. If min-ever-free
   is near zero, the heap is (or was) exhausted.
2. **Trend it:** `start_variable_tracking` on the free-heap metric (e.g. `xFreeBytesRemaining`),
   run the workload, `get_tracked_data` → a monotonic decline is a leak.
3. **Pin the leak:** breakpoint the allocator (`pvPortMalloc`/`malloc`) and the matching free,
   or `set_watchpoint` on the free-heap counter, to find allocations that are never released.

See `scenarios/heap_check.json`.

## An assert/configASSERT fired

Asserts usually park in an infinite loop in the handler. Catch it and read the context.

1. `set_breakpoint` on the assert handler (`assert_failed`, `__aeabi_assert`,
   `vAssertCalled`, or the project's `configASSERT` target), then `run_and_wait`.
2. `read_frame_variables` → the failing file/line/expression arguments.
3. `read_call_stack` → who triggered it. Fix the offending condition.

See `scenarios/assert_check.json`.

## Reproduce a complex logic bug with minimal steps

- Set a hypothesis trap and run hands-off in one call:
  `debug_until(location="fn", condition="state == BAD", timeout_sec=...)` — returns the
  stop event + decoded backtrace + locals.
- Save the whole repro as a scenario and replay it deterministically with `run_scenario`
  (inline steps or a JSON file). Bundle a shareable artifact with `export_debug_report`.

## Verify a fix

- Use `compare_expressions_after_action` / `assert_expressions` to prove the fix changed
  behavior, rather than eyeballing. Re-run the saved scenario to confirm green.

## Determinism & observability

Every tool call is journaled. Use `get_session_timeline` / `get_session_metrics` to review
what happened, and `export_debug_report` to hand someone a fully reproducible record.

## Reference

- `reference/tool-map.md` — tools grouped by purpose.
- `scenarios/*.json` — replayable `run_scenario` templates (edit the placeholders).
