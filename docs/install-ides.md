# Installing the STM32 debug kit in other IDEs / 在其他 IDE 中安装

## What's portable, and what isn't

The kit has two layers:

| Layer | Claude Code | Cursor / VSCode / Codex / Windsurf / Trae |
|---|---|---|
| **MCP server** (the debugging tools) | ✅ via plugin | ✅ via each client's MCP config |
| **Guidance** (golden rules, skills) | ✅ skills + SessionStart hook | ⚠️ via a **rules file** the client auto-reads |

The Claude Code **plugin** (marketplace, auto-loaded skills, SessionStart hook) is
Claude-Code-only. Everywhere else you wire up the **MCP server** for the tools, and drop a
**rules file** into your *firmware* project for the always-on guidance. Both are below.

> 插件(市场 / 自动加载技能 / 会话钩子)仅限 Claude Code。其他 IDE 里:用各自的 MCP 配置接上
> **服务器**(拿到工具),再在你的**固件项目**放一个**规则文件**(拿到常驻引导)。

## 1. Install the MCP server

One command per client (safe merge — it backs up and keeps your other servers):

```bash
python scripts/install_mcp.py --list                 # supported clients
python scripts/install_mcp.py cursor                 # global ~/.cursor/mcp.json
python scripts/install_mcp.py vscode --project .     # ./.vscode/mcp.json  (Copilot agent mode)
python scripts/install_mcp.py windsurf               # ~/.codeium/windsurf/mcp_config.json
python scripts/install_mcp.py claude-desktop         # Claude Desktop
python scripts/install_mcp.py trae                   # Trae
python scripts/install_mcp.py codex                  # prints the TOML to paste into ~/.codex/config.toml
```

### Manual config (if you prefer)

JSON clients — **Cursor / Windsurf / Trae / Claude Desktop** use `"mcpServers"`:

```json
{
  "mcpServers": {
    "stm32-gdb-mcp": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python313\\Scripts\\stm32-gdb-mcp.exe",
      "args": [],
      "env": { "STM32_GDB_MCP_COMPACT": "1" }
    }
  }
}
```

**VSCode** (`.vscode/mcp.json`) uses `"servers"` and a transport `"type"`:

```json
{
  "servers": {
    "stm32-gdb-mcp": {
      "type": "stdio",
      "command": "C:\\…\\Scripts\\stm32-gdb-mcp.exe",
      "args": [],
      "env": { "STM32_GDB_MCP_COMPACT": "1" }
    }
  }
}
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.stm32-gdb-mcp]
command = "C:/…/Scripts/stm32-gdb-mcp.exe"
args = []
env = { STM32_GDB_MCP_COMPACT = "1" }
```

> **Use the absolute exe path.** GUI apps (Cursor/VSCode) often don't see `stm32-gdb-mcp` on
> PATH — the bare command fails there. `python scripts/install_mcp.py` fills in the absolute
> path automatically. The server must be installed first: `pip install -e .`.
> Compact mode keeps it to ~31 tools; reach any other via `call(tool, args)`.

## 2. Add the guidance (rules file)

Other IDEs don't run Claude Code skills/hooks, so put the golden rules where each client looks.
Drop this into your **firmware project** (the repo you debug, not this one):

| Client | Rules file it auto-reads |
|---|---|
| Codex, Cursor, many tools | `AGENTS.md` (project root) |
| Cursor | `.cursor/rules/stm32.mdc` |
| VSCode + Copilot | `.github/copilot-instructions.md` |

Paste this content (a condensed version of the `stm32-debug` skill):

```md
# STM32 hardware debugging (stm32-gdb-mcp)

When debugging STM32 firmware on hardware, drive the `stm32-gdb-mcp` MCP server as a loop:
observe → orient (symbolize) → hypothesize → act safely → verify.

- A tool not listed? Reach any via `call(tool="<name>", args={…})`; batch several with `batch`.
- Run `self_check` immediately after `start_debug_session` (validates byte order, core, family).
- Set `debug_profile(action=set, mcu, elf_path, svd_path)` so symbols/peripherals resolve.
- Reads (registers/memory/frames) need a HALTED core; if a read says target_unresponsive, `halt_execution` first.
- A breakpoint TIMEOUT means the code path was NOT reached — do NOT just retry. Halt, `capture_state`,
  `breakpoint(action=list)` (hit_count=0 confirms), read the gating flag, set a breakpoint earlier or
  drive the precondition.
- Memory writes are guarded (option bytes/IWDG/WWDG blocked); `write_guard(action=policy)` to allow.
- Don't hard-kill OpenOCD (it can wedge the probe's USB) — use `recover_session`.
- ST-Link SWD is exclusive; reset via `reset_target`, not a second OpenOCD.
- Find hot-spots/hangs with `sample_pc` (symbolized PC histogram). For printf over SWO,
  `setup_swo(hclk_hz=…)` then `logging(action=start, channel="swo", file="swo_itm.log")`.
- Multiple boards: pass `session="name"` to any tool.
```

That gives non-Claude-Code IDEs the same "locked-in at start" behavior the SessionStart hook
provides in Claude Code.

## 3. Claude Code (best experience)

```text
/plugin marketplace add Zeraissh/stm32-gdb-mcp
/plugin install stm32-debug-kit@zeraissh-stm32
```

Bundles the server + both skills + the SessionStart hook in one install (~175 always-on tokens).
