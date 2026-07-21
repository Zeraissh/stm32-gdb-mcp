# STM32 GDB MCP UX Convergence / 体验收敛

### Task 0: Establish the resumable execution plan / 建立可恢复执行清单
**Files:** Create: `docs/superpowers/plans/2026-07-20-ux-convergence.md`

- [x] **Step 1: Record the approved implementation plan / 记录已批准的实施计划**
  Create this checklist from the user-approved onboarding, configuration, MCP UX, diagnostics, HIL, CI, and bilingual-documentation plan.
  Expected: the plan contains Tasks 1-4 with explicit files, tests, and acceptance checks.

### Task 1: First-run installation and deployment / 首次安装与部署
**Files:** Modify: `setup_env.py`, `scripts/deploy.py`, `scripts/install_mcp.py`, `scripts/check_dist_contents.py`, `src/mcp_server/env_check.py`, `src/mcp_server/install_mcp.py`, `src/mcp_server/deploy.py`, `tests/test_release_readiness.py`; Create: `tests/test_env_check.py`, `tests/test_install_mcp.py`, `tests/test_deploy.py`

- [x] **Step 1: Add failing first-run tests / 添加首次安装失败测试**
  Cover `python -S` source entry points, environment readiness matrices and exit codes, valid Codex TOML, Codex CLI add/get idempotency and conflicts, deploy abort behavior, and unambiguous project discovery.
  Expected: the focused tests fail only for the missing Task 1 behavior.

- [x] **Step 2: Make source entry points cold-clone safe / 修通干净克隆入口**
  Bootstrap the repository `src` directory before importing `mcp_server` from the four source entry points.
  Expected: every source entry point reaches its own argument parser or environment check under `python -S`.

- [x] **Step 3: Fix environment and Codex installation semantics / 修复环境与 Codex 安装语义**
  Require GDB plus at least one supported backend, add JSON output and meaningful exit codes, install Codex through `codex mcp add` with `get --json` verification, retain valid TOML printing, and make deploy stop on failed installation.
  Expected: all environment, installer, and deploy unit tests pass.

- [x] **Step 4: Reuse project inspection safely / 安全复用项目识别**
  Reuse `inspect_project`, select an ELF only when exactly one candidate exists, preserve candidate lists when ambiguous, and retain existing OpenOCD/debug-config discovery.
  Expected: MCU and artifact discovery is deterministic and never guesses among multiple ELF files.

- [x] **Step 5: Verify Task 1 / 验证 Task 1**
  Run focused tests, Ruff on changed Python files, and the four `python -S` entry-point checks.
  Expected: all Task 1 checks pass.

### Task 2: Configuration as the single source of truth / 配置成为单一事实源
**Files:** Modify: `src/mcp_server/debug_config.py`, `src/mcp_server/debug_profile.py`, `src/mcp_server/server.py`, `src/mcp_server/composites.py`, `src/mcp_server/openocd_config.py`, `tests/test_debug_config.py`, `tests/test_debug_profile.py`, `tests/test_server_tools.py`, `tests/test_composites.py`, `tests/test_openocd_config.py`, `README.md`, `docs/install-ides.md`

- [x] **Step 1: Add failing configuration-flow tests / 添加配置流失败测试**
  Cover relative path resolution, profile retention of serial/logging settings, profile-backed session start, logging defaults, reset strategy reuse, and adapter speed propagation.
  Expected: the focused tests fail only for the missing Task 2 behavior.

- [x] **Step 2: Resolve and retain complete configuration / 解析并保留完整配置**
  Resolve path fields relative to the config file and retain serial, RTT, UART, and SWO configuration in the active profile.
  Expected: config load produces deterministic absolute paths and complete profile state.

- [x] **Step 3: Apply profile defaults across runtime tools / 在运行工具中应用配置默认值**
  Let session start, logging, reset, flash, and flash-and-run consume profile defaults; expose `speed_khz`; remove hard-coded reset behavior from the composite.
  Expected: config load followed by parameter-light tool calls uses one coherent profile.

- [x] **Step 4: Document the one-round-trip bring-up recipe / 记录单轮往返连接配方**
  Document `batch` for config load, session start, and self-check without adding a duplicate connection tool.
  Expected: README and install guidance show an executable profile-backed recipe.

- [x] **Step 5: Verify Task 2 / 验证 Task 2**
  Run focused tests and Ruff.
  Expected: all Task 2 checks pass without hardware access.

