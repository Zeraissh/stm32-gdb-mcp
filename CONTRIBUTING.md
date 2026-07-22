# Contributing / 贡献指南

English: Thanks for helping improve `stm32-gdb-mcp`. This project sits close to
real hardware, so good evidence matters more than guesswork.

中文：感谢你帮助改进 `stm32-gdb-mcp`。这个项目直接贴近真实硬件，因此可靠证据比猜测更重要。

## Development Setup / 开发环境

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python setup_env.py
```

English: `setup_env.py` checks for host tools such as `arm-none-eabi-gdb`,
OpenOCD, SEGGER J-Link tools, and ST-Link tools. A missing tool can be acceptable
for a pure unit-test change, but it should be called out in the pull request.

中文：`setup_env.py` 会检查主机工具，例如 `arm-none-eabi-gdb`、OpenOCD、SEGGER
J-Link 工具和 ST-Link 工具。对于纯单元测试变更，某些工具缺失可以接受，但需要在 PR 中说明。

## Quality Gate / 质量门禁

Run the same checks used by CI before opening a pull request / 提交 PR 前运行与 CI 相同的检查：

```bash
python -m ruff check .
python -m mypy
python -m pytest -q --cov=mcp_server
python -m compileall src tests
python -m build
```

English: New modules should be mypy-clean; `pyproject.toml` carries temporary
`ignore_errors` overrides only for legacy modules pending decomposition. Shared
test fakes (`FakeGdb`, `FakeGdbClient`, `FakeGdbManager`, `FakeProfile`) live in
`tests/conftest.py` — reuse them instead of re-declaring per-file copies.

中文：新模块应保持 mypy 零错误;`pyproject.toml` 中的 `ignore_errors` 仅为等待拆分的
遗留模块临时保留。共享测试替身(`FakeGdb`、`FakeGdbClient`、`FakeGdbManager`、
`FakeProfile`)位于 `tests/conftest.py`,请复用而非在各文件重复定义。

## Change Guidelines / 变更原则

- Prefer small, evidence-backed changes. / 优先提交小而有证据支撑的变更。
- Keep MCP tool responses stable unless the change is explicitly about response schema migration. / 除非目标就是响应结构迁移，否则保持 MCP 工具响应稳定。
- Add unit tests for parser, config, response-shape, and GDB interaction logic. / 为解析器、配置、响应结构和 GDB 交互逻辑添加单元测试。
- Use hardware-in-the-loop validation for changes that affect flashing, reset, target execution, probe behavior, RTT, UART, or live RTOS inspection. / 涉及烧录、复位、目标执行、调试器行为、RTT、UART 或实时 RTOS 检查的变更，需要做硬件在环验证。
- Do not include proprietary firmware, private source paths, credentials, serial numbers, or vendor license data in issues, tests, logs, or snapshots. / 不要在 issue、测试、日志或快照中包含专有固件、私有源码路径、凭据、序列号或厂商授权数据。

## Reporting Bugs / 报告 Bug

Use the bug report template and include sanitized evidence / 使用 Bug 模板，并提供脱敏后的证据：

- MCP tool call sequence / MCP 工具调用序列
- debug config shape / 调试配置结构
- GDB server type and probe / GDB Server 类型和调试器
- MCU and board / MCU 和开发板
- relevant GDB, RTT, UART, or fault-register output / 相关 GDB、RTT、UART 或故障寄存器输出

English: For HardFault and FreeRTOS issues, attach the output of
`capture_debug_snapshot` with private symbols and source paths removed.

中文：对于 HardFault 和 FreeRTOS 问题，请附上 `capture_debug_snapshot` 输出，并移除私有符号和源码路径。
