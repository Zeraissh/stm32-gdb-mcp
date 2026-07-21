from __future__ import annotations

import argparse
import json
import shutil

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
    args = parser.parse_args(argv)
    return 0 if check_env(json_output=args.json) else 1


if __name__ == "__main__":
    raise SystemExit(main())
