# Current Limits Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement every capability currently listed under README `Current Limits`: reset strategy profiles, HIL regression experiments, SWO/ITM capture, STM32L431 example firmware, and stable JSON response envelopes.

**Architecture:** Keep `server.py` as the MCP adapter and move new behavior into focused modules. Use test-first implementation for reset strategy resolution, HIL orchestration, SWO log capture, and response envelope formatting. Hardware-facing tests are skipped by default and only run when explicitly enabled through environment variables.

**Tech Stack:** Python 3.10+, MCP Python SDK, pytest, PyYAML, pyserial, OpenOCD/GDB for HIL, CMake and Arm GNU Toolchain for optional example firmware builds.

---

## File Map

- Create `src/mcp_server/reset_strategy.py`: resolves reset commands by server type, strategy, halt mode, and custom command.
- Create `src/mcp_server/hil_smoke.py`: orchestrates non-destructive hardware smoke checks with injectable GDB server/client objects.
- Modify `src/mcp_server/tool_response.py`: add `content_success`, `content_error`, and JSON serialization helpers for MCP `TextContent`.
- Modify `src/mcp_server/server.py`: expose reset options, SWO tools, stable envelopes, and new log context.
- Modify `src/mcp_server/debug_config.py`: validate optional `reset` and `hil` config sections.
- Modify `src/mcp_server/debug_profile.py`: allow reset/HIL profile fields if needed by config load.
- Modify `src/mcp_server/debug_snapshot.py`: accept SWO log context.
- Modify `src/mcp_server/log_reader.py`: reuse existing process reader for SWO through server-level instance.
- Create `tests/test_reset_strategy.py`: unit tests for reset command resolution.
- Create `tests/test_hil_smoke.py`: unit tests for HIL orchestration using fakes.
- Create `tests/hil/test_hil_smoke.py`: skipped-by-default real hardware smoke test.
- Modify `tests/test_tool_response.py`: tests for `TextContent` JSON envelope helpers.
- Modify `tests/test_server_tools.py`: tool exposure and selected response-envelope tests.
- Modify `tests/test_debug_config.py`: reset/HIL config validation tests.
- Modify `tests/test_debug_snapshot.py`: SWO log context test.
- Modify `tests/test_log_reader.py`: SWO process capture behavior through shared reader pattern if needed.
- Modify `pyproject.toml`: add `hil` pytest marker.
- Modify `.github/workflows/hil.yml`: run `tests/hil -m hil` with explicit env gates.
- Create `examples/firmware/stm32l431_blinky/`: minimal optional STM32L431 firmware example.
- Modify `README.md`, `docs/hil-validation.md`, `docs/release.md`, and examples docs: replace `Current Limits` with implemented capability docs.

---

### Task 1: Reset Strategy Profiles

**Files:**
- Create: `src/mcp_server/reset_strategy.py`
- Modify: `src/mcp_server/debug_config.py`
- Modify: `src/mcp_server/debug_profile.py`
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_reset_strategy.py`
- Test: `tests/test_debug_config.py`
- Test: `tests/test_server_tools.py`

- [ ] **Step 1: Write failing reset strategy tests**

Add `tests/test_reset_strategy.py`:

```python
import pytest

from mcp_server.reset_strategy import resolve_reset_command


def test_openocd_default_preserves_existing_halt_and_run_commands():
    assert resolve_reset_command("openocd", halt=True) == {
        "server_type": "openocd",
        "strategy": "default",
        "command": "monitor reset halt",
    }
    assert resolve_reset_command("openocd", halt=False)["command"] == "monitor reset run"


def test_probe_specific_strategies_resolve_to_monitor_commands():
    assert resolve_reset_command("openocd", halt=True, strategy="software")["command"] == "monitor soft_reset_halt"
    assert resolve_reset_command("jlink", halt=True, strategy="under_reset")["command"] == "monitor reset halt"
    assert resolve_reset_command("stlink", halt=False, strategy="software")["command"] == "monitor reset run"


def test_custom_reset_command_overrides_strategy():
    result = resolve_reset_command("openocd", halt=True, strategy="software", command="monitor reset init")

    assert result == {
        "server_type": "openocd",
        "strategy": "custom",
        "command": "monitor reset init",
    }


def test_unknown_reset_strategy_raises_clear_error():
    with pytest.raises(ValueError, match="Unsupported reset strategy"):
        resolve_reset_command("openocd", halt=True, strategy="bad")
