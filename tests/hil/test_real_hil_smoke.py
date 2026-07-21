import os

import pytest

from mcp_server.debug_config import load_debug_config
from mcp_server.hil_smoke import run_hil_smoke

pytestmark = pytest.mark.hil


@pytest.mark.skipif(os.environ.get("STM32_GDB_MCP_HIL") != "1", reason="Set STM32_GDB_MCP_HIL=1 to run hardware tests")
def test_real_hardware_hil_smoke():
    config_path = os.environ.get("STM32_GDB_MCP_HIL_CONFIG", "examples/configs/stm32l431_openocd.yaml")
    loaded = load_debug_config(config_path)
    assert loaded["validation"]["valid"], loaded["validation"]

    result = run_hil_smoke(loaded["config"])

    assert result["ok"] is True
    assert result["identity"]["cpuid"].startswith("0x41")
    assert result["identity"]["core"].startswith("Cortex-M")
    checks = {check["name"]: check["ok"] for check in result["identity"]["checks"]}
    assert checks == {"byte_order": True, "cortex_m_core": True, "dbgmcu_dev_id": True}
    assert result["expected_family"] == loaded["config"]["mcu"]
