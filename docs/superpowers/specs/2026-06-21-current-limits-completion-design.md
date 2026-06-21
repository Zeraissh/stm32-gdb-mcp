# Current Limits Completion Design / Current Limits 完成设计

## Purpose / 目的

English: Convert every item currently listed under `Current Limits` in the
README into implemented, testable project capabilities while keeping existing
MCP clients compatible.

中文：将 README `Current Limits` 中列出的每一项限制转化为已实现、可测试的项目能力，同时保持现有
MCP 客户端兼容。

## Scope / 范围

English: This design covers five capabilities:

中文：本设计覆盖五项能力：

1. probe-specific reset strategy profiles / 面向不同调试器的复位策略 profile
2. board-specific hardware-in-the-loop regression experiments / 面向具体板卡的硬件在环回归实验
3. SWO/ITM log capture / SWO/ITM 日志采集
4. real hardware integration tests with example firmware / 带示例固件的真实硬件集成测试
5. full migration of tool responses to the stable JSON envelope / 工具响应完整迁移到稳定 JSON 包络

## Non-Goals / 非目标

English:

- Do not silently erase or flash connected hardware during normal CI.
- Do not require proprietary ST, SEGGER, or vendor assets to run unit tests.
- Do not remove the MCP `TextContent` transport wrapper, because the MCP Python
  SDK expects tool handlers to return content objects.
- Do not implement a complete SWO/TPIU configuration wizard in this phase.

中文：

- 普通 CI 不得静默擦除或烧录已连接硬件。
- 单元测试不得依赖 ST、SEGGER 或其他厂商的专有资源。
- 不移除 MCP `TextContent` 传输外壳，因为 MCP Python SDK 期望工具 handler 返回 content 对象。
- 本阶段不实现完整的 SWO/TPIU 配置向导。

## Architecture / 架构

English: Add small support modules and keep `server.py` as a thin MCP adapter.
Reset behavior moves into a reset-strategy module. Hardware smoke behavior moves
into a HIL module so it can be tested without hardware by injecting fake GDB
clients and managers. SWO/ITM capture reuses the existing process-log reader
pattern. Tool responses are wrapped through one helper so legacy handler bodies
can migrate incrementally but produce the same envelope shape externally.

中文：新增小型支持模块，让 `server.py` 继续作为轻量 MCP 适配层。复位行为进入 reset strategy
模块；硬件烟测行为进入 HIL 模块，并通过注入 fake GDB client/manager 实现无硬件测试；
SWO/ITM 采集复用现有进程日志读取模式；工具响应通过统一 helper 包装，使旧 handler 可以逐步迁移，
但对外输出同一种响应包络。

## Capability Details / 能力细节

### Reset Strategy Profiles / 复位策略 Profile

English: Add a reset strategy registry keyed by server type and strategy name.
The default strategy preserves current behavior: OpenOCD uses `monitor reset
halt` or `monitor reset run` depending on `halt`; other backends get conservative
commands that can be overridden. `reset_target` gains optional `strategy` and
`command` fields. YAML debug configs gain an optional `reset` object.

中文：新增按 server type 和 strategy name 索引的复位策略注册表。默认策略保持当前行为：
OpenOCD 根据 `halt` 使用 `monitor reset halt` 或 `monitor reset run`；其他 backend 使用保守命令，
并允许覆盖。`reset_target` 新增可选 `strategy` 和 `command` 字段。YAML 调试配置新增可选
`reset` 对象。

Acceptance / 验收：

- Unit tests verify OpenOCD, ST-Link, and J-Link default strategies.
- Unit tests verify `under_reset`, `software`, and custom command behavior.
- `validate_debug_config` accepts valid `reset.strategy` and rejects invalid reset shapes.

### HIL Regression Experiments / HIL 回归实验

English: Add `tests/hil/` smoke tests that are skipped unless
`STM32_GDB_MCP_HIL=1`. The smoke reads a YAML config, starts the configured GDB
server, connects GDB, optionally halts, reads CPUID and DBGMCU IDCODE, resumes,
and always stops the session. Flashing remains opt-in.

中文：新增 `tests/hil/` 烟测，只有设置 `STM32_GDB_MCP_HIL=1` 时才运行。烟测读取 YAML 配置、
启动配置指定的 GDB Server、连接 GDB、可选暂停、读取 CPUID 和 DBGMCU IDCODE、恢复运行，并始终停止会话。
烧录保持显式 opt-in。