```

- [ ] **Step 2: Run reset strategy tests and verify RED**

Run:

```bash
python -m pytest tests/test_reset_strategy.py -q
```

Expected: FAIL because `mcp_server.reset_strategy` does not exist.

- [ ] **Step 3: Implement minimal reset strategy resolver**

Create `src/mcp_server/reset_strategy.py`:

```python
DEFAULT_STRATEGY = "default"

RESET_COMMANDS = {
    "openocd": {
        "default": {True: "monitor reset halt", False: "monitor reset run"},
        "under_reset": {True: "monitor reset halt", False: "monitor reset run"},
        "software": {True: "monitor soft_reset_halt", False: "monitor reset run"},
    },
    "stlink": {
        "default": {True: "monitor reset halt", False: "monitor reset run"},
        "under_reset": {True: "monitor reset halt", False: "monitor reset run"},
        "software": {True: "monitor reset halt", False: "monitor reset run"},
    },
    "jlink": {
        "default": {True: "monitor reset halt", False: "monitor reset go"},
        "under_reset": {True: "monitor reset halt", False: "monitor reset go"},
        "software": {True: "monitor reset halt", False: "monitor reset go"},
    },
}


def resolve_reset_command(server_type: str | None, halt: bool, strategy: str | None = None, command: str | None = None) -> dict:
    if command and command.strip():
        return {
            "server_type": (server_type or "unknown").lower(),
            "strategy": "custom",
            "command": command.strip(),
        }

    normalized_server = (server_type or "openocd").lower()
    normalized_strategy = strategy or DEFAULT_STRATEGY
    strategies = RESET_COMMANDS.get(normalized_server)
    if not strategies or normalized_strategy not in strategies:
        raise ValueError(f"Unsupported reset strategy '{normalized_strategy}' for server type '{normalized_server}'")

    return {
        "server_type": normalized_server,
        "strategy": normalized_strategy,
        "command": strategies[normalized_strategy][bool(halt)],
    }
```

- [ ] **Step 4: Run reset strategy tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_reset_strategy.py -q
```

Expected: PASS.

- [ ] **Step 5: Write failing debug config tests for reset/HIL sections**

Append to `tests/test_debug_config.py`:

```python
def test_validate_debug_config_accepts_reset_and_hil_sections():
    result = validate_debug_config({
        "server_type": "openocd",
        "reset": {"strategy": "under_reset", "halt": True},
        "hil": {"read_cpuid": True, "read_dbgmcu_idcode": True, "flash": False},
    })

    assert result["valid"] is True
    assert result["errors"] == []


def test_validate_debug_config_rejects_invalid_reset_and_hil_sections():
    result = validate_debug_config({
        "reset": {"strategy": 123, "halt": "yes"},
        "hil": {"flash": "sometimes"},
    })

    assert result["valid"] is False
    assert "reset.strategy must be a string" in result["errors"]
    assert "reset.halt must be a boolean" in result["errors"]
    assert "hil.flash must be a boolean" in result["errors"]
```

- [ ] **Step 6: Run config tests and verify RED**

Run:

```bash
python -m pytest tests/test_debug_config.py -q
```

Expected: FAIL because `reset` and `hil` are unknown or not validated.

- [ ] **Step 7: Implement reset/HIL config validation**

Modify `src/mcp_server/debug_config.py`:

- Add `"reset"` and `"hil"` to `TOP_LEVEL_FIELDS`.
- Add `_validate_reset(config.get("reset"), errors)`.
- Add `_validate_hil(config.get("hil"), errors)`.
- Implement:

```python
def _validate_reset(reset, errors: list[str]):
    if reset is None:
        return
    if not isinstance(reset, dict):
        errors.append("reset must be an object")
        return
    strategy = reset.get("strategy")
    if strategy is not None and not isinstance(strategy, str):
        errors.append("reset.strategy must be a string")
    command = reset.get("command")
    if command is not None and (not isinstance(command, str) or not command.strip()):
        errors.append("reset.command must not be empty when provided")
    halt = reset.get("halt")
    if halt is not None and not isinstance(halt, bool):
        errors.append("reset.halt must be a boolean")


def _validate_hil(hil, errors: list[str]):
    if hil is None:
        return
    if not isinstance(hil, dict):
        errors.append("hil must be an object")
        return
    for field in ("flash", "halt", "read_cpuid", "read_dbgmcu_idcode"):
        value = hil.get(field)
        if value is not None and not isinstance(value, bool):
            errors.append(f"hil.{field} must be a boolean")
```

