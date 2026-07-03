# STM32 GDB MCP Server

An MCP server that lets an AI agent **debug STM32 firmware on real hardware** — drive
GDB + OpenOCD/ST-Link/J-Link to flash, breakpoint, inspect memory/registers/RTOS, triage
HardFaults, and profile — and get back **decoded, structured** evidence instead of raw GDB text.

让 AI 智能体在**真实硬件上调试 STM32**:通过 GDB + OpenOCD/ST-Link/J-Link 烧录、打断点、查
内存/寄存器/RTOS、定位 HardFault、做性能采样,返回**已解码的结构化**证据而非原始 GDB 文本。

## What you get / 能力一览

| | |
|---|---|
| **Bring-up & flash** | `suggest_server_args`, `build_firmware`, `flash_and_run`, `self_check`, `reset_target` |
| **Execution** | `run_and_wait` (structured stop events), `run_for_duration` (soak then halt/capture), `breakpoint`, `step`, `halt`/`continue`, `debug_until` |
| **Inspect** (halted) | `capture_state`, `read_memory`/`read_variable`, `read_registers`, `frame`, `read_peripheral_register` |
| **Fault triage** | `reconstruct_fault_context` (faulting PC → source), `diagnose_fault`, `analyze_stack` |
| **RTOS** | `detect_rtos`, `read_freertos`, `snapshot(scope=rtos)` |
| **Observe** | `logging` (RTT/SWO/UART), `setup_swo` (printf, no decoder), `sample_pc` (symbolized profiler) |
| **Determinism** | `run_scenario` (replay), `batch`, journal/metrics via `get_session`, `export_debug_report` |
| **Multi-board** | pass `session="name"` to any tool; `list_sessions`/`close_session` |

Two cross-cutting goals: **low comprehension cost** (decoded outputs, `suggested_next_actions`)
and **minimal repro steps** (composites like `flash_and_run` / `debug_until` collapse 5–15 calls
into one). Lean surface: ~31 tools in compact mode (full ~60); reach any tool via `call(tool, args)`.

## Install / 安装

| Client | One command |
|---|---|
| **Claude Code** (best — tools + skills + always-on rules) | `/plugin marketplace add Zeraissh/stm32-gdb-mcp` then `/plugin install stm32-debug-kit@zeraissh-stm32` |
| **Cursor / VSCode / Codex / Windsurf / Trae** | `python scripts/deploy.py --project "<firmware dir>" --ide vscode,cursor` |
| **Manual** (any MCP client) | `pip install -e .` then point your client at `stm32-gdb-mcp` |

`deploy.py` installs the server, writes the IDE's MCP config, and drops a project-aware rules
file into your firmware project. Per-client config snippets + the rules template are in
[`docs/install-ides.md`](docs/install-ides.md). Compact mode is on by default.

**Requirements** — on `PATH`: `arm-none-eabi-gdb`, plus one server (`openocd` / `JLinkGDBServerCL`
/ `st-util`). Check with `python setup_env.py`.

## 30-second quickstart / 快速上手

```text
suggest_server_args(mcu="STM32L431", probe="stlink")   # → the -f interface/target cfgs
start_debug_session(server_type="openocd", server_args=[...])
self_check()                                           # ALWAYS first: byte order, core, family
debug_profile(action=set, mcu="STM32L431", elf_path="build/app.elf", svd_path="STM32L4.svd")
flash_and_run(file_path="build/app.elf", run_to="main")
breakpoint(action=set, location="my_func", condition="state == BAD")
run_and_wait()                                         # structured stop event + next actions
run_for_duration(duration_sec=30, capture={"expressions": ["rx_count"]})
reconstruct_fault_context()                            # on a crash: faulting PC → file:line
```

The full tool reference (lean families with `action=`/`what=`) is
[`skills/stm32-debug/reference/tool-map.md`](skills/stm32-debug/reference/tool-map.md). The server
also ships always-on `instructions`, so any MCP client gets the debug loop without setup.

## Key rules (the target must cooperate) / 关键规则

- **Reads need a HALTED core.** If a read says `target_unresponsive`, `halt_execution` first.
- **A breakpoint TIMEOUT means the path was NOT reached** — don't just retry. Halt, `capture_state`,
  `breakpoint(action=list)` (hit_count=0 confirms), read the gating flag, set an earlier breakpoint
  or drive the precondition.
- **Writes are guarded** (option bytes/IWDG/WWDG blocked) — `write_guard(action=policy)` to allow.
- **Never hard-kill OpenOCD** (wedges the ST-Link USB) — use `recover_session`. SWD is exclusive.

## Response shape / 响应结构

Every tool returns a stable JSON envelope inside the MCP `TextContent` transport:

```json
{ "ok": true, "data": {}, "error": null, "raw_response": null, "suggested_next_actions": [] }
```

Human-readable text lives in `data.message` / `error.message`; raw GDB output stays in
`raw_response` when it aids diagnosis. Errors carry a `code` (e.g. `target_unresponsive`).

## Agent guidance — three layers / 三层引导

1. **Inline** — most results carry `suggested_next_actions` (the next loop step).
2. **Always-on** — the server's `instructions` (debug loop + key rules) inject automatically.
3. **On-demand** — skills: [`stm32-debug`](skills/stm32-debug/SKILL.md) (bring-up, HardFault, hang,
   minimal repro, replayable scenarios) and [`stm32-instrument`](skills/stm32-instrument/SKILL.md)
   (write-time SWO/ITM trace). In other IDEs the same guidance travels as a rules file (AGENTS.md).

## Repeatable config / 可复现配置

Load a YAML debug profile so sessions reproduce across clients:
`debug_config(action=load, path="mcp/board.yaml")`.

```yaml
mcu: STM32L431CCUx
probe: stlink
server_type: openocd
server_args: ["-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg"]
elf_path: build/app.elf
svd_path: STM32L4.svd
```

See `examples/configs/` for J-Link and OpenOCD samples.

## Develop / 开发

```bash
pip install -e ".[dev]"
python -m ruff check . && python -m pytest && python -m compileall src tests
```

- [`docs/install-ides.md`](docs/install-ides.md) — per-IDE install + rules template
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common failures + recovery
- [`docs/hil-validation.md`](docs/hil-validation.md) — hardware-in-the-loop validation (`STM32_GDB_MCP_HIL=1`)
- `CONTRIBUTING.md`, `SECURITY.md`, `docs/release.md`

Hardware validation runs on a self-hosted runner labeled `stm32`; normal CI is hardware-free
(lint, tests, compile, packaging).
