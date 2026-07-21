from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

from . import __version__ as PACKAGE_VERSION
from .env_check import CONSOLE_SCRIPTS
from .install_mcp import CLIENTS, SERVER_NAME, install_codex, install_json
from .project_inspector import inspect_project

REPO = Path(__file__).resolve().parents[2]


def _server_installation_state() -> dict:
    try:
        installed_version = metadata.version(SERVER_NAME)
    except metadata.PackageNotFoundError:
        installed_version = None
    scripts = {name: shutil.which(name) for name in CONSOLE_SCRIPTS}
    return {
        "version": installed_version,
        "version_match": installed_version == PACKAGE_VERSION,
        "scripts": scripts,
        "entrypoints_ready": all(scripts.values()),
    }


def ensure_server_installed(no_install: bool, upgrade: bool = False) -> bool:
    state = _server_installation_state()
    complete = state["version_match"] and state["entrypoints_ready"]
    if complete and not upgrade:
        print(f"[ok] server {PACKAGE_VERSION} present")
        return True
    if no_install:
        print(
            f"! server install is stale or incomplete (installed={state['version']!r}, "
            f"expected={PACKAGE_VERSION!r}). Re-run without --no-install."
        )
        return False

    source_checkout = (REPO / "pyproject.toml").is_file()
    target = ["-e", str(REPO)] if source_checkout else [SERVER_NAME]
    command = [sys.executable, "-m", "pip", "install", "--upgrade", *target]
    print("Installing or upgrading the MCP server ...")
    result = subprocess.run(command, check=False)
    refreshed = _server_installation_state()
    ok = result.returncode == 0 and refreshed["version_match"] and refreshed["entrypoints_ready"]
    print("[ok] server installed" if ok else "! install failed - install it manually")
    return bool(ok)


def detect_project(project: str) -> dict:
    root = Path(project).resolve()
    inspected = inspect_project(str(root))
    info = {"project_root": inspected["project_root"]}
    if inspected.get("mcu"):
        info["mcu"] = inspected["mcu"]

    openocd_cfg = root / "openocd.cfg"
    if openocd_cfg.exists():
        text = openocd_cfg.read_text(encoding="utf-8", errors="replace")
        target = re.search(r"target/(\S+\.cfg)", text)
        interface = re.search(r"interface/(\S+\.cfg)", text)
        if interface and target:
            info["server_args"] = f'["-f","interface/{interface.group(1)}","-f","target/{target.group(1)}"]'

    for config_root in (root / "mcp", root):
        if not config_root.is_dir():
            continue
        for path in sorted(config_root.iterdir()):
            if path.suffix.lower() not in (".yaml", ".yml") or "openocd" not in path.name.lower():
                continue
            info["debug_config"] = path.relative_to(root).as_posix()
            match = re.search(r"(?m)^\s*mcu:\s*(\S+)", path.read_text(encoding="utf-8", errors="replace"))
            if match:
                info["mcu"] = match.group(1)
            break
        if "debug_config" in info:
            break

    if "mcu" not in info:
        for path in sorted(root.iterdir()):
            match = re.search(r"(stm32[a-z]\d{2,4})", path.name, re.IGNORECASE)
            if match:
                info["mcu"] = match.group(1).upper()
                break

    elf_candidates = sorted(Path(path).relative_to(root).as_posix() for path in inspected["files"]["elf"])
    if elf_candidates:
        info["elf_candidates"] = elf_candidates
    if len(elf_candidates) == 1:
        info["elf"] = elf_candidates[0]
    return info


