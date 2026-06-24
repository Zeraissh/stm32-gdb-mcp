#!/usr/bin/env python3
"""One-command deploy of the STM32 debug kit into a firmware project.

Does the whole setup in one call:
  1. ensures the `stm32-gdb-mcp` server is installed (pip install -e . if missing),
  2. wires it into each chosen IDE's MCP config (Cursor / VSCode / Codex / Windsurf / Trae),
  3. drops a project-aware rules file (AGENTS.md + .github/copilot-instructions.md) into the
     firmware project so non-Claude-Code IDEs get the golden rules.

  python scripts/deploy.py --project "D:/path/to/firmware" --ide vscode,cursor
  python scripts/deploy.py --project . --ide codex --no-install

For Claude Code, the plugin is the one-click path instead:
  /plugin marketplace add Zeraissh/stm32-gdb-mcp  &&  /plugin install stm32-debug-kit@zeraissh-stm32
"""
import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from install_mcp import CLIENTS, SERVER_NAME, install_json, print_codex_toml  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows consoles default to GBK; keep our prints from crashing on any stray glyph.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def ensure_server_installed(no_install: bool) -> bool:
    if importlib.util.find_spec("mcp_server") or shutil.which(SERVER_NAME):
        print("[ok] server present")
        return True
    if no_install:
        print(f"! server not found. Install it: pip install -e \"{REPO}\"")
        return False
    print("Installing the MCP server (pip install -e .) ...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-e", REPO], check=False)
    ok = importlib.util.find_spec("mcp_server") is not None or shutil.which(SERVER_NAME)
    print("[ok] server installed" if ok else "! install failed - install it manually")
    return bool(ok)


def detect_project(proj: str) -> dict:
    """Best-effort facts for the rules file: MCU family, OpenOCD cfgs, debug-config, ELF."""
    info = {}
    try:
        names = os.listdir(proj)
    except OSError:
        names = []
    ocd = os.path.join(proj, "openocd.cfg")
    if os.path.exists(ocd):
        txt = open(ocd, encoding="utf-8", errors="replace").read()
        t = re.search(r"target/(\S+\.cfg)", txt)
        i = re.search(r"interface/(\S+\.cfg)", txt)
        if i and t:
            info["server_args"] = f'["-f","interface/{i.group(1)}","-f","target/{t.group(1)}"]'
    for root in (os.path.join(proj, "mcp"), proj):  # a debug-config yaml for debug_config(action=load)
        if os.path.isdir(root):
            for f in sorted(os.listdir(root)):
                if f.lower().endswith((".yaml", ".yml")) and "openocd" in f.lower():
                    path = os.path.join(root, f)
                    info["debug_config"] = os.path.relpath(path, proj).replace("\\", "/")
                    mcu = re.search(r"(?m)^\s*mcu:\s*(\S+)", open(path, encoding="utf-8", errors="replace").read())
                    if mcu:  # the yaml carries the full part number, e.g. STM32L151CCUx
                        info["mcu"] = mcu.group(1)
                    break
        if "debug_config" in info:
            break
    if "mcu" not in info:  # fall back to the base family from a linker/startup/ioc filename
        for f in names:
            m = re.search(r"(stm32[a-z]\d{2,4})", f, re.IGNORECASE)
            if m:
                info["mcu"] = m.group(1).upper()
                break
    b = os.path.join(proj, "build")
    if os.path.isdir(b):
        elfs = [f for f in os.listdir(b) if f.endswith(".elf")]
        if elfs:
            info["elf"] = "build/" + elfs[0]
    return info


