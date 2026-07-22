from __future__ import annotations

import argparse
import json
import shutil
from importlib import metadata
from pathlib import Path

from . import __version__ as MODULE_VERSION

SERVER_NAME = "stm32-gdb-mcp"
CONSOLE_SCRIPTS = (
    SERVER_NAME,
    f"{SERVER_NAME}-check-env",
    f"{SERVER_NAME}-install",
    f"{SERVER_NAME}-deploy",
)

GDB = {
    "executable": "arm-none-eabi-gdb",
    "name": "ARM GNU Toolchain (GDB)",
    "install_guide": (
        "Download from https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads "
        "and add the 'bin' folder to PATH."
    ),
}

BACKENDS = {
    "openocd": {
        "executable": "openocd",
        "name": "OpenOCD",
        "install_guide": (
            "Download xPack OpenOCD from https://github.com/xpack-dev-tools/openocd-xpack/releases, "
            "extract it, and add the 'bin' folder to PATH."
        ),
    },
    "jlink": {
        "executable": "JLinkGDBServerCL",
        "name": "J-Link GDB Server",
        "install_guide": (
            "Download the J-Link Software and Documentation Pack from "
            "https://www.segger.com/downloads/jlink/ and add the installation folder to PATH."
        ),
    },
    "stlink": {
        "executable": "st-util",
        "name": "ST-LINK GDB Server (st-link tools)",
        "install_guide": "Download from https://github.com/stlink-org/stlink/releases, extract, and add the 'bin' folder to PATH.",
    },
}


def _which(exe: str) -> str | None:
    if exe == "JLinkGDBServerCL":
        return shutil.which("JLinkGDBServerCL") or shutil.which("JLinkGDBServer") or shutil.which("jlinkgdbserver")
    return shutil.which(exe)


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
    gdb_path = _which(GDB["executable"])
    backends = {}
    for key, info in BACKENDS.items():
        path = _which(info["executable"])
        backends[key] = {
            "name": info["name"],
            "executable": info["executable"],
            "found": bool(path),
            "path": path,
        }
    available = [key for key, value in backends.items() if value["found"]]
    return {
        "ready": bool(gdb_path and available),
        "gdb": {
            "name": GDB["name"],
            "executable": GDB["executable"],
            "found": bool(gdb_path),
            "path": gdb_path,
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
        print(f"\n- {GDB['name']}:\n  {GDB['install_guide']}")
    if not report["available_backends"]:
        print("\nInstall at least one GDB server backend:")
        for backend in BACKENDS.values():
            print(f"\n- {backend['name']}:\n  {backend['install_guide']}")
    print("\n[NOTE] After installing, please ensure you restart your terminal to apply the PATH changes.")
    return False


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
