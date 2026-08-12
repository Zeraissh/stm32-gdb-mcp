from __future__ import annotations

import os
import subprocess
from importlib import metadata

from . import __version__

SERVER_INSTRUCTIONS = """\
STM32 on-chip debugging over GDB + OpenOCD/ST-Link/J-Link. Drive it as a loop:
observe -> orient (symbolize) -> hypothesize -> act safely -> verify.

Tool not in your list? Some clients cap how many tools they expose, so a tool you need
(e.g. start_debug_session) may be hidden. Reach ANY tool via call(tool="<name>", args={...}),
or run several with batch - these always work even when the tool isn't directly listed.
For a hidden tool that only READS state, prefer call_read(tool=..., args=...): it is
annotated read-only, so it does not trigger the approval prompt that call can. It judges
the ACTION, not the family: call_read(tool="debug_profile", args={"action":"get"}) and
hub(action=describe|measure) go through; the writing actions do not, and a refusal names
the actions it accepts.
If a tool you EXPECT does not exist AT ALL, call mcp_info first - it reports the version,
commit, install path and compact_mode of the build actually serving you. Never read the
client's plugin manifest off disk to work that out, and never restart to find out.

The surface is lean: related ops are action-dispatched families - pass the discriminator.
breakpoint(action=set|delete|list|watch), logging(action=start|stop|get|clear, channel=...),
expressions(action=assert|capture|compare), debug_profile(action=get|set),
debug_config(action=load|save|validate), read_registers(what=core|fault|cycle),
inspect_symbol(what=size|type|address|resolve|functions|variables), frame(action=select|source|
variables), write_guard(action=policy|audit), hub(action=describe|power|data), coredump,
timeouts, typed_memory, snapshot, session_diagnostics.
(Old standalone names still work if you call them.)

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
- A wedged ST-Link USB endpoint cannot be cleared in software. If a programmable USB hub
  is configured (debug_profile hub.channel), hub(action=power, state=off/on) is the only
  in-band way to force a re-enumeration - and hub(action=data) un-enumerates a probe
  without rebooting its board. Called directly, both need confirm=true - always in the
  default confirm guard mode, and in ANY mode while THIS session's own GDB server is live
  on that port. That check sees only the calling session, so on a rack it will not notice
  another session mid-flash on the same channel; isolate first with
  hub(action=data, exclusive=true).
  reset_target(strategy="cold") and recover_session's hub ladder are the one deliberate
  exception: they cut power with no confirm argument, because naming them is the
  confirmation, and they stop the session before the cut. Every decision is audited
  either way. Without a hub, that recovery is still a physical replug.
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


def _install_root() -> str:
    """The directory the running code was imported from — repo root, or site-packages' parent."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git_commit(root: str) -> str | None:
    """This checkout's short sha, or None when the code is not running from a checkout.

    The .git probe matters: without it, a wheel installed under site-packages made
    ``git -C`` walk upward and report some UNRELATED repository's HEAD as this
    server's version. A worktree's .git is a file rather than a directory, so test
    existence, not isdir.
    """
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    return None


def mcp_version() -> str:
    commit = _git_commit(_install_root())
    if commit:
        return commit

    try:
        return metadata.version("stm32-gdb-mcp")
    except metadata.PackageNotFoundError:
        return "unknown"


def server_info() -> dict:
    """Identify the build that is actually SERVING this call.

    ``mcp_version`` answers with two different kinds of string — a git short sha in a
    source checkout, the released package version otherwise — and the caller cannot
    tell which it got. An agent asking "why is my new tool missing?" needs the facts
    separately: the release, the commit, and the directory the code was imported from
    (the usual cause is a second checkout still being served). It also needs
    ``compact_mode``, which hides most of the surface and is the other half of that
    same question. Nothing here touches the target, a probe, or a debug session, so it
    answers when everything is disconnected — which is exactly when it gets asked.
    """
    root = _install_root()
    info: dict[str, str | bool] = {
        "name": "stm32-gdb-mcp",
        "version": __version__,
        # Where the code REALLY is. install_root is three levels up from this file,
        # which is the repo root for the src-layout checkout but the parent of
        # site-packages for a plain `pip install` -- a directory that does not
        # contain mcp_server at all. It is still worth reporting because it is the
        # root the git probe uses, but package_dir is the one an agent should look
        # at, so it must not be the one that is sometimes wrong.
        "package_dir": os.path.dirname(os.path.abspath(__file__)),
        "install_root": root,
        "compact_mode": bool(os.environ.get("STM32_GDB_MCP_COMPACT")),
    }
    commit = _git_commit(root)
    if commit:
        info["commit"] = commit
    return info