def render_agents(info: dict) -> str:
    mcu = info.get("mcu", "your STM32")
    if info.get("debug_config"):
        start = f'`debug_config(action=load, path="{info["debug_config"]}")`'
    elif info.get("server_args"):
        start = f'`start_debug_session(server_type="openocd", server_args={info["server_args"]})`'
    else:
        start = "`start_debug_session(...)`"
    if info.get("elf"):
        elf = f"\n- ELF: `{info['elf']}` (load symbols / flash from here)."
    elif info.get("elf_candidates"):
        candidates = ", ".join(f"`{path}`" for path in info["elf_candidates"])
        elf = f"\n- ELF candidates: {candidates}. Select the intended image explicitly."
    else:
        elf = ""
    return f"""# Agent guide - {mcu} hardware debugging (stm32-gdb-mcp)

Debug this firmware on real hardware through the **`stm32-gdb-mcp`** MCP server. Loop:
observe -> orient (symbolize) -> hypothesize -> act safely -> verify.

## This target
- MCU: {mcu}. Probe: ST-Link/SWD. Server: OpenOCD.
- Start: {start} -> `start_debug_session` -> **`self_check`** immediately.{elf}
- For `printf`/profiling clock, confirm `SystemCoreClock`, then `setup_swo(hclk_hz=<HCLK>)`.

## Project safety - review and edit for this board
- If this runs on a sealed/wafer/production target, prefer a power-cycle over an SWD reset
  and ask before flashing. Delete this note if it is a dev board.
- Never hard-kill OpenOCD; use `recover_session`. ST-Link SWD is exclusive.

## Debug rules
- A tool not listed? `call(tool="<name>", args={{...}})`; batch with `batch`.
- Reads need a HALTED core; `halt_execution` if a read says target_unresponsive.
- A breakpoint TIMEOUT means the path was NOT reached. Halt, `capture_state`,
  `breakpoint(action=list)` (hit_count=0), read the gating flag, set an earlier breakpoint or drive
  the precondition.
- Crash -> `reconstruct_fault_context`; stack overflow -> `analyze_stack`; hot-spots/hangs ->
  `sample_pc` (symbolized histogram, no SWO pin needed). Verify a fix with `expressions(action=compare)`.
- Memory writes are guarded; `write_guard(action=policy)` to allow.

Lean tool families: `breakpoint`/`logging`/`expressions`/`debug_profile`/`read_registers`/
`inspect_symbol`/`frame`/`write_guard`/`snapshot`/`coredump`/`timeouts`/`session_diagnostics`
(pass action=/what=). Any tool via `call(tool, args)`.
"""


def render_copilot(info: dict) -> str:
    mcu = info.get("mcu", "your STM32")
    return f"""# Copilot instructions - {mcu} (stm32-gdb-mcp)

Debug on hardware via the `stm32-gdb-mcp` MCP server. Full guide: [`AGENTS.md`](../AGENTS.md).

- Start with the debug profile / `start_debug_session` -> **`self_check`**.
- A tool not listed? `call(tool, args)`. Reads need a HALTED core.
- A breakpoint TIMEOUT means the path was not reached. Halt, `capture_state`,
  `breakpoint(action=list)`, read the gating flag; do not just retry.
- On a sealed/production target, prefer a power-cycle over an SWD reset and ask before flashing.
- Never hard-kill OpenOCD; use `recover_session`.
"""


def write_file(path: str, content: str, force: bool) -> None:
    if os.path.exists(path) and not force:
        print(f"  skip (exists): {path}  - use --force to overwrite")
        return
    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"  backed up -> {backup}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    print(f"  wrote {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-command deploy of the STM32 debug kit.")
    parser.add_argument("--project", required=True, help="firmware project directory")
    parser.add_argument("--ide", default="", help="comma-separated: cursor,vscode,codex,windsurf,trae,claude-desktop")
    parser.add_argument("--no-install", action="store_true", help="do not pip-install the server")
    parser.add_argument("--upgrade", action="store_true", help="reinstall/upgrade the server and console scripts")
    parser.add_argument("--no-rules", action="store_true", help="do not write AGENTS.md / copilot-instructions")
    parser.add_argument("--force", action="store_true", help="overwrite existing rules files")
    args = parser.parse_args(argv)

    project = os.path.abspath(args.project)
    if not os.path.isdir(project):
        print(f"! project dir not found: {project}", file=sys.stderr)
        return 2

    print("== 1. server ==")
    if not ensure_server_installed(args.no_install, upgrade=args.upgrade):
        return 1

    ides = [client.strip() for client in args.ide.split(",") if client.strip()]
    if ides:
        print("== 2. IDE MCP config ==")
        for ide in ides:
            if ide == "codex":
                if not install_codex(force=args.force):
                    return 1
            elif ide in CLIENTS:
                path_builder, key, wants_type = CLIENTS[ide]
                install_json(path_builder(project), key, wants_type)
            else:
                print(f"  unknown ide '{ide}' (cursor,vscode,codex,windsurf,trae,claude-desktop)")
    else:
        print("== 2. IDE MCP config == (none requested; pass --ide ...)")

    if not args.no_rules:
        print("== 3. rules files ==")
        info = detect_project(project)
        if info:
            print(f"  detected: {info}")
        write_file(os.path.join(project, "AGENTS.md"), render_agents(info), args.force)
        write_file(os.path.join(project, ".github", "copilot-instructions.md"), render_copilot(info), args.force)

    print("\nDone. Restart the IDE to load the server. Review AGENTS.md's safety note for this board.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
