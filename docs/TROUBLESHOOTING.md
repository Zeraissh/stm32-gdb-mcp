# Troubleshooting / 故障排查

Recovery and fallback modes for the `stm32-gdb-mcp` server.

`stm32-gdb-mcp` 的常见故障、恢复路径与降级方式。

## `no_session`: "GDB is not running" / `no_session`：GDB 未运行

The session ended or never started. Recover in this order:

会话已经结束或从未启动。按以下顺序恢复：

1. Call **`start_debug_session`** again. It cleans up a previous partial start and retries
   transient USB/probe contention.
   / 再次调用 **`start_debug_session`**。它会清理上一次未完成的启动，并只重试瞬时 USB/探针占用故障。
2. Call **`recover_session`** after `probe_unavailable` or `connection_lost`; it reuses the
   last successful session arguments.
   / 遇到 `probe_unavailable` 或 `connection_lost` 时调用 **`recover_session`**，复用上次成功参数。
3. If `start_debug_session` is hidden by compact mode, invoke
   `call(tool="start_debug_session", args={...})`.
   / 若 compact 模式隐藏了该工具，使用 `call(tool="start_debug_session", args={...})`。
4. If OpenOCD `server_args` are omitted, set profile `mcu`; set `probe` too, or connect exactly
   one supported probe so it can be detected safely.
   / 若省略 OpenOCD `server_args`，请先设置 profile 的 `mcu`；同时设置 `probe`，或只连接一个
   受支持探针以便安全自动识别。

Restarting the whole MCP server should not be necessary. /
正常情况下无需重启整个 MCP 服务器。

## Missing or unknown tools / 工具缺失或 `Unknown tool`

Some MCP clients cap the tools they display. The server provides two stable escape hatches:

部分 MCP 客户端会限制可展示的工具。服务器提供两种稳定入口：

- `call(tool="<name>", args={...})` invokes any tool by name; `batch` invokes several. /
  `call(tool="<name>", args={...})` 可按名称调用任意工具；`batch` 可批量调用。
- `tool_help(name="<name>")` returns the hidden tool's full description and schema;
  `tool_help(query="<text>")` searches the catalog. /
  `tool_help(name="<name>")` 返回隐藏工具的完整说明与 schema；
  `tool_help(query="<text>")` 可搜索目录。
- `mcp_info` reports the version, git commit, install path and compact-mode state of the
  server actually serving you. Call it when a tool you expect does not exist at all,
  instead of inspecting the client's plugin manifest or restarting to find out. /
  `mcp_info` 返回当前正在服务的服务器版本、git commit、安装路径与 compact 模式状态。
  当你预期的工具完全不存在时先调用它，而不是去查客户端插件清单或重启客户端。

Old standalone names remain callable through `call`; errors also point to the merged form:

旧版独立工具名仍可通过 `call` 调用；错误信息也会指出新的合并形式：

| Old / 旧名 | New / 新形式 |
|---|---|
| `start/stop/get/clear_{rtt,swo,uart}_log*` | `logging(action=..., channel=...)` |
| `step_over` / `step_into` / `step_out` / `step_instruction` | `step(kind=...)` |
| `read_freertos_*` / `read_current_task` | `read_freertos(what=...)` |
| `start/stop_variable_tracking`, `get_tracked_data` | `track_variable(action=...)` |
| `get_session_{journal,timeline,metrics}` | `get_session(view=...)` |

## Compact mode and discovery / Compact 模式与工具发现

Set `STM32_GDB_MCP_COMPACT=1` in the MCP server environment. Compact mode exposes the
workflow core while every other tool stays reachable through `call` and discoverable with
`tool_help`. Remove the variable to expose the full catalog, then restart the MCP client.

在 MCP 服务器环境中设置 `STM32_GDB_MCP_COMPACT=1`。compact 模式只展示核心工作流工具，
其余工具仍可通过 `call` 调用、通过 `tool_help` 查询。删除该变量即可展示完整目录，修改后需重启客户端。

```json
"stm32-gdb-mcp": {
  "type": "stdio",
  "command": "C:/path/to/stm32-gdb-mcp.exe",
  "args": [],
  "env": { "STM32_GDB_MCP_COMPACT": "1" }
}
```

## Hardware connection error codes / 硬件连接错误码

Call `detect_probe` first when probe identity is unclear. Detection uses host USB state, not
OpenOCD's list of compiled adapter drivers. One probe may be selected automatically; zero
probes return discovery evidence, and multiple probes return `multiple_probes` until a probe
type/serial or explicit server arguments are supplied. / 探针身份不清楚时先调用 `detect_probe`。
它读取主机 USB 状态，而不是 OpenOCD 编译进来的 adapter 驱动列表。仅唯一探针可自动采用；
零个探针会返回发现证据，多个探针会返回 `multiple_probes`，直到明确提供类型/序列号或 server 参数。

