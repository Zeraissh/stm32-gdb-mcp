from __future__ import annotations

import os
import subprocess
from importlib import metadata

SERVER_INSTRUCTIONS = """\
STM32 on-chip debugging over GDB + OpenOCD/ST-Link/J-Link. Drive it as a loop:
observe -> orient (symbolize) -> hypothesize -> act safely -> verify.

Tool not in your list? Some clients cap how many tools they expose, so a tool you need
(e.g. start_debug_session) may be hidden. Reach ANY tool via call(tool="<name>", args={...}),
or run several with batch - these always work even when the tool isn't directly listed.

The surface is lean: related ops are action-dispatched families - pass the discriminator.
breakpoint(action=set|delete|list|watch), logging(action=start|stop|get|clear, channel=...),
expressions(action=assert|capture|compare), debug_profile(action=get|set),
debug_config(action=load|save|validate), read_registers(what=core|fault|cycle),
inspect_symbol(what=size|type|address|resolve|functions|variables), frame(action=select|source|
variables), write_guard(action=policy|audit), coredump, timeouts, typed_memory, snapshot,
session_diagnostics. (Old standalone names still work if you call them.)

Core workflow:
0. Need OpenOCD server_args? Call suggest_server_args(mcu, probe) - it returns the
   right -f interface/target cfgs (validated against OpenOCD's bundled scripts).
   NEVER search the disk for .cfg files; OpenOCD resolves them from its scripts dir.
1. start_debug_session, then ALWAYS run self_check first - it validates byte order,
   the Cortex-M core, and the device family, catching link/config faults early.
2. Set debug_profile(action=set, mcu, elf_path, svd_path) - symbols then auto-load on every
   connect/recover_session (symbols are per-session). To load symbols mid-session without
   flashing, call load_symbols. Without symbols, breakpoints on function names won't resolve.
3. Reproduce with the fewest calls: prefer the composites over manual sequences -
   flash_and_run (ELF -> halted at entry), debug_until (conditional breakpoint + run +
   decoded backtrace/locals in one call), capture_state ("where am I" in one call).
4. Diagnose a crash with reconstruct_fault_context: it unwinds the stacked exception
   frame and resolves the true faulting PC to source file:line.
5. Verify a fix with expressions(action=compare) / expressions(action=assert).

Key rules (the target must cooperate):
- Reads (registers/memory/frames) require a HALTED core. If a read fails with
  target_unresponsive, the core is running - call halt_execution first.
- run_and_wait returns a structured stop event; on timeout it leaves the core RUNNING.
- A breakpoint TIMEOUT means the code path was NOT reached - do NOT just retry run_and_wait.
  The location is usually gated by a flag/state/stimulus. Instead: halt_execution, then
  capture_state + breakpoint(action=list) (hit_count=0 confirms it was never reached); read the
  gating flag; then either set a breakpoint EARLIER on the path (or where the flag is set),
  drive the precondition (write the flag/variable, send the UART/input stimulus), or use a
  conditional breakpoint. Forcing a flag via write_memory changes behavior - note it.
- Memory writes are guarded: option bytes, IWDG, and WWDG are blocked by default;
  use write_guard(action=policy) to allow, or dry_run to simulate. Every write is audited.
- If halting causes mysterious resets, configure_debug_freeze (freeze IWDG/WWDG/timers).
- On probe_unavailable / connection_lost, call recover_session; tune flaky probes with
  timeouts(action=set).
- The ST-Link SWD/debug interface is EXCLUSIVE: while this MCP session is active, never
  start a second OpenOCD/GDB on the same probe (e.g. a verify script's own --reset will
  fail with "ST-Link in use"). Reset via reset_target instead. The ST-Link virtual COM
  port (e.g. COM3) is a SEPARATE USB endpoint and DOES coexist with debugging - read it
  with logging(action=start, channel="uart"), or let an external serial script use it without resetting.

Observability: to find where firmware spends time or what loop it is stuck in, use
sample_pc - a non-intrusive statistical profiler that runs over SWD (no SWO pin / firmware
change) and returns a symbolized hot-spot histogram. For printf-over-SWO, call
setup_swo(hclk_hz, swo_hz) once (it configures TPIU+ITM from the debugger), then capture with
logging(action=start, channel="swo", file=<output>) - no external decoder needed.

Every result is one compact JSON envelope: {ok, data?, error?, raw_response?,
suggested_next_actions?} - empty fields are omitted. Successful results omit the raw
GDB/MI records unless the server runs with STM32_GDB_MCP_VERBOSE=1; failures always
include them as evidence.

Determinism & sharing: every call is journaled - review it with get_session(view=journal|
timeline|metrics). Replay a repro with run_scenario; bundle a full, shareable report
with export_debug_report. Most results carry suggested_next_actions - follow them.

Self-reporting: if a tool behaves wrongly, gives a confusing result, or you get stuck on
what looks like an MCP bug (not a target bug), call report_issue(title, description) - it
files a structured GitHub issue auto-bundling this session's journal so the MCP can be fixed.

Multiple boards: pass session="<name>" to ANY tool to target an isolated debug session
(its own connection, profile, breakpoints, logs). Omit it for the single 'default' session.
list_sessions / close_session manage them. For truly concurrent OpenOCD instances, give each
session distinct server_args (gdb_port and adapter serial).
"""


def mcp_version() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass

    try:
        return metadata.version("stm32-gdb-mcp")
    except metadata.PackageNotFoundError:
        return "unknown"
