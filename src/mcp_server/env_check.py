from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from . import __version__ as MODULE_VERSION

SERVER_NAME = "stm32-gdb-mcp"
CONSOLE_SCRIPTS = (
    SERVER_NAME,
    f"{SERVER_NAME}-check-env",
    f"{SERVER_NAME}-install",
    f"{SERVER_NAME}-deploy",
)

GDB_ENV_VAR = "STM32_GDB_MCP_GDB"
# gdb-multiarch is what Debian/Ubuntu ship instead of arm-none-eabi-gdb; it debugs Cortex-M fine.
GDB_FALLBACKS = ("gdb-multiarch",)

GDB: dict[str, Any] = {
    "executable": "arm-none-eabi-gdb",
    "name": "ARM GNU Toolchain (GDB)",
    "install_guide": (
        "Download from https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads "
        "and add the 'bin' folder to PATH."
    ),
    "install_commands": {
        "windows": ["scoop bucket add extras", "scoop install gcc-arm-none-eabi"],
        "macos": ["brew install --cask gcc-arm-embedded"],
        "linux": [
            "sudo apt install gdb-multiarch        # Debian/Ubuntu",
            "sudo pacman -S arm-none-eabi-gdb      # Arch",
        ],
    },
}

BACKENDS: dict[str, dict[str, Any]] = {
    "openocd": {
        "executable": "openocd",
        "name": "OpenOCD",
        "install_guide": (
            "Download xPack OpenOCD from https://github.com/xpack-dev-tools/openocd-xpack/releases, "
            "extract it, and add the 'bin' folder to PATH."
        ),
        "install_commands": {
            "windows": ["scoop install openocd"],
            "macos": ["brew install open-ocd"],
            "linux": [
                "sudo apt install openocd              # Debian/Ubuntu",
                "sudo pacman -S openocd                # Arch",
            ],
        },
    },
    "jlink": {
        "executable": "JLinkGDBServerCL",
        "name": "J-Link GDB Server",
        "install_guide": (
            "Download the J-Link Software and Documentation Pack from "
            "https://www.segger.com/downloads/jlink/ and add the installation folder to PATH."
        ),
        "install_commands": {
            "macos": ["brew install --cask segger-jlink"],
        },
    },
    "stlink": {
        "executable": "st-util",
        "name": "ST-LINK GDB Server (st-link tools)",
        "install_guide": "Download from https://github.com/stlink-org/stlink/releases, extract, and add the 'bin' folder to PATH.",
        "install_commands": {
            "windows": ["scoop install stlink"],
            "macos": ["brew install stlink"],
            "linux": [
                "sudo apt install stlink-tools         # Debian/Ubuntu",
                "sudo pacman -S stlink                 # Arch",
            ],
        },
    },
}


def _which(exe: str) -> str | None:
    if exe == "JLinkGDBServerCL":
        return shutil.which("JLinkGDBServerCL") or shutil.which("JLinkGDBServer") or shutil.which("jlinkgdbserver")
    return shutil.which(exe)


def host_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def install_commands(info: dict, platform: str | None = None) -> list[str]:
    return list(info.get("install_commands", {}).get(platform or host_platform(), []))


def resolve_gdb() -> str | None:
    """The GDB the server will run: $STM32_GDB_MCP_GDB, else arm-none-eabi-gdb, else gdb-multiarch."""
    override = os.environ.get(GDB_ENV_VAR)
    if override:
        return _which(override)
    for exe in (GDB["executable"], *GDB_FALLBACKS):
        if path := _which(exe):
            return path
    return None


