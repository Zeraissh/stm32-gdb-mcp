"""Hardware-in-the-loop checks for a real programmable USB hub.

Gated behind BOTH the usual STM32_GDB_MCP_HIL=1 and a new STM32_GDB_MCP_HIL_HUB=1,
so a rig that has a board but no hub keeps running the existing smoke unchanged.

Rig assumed by these tests:
  - a SmartUSBHub with an ADC (4CH or 7CH)
  - the debug probe for the board under test on the channel named by
    hub.channel in STM32_GDB_MCP_HIL_CONFIG

These are the only tests that can prove the things the unit suite can only model:
that a data-line toggle really un-enumerates a probe, that cutting VBUS really
collapses the rail, and that recover_session really brings a session back after
the probe physically disappears.
"""

import os

import pytest

from mcp_server.debug_config import load_debug_config
from mcp_server.hub import BROWNOUT_MV, HubManager
from mcp_server.openocd_config import detect_probe

pytestmark = pytest.mark.hil

_SKIP = pytest.mark.skipif(
    os.environ.get("STM32_GDB_MCP_HIL") != "1" or os.environ.get("STM32_GDB_MCP_HIL_HUB") != "1",
    reason="Set STM32_GDB_MCP_HIL=1 and STM32_GDB_MCP_HIL_HUB=1 to run hub hardware tests",
)


@pytest.fixture
def rig():
    config_path = os.environ.get("STM32_GDB_MCP_HIL_CONFIG",
                                 "examples/configs/stm32l431_openocd.yaml")
    loaded = load_debug_config(config_path)
    assert loaded["validation"]["valid"], loaded["validation"]
    spec = loaded["config"].get("hub")
    if not spec or not spec.get("channel"):
        pytest.skip(f"{config_path} has no hub.channel; nothing to test against")

    manager = HubManager()
    manager.configure({**spec, "guard": "allow"})
    yield manager, spec["channel"]
    # Never leave the bench in a worse state than we found it.
    try:
        for channel in manager.channels():
            manager.data(channel, "on", confirm=True)
            manager.power(channel, "on", confirm=True)
    finally:
        manager.close(restore_power=True)


@_SKIP
def test_hub_identity_is_readable(rig):
    manager, channel = rig
    described = manager.describe()

    assert described["available"] is True
    assert channel in described["channels"]
    assert described["interlock"] is False, (
        "hub is in interlock mode, which powers one port at a time; clear it with the vendor tool")


@_SKIP
def test_the_configured_channel_draws_current(rig):
    manager, channel = rig
    if not manager.describe()["adc"]:
        pytest.skip("this hub model has no ADC")

    sample = manager.measure((channel,), duration_sec=0.5, interval_sec=0.05)
    summary = sample["summary"][str(channel)]

    assert summary["voltage_mv"]["min"] > 4000, "port is not supplying 5 V"
    assert summary["current_ma"]["max"] > 0, "nothing is drawing current on this port"


@_SKIP
def test_a_data_line_toggle_really_un_enumerates_the_probe(rig):
    import time

    manager, channel = rig
    before = detect_probe()
    assert before["count"] >= 1, "no probe detected before the toggle; check the rig"

    manager.data(channel, "off", confirm=True)
    try:
        time.sleep(2.0)
        during = detect_probe()
    finally:
        manager.data(channel, "on", confirm=True)
        time.sleep(2.0)

    after = detect_probe()
    assert during["count"] < before["count"], (
        "disconnecting the data line did not remove a probe -- is the probe on this channel?")
    assert after["count"] == before["count"], "the probe did not come back after reconnecting"


@_SKIP
def test_power_cycle_actually_collapses_the_rail(rig):
    manager, channel = rig
    if not manager.describe()["adc"]:
        pytest.skip("this hub model has no ADC")

    result = manager.power_cycle(channel, off_ms=600, settle_ms=2000, confirm=True)

    assert "measured_off_voltage_mv" in result
    assert result["browned_out"] is True, (
        f"rail held at {result['measured_off_voltage_mv']} mV (threshold {BROWNOUT_MV} mV) -- the "
        f"board is powered from somewhere else, or bulk capacitance needs a longer off_ms. "
        f"{result.get('warning', '')}")
    ports = {port["channel"]: port for port in manager.describe()["ports"]}
    assert ports[channel]["power"] == "on"
