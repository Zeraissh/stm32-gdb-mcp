# STM32L151 Telemetry / STM32L151 遥测固件

English: A bare-metal STM32L151xC (Cortex-M3) firmware that demonstrates
telemetry collection, `.noinit` persistence across warm resets, and
HardFault context latching. It switches the system clock from MSI to HSI
16 MHz, runs a 1 ms SysTick, collects uptime and boot counts, and provides
a fault-injection trigger (`g_fault_request = 1`) that deliberately causes
a divide-by-zero HardFault. The fault context (CFSR, HFSR, stacked PC/LR)
is latched into `.noinit` RAM for post-mortem inspection by a debugger.

中文：一个 STM32L151xC（Cortex-M3）裸机固件，演示遥测采集、`.noinit` 跨热复位数据保持、
以及 HardFault 上下文闩锁。它将系统时钟从 MSI 切换到 HSI 16 MHz，运行 1 ms SysTick，
采集运行时间和启动次数，并提供故障注入触发（`g_fault_request = 1`）来故意触发除零
HardFault。故障上下文（CFSR、HFSR、压栈 PC/LR）被闩锁到 `.noinit` RAM 中，供调试器
事后检查。

English: Key features:
- Clock: explicit switch from MSI (reset default) to HSI 16 MHz via RCC registers.
- SysTick: 1 ms tick using HCLK (16 MHz), no external crystal required.
- Telemetry (`g_telemetry`): magic `0x54454C4D`, `uptime_ms`, `hclk_hz` — reset on every boot.
- `.noinit` persistence: `g_boot_count` and `g_fault_latch` survive warm resets.
  On first cold boot the `.noinit` section is zeroed and a magic token
  (`0x4E4F494E` = "NOIN") is planted; on subsequent warm resets the section is
  preserved.
- Fault latch (`g_fault_latch`): HardFault_Handler reads MSP, extracts stacked
  PC/LR from the exception frame, reads SCB CFSR/HFSR, and writes everything
  with magic `0x464C5448` ("FLTH") into `.noinit`.
- Fault injection: set `g_fault_request = 1` (via debugger) to enable
  `SCB_CCR.DIV_0_TRP` and execute `1 / 0`.

中文：主要特性：
- 时钟：通过 RCC 寄存器显式从 MSI（复位默认）切换到 HSI 16 MHz。
- SysTick：使用 HCLK（16 MHz）产生 1 ms 节拍，无需外部晶振。
- 遥测（`g_telemetry`）：magic `0x54454C4D`、`uptime_ms`、`hclk_hz`——每次启动复位。
- `.noinit` 持久化：`g_boot_count` 和 `g_fault_latch` 在热复位后保持。首次冷启动时
  `.noinit` 段被清零并植入 magic token（`0x4E4F494E` = "NOIN"）；后续热复位时该段保持不变。
- 故障闩锁（`g_fault_latch`）：HardFault_Handler 读取 MSP，从异常压栈帧提取 PC/LR，
  读取 SCB CFSR/HFSR，并将所有信息连同 magic `0x464C5448`（"FLTH"）写入 `.noinit`。
- 故障注入：通过调试器设置 `g_fault_request = 1`，开启 `SCB_CCR.DIV_0_TRP` 并执行 `1 / 0`。

## Build / 构建

```bash
cmake -S examples/firmware/stm32l151_telemetry -B examples/firmware/stm32l151_telemetry/build -G Ninja -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l151_telemetry/cmake/arm-none-eabi.cmake
cmake --build examples/firmware/stm32l151_telemetry/build
```

Or from the project directory:

```bash
cmake -S . -B build -G Ninja -DCMAKE_TOOLCHAIN_FILE=cmake/arm-none-eabi.cmake
cmake --build build
```

Outputs / 输出：

- `build/stm32l151_telemetry.elf`
- `build/stm32l151_telemetry.bin`
- `build/stm32l151_telemetry.map`

## File layout / 文件结构

```
stm32l151_telemetry/
├── CMakeLists.txt
├── README.md
├── cmake/
│   └── arm-none-eabi.cmake
├── linker/
│   └── STM32L151RCTx_FLASH.ld
└── src/
    ├── main.c
    └── startup_stm32l1xx.c
```

## Memory layout / 内存布局

English: The `.noinit` output section (`NOLOAD`, RAM, 4-byte aligned) sits between
`.bss` and `._user_heap_stack`. Symbols `_snoinit` / `_enoinit` delimit the region.
A dedicated `.noinit_head` input section holds the startup validation token,
guaranteeing `*(&_snoinit)` is always the token, independent of how the linker
orders the remaining `.noinit` variables.

中文：`.noinit` 输出段（`NOLOAD`，位于 RAM，4 字节对齐）位于 `.bss` 和 `._user_heap_stack`
之间。`_snoinit` / `_enoinit` 符号界定该区域。专用的 `.noinit_head` 输入段保存启动验证
token，确保无论链接器如何排列其余 `.noinit` 变量，`*(&_snoinit)` 始终是 token。
