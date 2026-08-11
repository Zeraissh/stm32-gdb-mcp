# STM32 GDB MCP Server / STM32 GDB MCP 服务器

An MCP server that lets an AI agent **debug STM32 firmware on real hardware** — drive
GDB + OpenOCD/ST-Link/J-Link to flash, breakpoint, inspect memory/registers/RTOS, triage
HardFaults, and profile — and get back **decoded, structured** evidence instead of raw GDB text.

让 AI 智能体在**真实硬件上调试 STM32**:通过 GDB + OpenOCD/ST-Link/J-Link 烧录、打断点、查
内存/寄存器/RTOS、定位 HardFault、做性能采样,返回**已解码的结构化**证据而非原始 GDB 文本。

## What you get / 能力一览

| | |
|---|---|
| **Bring-up & flash / 启动与烧录** | `detect_probe`, `suggest_server_args`, `build_firmware`, `flash_and_run`, `self_check`, `reset_target` |
| **Execution / 执行控制** | `run_and_wait`（结构化停止事件）, `run_for_duration`（运行/采样后暂停并采集）, `breakpoint`, `step`, `halt`/`continue`, `debug_until` |
| **Inspect / 状态检查**（需暂停） | `capture_state`, `read_memory`/`read_variable`, `read_registers`, `frame`, `read_peripheral_register` |
| **Fault triage / 故障诊断** | `reconstruct_fault_context`（故障 PC → 源码）, `diagnose_fault`, `analyze_stack` |
| **RTOS / 实时系统** | `detect_rtos`, `read_freertos`, `snapshot(scope=rtos)` |
| **Observe / 可观测性** | `logging`（RTT/SWO/UART）, `setup_swo`（无需额外解码器的 printf）, `sample_pc`（符号化采样器） |
| **Determinism / 可复现性** | `run_scenario`（回放）, `batch`, `get_session` 日志/指标, `export_debug_report` |
| **Multi-board / 多板卡** | 任意工具传 `session="name"`；使用 `list_sessions`/`close_session` 管理 |

Two cross-cutting goals: **low comprehension cost** (decoded outputs, `suggested_next_actions`)
and **minimal repro steps** (composites like `flash_and_run` / `debug_until` collapse 5–15 calls
into one). Compact mode keeps the visible surface small; every tool remains reachable through
`call(tool, args)`, and `tool_help` exposes hidden schemas.

两个贯穿目标是：通过解码结果和 `suggested_next_actions` **降低理解成本**，并通过
`flash_and_run`、`debug_until` 等组合工具把多次调用压缩为一次，**缩短复现路径**。
compact 模式只展示核心工具；所有隐藏工具仍可由 `call(tool, args)` 调用，并可用
`tool_help` 查询完整说明与 schema。

## Install / 安装

| Client | One command |
|---|---|
| **Claude Code**（工具 + skills + 常驻规则） | `/plugin marketplace add Zeraissh/stm32-gdb-mcp`，然后 `/plugin install stm32-debug-kit@zeraissh-stm32` |
| **Cursor / VSCode / Codex / Windsurf / Trae** | `python scripts/deploy.py --project "<firmware dir>" --ide vscode,cursor` |
| **Manual / 手动配置**（任意 MCP 客户端） | `pip install -e .`，再将客户端指向 `stm32-gdb-mcp` |

`deploy.py` installs the server, writes the IDE's MCP config, and drops a project-aware rules
file into your firmware project. Per-client config snippets + the rules template are in
[`docs/install-ides.md`](docs/install-ides.md). Compact mode is on by default.
PyPI/console installs also expose `stm32-gdb-mcp-check-env`, `stm32-gdb-mcp-install`,
and `stm32-gdb-mcp-deploy`.

`deploy.py` 会安装服务器、写入 IDE 的 MCP 配置，并在固件项目中生成带项目上下文的规则文件。
各客户端配置片段和规则模板见 [`docs/install-ides.md`](docs/install-ides.md)。默认启用 compact
模式；PyPI/console 安装还会提供上述三个辅助命令。

`stm32-gdb-mcp-check-env --json` also reports the imported module version/path, installed
distribution version, and all four console scripts. Use `stm32-gdb-mcp-deploy --upgrade`
when those installation fields drift. / `stm32-gdb-mcp-check-env --json` 还会报告当前导入
模块的版本/路径、已安装发行版本和四个 console script；这些安装字段发生漂移时，使用
`stm32-gdb-mcp-deploy --upgrade` 修复。

