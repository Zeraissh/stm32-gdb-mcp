from mcp_server.project_inspector import inspect_project


def test_inspect_project_discovers_firmware_artifacts_and_ioc_metadata(tmp_path):
    project = tmp_path / "firmware"
    project.mkdir()
    (project / "build").mkdir()
    (project / "Core").mkdir()
    (project / "STM32F407VGTx_FLASH.ld").write_text("MEMORY {}", encoding="utf-8")
    (project / "STM32F407.svd").write_text("<device />", encoding="utf-8")
    (project / "firmware.ioc").write_text(
        "\n".join([
            "Mcu.Name=STM32F407VGTx",
            "Mcu.Package=LQFP100",
            "ProjectManager.ProjectName=MotorCtrl",
            "ProjectManager.TargetToolchain=STM32CubeIDE",
        ]),
        encoding="utf-8",
    )
    (project / "build" / "motor.elf").write_bytes(b"\x7fELF")
    (project / "build" / "motor.map").write_text("Memory Configuration", encoding="utf-8")
    (project / ".venv").mkdir()
    (project / ".venv" / "ignored.elf").write_bytes(b"\x7fELF")

    result = inspect_project(str(project))

    assert result["project_root"] == str(project)
    assert result["mcu"] == "STM32F407VGTx"
    assert result["project_name"] == "MotorCtrl"
    assert result["toolchain"] == "STM32CubeIDE"
    assert result["files"]["elf"] == [str(project / "build" / "motor.elf")]
    assert result["files"]["map"] == [str(project / "build" / "motor.map")]
    assert result["files"]["linker_script"] == [str(project / "STM32F407VGTx_FLASH.ld")]
    assert result["files"]["svd"] == [str(project / "STM32F407.svd")]
    assert result["files"]["ioc"] == [str(project / "firmware.ioc")]


def test_inspect_project_uses_profile_paths_when_no_root_scan(tmp_path):
    elf = tmp_path / "app.elf"
    svd = tmp_path / "device.svd"
    elf.write_bytes(b"\x7fELF")
    svd.write_text("<device />", encoding="utf-8")

    result = inspect_project(None, {"elf_path": str(elf), "svd_path": str(svd), "mcu": "STM32G474RE"})

    assert result["mcu"] == "STM32G474RE"
    assert result["files"]["elf"] == [str(elf)]
    assert result["files"]["svd"] == [str(svd)]
