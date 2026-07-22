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
- Optional RTT or UART host tools when validating log capture / 验证日志采集时需要可选的 RTT 或 UART 主机工具

## Manual Workflow / 手动工作流

Run the GitHub Actions workflow named `Hardware-in-the-loop`.

运行名为 `Hardware-in-the-loop` 的 GitHub Actions 工作流。

Inputs / 输入参数：

- `board`: connected board profile, one of `l151`, `l431`, or `u535` / 当前连接的板卡配置，可选 `l151`、`l431` 或 `u535`
- `config_path`: optional YAML override; empty uses `examples/configs/stm32<board>_openocd.yaml` / 可选 YAML 路径覆盖；留空时使用 `examples/configs/stm32<board>_openocd.yaml`
- `smoke_command`: optional command executed after setup and config validation / 安装和配置校验后执行的可选烟测命令

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
