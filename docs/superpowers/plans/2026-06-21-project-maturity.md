# Project Maturity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the STM32 GDB MCP prototype into a maintainable, installable project with config files, response helpers, CI, examples, and repository hygiene.

**Architecture:** Add focused support modules for YAML debug configs and structured tool responses. Keep MCP handlers thin and expose config helpers as tools. Add packaging, CI, documentation, and examples without changing existing debug behavior.

**Tech Stack:** Python 3.10+, MCP Python SDK, pytest, PyYAML, pyserial, GitHub Actions.

---

### Task 1: Debug Config Support

**Files:**
- Create: `src/mcp_server/debug_config.py`
- Test: `tests/test_debug_config.py`
- Modify: `src/mcp_server/server.py`
- Modify: `pyproject.toml`

- [ ] Write failing tests for config validation, save, and load.
- [ ] Implement YAML config load/save/validation.
- [ ] Add `load_debug_config`, `save_debug_config`, and `validate_debug_config` MCP tools.

### Task 2: Structured Response Helpers

**Files:**
- Create: `src/mcp_server/tool_response.py`
- Test: `tests/test_tool_response.py`

- [ ] Write failing tests for success/error response envelopes.
- [ ] Implement small JSON-safe response helpers for gradual adoption.

### Task 3: Project Hygiene and CI

**Files:**
- Create: `.gitignore`
- Create: `LICENSE`
- Create: `CHANGELOG.md`
- Create: `.github/workflows/ci.yml`
- Modify: `README.md`

- [ ] Add project metadata and contribution-ready defaults.
- [ ] Add CI running pytest, compileall, and package build.
- [ ] Remove generated `__pycache__` directories from the workspace.

### Task 4: Examples

**Files:**
- Create: `examples/configs/stm32f4_jlink.yaml`
- Create: `examples/prompts/debug_hardfault.md`
- Create: `examples/prompts/freertos_hang.md`

- [ ] Add a ready-to-edit J-Link STM32F4 config.
- [ ] Add prompt templates for hardfault and FreeRTOS hang workflows.

### Task 5: Verification

- [ ] Run `python -m pytest -q`.
- [ ] Run `python -m compileall src tests`.
- [ ] Run package build check.
