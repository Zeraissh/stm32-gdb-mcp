#!/usr/bin/env python3
"""SessionStart hook: announce the STM32 Debug Kit so its guidance is in context from
the first turn (the "locked-in at init" behavior). Kept short on purpose — the always-on
depth lives in the MCP server's own instructions (loaded when the server connects) and in
the on-demand skills; this just points the agent at them.

Emits the documented SessionStart `additionalContext` payload. Any failure is swallowed so
it can never block a session.
"""
import json
import sys

BANNER = (
    "🔧 STM32 Debug Kit active.\n"
    "- MCP `stm32-gdb-mcp` (compact ~31 tools; reach ANY tool via call(tool, args)). "
    "Drives GDB + OpenOCD/ST-Link/J-Link on real hardware.\n"
    "- Skills: `stm32-debug` (the observe→orient→hypothesize→act→verify debug loop) and "
    "`stm32-instrument` (write-time SWO/ITM trace) — invoke them for embedded work.\n"
    "- Golden rule: run `self_check` immediately after `start_debug_session`; reads need a "
    "halted core; a breakpoint TIMEOUT means the path wasn't reached (don't just retry). "
    "Multi-board: pass session=\"name\"."
)


def main():
    try:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": BANNER,
            }
        }))
    except Exception:
        pass  # never break a session over a banner
    sys.exit(0)


if __name__ == "__main__":
    main()