- [ ] **Step 8: Update server `reset_target` schema and handler**

Modify `src/mcp_server/server.py`:

- Import `resolve_reset_command`.
- Add optional `strategy` and `command` fields to `reset_target`.
- In handler:

```python
profile = debug_profile.get()
reset_config = profile.get("reset", {})
resolved = resolve_reset_command(
    gdb_manager.server_type or profile.get("server_type"),
    halt=arguments["halt"],
    strategy=arguments.get("strategy") or reset_config.get("strategy"),
    command=arguments.get("command") or reset_config.get("command"),
)
resp = gdb_client.reset_halt(command=resolved["command"])
return content_success({"message": "Target reset", "reset": resolved}, raw_response=resp)
```

If response-envelope helpers are not implemented yet, return the existing text form temporarily and migrate in Task 2.

- [ ] **Step 9: Run targeted tests**

Run:

```bash
python -m pytest tests/test_reset_strategy.py tests/test_debug_config.py tests/test_server_tools.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit Task 1**

```bash
git add src/mcp_server/reset_strategy.py src/mcp_server/debug_config.py src/mcp_server/debug_profile.py src/mcp_server/server.py tests/test_reset_strategy.py tests/test_debug_config.py tests/test_server_tools.py
git commit -m "Add probe reset strategy profiles"
```

---

### Task 2: Stable JSON Envelope for MCP Tool Responses

**Files:**
- Modify: `src/mcp_server/tool_response.py`
- Modify: `src/mcp_server/server.py`
- Test: `tests/test_tool_response.py`
- Test: `tests/test_server_tools.py`

- [ ] **Step 1: Write failing TextContent envelope tests**

Extend `tests/test_tool_response.py`:

```python
import json

from mcp.types import TextContent
from mcp_server.tool_response import content_error, content_success, parse_content_text


def test_content_success_returns_textcontent_with_json_envelope():
    content = content_success({"message": "ok"}, raw_response=[{"message": "done"}])

    assert isinstance(content, TextContent)
    payload = json.loads(content.text)
    assert payload["ok"] is True
    assert payload["data"] == {"message": "ok"}
    assert payload["raw_response"] == [{"message": "done"}]
    assert payload["error"] is None


def test_content_error_returns_textcontent_with_json_envelope():
    content = content_error("Unknown tool", code="unknown_tool", suggested_next_actions=["list_tools"])

    payload = parse_content_text(content)
    assert payload["ok"] is False
    assert payload["error"] == {"message": "Unknown tool", "code": "unknown_tool"}
    assert payload["suggested_next_actions"] == ["list_tools"]
```

- [ ] **Step 2: Run envelope tests and verify RED**

Run:

```bash
python -m pytest tests/test_tool_response.py -q
```

Expected: FAIL because helpers do not exist.

- [ ] **Step 3: Implement TextContent envelope helpers**

Modify `src/mcp_server/tool_response.py`:

```python
import json

from mcp.types import TextContent


def success_response(data=None, raw_response=None, suggested_next_actions=None):
    return {
        "ok": True,
        "data": data,
        "error": None,
        "raw_response": raw_response,
        "suggested_next_actions": suggested_next_actions or [],
    }


def error_response(message: str, code: str | None = None, raw_response=None, suggested_next_actions=None):
    return {
        "ok": False,
        "data": None,
        "error": {
            "message": message,
            "code": code,
        },
        "raw_response": raw_response,
        "suggested_next_actions": suggested_next_actions or [],
    }


def content_success(data=None, raw_response=None, suggested_next_actions=None) -> TextContent:
    return TextContent(
        type="text",
        text=json.dumps(success_response(data, raw_response, suggested_next_actions), indent=2),
    )


def content_error(message: str, code: str | None = None, raw_response=None, suggested_next_actions=None) -> TextContent:
    return TextContent(
        type="text",
        text=json.dumps(error_response(message, code, raw_response, suggested_next_actions), indent=2),
    )


def parse_content_text(content: TextContent) -> dict:
    return json.loads(content.text)
```

- [ ] **Step 4: Run envelope tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_tool_response.py -q
```

Expected: PASS.

- [ ] **Step 5: Write failing selected server response tests**

Extend `tests/test_server_tools.py`:

```python
import json

from mcp_server.server import handle_call_tool


def _payload(result):
    return json.loads(result[0].text)


def test_get_debug_profile_returns_stable_json_envelope():
    payload = _payload(asyncio.run(handle_call_tool("get_debug_profile", {})))

    assert payload["ok"] is True
    assert isinstance(payload["data"], dict)
    assert payload["error"] is None


def test_unknown_tool_returns_stable_json_error_envelope():
    payload = _payload(asyncio.run(handle_call_tool("does_not_exist", {})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_execution_error"
    assert "Unknown tool" in payload["error"]["message"]
```

- [ ] **Step 6: Run server response tests and verify RED**

Run:

```bash
python -m pytest tests/test_server_tools.py -q
```

Expected: FAIL because selected handlers still return plain text/JSON without envelope.

- [ ] **Step 7: Migrate `server.py` handler returns to envelope helpers**

Modify `src/mcp_server/server.py`:

- Import `content_success` and `content_error`.
- Replace direct `TextContent(type="text", text=...)` returns with:
  - `content_success({"message": "...", ...}, raw_response=resp)` for command-style actions.
  - `content_success(result)` for structured data.
  - `content_error(str(e), code="tool_execution_error", suggested_next_actions=["capture_debug_snapshot"])` in the exception block.
- Keep GDB MI responses in `raw_response` when the user may need the original records.

- [ ] **Step 8: Run server response tests and all existing tests**

Run:

```bash
python -m pytest tests/test_tool_response.py tests/test_server_tools.py -q
python -m pytest -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 2**

```bash
git add src/mcp_server/tool_response.py src/mcp_server/server.py tests/test_tool_response.py tests/test_server_tools.py
git commit -m "Migrate MCP tools to stable JSON envelopes"
```

---

### Task 3: SWO/ITM Capture

**Files:**
- Modify: `src/mcp_server/server.py`
- Modify: `src/mcp_server/debug_snapshot.py`
- Test: `tests/test_server_tools.py`
- Test: `tests/test_debug_snapshot.py`

- [ ] **Step 1: Write failing SWO tool exposure test**

Extend `tests/test_server_tools.py`:

```python
def test_server_exposes_swo_log_tools():
    tools = asyncio.run(handle_list_tools())
    tool_names = {tool.name for tool in tools}

    assert "start_swo_logging" in tool_names
    assert "stop_swo_logging" in tool_names
    assert "get_swo_logs" in tool_names
    assert "clear_swo_logs" in tool_names
```

- [ ] **Step 2: Run server tool tests and verify RED**

Run:

```bash
python -m pytest tests/test_server_tools.py::test_server_exposes_swo_log_tools -q
```

Expected: FAIL because SWO tools are not exposed.

- [ ] **Step 3: Add SWO tool schemas and handlers**

Modify `src/mcp_server/server.py`:

- Add `swo_log_reader = ProcessLogReader("swo")`.
- Add tools `start_swo_logging`, `stop_swo_logging`, `get_swo_logs`, `clear_swo_logs`.
- Implement handlers mirroring RTT:

```python
elif name == "start_swo_logging":
    command = [arguments["command"]]
    command.extend(arguments.get("args", []))
    swo_log_reader.start(command)
    return content_success(swo_log_reader.status())
```

For `get_swo_logs`, return `{"status": swo_log_reader.status(), "entries": ...}`.

- [ ] **Step 4: Run SWO exposure tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_server_tools.py -q
```

Expected: PASS.

- [ ] **Step 5: Write failing debug snapshot SWO context test**

Extend `tests/test_debug_snapshot.py`:

```python
def test_collect_debug_snapshot_can_include_swo_log_context():
    logs = {
        "rtt": {"entries": []},
        "uart": {"entries": []},
        "swo": {"entries": [{"source": "swo", "line": "ITM: boot"}]},
    }

    snapshot = collect_debug_snapshot(FakeGdbClient(), FakeGdbServerManager(), log_context=logs)

    assert snapshot["logs"]["swo"]["entries"][0]["line"] == "ITM: boot"
```

This may already pass because `log_context` is generic. If it passes immediately, keep it as a regression test and proceed to server snapshot assembly.

- [ ] **Step 6: Include SWO logs in `capture_debug_snapshot`**

Modify `src/mcp_server/server.py` so `include_logs=true` builds:

```python
log_context = {
    "rtt": {"status": rtt_log_reader.status(), "entries": rtt_log_reader.get_logs(limit=log_limit)},
    "uart": {"status": uart_log_reader.status(), "entries": uart_log_reader.get_logs(limit=log_limit)},
    "swo": {"status": swo_log_reader.status(), "entries": swo_log_reader.get_logs(limit=log_limit)},
}
```

- [ ] **Step 7: Run targeted tests**

Run:

```bash
python -m pytest tests/test_server_tools.py tests/test_debug_snapshot.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 3**

```bash
git add src/mcp_server/server.py tests/test_server_tools.py tests/test_debug_snapshot.py
git commit -m "Add SWO ITM log capture tools"
```

---

### Task 4: HIL Smoke Tests and Workflow

**Files:**
- Create: `src/mcp_server/hil_smoke.py`
- Create: `tests/test_hil_smoke.py`
- Create: `tests/hil/test_hil_smoke.py`
- Modify: `pyproject.toml`
- Modify: `.github/workflows/hil.yml`
- Modify: `docs/hil-validation.md`

- [ ] **Step 1: Write failing HIL orchestration unit tests**

Create `tests/test_hil_smoke.py`:

```python
from mcp_server.hil_smoke import run_hil_smoke


class FakeServer:
    def __init__(self):
        self.started = []
        self.stopped = False
        self.server_type = None
        self.port = None

    def start(self, server_type, args):
        self.started.append((server_type, args))
        self.server_type = server_type
        self.port = 3333
        return self.port

    def stop(self):
        self.stopped = True


class FakeGdb:
    def __init__(self):
        self.commands = []
        self.stopped = False

    def start_gdb(self):
        self.commands.append("start_gdb")

    def connect(self, host="localhost", port=3333):
        self.commands.append(("connect", host, port))
        return [{"message": "connected"}]

    def execute_cli_command(self, cmd, timeout_sec=1.0):
        self.commands.append(cmd)
        return [{"message": "done"}]

    def read_typed_memory(self, address, width_bits=32, count=1):
        self.commands.append(("read", address, width_bits, count))
        return [{"payload": {"memory": [{"contents": "41c20f41"}]}}]

    def stop_gdb(self):
        self.stopped = True


def test_run_hil_smoke_connects_reads_ids_resumes_and_stops():
    server = FakeServer()
    gdb = FakeGdb()

    result = run_hil_smoke(
        {"server_type": "openocd", "server_args": ["-f", "target/stm32l4x.cfg"], "hil": {"halt": True}},
        server,
        gdb,
    )

    assert result["ok"] is True
    assert result["server"]["port"] == 3333
    assert result["cpuid"]["address"] == "0xE000ED00"
    assert result["dbgmcu_idcode"]["address"] == "0xE0042000"
    assert "monitor halt" in gdb.commands
    assert "monitor resume" in gdb.commands
    assert gdb.stopped is True
    assert server.stopped is True
```

- [ ] **Step 2: Run HIL unit test and verify RED**

Run:

```bash
python -m pytest tests/test_hil_smoke.py -q
```

Expected: FAIL because `mcp_server.hil_smoke` does not exist.

- [ ] **Step 3: Implement HIL smoke orchestrator**

Create `src/mcp_server/hil_smoke.py`:

```python
CPUID_ADDRESS = "0xE000ED00"
DBGMCU_IDCODE_ADDRESS = "0xE0042000"


def run_hil_smoke(config: dict, gdb_server, gdb_client) -> dict:
    server_type = config["server_type"]
    server_args = config.get("server_args", [])
    hil = config.get("hil", {})
    halt = hil.get("halt", True)
    result = {"ok": False, "server": {"type": server_type}, "steps": []}

    try:
        port = gdb_server.start(server_type, server_args)
        result["server"]["port"] = port
        gdb_client.start_gdb()
        result["connect"] = gdb_client.connect("localhost", port)
        if halt:
            result["halt"] = gdb_client.execute_cli_command("monitor halt", timeout_sec=5.0)
        result["cpuid"] = _read_word(gdb_client, CPUID_ADDRESS)
        result["dbgmcu_idcode"] = _read_word(gdb_client, DBGMCU_IDCODE_ADDRESS)
        result["resume"] = gdb_client.execute_cli_command("monitor resume", timeout_sec=2.0)
        result["ok"] = True
        return result
    finally:
        gdb_client.stop_gdb()
        gdb_server.stop()


