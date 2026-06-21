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