- **`probe_busy`**: another debugger owns the probe, or USB reports a busy/open failure.
  Close the competing OpenOCD/GDB process and retry. /
  **`probe_busy`**：探针被其他调试器占用，或 USB 返回 busy/open failure。关闭冲突进程后重试。
- **`probe_unavailable`**: the probe disappeared or a transient USB connection failed.
  Check the cable/power, then use `recover_session`. /
  **`probe_unavailable`**：探针消失或 USB 瞬时断连。检查线缆与供电，再用 `recover_session`。
- **`target_unreachable`**: the probe opened, but the target could not be examined.
  Check target power, SWD wiring, reset state, selected target config, and adapter speed;
  identical retries are intentionally not performed. /
  **`target_unreachable`**：探针已打开，但无法识别目标。检查目标供电、SWD 接线、复位状态、
  target 配置和适配器速率；服务器不会做无意义的同参重试。
- **`debug_auth_required`**: the target is locked or Debug Authentication/RDP blocks access.
  Authentication or security-state changes remain explicit operator actions. /
  **`debug_auth_required`**：目标被锁定，或 Debug Authentication/RDP 阻止访问。
  认证和安全状态修改必须由操作者明确执行。
- **`invalid_target_config`**: the OpenOCD/J-Link target or interface arguments are invalid.
  Regenerate them with `suggest_server_args`, or load a validated debug config. /
  **`invalid_target_config`**：目标或接口参数无效。用 `suggest_server_args` 重新生成，
  或加载已验证的调试配置。
- **`tool_missing`**: GDB or the selected GDB server executable is absent.
  Run `stm32-gdb-mcp-check-env --json` and install the missing host tool. /
  **`tool_missing`**：缺少 GDB 或所选 GDB Server。运行
  `stm32-gdb-mcp-check-env --json`，再安装缺失的主机工具。

Startup errors include the attempted backend, adapter speed, server arguments, and a bounded
server-log tail. Use those fields before changing hardware state.

启动错误会返回尝试过的 backend、适配器速率、服务器参数和受限长度的日志尾部。改变硬件状态前先检查这些证据。

## Installed version or commands drift / 安装版本或命令漂移

Run `stm32-gdb-mcp-check-env --json`. Compare `installation.module_version`,
`distribution_version`, and `console_scripts`; the warning list names stale metadata or
missing entry points. Repair with `stm32-gdb-mcp-deploy --upgrade --project <path>`, then
restart the MCP client so it reloads the executable. / 运行 `stm32-gdb-mcp-check-env --json`，
对比 `installation.module_version`、`distribution_version` 和 `console_scripts`；warning 会指出
过期元数据或缺失入口。使用 `stm32-gdb-mcp-deploy --upgrade --project <path>` 修复后，重启 MCP
客户端以重新加载可执行文件。

## `self_check` reports an unknown device / `self_check` 报告未知器件

Update the MCP first. An unknown DBGMCU ID does not by itself block debugging, but a failed
`dbgmcu_dev_id` check means the configured family was not verified.

先更新 MCP。未知 DBGMCU ID 本身不会阻止调试，但 `dbgmcu_dev_id` 检查失败表示配置的 MCU
系列尚未得到验证。

For STM32U5, DBGMCU IDCODE `0x00000000` is advisory when CPUID still reports Cortex-M33
and `expected_family` starts with `STM32U5`.

对于 STM32U5，如果 CPUID 仍识别为 Cortex-M33 且 `expected_family` 以 `STM32U5` 开头，
DBGMCU IDCODE 为 `0x00000000` 会作为 advisory 处理。

## `missing_argument` / 缺少参数

The error names the required field. Supply it and retry. In compact mode, query the exact
schema with `tool_help(name="<tool>")`.

错误信息会指出缺少的字段。补齐后重试；compact 模式下可用
`tool_help(name="<tool>")` 查询精确 schema。

## ST-Link contention / ST-Link 占用冲突

- Do not hard-kill OpenOCD; use `stop_debug_session` or `recover_session`. /
  不要强杀 OpenOCD；使用 `stop_debug_session` 或 `recover_session`。
- Do not launch a second OpenOCD from a serial verification script while SWD is active.
  Reset through `reset_target`. /
  SWD 活跃时，不要从串口验证脚本启动第二个 OpenOCD；使用 `reset_target` 复位。
- The ST-Link virtual COM port is a separate USB endpoint and can coexist with SWD. /
  ST-Link 虚拟串口是独立 USB 端点，可与 SWD 同时使用。

## Self-reporting / 自助报告

Call `report_issue(title, description)` for an apparent MCP defect. It files a GitHub issue
with the session journal; sanitize proprietary paths, symbols, serials, and memory first.

遇到疑似 MCP 缺陷时调用 `report_issue(title, description)`。它会附带会话日志创建 GitHub issue；
提交前请脱敏专有路径、符号、序列号和内存内容。