def installation_report() -> dict:
    try:
        distribution_version = metadata.version(SERVER_NAME)
    except metadata.PackageNotFoundError:
        distribution_version = None

    scripts = {
        name: {"found": bool(path := _which(name)), "path": path}
        for name in CONSOLE_SCRIPTS
    }
    version_match = distribution_version == MODULE_VERSION
    warnings = []
    if distribution_version is None:
        warnings.append("The package is running from source but is not installed as a distribution.")
    elif not version_match:
        warnings.append(
            f"Loaded module version {MODULE_VERSION} differs from installed distribution {distribution_version}."
        )
    missing = [name for name, value in scripts.items() if not value["found"]]
    if missing:
        warnings.append("Missing console scripts: " + ", ".join(missing))

    return {
        "module_version": MODULE_VERSION,
        "module_path": str(Path(__file__).resolve().parent),
        "distribution_version": distribution_version,
        "version_match": version_match,
        "console_scripts": scripts,
        "entrypoints_ready": not missing,
        "warnings": warnings,
    }


def environment_report() -> dict:
    gdb_path = resolve_gdb()
    platform = host_platform()
    backends = {}
    for key, info in BACKENDS.items():
        path = _which(info["executable"])
        backends[key] = {
            "name": info["name"],
            "executable": info["executable"],
            "found": bool(path),
            "path": path,
            "install_commands": install_commands(info, platform),
        }
    available = [key for key, value in backends.items() if value["found"]]
    return {
        "ready": bool(gdb_path and available),
        "platform": platform,
        "gdb": {
            "name": GDB["name"],
            "executable": GDB["executable"],
            "found": bool(gdb_path),
            "path": gdb_path,
            "install_commands": install_commands(GDB, platform),
        },
        "backends": backends,
        "available_backends": available,
        "installation": installation_report(),
    }


def check_env(json_output: bool = False) -> bool:
    report = environment_report()
    if json_output:
        print(json.dumps(report, indent=2))
        return report["ready"]

    print("=" * 60)
    print(" STM32 GDB MCP Server - Environment Setup Check")
    print("=" * 60)

    gdb = report["gdb"]
    print(
        f"[OK] {gdb['name']} found at: {gdb['path']}"
        if gdb["found"]
        else f"[MISSING] {gdb['name']} ({gdb['executable']}) is NOT found in PATH."
    )
    for backend in report["backends"].values():
        status = "OK" if backend["found"] else "OPTIONAL"
        detail = f"found at: {backend['path']}" if backend["found"] else "not found"
        print(f"[{status}] {backend['name']} ({backend['executable']}): {detail}")

    installation = report["installation"]
    installed = installation["distribution_version"] or "not installed"
    print(f"[INFO] {SERVER_NAME}: module {installation['module_version']}, distribution {installed}")
    print(f"       module path: {installation['module_path']}")
    for warning in installation["warnings"]:
        print(f"[WARNING] {warning}")

    print("-" * 60)
    if report["ready"]:
        names = ", ".join(report["available_backends"])
        print(f"Environment ready: GDB + backend(s): {names}.")
        return True

    print("Environment is not ready.")
    if not gdb["found"]:
        _print_install(GDB, gdb["install_commands"])
        print(f"  Or point {GDB_ENV_VAR} at any GDB that supports ARM.")
    if not report["available_backends"]:
        print("\nInstall at least one GDB server backend:")
        for key, backend in BACKENDS.items():
            _print_install(backend, report["backends"][key]["install_commands"])
    print("\n[NOTE] After installing, please ensure you restart your terminal to apply the PATH changes.")
    return False


def _print_install(info: dict, commands: list[str]) -> None:
    print(f"\n- {info['name']}:")
    for command in commands:
        print(f"    {command}")
    print(f"  {'Or: ' if commands else ''}{info['install_guide']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check host prerequisites for stm32-gdb-mcp.")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable readiness report")
    parser.add_argument("--version", action="store_true", help="print the loaded stm32-gdb-mcp version")
    args = parser.parse_args(argv)
    if args.version:
        print(MODULE_VERSION)
        return 0
    return 0 if check_env(json_output=args.json) else 1


if __name__ == "__main__":
    raise SystemExit(main())