Acceptance / 验收：

- Normal `python -m pytest -q` skips HIL tests.
- HIL workflow can run `python -m pytest -q tests/hil -m hil`.
- The smoke command supports a config path from `STM32_GDB_MCP_HIL_CONFIG`.

### SWO/ITM Capture / SWO/ITM 采集

English: Add process-based SWO/ITM capture with the same semantics as RTT:
start, stop, get, and clear. The caller provides the command and args, allowing
OpenOCD, J-Link, ST tools, or custom decoders to be used without hard-coding one
vendor pipeline.

中文：新增基于进程输出的 SWO/ITM 采集，语义与 RTT 一致：启动、停止、读取、清空。调用方提供命令和参数，
从而可使用 OpenOCD、J-Link、ST 工具或自定义解码器，而不把某一家工具链写死。

Acceptance / 验收：

- MCP exposes `start_swo_logging`, `stop_swo_logging`, `get_swo_logs`, and `clear_swo_logs`.
- Unit tests verify tool exposure and log capture behavior with injected process output.
- Debug snapshots can include SWO logs when `include_logs=true`.

### Example Firmware and Hardware Integration / 示例固件与硬件集成

English: Add a minimal STM32L431 example firmware project that builds with
`arm-none-eabi-gcc` and CMake when the toolchain is present. The example should
be small and inspectable: startup, linker script, `main.c`, and CMake metadata.
HIL can optionally flash this ELF only when the operator explicitly enables it.

中文：新增一个最小 STM32L431 示例固件项目，在工具链存在时可用 `arm-none-eabi-gcc` 和 CMake 构建。
示例应小而可审阅：包含 startup、linker script、`main.c` 和 CMake 元数据。只有操作者显式启用时，
HIL 才可以选择烧录该 ELF。

Acceptance / 验收：

- Example firmware files are documented and do not affect normal package builds.
- A smoke build script or documented CMake command exists.
- CI does not fail when the embedded GCC toolchain is absent.

### Stable JSON Envelope Migration / 稳定 JSON 包络迁移

English: All MCP tool calls continue returning `TextContent(type="text", ...)`,
but the text payload becomes JSON using the stable envelope:

中文：所有 MCP 工具调用仍返回 `TextContent(type="text", ...)`，但 text payload 统一为稳定 JSON 包络：

```json
{
  "ok": true,
  "data": {},
  "error": null,
  "raw_response": null,
  "suggested_next_actions": []
}
```

English: Error responses use the same envelope with `ok=false`. Human-readable
messages move into `data.message` or `error.message`, not top-level plain text.

中文：错误响应也使用同一包络，并设置 `ok=false`。人类可读消息进入 `data.message` 或
`error.message`，不再使用顶层纯文本。

Acceptance / 验收：

- Unit tests call selected server tools and parse their `TextContent.text` as JSON.
- Unknown tool and raised exceptions return the stable error envelope.
- Existing raw GDB responses remain available in `raw_response` where useful.

## Testing Strategy / 测试策略

English:

- Use TDD for new behavior: write failing tests first, verify failure, implement,
  then verify green.
- Unit-test reset strategy, HIL smoke orchestration, SWO log capture, config
  validation, and response envelope formatting.
- Keep hardware tests skipped by default and gated by explicit environment variables.
- Run the full quality gate before commit: Ruff, pytest, compileall, build, and
  YAML parse checks for GitHub templates/workflows.

中文：

- 新行为采用 TDD：先写失败测试，确认失败，再实现并确认通过。
- 单元测试覆盖 reset strategy、HIL smoke 编排、SWO 日志采集、配置校验和响应包络格式。
- 硬件测试默认跳过，并由明确的环境变量启用。
- 提交前运行完整质量门禁：Ruff、pytest、compileall、build，以及 GitHub 模板/workflow 的 YAML 解析检查。

## Rollout / 落地方式

English: Implement in small commits by capability. Keep README `Current Limits`
until all tasks are implemented, then replace it with a `Completed Capabilities`
or `Roadmap` section that documents what now exists and what remains future work.

中文：按能力小步提交。所有任务完成前保留 README `Current Limits`；全部实现后，将其替换为
`Completed Capabilities` 或 `Roadmap` 章节，说明当前已具备的能力和后续规划。
