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
   - `reconstruct_fault_context` → CFSR `STKERR`/`MSTKERR` confirms a stacking fault.
   - `read_call_stack` → the deep chain / the function with the huge local that ate the stack.
   - FreeRTOS: `read_freertos_tasks` → the offending task's stack high-water mark ≈ 0.
5. **Fix & verify:** move the big buffer off the stack (e.g. `static`/`.bss`) or grow the
   stack; then `build_firmware` → `flash_and_run` → re-run `scenarios/stack_overflow.json`
   → `analyze_stack` shows healthy headroom.

See `scenarios/stack_overflow.json`.

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