**Requirements / 环境要求**：`PATH` 中需要 `arm-none-eabi-gdb`，以及至少一种 GDB Server
（`openocd` / `JLinkGDBServerCL` / `st-util`）。运行 `python setup_env.py` 检查。

## 30-second quickstart / 快速上手

```text
detect_probe()                                           # USB evidence; auto-select only one probe
debug_profile(action=set, mcu="STM32L431", probe="stlink", elf_path="build/app.elf", svd_path="STM32L4.svd")
suggest_server_args(mcu="STM32L431")                  # probe omitted -> use profile probe
start_debug_session(server_type="openocd")            # server_args omitted -> infer from profile mcu/probe
self_check()                                           # ALWAYS first: byte order, core, family
flash_and_run(file_path="build/app.elf", run_to="main")
breakpoint(action=set, location="my_func", condition="state == BAD")
run_and_wait()                                         # structured stop event + next actions
run_for_duration(duration_sec=30, capture={"expressions": ["rx_count"]})
run_for_duration(duration_sec=60, sample={"interval_ms": 500, "expressions": ["rx_count", "state"]})
reconstruct_fault_context()                            # on a crash: faulting PC → file:line
```

`detect_probe` reads physical USB devices, keeps serials for identical probes, and never
chooses between multiple connected probes. / `detect_probe` 读取真实 USB 设备，同型号探针按
序列号分别保留；连接多个探针时绝不会擅自选择。

The full tool reference (lean families with `action=`/`what=`) is
[`skills/stm32-debug/reference/tool-map.md`](skills/stm32-debug/reference/tool-map.md). The server
also ships always-on `instructions`, so any MCP client gets the debug loop without setup.

完整工具索引见 [`skills/stm32-debug/reference/tool-map.md`](skills/stm32-debug/reference/tool-map.md)，
其中按 `action=` / `what=` 归并工具族。服务器还会发送常驻 `instructions`，因此任意 MCP
客户端连接后都能获得同一套调试闭环。

## Key rules (the target must cooperate) / 关键规则

- **Reads need a HALTED core.** If a read says `target_unresponsive`, `halt_execution` first. /
  **读取要求内核已暂停。** 若返回 `target_unresponsive`，先执行 `halt_execution`。
- `run_for_duration(sample=...)` is best-effort low-rate debugger polling. It does not halt
  the target itself, but running-target expression reads may fail on some probes/MCUs; use
  SWO/ring-buffer firmware telemetry for higher-rate or guaranteed capture. /
  `run_for_duration(sample=...)` 是尽力而为的低速调试器轮询；某些探针或 MCU 无法在运行中读取
  表达式。需要更高采样率或可靠采集时，请使用 SWO 或固件环形缓冲。
- **A breakpoint TIMEOUT means the path was NOT reached** — don't just retry. Halt, `capture_state`,
  `breakpoint(action=list)` (hit_count=0 confirms), read the gating flag, set an earlier breakpoint
  or drive the precondition. / **断点超时表示路径没有到达。** 不要原样重试；暂停后采集状态、确认
  命中次数、读取门控条件，再前移断点或驱动前置条件。
- **Writes are guarded** (option bytes/IWDG/WWDG blocked) — `write_guard(action=policy)` to allow. /
  **写操作受保护。** option bytes、IWDG、WWDG 默认禁止，需用 `write_guard(action=policy)` 显式放行。
- **Never hard-kill OpenOCD** (wedges the ST-Link USB) — use `recover_session`. SWD is exclusive. /
  **不要强杀 OpenOCD。** 使用 `recover_session`；同一探针的 SWD 调试连接是独占的。
- **A wedged probe needs a physical re-enumeration.** With the optional `[hub]` extra and a
  SmartUSBHub, `hub(action=power)` cuts VBUS and `hub(action=data)` drops the USB data lines,
  so that re-plug becomes a tool call. Without a hub it is still a human with a cable. /
  **卡死的探针必须物理重新枚举。** 装上可选的 `[hub]` extra 并接可编程 USB Hub 后，
  `hub(action=power)` 可断 VBUS、`hub(action=data)` 可断数据线，那次插拔就变成一次工具调用；
  没有 Hub 时仍然只能靠人手拔插。

### Programmable USB hub (optional) / 可编程 USB Hub（可选）

```bash
pip install 'stm32-gdb-mcp[hub]'
```