### Task 3: MCP schemas, structured results, and diagnostics / MCP Schema、结构化结果与诊断
**Files:** Modify: `src/mcp_server/tool_surface.py`, `src/mcp_server/tool_response.py`, `src/mcp_server/error_taxonomy.py`, `src/mcp_server/reliability.py`, `src/mcp_server/server.py`, `src/mcp_server/gdb_manager.py`, `tests/test_server_tools.py`, `tests/test_tool_response.py`, `tests/test_error_taxonomy.py`, `tests/test_reliability.py`, `tests/test_gdb_manager.py`; Create: `tests/test_tool_surface.py`

- [x] **Step 1: Add failing MCP contract tests / 添加 MCP 契约失败测试**
  Cover action-specific merged schemas, the common session argument, hidden-tool help, native structured content, MCP error signaling, annotations, refined error codes, retryability, and partial-start cleanup evidence.
  Expected: the focused tests fail only for the missing Task 3 behavior.

- [x] **Step 2: Build precise schemas and hidden-tool help / 构建精确 Schema 与隐藏工具帮助**
  Derive merged schemas from underlying tools, add the common session property, and expose compact `tool_help`.
  Expected: clients can discover every hidden tool and validate every visible action.

- [x] **Step 3: Return native structured MCP results / 返回原生结构化 MCP 结果**
  Preserve the existing JSON TextContent while returning identical structured content, a shared output schema, and `isError=true` for error envelopes.
  Expected: old text consumers and modern structured consumers receive equivalent data.

- [x] **Step 4: Refine hardware diagnostics and startup cleanup / 细化硬件诊断与启动清理**
  Distinguish probe busy, unreachable target, debug authentication, invalid target config, and missing tools; retry only transient failures; clean up partial starts and return attempted settings plus bounded server logs.
  Expected: replayed U535 failures are actionable and are not retried as generic probe failures.

- [x] **Step 5: Verify Task 3 / 验证 Task 3**
  Run focused tests, compact/full tool inspection, and Ruff.
  Expected: all Task 3 checks pass.

### Task 4: HIL trust, CI, bilingual docs, and release / HIL 可信度、CI、双语文档与发行
**Files:** Modify: `src/mcp_server/hil_smoke.py`, `tests/hil/test_real_hil_smoke.py`, `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `pyproject.toml`, `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `docs/TROUBLESHOOTING.md`, `docs/hil-validation.md`, `docs/install-ides.md`, `docs/release.md`, `skills/stm32-debug/SKILL.md`, `skills/stm32-debug/reference/tool-map.md`, `skills/stm32-instrument/SKILL.md`, `examples/firmware/stm32l431_assertdemo/README.md`, `examples/firmware/stm32l431_blinky/README.md`, `examples/firmware/stm32l431_faultdemo/README.md`, `examples/firmware/stm32l431_heapdemo/README.md`, `examples/firmware/stm32l431_periphdemo/README.md`, `examples/firmware/stm32l431_stackdemo/README.md`, `examples/prompts/debug_hardfault.md`, `examples/prompts/freertos_hang.md`, `tests/test_release_readiness.py`

- [x] **Step 1: Strengthen HIL evidence / 强化 HIL 证据**
  Exercise the public debug path and assert decoded CPUID, Cortex core, and expected MCU family instead of accepting raw reads alone.
  Expected: hardware-free unit coverage passes and real HIL remains explicitly gated and non-flashing.

- [x] **Step 2: Add clean-source and clean-wheel CI gates / 添加干净源码与 Wheel CI 门禁**
  Validate source entry points, console scripts, Codex configuration fallback, distribution contents, and clean-wheel installation.
  Expected: CI and release workflows reject broken onboarding or contaminated artifacts.

- [x] **Step 3: Complete public bilingual documentation / 完成公开双语文档**
  Make public docs, skills, examples, and prompts bilingual while leaving historical implementation plans unchanged; remove stale hard-coded tool counts.
  Expected: every public explanatory Markdown file contains equivalent English and Chinese guidance.

- [x] **Step 4: Prepare version 0.4.0 / 准备 0.4.0**
  Update package/plugin-facing release metadata and changelog for the user-visible CLI and MCP contract improvements.
  Expected: version references and release notes are internally consistent.

- [x] **Step 5: Run final verification / 运行最终验证**
  Run Ruff, full pytest, compileall, build, distribution audit, and clean-wheel console smoke tests. Run real HIL only when an explicitly configured board is available.
  Expected: all hardware-free gates pass; HIL is either verified or explicitly reported as not run.