def _read_word(gdb_client, address: str) -> dict:
    response = gdb_client.read_typed_memory(address, width_bits=32, count=1)
    return {
        "address": address,
        "raw_response": response,
    }
```

- [ ] **Step 4: Run HIL unit test and verify GREEN**

Run:

```bash
python -m pytest tests/test_hil_smoke.py -q
```

Expected: PASS.

- [ ] **Step 5: Add skipped-by-default real HIL test**

Create `tests/hil/test_hil_smoke.py`:

```python
import os

import pytest

from mcp_server.debug_config import load_debug_config
from mcp_server.gdb_client import GdbClientManager
from mcp_server.gdb_manager import GdbServerManager
from mcp_server.hil_smoke import run_hil_smoke


pytestmark = pytest.mark.hil


@pytest.mark.skipif(os.environ.get("STM32_GDB_MCP_HIL") != "1", reason="Set STM32_GDB_MCP_HIL=1 to run hardware tests")
def test_real_hardware_hil_smoke():
    config_path = os.environ.get("STM32_GDB_MCP_HIL_CONFIG", "examples/configs/stm32l431_openocd.yaml")
    loaded = load_debug_config(config_path)
    assert loaded["validation"]["valid"], loaded["validation"]

    result = run_hil_smoke(loaded["config"], GdbServerManager(), GdbClientManager())

    assert result["ok"] is True
```

- [ ] **Step 6: Register pytest marker**

Modify `pyproject.toml`:

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
markers = [
    "hil: hardware-in-the-loop tests that require a connected STM32 target",
]
```

- [ ] **Step 7: Add STM32L431 OpenOCD config example**

Create `examples/configs/stm32l431_openocd.yaml`:

```yaml
mcu: STM32L431CCT6
board: Custom STM32L431 board
probe: stlink
server_type: openocd
server_args:
  - -f
  - interface/stlink.cfg
  - -f
  - target/stm32l4x.cfg
project_root: .
reset:
  strategy: under_reset
  halt: true
hil:
  halt: true
  read_cpuid: true
  read_dbgmcu_idcode: true
  flash: false
uart:
  port: COM3
  baudrate: 115200
  timeout: 0.1
notes: "STM32L431CCT6 OpenOCD/ST-Link smoke config. / STM32L431CCT6 OpenOCD/ST-Link 烟测配置。"
```

- [ ] **Step 8: Update HIL workflow**

Modify `.github/workflows/hil.yml`:

- Set default `config_path` to `examples/configs/stm32l431_openocd.yaml`.
- Add env:

```yaml
env:
  STM32_GDB_MCP_HIL: "1"
  STM32_GDB_MCP_HIL_CONFIG: ${{ inputs.config_path }}
```

- Run:

```yaml
- name: Run HIL smoke tests
  run: python -m pytest -q tests/hil -m hil
```

- [ ] **Step 9: Run tests**

Run:

```bash
python -m pytest tests/test_hil_smoke.py tests/hil/test_hil_smoke.py -q
python -m pytest -q
```

Expected: Unit test passes; real HIL test is skipped during normal run unless env is set.

- [ ] **Step 10: Commit Task 4**

```bash
git add src/mcp_server/hil_smoke.py tests/test_hil_smoke.py tests/hil/test_hil_smoke.py pyproject.toml .github/workflows/hil.yml examples/configs/stm32l431_openocd.yaml docs/hil-validation.md
git commit -m "Add HIL smoke regression tests"
```

---

### Task 5: STM32L431 Example Firmware

**Files:**
- Create: `examples/firmware/stm32l431_blinky/CMakeLists.txt`
- Create: `examples/firmware/stm32l431_blinky/README.md`
- Create: `examples/firmware/stm32l431_blinky/linker/STM32L431CCTx_FLASH.ld`
- Create: `examples/firmware/stm32l431_blinky/src/main.c`
- Create: `examples/firmware/stm32l431_blinky/src/startup_stm32l431xx.c`
- Create: `examples/firmware/stm32l431_blinky/cmake/arm-none-eabi.cmake`

- [ ] **Step 1: Create minimal firmware files**

Add a tiny bare-metal example:

- Vector table and reset handler in `startup_stm32l431xx.c`
- Stack top at end of SRAM
- Linker script with 256 KiB flash and 64 KiB SRAM
- `main.c` toggles a GPIO or increments a volatile heartbeat counter if board LED pin is unknown
- CMake uses `arm-none-eabi-gcc`, `arm-none-eabi-objcopy`, and emits `.elf`, `.bin`, `.map`

