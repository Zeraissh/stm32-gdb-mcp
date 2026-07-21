from pathlib import Path

from mcp_server import deploy


def test_deploy_stops_when_server_installation_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, "ensure_server_installed", lambda no_install: False)

    result = deploy.main(["--project", str(tmp_path), "--no-rules"])

    assert result == 1


def test_detect_project_reuses_ioc_metadata_and_single_elf(tmp_path):
    (tmp_path / "board.ioc").write_text(
        "Mcu.Name=STM32L431CCT6\nProjectManager.ProjectName=demo\n",
        encoding="utf-8",
    )
    build = tmp_path / "build"
    build.mkdir()
    (build / "demo.elf").write_bytes(b"")

    info = deploy.detect_project(str(tmp_path))

    assert info["mcu"] == "STM32L431CCT6"
    assert info["elf"] == "build/demo.elf"
    assert info["elf_candidates"] == ["build/demo.elf"]


def test_detect_project_does_not_guess_between_multiple_elfs(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    (build / "app.elf").write_bytes(b"")
    (build / "bootloader.elf").write_bytes(b"")

    info = deploy.detect_project(str(tmp_path))

    assert "elf" not in info
    assert info["elf_candidates"] == ["build/app.elf", "build/bootloader.elf"]


def test_detect_project_keeps_openocd_and_debug_config_discovery(tmp_path):
    (tmp_path / "openocd.cfg").write_text(
        "source [find interface/stlink.cfg]\nsource [find target/stm32l4x.cfg]\n",
        encoding="utf-8",
    )
    config_dir = tmp_path / "mcp"
    config_dir.mkdir()
    (config_dir / "board_openocd.yaml").write_text("mcu: STM32L431CCT6\n", encoding="utf-8")

    info = deploy.detect_project(str(tmp_path))

    assert info["debug_config"] == "mcp/board_openocd.yaml"
    assert info["server_args"] == '["-f","interface/stlink.cfg","-f","target/stm32l4x.cfg"]'
    assert Path(info["project_root"]) == tmp_path.resolve()