def render_agents(info: dict) -> str:
    mcu = info.get("mcu", "your STM32")
    start = (f'`debug_config(action=load, path="{info["debug_config"]}")`'
             if info.get("debug_config")
             else (f'`start_debug_session(server_type="openocd", server_args={info["server_args"]})`'
                   if info.get("server_args") else "`start_debug_session(...)`"))
    elf = f"\n- ELF: `{info['elf']}` (load symbols / flash from here)." if info.get("elf") else ""
    return f"""# Agent guide — {mcu} hardware debugging (stm32-gdb-mcp)

Debug this firmware on real hardware through the **`stm32-gdb-mcp`** MCP server. Loop:
observe → orient (symbolize) → hypothesize → act safely → verify.

## This target
- MCU: {mcu}. Probe: ST-Link/SWD. Server: OpenOCD.
- Start: {start} → `start_debug_session` → **`self_check`** immediately.{elf}
- For `printf`/profiling clock, confirm `SystemCoreClock`, then `setup_swo(hclk_hz=<HCLK>)`.

## ⚠️ Project safety — REVIEW & EDIT for this board
- If this runs on a sealed/wafer/production target, **prefer a power-cycle over an SWD reset**
  and **ask before flashing** (it overwrites the running firmware). Delete this note if it's a dev board.
- Never hard-kill OpenOCD (wedges the ST-Link USB) — use `recover_session`. ST-Link SWD is exclusive.

## Debug rules
- A tool not listed? `call(tool="<name>", args={{…}})`; batch with `batch`.
- Reads need a HALTED core — `halt_execution` if a read says target_unresponsive.
- A breakpoint TIMEOUT means the path was NOT reached — don't just retry: halt, `capture_state`,
  `breakpoint(action=list)` (hit_count=0), read the gating flag, set an earlier breakpoint or drive
  the precondition.
- Crash → `reconstruct_fault_context`; stack overflow → `analyze_stack`; hot-spots/hangs →
  `sample_pc` (symbolized histogram, no SWO pin needed). Verify a fix with `expressions(action=compare)`.
- Memory writes are guarded; `write_guard(action=policy)` to allow.

Lean tool families: `breakpoint`/`logging`/`expressions`/`debug_profile`/`read_registers`/
`inspect_symbol`/`frame`/`write_guard`/`snapshot`/`coredump`/`timeouts`/`session_diagnostics`
(pass action=/what=). Any tool via `call(tool, args)`.
"""


def render_copilot(info: dict) -> str:
    mcu = info.get("mcu", "your STM32")
    return f"""# Copilot instructions — {mcu} (stm32-gdb-mcp)

Debug on hardware via the `stm32-gdb-mcp` MCP server. Full guide: [`AGENTS.md`](../AGENTS.md).

- Start with the debug profile / `start_debug_session` → **`self_check`**.
- A tool not listed? `call(tool, args)`. Reads need a HALTED core.
- A breakpoint TIMEOUT means the path wasn't reached — halt, `capture_state`, `breakpoint(action=list)`,
  read the gating flag; don't just retry.
- ⚠️ On a sealed/production target, prefer a power-cycle over an SWD reset and **ask before flashing**.
- Never hard-kill OpenOCD; use `recover_session`.
"""


def write_file(path: str, content: str, force: bool) -> None:
    if os.path.exists(path) and not force:
        print(f"  skip (exists): {path}  - use --force to overwrite")
        return
    if os.path.exists(path):
        bak = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, bak)
        print(f"  backed up -> {bak}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  wrote {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="One-command deploy of the STM32 debug kit.")
    ap.add_argument("--project", required=True, help="firmware project directory")
    ap.add_argument("--ide", default="", help="comma-separated: cursor,vscode,codex,windsurf,trae,claude-desktop")
    ap.add_argument("--no-install", action="store_true", help="don't pip-install the server")
    ap.add_argument("--no-rules", action="store_true", help="don't write AGENTS.md / copilot-instructions")
    ap.add_argument("--force", action="store_true", help="overwrite existing rules files")
    args = ap.parse_args()

    proj = os.path.abspath(args.project)
    if not os.path.isdir(proj):
        print(f"! project dir not found: {proj}", file=sys.stderr)
        return 2

    print("== 1. server ==")
    ensure_server_installed(args.no_install)

    ides = [c.strip() for c in args.ide.split(",") if c.strip()]
    if ides:
        print("== 2. IDE MCP config ==")
        for ide in ides:
            if ide == "codex":
                print_codex_toml()
            elif ide in CLIENTS:
                path_builder, key, wants_type = CLIENTS[ide]
                install_json(path_builder(proj), key, wants_type)
            else:
                print(f"  unknown ide '{ide}' (cursor,vscode,codex,windsurf,trae,claude-desktop)")
    else:
        print("== 2. IDE MCP config == (none requested; pass --ide ...)")

    if not args.no_rules:
        print("== 3. rules files ==")
        info = detect_project(proj)
        if info:
            print(f"  detected: {info}")
        write_file(os.path.join(proj, "AGENTS.md"), render_agents(info), args.force)
        write_file(os.path.join(proj, ".github", "copilot-instructions.md"), render_copilot(info), args.force)

    print("\nDone. Restart the IDE to load the server. Review AGENTS.md's safety note for this board.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
