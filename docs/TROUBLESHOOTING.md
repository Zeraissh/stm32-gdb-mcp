# Troubleshooting / 故障排查

Recovery and fallback modes for the stm32-gdb-mcp server.

## `no_session` — operations fail with "GDB is not running"

The session ended (or never started). To recover:

1. **`start_debug_session`** again — it now **auto-retries a busy probe** (a previous
   `stop_debug_session` may not have released the ST-Link yet) and cleans up any leftover
   server first. So `stop → start` and CI loops work without restarting the MCP.
2. **`recover_session`** — tears down and restarts from the last `start_debug_session`
   arguments (with retry/backoff). Use it after `probe_unavailable` / `connection_lost`.
3. If `start_debug_session` **isn't in your tool list** (see compact mode below), invoke it
   via the escape hatch: `call(tool="start_debug_session", args={...})`.

You should never need to restart the whole MCP server to start a new session.

## A tool you need isn't listed (`Unknown tool`, or it's missing)

Some MCP clients cap how many tools they expose (VS Code Copilot ≈ 128 across all servers),
so a tool may be hidden when many servers are active. Two answers:

- **`call(tool="<name>", args={...})`** invokes ANY tool by name, even one not in the list —
  a guaranteed escape hatch. `batch` does the same for several calls at once.
- **Compact mode** (below) keeps the surface small so nothing gets truncated.

If you call an **old (pre-consolidation) tool name**, the error tells you the new form,
e.g. `start_uart_logging` → `start_logging(channel="uart")`. See the rename map:

| old | new |
|---|---|
| `start/stop/get/clear_{rtt,swo,uart}_log*` | `start_logging`/`stop_logging`/`get_logs`/`clear_logs` (channel=…) |
| `step_over` / `step_into` / `step_out` / `step_instruction` | `step(kind=…)` |
| `read_freertos_*` / `read_current_task` | `read_freertos(what=…)` |
| `start/stop_variable_tracking`, `get_tracked_data` | `track_variable(action=…)` |
| `get_session_{journal,timeline,metrics}` | `get_session(view=…)` |

## Compact mode — fit under tight tool-count caps

Set the env var **`STM32_GDB_MCP_COMPACT=1`** on the MCP server. It then exposes only a
~30-tool core (the workflow essentials + `call`/`batch`); every other tool stays reachable
via `call(tool, args)`. Configure it in the client, e.g. VS Code `mcp.json`:

```json
"stm32-gdb-mcp": {
  "type": "stdio",
  "command": "…/stm32-gdb-mcp.exe",
  "args": [],
  "env": { "STM32_GDB_MCP_COMPACT": "1" }
}
```

Remove the `env` line to expose all tools (better discoverability when this is your only
server). Restart the MCP after changing it.

## `self_check` reports `device="unknown"`

The DBGMCU dev_id wasn't in the lookup table. The table now covers the full STM32 range
(F0/F1/F2/F3/F4/F7/L0/L1/L4/L5/G0/G4/H7/WB/WL/U5). If you still see `unknown`, **update the
MCP** and restart it. Flashing/debugging is unaffected either way — it's only the friendly
device-name check.

For STM32U5 targets, a DBGMCU IDCODE read of `0x00000000` is treated as advisory when
CPUID still reports Cortex-M33 and `expected_family` starts with `STM32U5`; this avoids
failing an otherwise usable session solely because the U5 debug ID register was unavailable.

## A tool returns `missing_argument`

You omitted a required argument; the message names it (e.g. "Missing required argument:
'name'" from `read_variable`). Provide it and retry.

## The ST-Link is wedged (`open failed`, `probe_unavailable`)

- Call **`recover_session`** (retries the probe).
- Don't hard-kill OpenOCD — it can wedge the ST-Link USB until a physical replug.
- A serial verify script's `--reset` spawns its own OpenOCD and conflicts with the active
  session; reset via `reset_target` instead. The ST-Link virtual COM port is separate and
  coexists with debugging (`start_logging(channel="uart")`).

## Self-reporting

Hit something not covered here? Call **`report_issue(title, description)`** — it files a
GitHub issue with this session's journal so the MCP can be fixed.
