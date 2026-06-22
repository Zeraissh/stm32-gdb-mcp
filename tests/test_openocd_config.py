import os

import pytest

from mcp_server.openocd_config import find_openocd_scripts, suggest_server_args


def test_l431_stlink_resolves_to_l4_target():
    r = suggest_server_args("STM32L431", "stlink")
    assert r["server_args"] == ["-f", "interface/stlink.cfg", "-f", "target/stm32l4x.cfg"]
    assert r["interface"] == "stlink.cfg"
    assert r["target"] == "stm32l4x.cfg"


def test_various_families_and_probes():
    assert suggest_server_args("STM32F407", "stlink")["target"] == "stm32f4x.cfg"
    assert suggest_server_args("STM32H743", "jlink")["target"] == "stm32h7x.cfg"
    assert suggest_server_args("STM32G431", "cmsis-dap")["target"] == "stm32g4x.cfg"
    assert suggest_server_args("stm32f103", "stlink")["target"] == "stm32f1x.cfg"
    assert suggest_server_args("STM32L431", "jlink")["interface"] == "jlink.cfg"


def test_unknown_family_or_probe_raises():
    with pytest.raises(ValueError, match="family"):
        suggest_server_args("STM32Z999", "stlink")
    with pytest.raises(ValueError, match="probe"):
        suggest_server_args("STM32L431", "wiggler")


def test_find_openocd_scripts_locates_bundled_dir(tmp_path):
    # Simulate the xpack layout: <base>/bin/openocd(.exe) + <base>/openocd/scripts/
    base = tmp_path / "xpack-openocd"
    (base / "bin").mkdir(parents=True)
    exe = base / "bin" / "openocd.exe"
    exe.write_text("")
    scripts = base / "openocd" / "scripts" / "interface"
    scripts.mkdir(parents=True)
    (scripts / "stlink.cfg").write_text("")

    found = find_openocd_scripts(str(exe))

    assert found is not None
    assert os.path.isfile(os.path.join(found, "interface", "stlink.cfg"))


def test_find_openocd_scripts_returns_none_when_absent(tmp_path):
    exe = tmp_path / "bin" / "openocd.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert find_openocd_scripts(str(exe)) is None
