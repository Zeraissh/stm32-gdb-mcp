# STM32L431 Fault Demo / STM32L431 故障演示

English: A tiny bare-metal STM32L431CCTx firmware that deliberately triggers a
Cortex-M fault, used to validate the fault-analysis tools (`diagnose_fault`,
`reconstruct_fault_context`). It enables divide-by-zero trapping
(`SCB->CCR.DIV_0_TRP`), runs briefly, then executes `100 / g_divisor` with
`g_divisor == 0`. That raises a `DIVBYZERO` UsageFault which escalates to a
HardFault; the strong `HardFault_Handler` parks in a tight loop so a debugger can
halt and inspect the stacked exception frame.

中文：一个很小的 STM32L431CCTx 裸机固件，故意触发 Cortex-M 故障，用于验证故障分析工具
（`diagnose_fault`、`reconstruct_fault_context`）。它开启除零陷阱（`SCB->CCR.DIV_0_TRP`），
短暂运行后执行 `100 / g_divisor`（`g_divisor == 0`），产生 `DIVBYZERO` UsageFault 并升级为
HardFault；强符号 `HardFault_Handler` 停在死循环，便于调试器停下来检查压栈帧。

## Build / 构建

```bash
cmake -S examples/firmware/stm32l431_faultdemo -B build/stm32l431_faultdemo -G Ninja -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l431_faultdemo/cmake/arm-none-eabi.cmake
cmake --build build/stm32l431_faultdemo
```

Outputs / 输出：

- `build/stm32l431_faultdemo/stm32l431_faultdemo.elf`
- `build/stm32l431_faultdemo/stm32l431_faultdemo.bin`
- `build/stm32l431_faultdemo/stm32l431_faultdemo.map`

## Expected fault-tool output / 预期工具输出

After flashing, set a breakpoint on `HardFault_Handler`, `run_and_wait`, then
`reconstruct_fault_context` reports:

- `fault_classes: ['UsageFault']`, `active_flags: ['DIVBYZERO']`, `hfsr: ['FORCED']`
- faulting PC resolving to the `100 / g_divisor` line in `src/main.c`
  (`trigger_divzero`)

This is the exact scenario used to HIL-validate the fault tools on an
STM32L431CCT6 over ST-Link/OpenOCD.

烧录后，在 `HardFault_Handler` 设置断点并执行 `run_and_wait`，再运行
`reconstruct_fault_context`。预期得到：

- `fault_classes: ['UsageFault']`、`active_flags: ['DIVBYZERO']`、`hfsr: ['FORCED']`
- 故障 PC 解析到 `src/main.c` 中 `trigger_divzero` 的 `100 / g_divisor` 行

该场景用于在 STM32L431CCT6 + ST-Link/OpenOCD 上验证故障分析工具。

## Flashing / 烧录

English: Flashing is manual and opt-in, and **overwrites existing firmware**. Back
up the board first if you need to preserve it
(`openocd -f interface/stlink.cfg -f target/stm32l4x.cfg -c "init" -c "reset halt" -c "dump_image backup.bin 0x08000000 0x40000" -c "shutdown"`).

中文：烧录需手动、显式执行，并会**覆盖原有固件**。如需保留请先备份
（`openocd -f interface/stlink.cfg -f target/stm32l4x.cfg -c "init" -c "reset halt" -c "dump_image backup.bin 0x08000000 0x40000" -c "shutdown"`）。