- [ ] **Step 2: Add firmware README**

Document bilingual build commands:

```bash
cmake -S examples/firmware/stm32l431_blinky -B build/stm32l431_blinky -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l431_blinky/cmake/arm-none-eabi.cmake
cmake --build build/stm32l431_blinky
```

State clearly that flashing is manual/opt-in.

- [ ] **Step 3: Optional local build check**

Run only if CMake and `arm-none-eabi-gcc` are available:

```bash
cmake -S examples/firmware/stm32l431_blinky -B build/stm32l431_blinky -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l431_blinky/cmake/arm-none-eabi.cmake
cmake --build build/stm32l431_blinky
```

Expected: Builds `.elf`; if toolchain is absent, record skip reason.

- [ ] **Step 4: Commit Task 5**

```bash
git add examples/firmware/stm32l431_blinky
git commit -m "Add STM32L431 example firmware"
```

---

### Task 6: Documentation, README Current Limits Removal, and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/hil-validation.md`
- Modify: `docs/release.md`
- Modify: `CHANGELOG.md`
- Modify: `.github/workflows/hil.yml`
- Test: all tests

- [ ] **Step 1: Replace README Current Limits**

Replace `## Current Limits / 当前限制` with `## Completed Capabilities / 已完成能力` and document:

- reset strategy profiles
- HIL regression smoke tests
- SWO/ITM process log capture
- STM32L431 example firmware
- stable JSON envelopes

Keep any remaining future work in a smaller `Roadmap / 路线图` section.

- [ ] **Step 2: Update bilingual docs**

Update:

- `docs/hil-validation.md`: env vars, STM32L431 config, non-destructive default, optional flash.
- `docs/release.md`: include HIL marker command and optional firmware build.
- `CHANGELOG.md`: add an Unreleased section for the new capabilities.

- [ ] **Step 3: Run YAML parse checks**

Run:

```powershell
@'
import yaml
from pathlib import Path
for path in [
    Path('.github/ISSUE_TEMPLATE/bug_report.yml'),
    Path('.github/ISSUE_TEMPLATE/feature_request.yml'),
    Path('.github/workflows/hil.yml'),
    Path('examples/configs/stm32f4_jlink.yaml'),
    Path('examples/configs/stm32l431_openocd.yaml'),
]:
    with path.open(encoding='utf-8') as f:
        yaml.safe_load(f)
    print(f'OK {path}')
'@ | python -
```

Expected: all files print `OK`.

- [ ] **Step 4: Run full quality gate**

Run:

```bash
python -m ruff check . --output-format=concise
python -m pytest -q
python -m compileall src tests
python -m build
```

Expected: all commands exit 0. HIL tests are skipped unless `STM32_GDB_MCP_HIL=1`.

- [ ] **Step 5: Optional real STM32L431 HIL smoke**

Only with connected hardware and explicit env:

```powershell
$env:STM32_GDB_MCP_HIL = "1"
$env:STM32_GDB_MCP_HIL_CONFIG = "examples/configs/stm32l431_openocd.yaml"
python -m pytest -q tests/hil -m hil
```

Expected: Connects to STM32L431, reads CPUID and DBGMCU IDCODE, resumes target, and stops the debug session.

- [ ] **Step 6: Clean generated artifacts**

Remove generated local artifacts after verification:

```powershell
$root = (Resolve-Path '.').Path
$targets = @('.pytest_cache', 'build', 'dist', 'src\mcp_server\__pycache__', 'tests\__pycache__', 'src\stm32_gdb_mcp.egg-info', 'stm32_gdb_mcp.egg-info')
foreach ($rel in $targets) {
    $candidate = Join-Path $root $rel
    if (Test-Path -LiteralPath $candidate) {
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        if (-not $resolved.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove outside workspace: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
```

- [ ] **Step 7: Commit Task 6**

```bash
git add README.md docs/hil-validation.md docs/release.md CHANGELOG.md .github/workflows/hil.yml
git commit -m "Document completed current limit capabilities"
```

- [ ] **Step 8: Push and verify repository state**

```bash
git push
git status -sb
gh repo view Zeraissh/stm32-gdb-mcp --json visibility,url
```

Expected: branch is synced with `origin/main`; repository visibility remains `PRIVATE`.
