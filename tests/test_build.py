import sys

import pytest

from mcp_server.build import (
    is_build_success,
    resolve_build_command,
    run_build,
)


def test_keil_build_uses_uv4_batch_flags():
    cmd = resolve_build_command("keil", project="fw.uvprojx", uv4_path="UV4.exe", log_path="b.log")
    assert cmd == ["UV4.exe", "-b", "fw.uvprojx", "-j0", "-o", "b.log"]


def test_keil_rebuild_uses_dash_r():
    cmd = resolve_build_command("keil", project="fw.uvprojx", rebuild=True, uv4_path="UV4.exe")
    assert cmd[:3] == ["UV4.exe", "-r", "fw.uvprojx"]


def test_cmake_build_with_target_and_config():
    cmd = resolve_build_command("cmake", build_dir="build", target="app", config="Debug")
    assert cmd == ["cmake", "--build", "build", "--target", "app", "--config", "Debug"]


def test_make_build_with_target():
    assert resolve_build_command("make", directory="proj", target="all") == ["make", "-C", "proj", "all"]


def test_custom_command_passthrough():
    assert resolve_build_command("custom", command=["scons", "-j8"]) == ["scons", "-j8"]


def test_unknown_kind_and_missing_fields_raise():
    with pytest.raises(ValueError, match="Unsupported build kind"):
        resolve_build_command("xcode")
    with pytest.raises(ValueError, match="keil"):
        resolve_build_command("keil")
    with pytest.raises(ValueError, match="cmake"):
        resolve_build_command("cmake")


def test_keil_success_treats_warnings_as_ok_but_errors_as_failure():
    # UV4 exit codes: 0 = clean, 1 = warnings, >=2 = errors.
    assert is_build_success("keil", 0) is True
    assert is_build_success("keil", 1) is True
    assert is_build_success("keil", 2) is False
    # other kinds require a clean exit
    assert is_build_success("cmake", 0) is True
    assert is_build_success("cmake", 1) is False


def test_run_build_executes_and_captures_output():
    result = run_build([sys.executable, "-c", "print('built-ok')"], timeout=30)
    assert result["returncode"] == 0
    assert "built-ok" in result["output"]


def test_run_build_isolates_stdin_so_a_reading_build_cannot_hang():
    # A build step that reads stdin must get instant EOF (stdin=DEVNULL), not block on
    # the MCP server's JSON-RPC stdin. Without isolation this could hang forever.
    result = run_build(
        [sys.executable, "-c", "import sys; sys.stdin.read(); print('done')"], timeout=10
    )
    assert result["returncode"] == 0
    assert "done" in result["output"]
