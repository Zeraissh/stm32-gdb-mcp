#!/usr/bin/env python3
"""Plugin entry point: run the stm32-gdb-mcp server from this checkout's src/.

The Claude plugin starts the server through this file (rather than ``python -m``) so the
command is a plain file inside the plugin folder that the directory's validator can read.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_server.server import cli_main

if __name__ == "__main__":
    cli_main()
