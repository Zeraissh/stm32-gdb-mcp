# Project Maturity Implementation Plan / 项目成熟化实施计划

> **For agentic workers / 面向智能体执行者：** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task.
> 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或
> `superpowers:executing-plans` 按任务执行本计划。Steps use checkbox (`- [ ]`)
> syntax for tracking. 步骤使用 checkbox (`- [ ]`) 语法跟踪。

**Goal / 目标：** Turn the STM32 GDB MCP prototype into a maintainable,
installable project with config files, response helpers, CI, examples, and
repository hygiene. 将 STM32 GDB MCP 原型打造成可维护、可安装的项目，包含配置文件、
响应辅助模块、CI、示例和仓库规范。

**Architecture / 架构：** Add focused support modules for YAML debug configs and
structured tool responses. Keep MCP handlers thin and expose config helpers as
tools. Add packaging, CI, documentation, and examples without changing existing
debug behavior. 增加聚焦的 YAML 调试配置和结构化工具响应支持模块；保持 MCP handler
轻量，并将配置辅助能力暴露为工具；在不改变既有调试行为的前提下增加打包、CI、文档和示例。

**Tech Stack / 技术栈：** Python 3.10+, MCP Python SDK, pytest, PyYAML,
pyserial, GitHub Actions.

---

### Task 1: Debug Config Support / 任务 1：调试配置支持

**Files / 文件：**
- Create / 创建：`src/mcp_server/debug_config.py`
- Test / 测试：`tests/test_debug_config.py`
- Modify / 修改：`src/mcp_server/server.py`
- Modify / 修改：`pyproject.toml`

- [x] Write failing tests for config validation, save, and load. / 为配置校验、保存和加载编写失败测试。
- [x] Implement YAML config load/save/validation. / 实现 YAML 配置加载、保存和校验。
- [x] Add `load_debug_config`, `save_debug_config`, and `validate_debug_config` MCP tools. / 增加 `load_debug_config`、`save_debug_config` 和 `validate_debug_config` MCP 工具。

### Task 2: Structured Response Helpers / 任务 2：结构化响应辅助模块

**Files / 文件：**
- Create / 创建：`src/mcp_server/tool_response.py`
- Test / 测试：`tests/test_tool_response.py`

- [x] Write failing tests for success/error response envelopes. / 为成功/错误响应包络编写失败测试。
- [x] Implement small JSON-safe response helpers for gradual adoption. / 实现小型 JSON-safe 响应辅助函数，便于渐进采用。

### Task 3: Project Hygiene and CI / 任务 3：项目规范与 CI

**Files / 文件：**
- Create / 创建：`.gitignore`
- Create / 创建：`LICENSE`
- Create / 创建：`CHANGELOG.md`
- Create / 创建：`.github/workflows/ci.yml`
- Modify / 修改：`README.md`

- [x] Add project metadata and contribution-ready defaults. / 增加项目元数据和面向贡献的默认配置。
- [x] Add CI running pytest, compileall, and package build. / 增加运行 pytest、compileall 和包构建的 CI。
- [x] Remove generated `__pycache__` directories from the workspace. / 从工作区移除生成的 `__pycache__` 目录。

### Task 4: Examples / 任务 4：示例

**Files / 文件：**
- Create / 创建：`examples/configs/stm32f4_jlink.yaml`
- Create / 创建：`examples/prompts/debug_hardfault.md`
- Create / 创建：`examples/prompts/freertos_hang.md`

- [x] Add a ready-to-edit J-Link STM32F4 config. / 增加一个可直接编辑的 J-Link STM32F4 配置。
- [x] Add prompt templates for hardfault and FreeRTOS hang workflows. / 增加 HardFault 和 FreeRTOS 卡死工作流提示词模板。

### Task 5: Verification / 任务 5：验证

- [x] Run `python -m pytest -q`. / 运行 `python -m pytest -q`。
- [x] Run `python -m compileall src tests`. / 运行 `python -m compileall src tests`。
- [x] Run package build check. / 运行包构建检查。