Then point a session at its port (1-based, matching the hub's silkscreen):

```json
{"action": "set", "hub": {"channel": 2, "guard": "confirm"}}
```

`hub(action=describe)` is read-only and also reports per-port voltage/current on models
with an ADC. Power and data actions require `confirm=true` while the guard is in its
default `confirm` mode, or whenever a GDB server is live on that port — cutting power
mid-flash is how a board becomes a brick. Set `guard: allow` for scripted CI. /
`hub(action=describe)` 是只读的，带 ADC 的型号还会返回每端口电压/电流。默认 `confirm`
模式下，或该端口上有活跃 GDB 服务时，电源与数据线操作都必须显式传 `confirm=true`
——烧录中途断电正是把板子变砖的方式。CI 脚本可设 `guard: allow`。

## Response shape / 响应结构

Every tool returns a stable JSON envelope inside the MCP `TextContent` transport:

```json
{ "ok": true, "data": {}, "error": null, "raw_response": null, "suggested_next_actions": [] }
```

Human-readable text lives in `data.message` / `error.message`; raw GDB output stays in
`raw_response` when it aids diagnosis. Errors carry a `code` (e.g. `target_unresponsive`).

可读消息位于 `data.message` / `error.message`；仅在有助于诊断时保留原始 GDB 输出到
`raw_response`。错误包含稳定的 `code`，例如 `target_unresponsive`。现代 MCP 客户端还会收到
内容相同的原生 `structuredContent`，错误结果设置 `isError=true`。

## Agent guidance — three layers / 三层引导

1. **Inline / 内联** — most results carry `suggested_next_actions` (the next loop step) /
   大多数结果直接给出下一步动作。
2. **Always-on / 常驻** — the server's `instructions` inject automatically /
   服务器自动注入调试闭环与关键规则。
3. **On-demand** — skills: [`stm32-debug`](skills/stm32-debug/SKILL.md) (bring-up, HardFault, hang,
   minimal repro, replayable scenarios) and [`stm32-instrument`](skills/stm32-instrument/SKILL.md)
   (write-time SWO/ITM trace). In other IDEs the same guidance travels as a rules file (AGENTS.md). /
   **按需**加载两项 skill；其他 IDE 通过 `AGENTS.md` 等规则文件获得同样指导。

## Repeatable config / 可复现配置

Load a YAML debug profile so sessions reproduce across clients:
`debug_config(action=load, path="mcp/board.yaml")`.

加载 YAML 调试 profile 可让不同客户端复现同一会话：
`debug_config(action=load, path="mcp/board.yaml")`。

```yaml
mcu: STM32L431CCUx
probe: stlink
server_type: openocd
server_args: ["-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg"]
elf_path: build/app.elf
svd_path: STM32L4.svd
```

Relative `elf_path`, `svd_path`, `project_root`, and `swo.file` values are resolved from the
YAML file's directory. Session start, logging, reset, and flash tools then use the loaded
profile when their corresponding arguments are omitted.

相对的 `elf_path`、`svd_path`、`project_root` 和 `swo.file` 均以 YAML 所在目录为基准解析。
随后启动会话、日志、复位和烧录工具会在省略对应参数时自动使用该 profile。

Load the profile, connect, and run the mandatory identity check in one MCP round trip:

```text
batch(steps=[
  {"tool": "debug_config", "args": {"action": "load", "path": "mcp/board.yaml"}},
  {"tool": "start_debug_session", "args": {}},
  {"tool": "self_check", "args": {}}
], stop_on_error=true)
```

以上配方在一次 MCP 往返中完成“加载配置 -> 连接 -> 自检”，任一步失败即停止。

See `examples/configs/` for J-Link, OpenOCD, and non-flashing L151/L431/U535 HIL profiles. /
J-Link、OpenOCD 及不烧录的 L151/L431/U535 HIL 配置见 `examples/configs/`。

## Develop / 开发

```bash
pip install -e ".[dev]"
python -m ruff check . && python -m pytest && python -m compileall src tests
python -m build && python scripts/check_dist_contents.py dist/*
```

- [`docs/install-ides.md`](docs/install-ides.md) — per-IDE install + rules template / 各 IDE 安装与规则模板
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common failures + recovery / 常见故障与恢复
- [`docs/hil-validation.md`](docs/hil-validation.md) — HIL validation / 硬件在环验证（`STM32_GDB_MCP_HIL=1`）
- `CONTRIBUTING.md`, `SECURITY.md`, `docs/release.md`

Hardware validation runs on a self-hosted runner labeled `stm32`; normal CI is hardware-free
(lint, tests, compile, packaging).

硬件验证运行在带 `stm32` 标签的自托管 runner；普通 CI 不依赖硬件，只执行 lint、测试、
编译检查和发行包验证。
