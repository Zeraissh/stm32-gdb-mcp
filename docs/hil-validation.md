# Hardware-in-the-loop Validation / 硬件在环验证

English: Hardware-in-the-loop checks are manual by design. The normal CI
workflow must stay hardware-free, while the HIL workflow runs on a trusted
self-hosted runner that has a probe, board, GDB, and a supported GDB server installed.

中文：硬件在环检查设计为手动触发。普通 CI 必须保持不依赖硬件；HIL 工作流则运行在可信的自托管
runner 上，该机器需要连接探针和目标板，并安装 GDB 与受支持的 GDB Server。

## Runner Requirements / Runner 要求

- A self-hosted GitHub Actions runner with labels `self-hosted` and `stm32` / 带有 `self-hosted` 和 `stm32` 标签的自托管 GitHub Actions runner
- Python 3.10 or newer / Python 3.10 或更新版本
- `arm-none-eabi-gdb` on `PATH` / `PATH` 中可用的 `arm-none-eabi-gdb`
- One supported GDB server on `PATH`: OpenOCD, J-Link, or ST-Link / `PATH` 中至少有一种受支持的 GDB Server：OpenOCD、J-Link 或 ST-Link
- A connected STM32 target board / 已连接的 STM32 目标板
- A debug config YAML file for the target / 面向该目标板的调试 YAML 配置
- Optional: a SmartUSBHub plus `pip install 'stm32-gdb-mcp[hub]'` to run several boards in one dispatch (see Rack Mode below) / 可选：SmartUSBHub 与 `[hub]` extra，用于一次运行多块板（见下文机架模式）
- Optional RTT or UART host tools when validating log capture / 验证日志采集时需要可选的 RTT 或 UART 主机工具

## Manual Workflow / 手动工作流

Run the GitHub Actions workflow named `Hardware-in-the-loop`.

运行名为 `Hardware-in-the-loop` 的 GitHub Actions 工作流。

Inputs / 输入参数：

- `boards`: JSON array of board profiles to run, e.g. `["l431"]` or `["l151","l431","u535"]`. Each becomes one matrix leg. Without a hub, list exactly the board that is physically connected. / 要运行的板卡列表（JSON 数组），每项对应一个 matrix 分支。没有 Hub 时只填当前物理连接的那一块。
- `config_path`: optional YAML override, single-board runs only; empty uses `examples/configs/stm32<board>_openocd.yaml` / 可选 YAML 路径覆盖（仅单板）；留空时使用 `examples/configs/stm32<board>_openocd.yaml`
- `use_hub`: isolate each board on its hub channel before running it / 运行前用 Hub 隔离每块板
- `rack_config`: hub rack wiring, default `examples/configs/rack_hub.yaml` / Hub 机架接线配置
- `smoke_command`: optional command executed after setup and config validation / 安装和配置校验后执行的可选烟测命令

Legs always run one at a time (`max-parallel: 1`): there is one hub, one USB-CDC
control port, and SWD is exclusive, so overlapping legs would fight over all three.

分支始终串行执行：只有一个 Hub、一个 USB-CDC 控制口，且 SWD 是独占的。

## Rack Mode / 机架模式

With a programmable USB hub (`pip install 'stm32-gdb-mcp[hub]'`) the `boards` input
stops meaning "which board did a human plug in" and starts meaning "which boards should
this run cover". Each leg calls:

接上可编程 USB Hub 后，`boards` 的含义从"人插了哪块板"变成"这次要跑哪些板"。每个分支执行：

```bash
python scripts/hil_rack.py isolate --config examples/configs/rack_hub.yaml --board l431
```

That disconnects every other channel's USB data lines, so `detect_probe` sees exactly one
probe and `start_debug_session`'s single-probe auto-select stays on its safe path — the
server never has to choose between two probes, which is the failure the multi-probe
detection work was added to prevent. An `if: always()` step then restores the rack:

这会断开其余通道的 USB 数据线，使 `detect_probe` 只看到一个探针，单探针自动选择的安全路径
恒成立。结尾用 `if: always()` 步骤恢复机架：

```bash
python scripts/hil_rack.py restore --config examples/configs/rack_hub.yaml
```

### Wiring the rack / 机架接线

1. Plug every board's probe into a hub channel and copy `examples/configs/rack_hub.yaml`.
2. Set a `label` per channel matching the board profile names (`l151`, `l431`, `u535`).
   The label is matched against the **debug session name, exactly**, so those profile
   names are also the names each session must be started with (`session="l431"`).
3. Fill the serials automatically — with all boards attached, run `hub(action=discover, apply=true)`
   once. It drops each channel's data lines in turn and records which probe disappeared.
   This is slow on Windows (`detect_probe` walks the whole USB device tree, ~8.5 s per
   channel), which is why it is opt-in and run once rather than at every session start.
   Discovery never overwrites a `label` you set by hand. **Serial-less probes** (clone
   ST-Links reporting no serial and an identical `location`) are keyed by USB port
   instead, which identifies a channel but cannot select one — those channels *must*
   be selected by `label` or an explicit `hub: {channel: N}`.
4. Add `hub: {channel: N}` to each board's own config so `recover_session`,
   `reset_target(strategy="cold")`, and `hub(action=measure)` know which port that board is on.
5. **Load the rack config into every session that needs it** — the debug profile is
   per-session, so a map loaded into one session is invisible to the next, and the hub
   call fails with `hub_unavailable: hub channel unmapped`. The profile is in-memory
   only, so this is also re-run after each server restart. A successful selection
   reports `channel_source: "map_label"`; check that field rather than assuming.

