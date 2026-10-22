# STM32 GDB MCP Server / STM32 GDB MCP 服务器

<!-- mcp-name: io.github.Zeraissh/stm32-gdb-mcp -->

[![PyPI](https://img.shields.io/pypi/v/stm32-gdb-mcp)](https://pypi.org/project/stm32-gdb-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/stm32-gdb-mcp)](https://pypi.org/project/stm32-gdb-mcp/)
[![Downloads](https://static.pepy.tech/badge/stm32-gdb-mcp/month)](https://pepy.tech/projects/stm32-gdb-mcp)
[![MCP Registry](https://img.shields.io/badge/MCP_Registry-io.github.Zeraissh%2Fstm32--gdb--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.Zeraissh/stm32-gdb-mcp)
[![CI](https://github.com/Zeraissh/stm32-gdb-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Zeraissh/stm32-gdb-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/Zeraissh/stm32-gdb-mcp/blob/main/LICENSE)

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

**Requirements / 环境要求**：`PATH` 中需要 `arm-none-eabi-gdb`（Debian/Ubuntu 上的
`gdb-multiarch` 也可以，或用 `STM32_GDB_MCP_GDB` 指定任意支持 ARM 的 GDB），以及至少一种 GDB
Server（`openocd` / `JLinkGDBServerCL` / `st-util`）。运行 `stm32-gdb-mcp-check-env`（或
`python setup_env.py`）检查；缺什么，它会打印本机系统（Windows scoop / macOS brew /
Linux apt、pacman）对应的一行安装命令。
Needs `arm-none-eabi-gdb` (or `gdb-multiarch`, or any ARM-capable GDB named by
`STM32_GDB_MCP_GDB`) plus one GDB server; `stm32-gdb-mcp-check-env` prints the install
command for whatever is missing on your OS.

## What the plugin runs and connects to / 插件会运行和连接什么

The Claude Code plugin (`stm32-debug-kit`) runs everything locally on your machine:

- **MCP server**: `python scripts/run_mcp_server.py`, which runs the server from this
  repository's `src/` over stdio. The Python it runs under needs the dependencies in
  `pyproject.toml` (`pip install -e .` or `pip install stm32-gdb-mcp`).
- **SessionStart hook**: `python hooks/session_start.py` prints a short banner into the
  session context. It reads nothing and sends nothing.
- **Processes it starts**: `arm-none-eabi-gdb` (or the GDB named above), a GDB server (`openocd`, `JLinkGDBServerCL`
  or `st-util`), and, when you ask it to build, `cmake`/`make`. They talk to the debug probe
  over USB and to each other over TCP on `localhost`.
- **Local ports**: GDB server ports on `localhost`; the optional USB hub daemon and
  dashboard serve on `127.0.0.1` only (default port 8765). Serial ports are opened only
  when a tool reads a UART log.
- **Files**: it reads your firmware project (ELF, SVD, build files). It writes only where a
  tool is told to (a debug report, a saved debug config), plus small lock and state files
  under the system temp folder (`stm32-gdb-mcp/`).
- **Network**: nothing leaves your machine unless an agent calls `report_issue`, which
  files a GitHub issue on `Zeraissh/stm32-gdb-mcp` through your own `gh` CLI login. That
  issue contains the description the agent wrote, the MCP version and environment summary,
  and the session's tool journal and metrics. There is no telemetry.

Claude Code 插件（`stm32-debug-kit`）的所有内容都在本机运行：

- **MCP 服务器**：`python scripts/run_mcp_server.py`，通过 stdio 运行本仓库 `src/` 中的服务器。
  运行它的 Python 需要装好 `pyproject.toml` 中的依赖（`pip install -e .` 或
  `pip install stm32-gdb-mcp`）。
- **SessionStart hook**：`python hooks/session_start.py` 只向会话上下文输出一段简短说明，
  不读取也不发送任何数据。
- **启动的进程**：`arm-none-eabi-gdb`（或上文指定的 GDB）、GDB Server（`openocd`、`JLinkGDBServerCL` 或
  `st-util`），以及按需构建时的 `cmake`/`make`。它们通过 USB 连接调试器，彼此之间通过
  `localhost` 上的 TCP 通信。
- **本地端口**：GDB Server 端口只在 `localhost`；可选的 USB Hub 守护进程和看板只监听
  `127.0.0.1`（默认端口 8765）。只有在工具读取 UART 日志时才会打开串口。
- **文件**：读取你的固件工程（ELF、SVD、构建文件）。只在工具指定的位置写文件（调试报告、
  保存的调试配置），另外在系统临时目录的 `stm32-gdb-mcp/` 下写少量锁文件和状态文件。
- **网络**：除非 agent 调用 `report_issue`，否则不会有数据离开本机。`report_issue` 会用你
  本机 `gh` CLI 的登录身份，在 `Zeraissh/stm32-gdb-mcp` 上提交一个 GitHub issue，内容包括
  agent 写的描述、MCP 版本和环境摘要，以及本次会话的工具调用记录和指标。没有任何遥测。

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
with an ADC. `hub(action=power/data/cycle)` requires `confirm=true` while the guard is in
its default `confirm` mode, and — in every mode, `guard: allow` included — whenever **the
calling session's own** GDB server is live on that port: cutting power mid-flash is how a
board becomes a brick. Note the scope: in `local` mode the check sees only the session
making the call, so it will **not** notice that a *different* session is mid-flash on that
channel. With the shared hub daemon (the default; see below) it sees every registered
session on every server.
Three entry points confirm on their own behalf, deliberately, because naming them *is* the
confirmation — and their decisions land in the same audit trail `hub(action=describe)`
returns. `reset_target(strategy="cold")` and `recover_session`'s escalation ladder stop the
GDB session before the cut instead of yanking VBUS out from under it.
`hub(action=discover)` refuses outright while a session is live — **except with
`force=true`, which bypasses the interlock entirely**, so that flag is the one place you
are genuinely on your own. Set `guard: allow` for scripted CI. /
`hub(action=describe)` 是只读的，带 ADC 的型号还会返回每端口电压/电流。默认 `confirm`
模式下，`hub(action=power/data/cycle)` 必须显式传 `confirm=true`；**调用方自己的会话**在该端口
上有活跃 GDB 服务时，任何模式（含 `guard: allow`）下都强制要求——烧录中途断电正是把板子变砖的
方式。注意这条检查的范围：`local` 模式下它只看发起调用的那个会话，**不会**发现另一个会话正在该
通道上烧录；使用共享 Hub 守护进程时（默认，见下文）则能看到所有服务器登记的全部会话。
有三处入口会自行确认，这是刻意设计的——点名调用它们本身就是确认，且其决定同样进入
`hub(action=describe)` 返回的审计日志。`reset_target(strategy="cold")` 与 `recover_session`
的恢复阶梯会先停掉 GDB 会话再断电，而不是从活跃会话脚下拔掉 VBUS；`hub(action=discover)`
在有活跃会话时直接拒绝运行——**但 `force=true` 会完全绕过该联锁**，那是唯一真正需要你自己
负责的地方。CI 脚本可设 `guard: allow`。

#### Selecting a channel on a multi-board rack / 多板机架下的通道选择

`hub: {channel: N}` above pins one session to one port. With several boards, map the
whole rack once instead and let each session pick its own channel: /
上面的 `hub: {channel: N}` 把一个会话钉在一个端口上。多块板时改为一次性描述整个机架，
让每个会话各自选中自己的通道：

```yaml
# bench_hub.yaml
hub:
  port: COM7          # omit to auto-scan / 省略则自动扫描
  guard: confirm
  map:
    3: {label: TC}
    4: {label: AGS}
```

```json
{"action": "load", "path": "bench_hub.yaml"}
```

Three things about this that are easy to get wrong: /
其中有三点很容易搞错：

- **A `label` is matched against the debug session name, exactly.** It is not a free-form
  nickname: the channel is selected only when the session was started as
  `session="TC"`. This is the only way to select a channel for a **serial-less probe** —
  `hub(action=discover, apply=true)` keys those by USB port, which *identifies* a channel
  but cannot *select* one. /
  **`label` 是与调试会话名精确比对的**，不是自由昵称：只有会话以 `session="TC"` 启动时才会
  选中该通道。对于**无序列号的探针**这是唯一可行的选择方式——`hub(action=discover, apply=true)`
  只能按 USB 端口给它们建 key，那能*认出*通道，却无法*选中*通道。
- **The debug profile is per-session.** Loading the config into one session does not make
  the map visible to another; the hub call then fails with
  `hub_unavailable: hub channel unmapped`. Load it into **each** session:
  `debug_config(action=load, path="bench_hub.yaml", session="TC")`. The profile is also
  in-memory only, so this is re-run after every server restart. /
  **调试 profile 是按会话隔离的。** 在一个会话里加载配置，另一个会话看不到该映射，此时 hub
  调用会以 `hub_unavailable: hub channel unmapped` 失败。需要给**每个**会话都加载一次：
  `debug_config(action=load, path="bench_hub.yaml", session="TC")`。profile 还是纯内存的，
  因此服务器每次重启后都要重新加载。
- **`set` replaces a nested block; it does not merge into it.** `debug_profile(action=set,
  hub={...})` keeps every OTHER profile field, but the `hub` block you pass replaces the
  stored one whole — so writing `hub={map: {3: {label: TC}}}` after
  `hub(action=discover, apply=true)` throws away the probe identity discovery had written
  there, and a serial-less probe has no other identity. Pass `merge=true` to deep-merge into
  the stored block instead; leave it off when you mean to drop a stale channel, because
  replacing is the only way to remove one. /
  **`set` 对嵌套块是整块替换，不是合并。** 顶层其他字段会保留，但传入的 `hub` 块会整块覆盖已
  存的内容——在 `hub(action=discover, apply=true)` 之后再写 `hub={map: {3: {label: TC}}}`，
  会把 discovery 写入的探针身份一并丢掉（无序列号探针没有别的身份可用）。需要合并请加
  `merge=true`；要删除过期通道则保持默认的替换语义，因为那是唯一的删除方式。

When it works, the result carries `channel_source: "map_label"` — check that field rather
than assuming the right port was chosen. /
生效时结果会带 `channel_source: "map_label"`——请以该字段为准，不要想当然认为选对了端口。

#### Several MCP servers on one rack / 多个 MCP 服务器共用一个 Hub

The hub's control port opens in one process at a time, so by default the first hub call
spawns a small **hub daemon** that owns it, and every MCP server talks to that instead.
Nothing changes in how you call `hub(...)`. /
Hub 控制口同一时间只能被一个进程打开。因此默认情况下，第一次调用 hub 会拉起一个**Hub 守护进程**
独占它，所有 MCP 服务器都改为与守护进程通信；`hub(...)` 的用法完全不变。

- Reads are shared. Writes lock only the channels they touch: a power cycle on CH1 never
  interleaves with another switch on CH1, but a switch on CH4 runs alongside it. Exclusive
  isolation locks the whole rack, and `hub(action=discover)` holds a rack-wide lease for
  its whole run, so no other server's switch can corrupt the map. Waiting is FIFO among
  conflicting requests, so a rack-wide request is never starved. / 读取共享；写操作只锁住
  涉及的通道：CH1 的电源循环不会与 CH1 上的其他开关交错，但 CH4 的操作可以同时进行。exclusive
  隔离锁住整个机架；`hub(action=discover)` 在整个运行期间持有全机架租约，其他服务器的开关无法
  插入。冲突请求按先来后到排队，整机架请求不会被饿死。
- Each server's **guard mode stays its own** (`guard: allow` on a CI server does not relax
  anyone else), and the audit trail records which server did what. / 每个服务器的 **guard
  模式各自独立**，审计日志记录是哪台服务器执行的操作。
- What a server switched off is restored when it exits — or when the daemon finds its PID
  gone. / 某服务器断开的电源/数据线，在它退出（或守护进程发现其进程已消失）时自动恢复。
- **Cross-server interlock.** Every server registers its sessions that have a `hub` block
  (after each tool call, plus a 2 s heartbeat). Cutting power or data on a channel where
  *any* server has a live GDB server needs `confirm=true`, whatever the guard mode, and the
  refusal names that session and server. `hub(action=discover)` refuses while any server
  on the rack is live. A session with no `hub` block cannot be mapped to a channel and so
  is not protected. / **跨服务器互锁**：每个服务器都会登记带 `hub` 配置的会话（每次工具调用后
  同步登记，另有 2 秒心跳）。只要**任一**服务器在该通道上有活跃 GDB，断电/断数据就必须
  `confirm=true`（与 guard 模式无关），拒绝信息会指出是哪个服务器的哪个会话；任一服务器有活跃
  会话时 `hub(action=discover)` 拒绝运行。没有 `hub` 配置的会话无法映射到通道，因此不受保护。
- A write waits at most 30 s behind another server's write, then is refused (not run late)
  with the name of what held the hub; one that did wait reports `queued_ms`. Every switch
  also invalidates every server's `detect_probe` cache. / 写操作最多排队 30 秒，超时则拒绝执行
  （而不是延后执行）并指出占用者；排过队的结果带 `queued_ms`。每次切换都会让所有服务器的
  `detect_probe` 缓存失效。
- The daemon exits after 10 idle minutes unless it still owes a channel. /
  空闲 10 分钟且无待恢复通道时，守护进程自动退出。

```bash
python -m mcp_server.hub_daemon status     # URL, version, connected servers
python -m mcp_server.hub_daemon stop       # restores what clients switched off
python -m mcp_server.hub_daemon serve --config bench_hub.yaml   # run it yourself, foreground
```

`STM32_GDB_MCP_HUB_MODE=auto` (default) uses the daemon and falls back to in-process when
the `[hub]` extra is absent or the daemon cannot start; `daemon` never falls back; `local`
is the old single-process behaviour. Everything binds to `127.0.0.1`; writes need the token
in the per-user state file. /
`STM32_GDB_MCP_HUB_MODE=auto`（默认）使用守护进程，未安装 `[hub]` extra 或守护进程无法启动时
退回进程内模式；`daemon` 不回退；`local` 为原来的单进程行为。全部仅监听 `127.0.0.1`，写操作需
要用户私有状态文件中的 token。

#### Live dashboard / 实时看板

```text
hub(action=dashboard)                 # -> data.url, e.g. http://127.0.0.1:8765/
```

A read-only page: per-port power/data state, live V/I/P with a 1/5/15-minute history
(power-off spans shaded), and a timeline of hub actions. With the daemon it is the
daemon's page, showing every connected MCP server and what each still owes; in `local`
mode it runs inside this server (`state=off` stops it) and lists its debug sessions. /
只读页面：各端口电源/数据线状态、电压电流功率及 1/5/15 分钟历史（断电区间灰色）、hub 操作时间线。
使用守护进程时就是守护进程的页面，显示所有已连接的 MCP 服务器及其待恢复通道；`local` 模式下
在本服务器内运行（`state=off` 停止），并列出本服务器的调试会话。

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
