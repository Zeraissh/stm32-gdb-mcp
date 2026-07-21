# Installing the STM32 debug kit in other IDEs / 在其他 IDE 中安装 STM32 调试套件

## Portable layers / 可移植的两层能力

The kit has an MCP server for tools and a guidance layer for operating rules.
Claude Code loads both from the plugin. Other clients use their MCP configuration plus a
rules file in the firmware repository.

套件由提供调试工具的 MCP 服务器和提供操作规则的引导层组成。Claude Code 可通过插件同时加载；
其他客户端需要配置 MCP，并在固件仓库中放置规则文件。

| Layer / 层 | Claude Code | Cursor / VSCode / Codex / Windsurf / Trae |
|---|---|---|
| MCP server / MCP 服务器 | Plugin / 插件 | Client MCP config / 客户端 MCP 配置 |
| Guidance / 调试引导 | Skills + SessionStart hook | Project rules file / 项目规则文件 |

## 0. One-command deployment / 一键部署

```bash
python scripts/deploy.py --project "D:/path/to/firmware" --ide vscode,cursor
```

After a PyPI install, use the equivalent console command:

从 PyPI 安装后，可使用等价的 console 命令：

```bash
stm32-gdb-mcp-deploy --project "D:/path/to/firmware" --ide vscode,cursor
```

Deployment installs the server if needed, writes each requested client configuration, and
creates a project-aware `AGENTS.md` plus `.github/copilot-instructions.md`. It reuses
`inspect_project` to detect the MCU, debug config, OpenOCD arguments, and ELF candidates.
One ELF is selected automatically; multiple ELF files are listed for explicit selection.
Existing rules are kept unless `--force` is supplied, in which case they are backed up first.

部署命令会在需要时安装服务器、写入所选客户端配置，并生成带项目上下文的 `AGENTS.md` 和
`.github/copilot-instructions.md`。它复用 `inspect_project` 识别 MCU、调试配置、OpenOCD 参数和
ELF 候选；只有一个 ELF 时自动采用，多个 ELF 时仅列出候选并要求明确选择。默认保留已有规则文件；
使用 `--force` 时会先备份再覆盖。

Useful flags / 常用参数：

- `--no-install`: do not install the package / 不安装 Python 包
- `--no-rules`: do not write project rules / 不写项目规则
- `--ide codex`: install and verify through the Codex CLI / 通过 Codex CLI 安装并验证
- `--force`: replace conflicting Codex/rules configuration / 替换冲突的 Codex 或规则配置

Deployment stops immediately when package or client installation fails and never prints a
false success message. / 包安装或客户端安装失败时会立即停止，不会输出虚假的完成提示。

## 1. Install the MCP server / 安装 MCP 服务器

The source helpers safely merge JSON clients and back up existing files:

源码辅助命令会安全合并 JSON 配置，并备份已有文件：

```bash
python scripts/install_mcp.py --list
python scripts/install_mcp.py cursor
python scripts/install_mcp.py vscode --project .
python scripts/install_mcp.py windsurf
python scripts/install_mcp.py claude-desktop
python scripts/install_mcp.py trae
python scripts/install_mcp.py codex
```

PyPI installs expose the same functions:

PyPI 安装提供同样的功能：

```bash
stm32-gdb-mcp-install --list
stm32-gdb-mcp-check-env --json
stm32-gdb-mcp-install codex
```

For Codex, the installer prefers `codex mcp add`, verifies with `codex mcp get --json`, and
is idempotent when the existing entry matches. A conflicting entry requires `--force`.
Use `stm32-gdb-mcp-install codex --print` to print valid TOML without changing Codex.

对于 Codex，安装器优先执行 `codex mcp add`，再用 `codex mcp get --json` 验证；配置相同时可幂等
成功，配置冲突时需显式传入 `--force`。若只需输出 TOML 而不修改 Codex，请执行
`stm32-gdb-mcp-install codex --print`。

## Manual client configuration / 手动配置客户端

JSON clients such as Cursor, Windsurf, Trae, and Claude Desktop use `mcpServers`:

Cursor、Windsurf、Trae 和 Claude Desktop 等 JSON 客户端使用 `mcpServers`：

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

VS Code uses `servers` and an explicit stdio transport:

VS Code 使用 `servers` 和显式 stdio transport：

```json
{
  "servers": {
    "stm32-gdb-mcp": {
      "type": "stdio",
      "command": "C:\\path\\to\\Scripts\\stm32-gdb-mcp.exe",
      "args": [],
      "env": { "STM32_GDB_MCP_COMPACT": "1" }
    }
  }
}
```

Codex uses TOML / Codex 使用 TOML：

```toml
[mcp_servers.stm32-gdb-mcp]
command = "C:/path/to/Scripts/stm32-gdb-mcp.exe"
args = []
env = { STM32_GDB_MCP_COMPACT = "1" }
```

Use the absolute executable path because GUI applications often do not inherit the terminal
`PATH`. The installer resolves it automatically. Compact mode limits the visible surface;
every hidden tool remains callable through `call` and discoverable through `tool_help`.

请使用可执行文件绝对路径，因为 GUI 应用通常不会继承终端的 `PATH`；安装器会自动解析该路径。
compact 模式只限制可见工具，隐藏工具仍可由 `call` 调用，并可由 `tool_help` 查询。

## One-round-trip profile bring-up / 单轮往返配置启动

After the client loads the server, use `batch` to load the board profile, start from its
`server_type`, `server_args`, and `serial`, then immediately run `self_check`:

客户端加载服务器后，使用 `batch` 读取板卡 profile，从中取得 `server_type`、`server_args` 和
`serial`，随后立即执行 `self_check`：

```text
batch(steps=[
  {"tool": "debug_config", "args": {"action": "load", "path": "mcp/board.yaml"}},
  {"tool": "start_debug_session", "args": {}},
  {"tool": "self_check", "args": {}}
], stop_on_error=true)
```

Relative firmware, SVD, project-root, and SWO log paths resolve from the profile file's
directory. / 固件、SVD、项目根目录和 SWO 日志的相对路径均以 profile 文件目录为基准。

## 2. Add project guidance / 添加项目调试引导

Place the rules in the firmware repository, not in this MCP repository:

规则文件应放在固件仓库，而不是本 MCP 仓库：

| Client / 客户端 | Auto-read rules file / 自动读取的规则文件 |
|---|---|
| Codex, Cursor, and many agents | `AGENTS.md` |
| Cursor | `.cursor/rules/stm32.mdc` |
| VS Code + Copilot | `.github/copilot-instructions.md` |

Minimal bilingual template / 最小双语模板：

```md
# STM32 hardware debugging / STM32 硬件调试

Drive `stm32-gdb-mcp` as: observe → orient → hypothesize → act safely → verify.
按“观察 → 定位 → 假设 → 安全操作 → 验证”的闭环使用 `stm32-gdb-mcp`。

- Run `self_check` immediately after `start_debug_session`.
  `start_debug_session` 后立即运行 `self_check`。
- Reads need a HALTED core; call `halt_execution` after `target_unresponsive`.
  读取要求内核暂停；遇到 `target_unresponsive` 先调用 `halt_execution`。
- A breakpoint timeout means the path was not reached. Inspect the gate; do not just retry.
  断点超时表示路径未到达，应检查门控条件，不要原样重试。
- Writes are guarded. Ask before flashing or changing target security state.
  写操作受保护；烧录或修改目标安全状态前必须明确确认。
- Use `recover_session`, not a hard kill. ST-Link SWD is exclusive.
  使用 `recover_session`，不要强杀进程；ST-Link SWD 是独占连接。
- Reach hidden tools with `call`; inspect their schema with `tool_help`.
  使用 `call` 调用隐藏工具，使用 `tool_help` 查询 schema。
```

## 3. Claude Code plugin / Claude Code 插件

```text
/plugin marketplace add Zeraissh/stm32-gdb-mcp
/plugin install stm32-debug-kit@zeraissh-stm32
```

The plugin bundles the MCP server, both skills, and the SessionStart hook. /
插件同时包含 MCP 服务器、两项 skill 和 SessionStart hook。