插好所有板后跑一次 `hub(action=discover, apply=true)` 即可自动填好 serial；该调用在 Windows 上
较慢，因此是显式选项而非每次会话启动都做。

注意两点：`label` 是与**调试会话名精确比对**的，因此板卡 profile 名同时也是各会话必须使用的
名字；而**调试 profile 按会话隔离**，在一个会话里加载的映射对另一个会话不可见，此时会以
`hub_unavailable: hub channel unmapped` 失败，需要给每个会话都加载一次（profile 是纯内存的，
服务器重启后同样要重新加载）。选中成功时结果会带 `channel_source: "map_label"`，请以该字段为准。
无序列号的克隆 ST-Link 只能按 USB 端口建 key，那能认出通道却无法选中通道，因此这类通道**必须**
靠 `label` 或显式 `hub: {channel: N}` 选择。

### What the hub makes testable / Hub 让哪些测试成为可能

- **Cold vs warm boot.** `reset_target(strategy="cold")` removes power, so `.noinit`
  persistence, RCC_CSR POR flags, and RAM decay become testable instead of assumed.
  It fails rather than silently doing a warm reset when no channel is mapped.
- **Watchdog and brownout behaviour**, which no SWD reset can reproduce.
- **Low-power current draw.** `hub(action=measure)` reads the rail while the core runs, so
  a Stop/Standby claim becomes a number. An MCU that entered Stop and one that only thinks
  it did are identical over SWD and differ by orders of magnitude here.
- **Unattended recovery.** A wedged probe is cleared by `recover_session`'s escalation
  ladder instead of needing someone to walk over and replug it.

Hub 让冷/热启动、看门狗与掉电行为、低功耗电流、以及无人值守的探针恢复真正可测。

`tests/hil/test_real_hub_smoke.py` covers these on hardware and is gated behind
`STM32_GDB_MCP_HIL_HUB=1` in addition to `STM32_GDB_MCP_HIL=1`, so a rig with a board but
no hub keeps running the existing smoke unchanged.

该文件额外由 `STM32_GDB_MCP_HIL_HUB=1` 控制，因此没有 Hub 的台子照常只跑原有 smoke。

Example smoke command / 烟测命令示例：

```bash
python -m pytest -q tests -m hil
```

Local smoke run (replace the profile for L151 or U535) / 本地烟测（L151 或 U535 请替换配置）：

```powershell
$env:STM32_GDB_MCP_HIL = "1"
$env:STM32_GDB_MCP_HIL_CONFIG = "examples/configs/stm32l431_openocd.yaml"
$env:STM32_GDB_MCP_HIL_REPORT = "artifacts/hil-l431.json"
python -m pytest -q tests/hil -m hil --junitxml=artifacts/hil-l431-junit.xml
```

English: The default HIL smoke is non-flashing. It uses the same public MCP path as
an AI client: `start_debug_session` → `self_check` → `continue_execution` →
`stop_debug_session`. The test requires a decoded ARM CPUID, a recognized Cortex-M
core, the profile's exact expected core, and a successful expected-family/device check; a raw
memory read alone is not success.

中文：默认 HIL 烟测不烧录固件，并使用与 AI 客户端相同的公开 MCP 调用链：
`start_debug_session` → `self_check` → `continue_execution` → `stop_debug_session`。
测试必须得到已解码的 ARM CPUID、与配置完全一致的 Cortex-M 内核，并通过预期 MCU 系列/器件检查；
仅仅“原始内存读取未报错”不算成功。

English: The repository ships this generic, config-driven identity smoke test. Firmware
behavior tests that depend on target wiring, reset behavior, or proprietary images should
remain private until they can be sanitized and generalized.

中文：仓库内置的是通用、配置驱动的目标身份烟测。依赖具体接线、复位行为或专有固件镜像的行为测试，
在能够脱敏并泛化之前应保留在私有环境中。

## Suggested Smoke Coverage / 建议烟测覆盖

- validate the debug config / 校验调试配置
- start a debug session / 启动调试会话
- run `self_check` and assert decoded CPUID/core/family / 运行 `self_check` 并断言已解码的 CPUID、内核和系列
- resume and close the session / 恢复运行并关闭会话

Additional board-specific checks may include / 额外的板卡专用检查可包括：

- flash a known firmware image only with explicit approval / 仅在明确批准后烧录已知固件镜像
- reset and halt / 复位并暂停
- read core registers / 读取内核寄存器
- set and hit a breakpoint near `main` / 在 `main` 附近设置并命中断点
- capture a debug snapshot / 采集调试快照
- decode one SVD peripheral register / 解码一个 SVD 外设寄存器
- read current FreeRTOS task when the firmware uses FreeRTOS / 当固件使用 FreeRTOS 时读取当前任务
- collect RTT or UART logs when enabled / 启用时采集 RTT 或 UART 日志

## Evidence to Keep / 需要保留的证据

For every HIL run, keep / 每次 HIL 运行请保留：

- workflow run URL / 工作流运行 URL
- config path and sanitized config contents / 配置路径和脱敏后的配置内容
- probe and board identity / 调试器和目标板标识
- firmware commit or build ID / 固件 commit 或构建 ID
- MCP responses for the smoke sequence / 烟测序列对应的 MCP 响应
- target logs around failures / 故障前后的目标日志
- uploaded JSON result and JUnit report (`artifacts/`) / 上传的 JSON 结果与 JUnit 报告（`artifacts/`）

English: This evidence is what lets an AI client or maintainer distinguish host
setup problems from firmware, probe, and MCP bugs.

中文：这些证据能帮助 AI 客户端或维护者区分主机环境问题、固件问题、调试器问题和 MCP 自身问题。
