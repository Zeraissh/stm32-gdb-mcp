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
            "hil": {"halt": True, "flash": False},
        },
        caller,
    )

    assert result["ok"] is True
    assert result["identity"]["cpuid"] == "0x410fc241"
    assert result["identity"]["core"] == "Cortex-M4"
    assert result["identity"]["device"] == "STM32L43x/44x"
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
