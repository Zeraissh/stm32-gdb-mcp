import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10; the dev "build" dependency provides tomli.
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_BILINGUAL_DOCS = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/TROUBLESHOOTING.md",
    "docs/hil-validation.md",
    "docs/install-ides.md",
    "docs/release.md",
    "skills/stm32-debug/SKILL.md",
    "skills/stm32-debug/reference/tool-map.md",
    "skills/stm32-instrument/SKILL.md",
    "examples/firmware/stm32l431_assertdemo/README.md",
    "examples/firmware/stm32l431_blinky/README.md",
    "examples/firmware/stm32l431_faultdemo/README.md",
    "examples/firmware/stm32l431_heapdemo/README.md",
    "examples/firmware/stm32l431_periphdemo/README.md",
    "examples/firmware/stm32l431_stackdemo/README.md",
    "examples/prompts/debug_hardfault.md",
    "examples/prompts/freertos_hang.md",
]


@pytest.mark.parametrize(
    ("path", "args"),
    [
        ("setup_env.py", ["--help"]),
        ("scripts/install_mcp.py", ["--help"]),
        ("scripts/deploy.py", ["--help"]),
        ("scripts/check_dist_contents.py", ["--help"]),
    ],
)
def test_source_entry_points_work_before_package_install(path, args):
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / path), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_codex_fallback_from_clean_source_is_valid_toml():
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "scripts" / "install_mcp.py"), "codex", "--print"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    entry = tomllib.loads(result.stdout)["mcp_servers"]["stm32-gdb-mcp"]
    assert entry["command"]
    assert entry["env"] == {"STM32_GDB_MCP_COMPACT": "1"}


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".github/workflows/release.yml"])
def test_workflows_gate_clean_source_and_clean_wheel(path):
    workflow = (ROOT / path).read_text(encoding="utf-8")

    assert "Smoke source entry points before installation" in workflow
    assert "python -S setup_env.py --help" in workflow
    assert "scripts/install_mcp.py codex --print" in workflow
    assert "Smoke clean wheel install" in workflow
    assert "stm32-gdb-mcp-check-env" in workflow
    assert "stm32-gdb-mcp-install" in workflow
    assert "stm32-gdb-mcp-deploy" in workflow


def test_ci_covers_supported_windows_pythons_and_linux():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for version in ("3.10", "3.11", "3.12", "3.13"):
        assert f'python-version: "{version}"' in workflow
    assert "ubuntu-latest" in workflow
    assert "Smoke clean wheel install (POSIX)" in workflow


def test_release_publishes_checksummed_github_assets_after_pypi():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "SHA256SUMS.txt" in workflow
    assert "sha256sum --check" in workflow
    assert "needs: [build, publish-to-pypi]" in workflow
    assert "gh release create" in workflow
    assert "gh release upload" in workflow


@pytest.mark.parametrize("path", PUBLIC_BILINGUAL_DOCS)
def test_public_explanatory_docs_are_bilingual(path):
    text = (ROOT / path).read_text(encoding="utf-8")

    assert re.search(r"[A-Za-z]", text)
    assert len(re.findall(r"[\u4e00-\u9fff]", text)) >= 20
    if path != "CHANGELOG.md":
        headings = []
        in_fence = False
        for line in text.splitlines():
            if line.startswith("```"):
                in_fence = not in_fence
            elif not in_fence and re.match(r"^#{1,6} ", line):
                headings.append(line)
        assert all(re.search(r"[\u4e00-\u9fff]", heading) for heading in headings), path


@pytest.mark.parametrize(
    "path",
    ["README.md", "docs/TROUBLESHOOTING.md", "docs/install-ides.md", "hooks/session_start.py"],
)
def test_public_guidance_has_no_approximate_tool_counts(path):
    text = (ROOT / path).read_text(encoding="utf-8")

    assert not re.search(r"(?i)\b(?:compact|full)[^\n]{0,30}(?:~|≈)\s*\d+", text)


def test_dist_audit_rejects_local_state_files(tmp_path):
    from mcp_server.dist_audit import find_forbidden_members

    archive = tmp_path / "sample.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        local_state = tmp_path / "codegraph.db"
        local_state.write_text("local index", encoding="utf-8")
        tar.add(local_state, arcname="stm32_gdb_mcp-0.3.0/.codegraph/codegraph.db")

        package_file = tmp_path / "server.py"
        package_file.write_text("print('ok')\n", encoding="utf-8")
        tar.add(package_file, arcname="stm32_gdb_mcp-0.3.0/src/mcp_server/server.py")

    forbidden = find_forbidden_members([archive])

    assert forbidden == {
        str(archive): ["stm32_gdb_mcp-0.3.0/.codegraph/codegraph.db"],
    }


