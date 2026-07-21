from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVER_NAME = "stm32-gdb-mcp"

CLIENTS = {
    "cursor": (lambda proj: os.path.expanduser("~/.cursor/mcp.json"), "mcpServers", False),
    "cursor-project": (lambda proj: os.path.join(proj, ".cursor", "mcp.json"), "mcpServers", False),
    "vscode": (lambda proj: os.path.join(proj, ".vscode", "mcp.json"), "servers", True),
    "windsurf": (lambda proj: os.path.expanduser("~/.codeium/windsurf/mcp_config.json"), "mcpServers", False),
    "claude-desktop": (
        lambda proj: os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Claude", "claude_desktop_config.json"),
        "mcpServers",
        False,
    ),
    "trae": (
        lambda proj: os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Trae CN", "User", "mcp.json"),
        "mcpServers",
        False,
    ),
}


def server_entry(wants_type: bool) -> dict:
    exe = shutil.which(SERVER_NAME)
    if exe:
        entry = {"command": exe, "args": [], "env": {"STM32_GDB_MCP_COMPACT": "1"}}
    else:
        entry = {
            "command": sys.executable,
            "args": ["-m", "mcp_server.server"],
            "env": {"STM32_GDB_MCP_COMPACT": "1"},
        }
    if wants_type:
        entry = {"type": "stdio", **entry}
    return entry


def install_json(path: str, key: str, wants_type: bool) -> None:
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
        data = json.loads(text) if text else {}
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"  backed up existing config -> {backup}")
    data.setdefault(key, {})[SERVER_NAME] = server_entry(wants_type)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print(f"  wrote {SERVER_NAME} -> {path} (under \"{key}\")")


def render_codex_toml() -> str:
    entry = server_entry(False)
    env = ", ".join(f"{key} = {json.dumps(value)}" for key, value in entry["env"].items())
    args = ", ".join(json.dumps(arg) for arg in entry["args"])
    return "\n".join(
        [
            f"[mcp_servers.{SERVER_NAME}]",
            f"command = {json.dumps(entry['command'])}",
            f"args = [{args}]",
            f"env = {{ {env} }}",
            "",
        ]
    )


def print_codex_toml() -> None:
    print(render_codex_toml(), end="")


def codex_cli_candidates() -> list[str]:
    candidates = [
        os.environ.get("CODEX_CLI"),
        shutil.which("codex"),
        str(Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex.exe"),
    ]
    return list(dict.fromkeys(candidate for candidate in candidates if candidate and os.path.exists(candidate)))


def _codex_config_matches(payload: dict, expected: dict) -> bool:
    transport = payload.get("transport") or {}
    actual_command = os.path.normcase(os.path.normpath(transport.get("command") or ""))
    expected_command = os.path.normcase(os.path.normpath(expected["command"]))
    return (
        transport.get("type") == "stdio"
        and actual_command == expected_command
        and (transport.get("args") or []) == expected["args"]
        and (transport.get("env") or {}) == expected["env"]
    )


def _run_codex(runner, command: list[str]):
    return runner(command, capture_output=True, text=True, timeout=20)


def install_codex(force: bool = False, runner=None, candidates: list[str] | None = None) -> bool:
    runner = runner or subprocess.run
    expected = server_entry(False)
    errors = []

    for executable in candidates if candidates is not None else codex_cli_candidates():
        get_command = [executable, "mcp", "get", SERVER_NAME, "--json"]
        try:
            current = _run_codex(runner, get_command)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{executable}: {exc}")
            continue

        exists = current.returncode == 0
        if exists:
            try:
                payload = json.loads(current.stdout)
            except json.JSONDecodeError:
                errors.append(f"{executable}: codex mcp get returned invalid JSON")
                continue
            if _codex_config_matches(payload, expected):
                print(f"  {SERVER_NAME} is already configured in Codex.")
                return True
            if not force:
                print(f"  Codex already has a different {SERVER_NAME} entry; use --force to replace it.", file=sys.stderr)
                return False
            removed = _run_codex(runner, [executable, "mcp", "remove", SERVER_NAME])
            if removed.returncode != 0:
                print((removed.stderr or "codex mcp remove failed").strip(), file=sys.stderr)
                return False
        else:
            detail = (current.stderr or current.stdout or "").lower()
            if "not found" not in detail and "no mcp server named" not in detail:
                errors.append(f"{executable}: {(current.stderr or current.stdout).strip()}")
                continue

        add_command = [executable, "mcp", "add", SERVER_NAME]
        for key, value in expected["env"].items():
            add_command.extend(["--env", f"{key}={value}"])
        add_command.extend(["--", expected["command"], *expected["args"]])
        added = _run_codex(runner, add_command)
        if added.returncode != 0:
            print((added.stderr or "codex mcp add failed").strip(), file=sys.stderr)
            return False

        verified = _run_codex(runner, get_command)
        try:
            payload = json.loads(verified.stdout) if verified.returncode == 0 else {}
        except json.JSONDecodeError:
            payload = {}
        if _codex_config_matches(payload, expected):
            print(f"  installed and verified {SERVER_NAME} in Codex.")
            return True
        print("  Codex MCP verification failed after installation.", file=sys.stderr)
        return False

    if errors:
        print("  Codex CLI unavailable: " + "; ".join(errors), file=sys.stderr)
    else:
        print("  Codex CLI not found.", file=sys.stderr)
    print("# Fallback: add this block to ~/.codex/config.toml")
    print_codex_toml()
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install stm32-gdb-mcp into an MCP client.")
    parser.add_argument("client", nargs="?", help="cursor | cursor-project | vscode | windsurf | claude-desktop | trae | codex")
    parser.add_argument("--project", default=".", help="project dir for per-project clients (vscode, cursor-project)")
    parser.add_argument("--list", action="store_true", help="list supported clients")
    parser.add_argument("--force", action="store_true", help="replace a conflicting Codex MCP entry")
    parser.add_argument("--print", action="store_true", dest="print_only", help="print Codex TOML without changing config")
    args = parser.parse_args(argv)

    if args.list or not args.client:
        print("Supported clients: " + ", ".join(list(CLIENTS) + ["codex"]))
        return 0

    if args.client == "codex":
        if args.print_only:
            print_codex_toml()
            return 0
        return 0 if install_codex(force=args.force) else 1

    if args.client not in CLIENTS:
        print(f"Unknown client '{args.client}'. Use --list.", file=sys.stderr)
        return 2

    path_builder, key, wants_type = CLIENTS[args.client]
    path = path_builder(os.path.abspath(args.project))
    print(f"Installing into {args.client}:")
    install_json(path, key, wants_type)
    print("Done. Restart the client to pick up the server. For guidance in this client, add a rules file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
