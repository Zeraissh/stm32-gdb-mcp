# STM32L431 Blinky Example / STM32L431 闪灯示例

English: This is a tiny bare-metal STM32L431CCTx firmware example for hardware
integration smoke tests. It enables GPIOA, configures PA5 as an output, toggles
it in a loop, and increments the global `heartbeat` variable so GDB can inspect
visible target progress.

中文：这是一个很小的 STM32L431CCTx 裸机固件示例，用于硬件集成烟测。它启用 GPIOA，将
PA5 配置为输出，在循环中翻转 PA5，并递增全局变量 `heartbeat`，方便 GDB 观察目标是否在运行。

## Build / 构建

```bash
cmake -S examples/firmware/stm32l431_blinky -B build/stm32l431_blinky -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l431_blinky/cmake/arm-none-eabi.cmake
cmake --build build/stm32l431_blinky
```

Outputs / 输出：

- `build/stm32l431_blinky/stm32l431_blinky.elf`
- `build/stm32l431_blinky/stm32l431_blinky.bin`
- `build/stm32l431_blinky/stm32l431_blinky.map`

## Flashing / 烧录

English: Flashing is manual and opt-in. Do not run this on a board that contains
firmware you need to preserve.

中文：烧录需要手动、显式执行。不要在需要保留原固件的板卡上运行。

Example / 示例：

```bash
STM32_Programmer_CLI -c port=SWD mode=UR reset=HWrst -w build/stm32l431_blinky/stm32l431_blinky.elf -v -rst
```