def test_dist_audit_expands_globs_for_powershell(tmp_path):
    from mcp_server.dist_audit import expand_archive_args

    archive = tmp_path / "sample.whl"
    archive.write_bytes(b"not inspected here")

    assert expand_archive_args([tmp_path / "*"]) == [archive]


def test_pyproject_defines_release_console_scripts():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]

    assert scripts["stm32-gdb-mcp"] == "mcp_server.server:cli_main"
    assert scripts["stm32-gdb-mcp-check-env"] == "mcp_server.env_check:main"
    assert scripts["stm32-gdb-mcp-install"] == "mcp_server.install_mcp:main"
    assert scripts["stm32-gdb-mcp-deploy"] == "mcp_server.deploy:main"


def test_release_versions_are_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_init = (ROOT / "src" / "mcp_server" / "__init__.py").read_text(encoding="utf-8")
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert project["project"]["version"] == "0.14.0"
    assert '__version__ = "0.14.0"' in package_init
    # The plugin version moves with the server: marketplace.json ships the plugin
    # from source "./", so a server release users are meant to receive needs it.
    assert plugin["version"] == "0.10.0"
    assert marketplace["plugins"][0]["version"] == plugin["version"]
    assert "## [0.14.0] - 2026-08-15" in changelog


def test_sdist_excludes_local_workspace_state():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excludes = set(data["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"])

    assert ".codegraph/**" in excludes
    assert ".vs/**" in excludes
    assert ".claude/**" in excludes


def test_plugin_manifest_and_session_start_hook_are_loadable():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    server = plugin["mcpServers"]["stm32-gdb-mcp"]
    assert server["command"] == "python"
    assert server["args"] == ["-m", "mcp_server.server"]
    assert server["env"]["STM32_GDB_MCP_COMPACT"] == "1"
    assert Path("skills/stm32-debug/SKILL.md").exists()
    assert Path("skills/stm32-instrument/SKILL.md").exists()

    command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "hooks/session_start.py" in command.replace("\\", "/")

    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "session_start.py")],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "STM32 Debug Kit active" in context
    assert "self_check" in context


def test_server_metadata_falls_back_to_package_version(monkeypatch):
    from mcp_server import server_metadata

    def fail_git(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(server_metadata.subprocess, "run", fail_git)
    monkeypatch.setattr(server_metadata.metadata, "version", lambda name: "9.8.7")

    assert server_metadata.mcp_version() == "9.8.7"


def test_tool_surface_keeps_compact_core_and_merged_families():
    from mcp.types import Tool

    from mcp_server.tool_surface import advertised_tools

    base = [
        Tool(name="set_breakpoint", description="", inputSchema={}),
        Tool(name="start_debug_session", description="", inputSchema={}),
    ]

    advertised = advertised_tools(base)
    names = {tool.name for tool in advertised}

    assert "set_breakpoint" not in names
    assert "start_debug_session" in names
    assert "breakpoint" in names


# FIX 1 (mcp_info)
def test_server_info_separates_the_release_version_from_the_commit(monkeypatch):
    from mcp_server import __version__, server_metadata

    monkeypatch.setattr(server_metadata, "_git_commit", lambda root: "deadbee")

    info = server_metadata.server_info()

    # mcp_version() collapses these into one string the caller cannot decode;
    # "which build is running?" needs both answers side by side.
    assert info["name"] == "stm32-gdb-mcp"
    assert info["version"] == __version__
    assert info["commit"] == "deadbee"
    assert Path(info["install_root"]).is_dir()


# FIX 1 (mcp_info)
def test_server_info_omits_commit_outside_a_git_checkout(monkeypatch):
    from mcp_server import server_metadata

    monkeypatch.setattr(server_metadata, "_git_commit", lambda root: None)

    info = server_metadata.server_info()

    assert "commit" not in info
    assert info["version"]


# FIX 1 (mcp_info)
def test_git_commit_is_not_probed_outside_a_checkout(monkeypatch, tmp_path):
    from mcp_server import server_metadata

    def must_not_run(*args, **kwargs):
        raise AssertionError("git must not be invoked outside a checkout")

    monkeypatch.setattr(server_metadata.subprocess, "run", must_not_run)

    # Without the .git guard, `git -C <site-packages>` walked upward and reported
    # an unrelated repository's HEAD as this server's version.
    assert server_metadata._git_commit(str(tmp_path)) is None

