from pathlib import Path

import pytest

from mcp_server.debug_config import load_debug_config
from mcp_server.hil_smoke import run_hil_smoke


class FakeToolCaller:
    def __init__(self, identity_ok=True):
        self.calls = []
        self.identity_ok = identity_ok

    async def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "start_debug_session":
            data = {"server_type": "openocd", "port": 3333}
        elif name == "self_check":
            data = {
                "ok": self.identity_ok,
                "cpuid": "0x410fc241",
                "dbgmcu_idcode": "0x10006435",
                "core": "Cortex-M4",
                "device": "STM32L43x/44x",
                "checks": [
                    {"name": "byte_order", "ok": True},
                    {"name": "cortex_m_core", "ok": True},
                    {"name": "dbgmcu_dev_id", "ok": self.identity_ok},
                ],
            }
        else:
            data = {"message": name}
        return {"ok": True, "data": data}


def test_run_hil_smoke_uses_public_tools_and_validates_identity():
    caller = FakeToolCaller()

    result = run_hil_smoke(
        {
            "mcu": "STM32L431CCT6",
            "server_type": "openocd",
            "server_args": ["-f", "target/stm32l4x.cfg"],
            "hil": {
                "halt": True,
                "flash": False,
                "expected_core": "Cortex-M4",
                "expected_device": "STM32L43",
            },
        },
        caller,
    )

    assert result["ok"] is True
    assert result["identity"]["cpuid"] == "0x410fc241"
    assert result["identity"]["core"] == "Cortex-M4"
    assert result["identity"]["device"] == "STM32L43x/44x"
    assert all(check["ok"] for check in result["hil_checks"])
    assert caller.calls == [
        (
            "start_debug_session",
            {"server_type": "openocd", "server_args": ["-f", "target/stm32l4x.cfg"]},
        ),
        ("self_check", {"expected_family": "STM32L431CCT6", "halt": True}),
        ("continue_execution", {}),
        ("stop_debug_session", {}),
    ]
    assert not {"flash_firmware", "flash_and_run"} & {name for name, _ in caller.calls}


def test_run_hil_smoke_rejects_wrong_device_and_still_cleans_up():
    caller = FakeToolCaller(identity_ok=False)

    result = run_hil_smoke(
        {
            "mcu": "STM32L431CCT6",
            "server_type": "openocd",
            "server_args": ["-f", "target/stm32l4x.cfg"],
            "hil": {"halt": True},
        },
        caller,
    )

    assert result["ok"] is False
    assert [name for name, _ in caller.calls][-2:] == ["continue_execution", "stop_debug_session"]


def test_run_hil_smoke_reports_core_mismatch():
    caller = FakeToolCaller()

    result = run_hil_smoke(
        {
            "mcu": "STM32L151",
            "server_type": "openocd",
            "server_args": ["-f", "target/stm32l1.cfg"],
            "hil": {"halt": True, "expected_core": "Cortex-M3"},
        },
        caller,
    )

    assert result["ok"] is False
    core_check = next(check for check in result["hil_checks"] if check["name"] == "core")
    assert core_check == {
        "name": "core", "ok": False, "expected": "Cortex-M3", "actual": "Cortex-M4",
    }


@pytest.mark.parametrize(
    ("board", "core", "target"),
    [
        ("l151", "Cortex-M3", "target/stm32l1.cfg"),
        ("l431", "Cortex-M4", "target/stm32l4x.cfg"),
        ("u535", "Cortex-M33", "target/stm32u5x.cfg"),
    ],
)
def test_hil_board_matrix_configs_are_valid_and_non_flashing(board, core, target):
    root = Path(__file__).resolve().parents[1]
    loaded = load_debug_config(root / "examples" / "configs" / f"stm32{board}_openocd.yaml")

    assert loaded["validation"]["valid"], loaded["validation"]
    config = loaded["config"]
    assert config["hil"]["flash"] is False
    assert config["hil"]["expected_core"] == core
    assert target in config["server_args"]


def test_hil_workflow_selects_each_board_and_uploads_evidence():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "hil.yml").read_text(encoding="utf-8")

    for board in ("l151", "l431", "u535"):
        assert f"          - {board}" in workflow
    assert "STM32_GDB_MCP_HIL_REPORT" in workflow
    assert "--junitxml=" in workflow
    assert "if: always()" in workflow
    assert "actions/upload-artifact" in workflow
