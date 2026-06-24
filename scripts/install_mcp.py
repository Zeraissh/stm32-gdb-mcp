#!/usr/bin/env python3
"""Install the stm32-gdb-mcp server into a non-Claude-Code MCP client's config.

The Claude Code *plugin* (skills + SessionStart hook + marketplace) is Claude-Code-only,
but the MCP server is portable. This writes the server entry into another client's config,
merging (never clobbering) and backing up first.

  python scripts/install_mcp.py --list
  python scripts/install_mcp.py cursor                 # global ~/.cursor/mcp.json
  python scripts/install_mcp.py vscode --project .     # ./.vscode/mcp.json
  python scripts/install_mcp.py codex                  # prints the TOML block to paste

Pair it with a rules file for the guidance layer (see docs/install-ides.md) — other IDEs
don't run skills/hooks, so the golden rules travel as AGENTS.md / .cursor/rules / Copilot
instructions in your *firmware* project.
"""
import argparse
import json
import os
import shutil
import sys
import time

SERVER_NAME = "stm32-gdb-mcp"

# client -> (config path builder, top-level key, wants_type_stdio)
CLIENTS = {
    "cursor":         (lambda proj: os.path.expanduser("~/.cursor/mcp.json"), "mcpServers", False),
    "cursor-project": (lambda proj: os.path.join(proj, ".cursor", "mcp.json"), "mcpServers", False),
    "vscode":         (lambda proj: os.path.join(proj, ".vscode", "mcp.json"), "servers", True),
    "windsurf":       (lambda proj: os.path.expanduser("~/.codeium/windsurf/mcp_config.json"), "mcpServers", False),
    "claude-desktop": (lambda proj: os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Claude", "claude_desktop_config.json"), "mcpServers", False),
    "trae":           (lambda proj: os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Trae CN", "User", "mcp.json"), "mcpServers", False),
}


def server_entry(wants_type: bool) -> dict:
    """Prefer the installed console script (robust in GUI clients that lack PATH); else
    fall back to running the bundled source with the current interpreter."""
    exe = shutil.which(SERVER_NAME)
    if exe:
        entry = {"command": exe, "args": [], "env": {"STM32_GDB_MCP_COMPACT": "1"}}
    else:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        entry = {
            "command": sys.executable,
            "args": ["-m", "mcp_server.server"],
            "env": {"PYTHONPATH": os.path.join(repo, "src"), "STM32_GDB_MCP_COMPACT": "1"},
        }
    if wants_type:  # VSCode's .vscode/mcp.json expects a transport type
        entry = {"type": "stdio", **entry}
    return entry


def install_json(path: str, key: str, wants_type: bool) -> None:
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        data = json.loads(text) if text else {}
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"  backed up existing config -> {backup}")
    data.setdefault(key, {})[SERVER_NAME] = server_entry(wants_type)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"  wrote {SERVER_NAME} -> {path} (under \"{key}\")")


def print_codex_toml() -> None:
    entry = server_entry(False)
    env = ", ".join(f'{k} = "{v}"' for k, v in entry["env"].items())
    args = ", ".join(f'"{a}"' for a in entry["args"])
    print("# Add to ~/.codex/config.toml (Codex reads MCP from TOML, and AGENTS.md for guidance):\n")
    print(f"[mcp_servers.{SERVER_NAME}]")
    print(f'command = "{entry["command"]}"')
    print(f"args = [{args}]")
    print(f"env = {{ {env} }}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Install stm32-gdb-mcp into an MCP client.")
    ap.add_argument("client", nargs="?", help="cursor | cursor-project | vscode | windsurf | claude-desktop | trae | codex")
    ap.add_argument("--project", default=".", help="project dir for per-project clients (vscode, cursor-project)")
    ap.add_argument("--list", action="store_true", help="list supported clients")
    args = ap.parse_args()

    if args.list or not args.client:
        print("Supported clients: " + ", ".join(list(CLIENTS) + ["codex"]))
        return 0

    if args.client == "codex":
        print_codex_toml()
        return 0

    if args.client not in CLIENTS:
        print(f"Unknown client '{args.client}'. Use --list.", file=sys.stderr)
        return 2

    path_builder, key, wants_type = CLIENTS[args.client]
    path = path_builder(os.path.abspath(args.project))
    print(f"Installing into {args.client}:")
    install_json(path, key, wants_type)
    print("Done. Restart the client to pick up the server. For guidance in this client, add a "
          "rules file (see docs/install-ides.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
