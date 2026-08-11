"""The [hub] extra is OPTIONAL: everything must degrade cleanly without it.

This is the load-bearing test file for the whole feature. The hub is a nice-to-
have on one bench and absent everywhere else, so the failure mode that actually
matters is not "the hub misbehaved" but "a user who never installed the extra
finds their debugging broken". Every test here runs with the vendor import
forced to fail.
"""

import asyncio
import json

import pytest

import mcp_server.hub as hub_module
from mcp_server.hub import HubManager, HubUnavailableError
from mcp_server.server import handle_call_tool


@pytest.fixture
def extra_missing(monkeypatch):
    """Force the single vendor-import seam to behave as if nothing is installed."""
    def _boom():
        raise ImportError("No module named 'smartusbhub'")

    monkeypatch.setattr(hub_module, "_import_backend", _boom)
    return _boom


def _payload(result):
    return json.loads(result[0].text)


def test_importing_the_server_never_requires_the_extra(extra_missing):
    # The import already happened at module scope above; the point is that it
    # neither raised nor pulled the vendor package in.
    import mcp_server.server as server_module

    assert server_module.hub_manager is not None


def test_manager_reports_unavailable_with_an_install_hint(extra_missing):
    with pytest.raises(HubUnavailableError, match="pip install 'stm32-gdb-mcp\\[hub\\]'"):
        HubManager().describe()


def test_is_available_is_false_not_an_exception(extra_missing):
    assert HubManager().is_available() is False


def test_describe_hub_returns_a_clean_error_envelope(extra_missing, default_session):
    payload = _payload(asyncio.run(handle_call_tool("hub", {"action": "describe"})))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "hub_unavailable"
    assert "smartusbhub is not installed" in payload["error"]["message"]
    assert "stm32-gdb-mcp[hub]" in payload["error"]["message"]


def test_power_and_data_also_degrade_cleanly(extra_missing, default_session):
    for action in ("power", "data"):
        payload = _payload(asyncio.run(handle_call_tool(
            "hub", {"action": action, "state": "off", "channel": 1, "confirm": True})))
        assert payload["ok"] is False, action
        assert payload["error"]["code"] == "hub_unavailable", action


def test_hub_tools_never_raise_out_of_the_handler(extra_missing, default_session):
    # A traceback escaping the handler would be classified as a generic
    # tool_execution_error and read as a server bug rather than a missing extra.
    result = asyncio.run(handle_call_tool("hub", {"action": "describe"}))
    assert _payload(result)["error"]["code"] == "hub_unavailable"


def test_close_is_safe_when_nothing_was_ever_connected(extra_missing):
    assert HubManager().close()["restored_channels"] == []


def test_shutdown_does_not_raise_without_a_hub(extra_missing):
    import mcp_server.server as server_module

    server_module._shutdown_all_sessions()  # must not raise


def test_recover_session_is_unaffected_by_a_missing_hub(extra_missing, default_session):
    # No prior session -> the same error as before the hub existed, with no hub
    # code on the path.
    payload = _payload(asyncio.run(handle_call_tool("recover_session", {})))
    assert payload["ok"] is False
    assert "hub" not in payload["error"]["message"].lower()


def test_debug_profile_still_accepts_a_hub_block_without_the_extra(extra_missing, default_session):
    payload = _payload(asyncio.run(handle_call_tool(
        "debug_profile", {"action": "set", "hub": {"channel": 2}})))

    assert payload["ok"] is True
    assert payload["data"]["hub"] == {"channel": 2}
