# STM32L151 Fault Demo / STM32L151 故障演示

English: A tiny bare-metal STM32L151xC (Cortex-M3) firmware that deliberately
triggers a Cortex-M fault, used to validate the fault-analysis tools
(`diagnose_fault`, `reconstruct_fault_context`) on the STM32L1 family. It enables
divide-by-zero trapping (`SCB->CCR.DIV_0_TRP`), runs briefly, then calls
`main → app_init → process_config`, where `process_config` executes
`100 / g_divisor` with `g_divisor == 0`. That raises a `DIVBYZERO` UsageFault
which escalates to a HardFault; the strong `HardFault_Handler` parks in a tight
loop so a debugger can halt and inspect the stacked exception frame and call
chain.

中文：一个很小的 STM32L151xC（Cortex-M3）裸机固件，故意触发 Cortex-M 故障，用于在 STM32L1
系列上验证故障分析工具（`diagnose_fault`、`reconstruct_fault_context`）。它开启除零陷阱
（`SCB->CCR.DIV_0_TRP`），短暂运行后经 `main → app_init → process_config` 调用链，在
`process_config` 中执行 `100 / g_divisor`（`g_divisor == 0`），产生 `DIVBYZERO` UsageFault
并升级为 HardFault；强符号 `HardFault_Handler` 停在死循环，便于调试器停下来检查压栈帧与调用链。

This is the L1/Cortex-M3 counterpart to `stm32l431_faultdemo` (L4/Cortex-M4).
The startup vector table and `main.c` are architecture-generic and identical in
spirit; only the compile flags (`-mcpu=cortex-m3`, soft float) and the linker
memory sizes (256K flash / 32K RAM, dev_id 0x427) differ.

本例是 `stm32l431_faultdemo`（L4/Cortex-M4）的 L1/Cortex-M3 对应版本。startup 向量表与
`main.c` 属架构通用、思路一致；仅编译选项（`-mcpu=cortex-m3`、软浮点）与链接脚本内存大小
（256K flash / 32K RAM，dev_id 0x427）不同。

## Build / 构建

```bash
cmake -S examples/firmware/stm32l151_faultdemo -B build/stm32l151_faultdemo -G Ninja -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l151_faultdemo/cmake/arm-none-eabi.cmake
cmake --build build/stm32l151_faultdemo
```

Outputs / 输出：

- `build/stm32l151_faultdemo/stm32l151_faultdemo.elf`
- `build/stm32l151_faultdemo/stm32l151_faultdemo.bin`
- `build/stm32l151_faultdemo/stm32l151_faultdemo.map`

## Expected fault-tool output / 预期工具输出

After flashing, halt the core (it parks in `HardFault_Handler`), `load_symbols`,
then `reconstruct_fault_context` reports:

- `fault_classes: ['UsageFault']`, `active_flags: ['DIVBYZERO']`, `hfsr: ['FORCED']`
  (CFSR = 0x02000000, HFSR = 0x40000000)
- faulting PC `0x08000042` resolving to the `100 / divisor` line in `src/main.c`
  (`process_config`)
- call chain `main → app_init → process_config`

This is the exact scenario used to HIL-validate the fault tools on an STM32L151xC
(STM32L151/152 Cat.3) over ST-Link/OpenOCD.

烧录后，让核心停下（它停在 `HardFault_Handler`），`load_symbols`，再运行
`reconstruct_fault_context`。预期得到：

- `fault_classes: ['UsageFault']`、`active_flags: ['DIVBYZERO']`、`hfsr: ['FORCED']`
  （CFSR = 0x02000000、HFSR = 0x40000000）
- 故障 PC `0x08000042` 解析到 `src/main.c` 中 `process_config` 的 `100 / divisor` 行
- 调用链 `main → app_init → process_config`

该场景用于在 STM32L151xC（STM32L151/152 Cat.3）+ ST-Link/OpenOCD 上验证故障分析工具。

## Flashing / 烧录

English: Flashing is manual and opt-in, and **overwrites existing firmware**. Back
up the board first if you need to preserve it
(`openocd -f interface/stlink.cfg -f target/stm32l1.cfg -c "init" -c "reset halt" -c "dump_image backup.bin 0x08000000 0x40000" -c "shutdown"`).
If the board's current firmware has locked out SWD (low-power mode or remapped
PA13/PA14) and NRST is not wired, power-cycle the board and connect during the
brief boot window before the firmware runs.

中文：烧录需手动、显式执行，并会**覆盖原有固件**。如需保留请先备份
（`openocd -f interface/stlink.cfg -f target/stm32l1.cfg -c "init" -c "reset halt" -c "dump_image backup.bin 0x08000000 0x40000" -c "shutdown"`）。
若板上原固件已锁死 SWD（进入低功耗或重配了 PA13/PA14）且未接 NRST，请给板子上电复位，
并在固件运行前的短暂启动窗口内连接。
