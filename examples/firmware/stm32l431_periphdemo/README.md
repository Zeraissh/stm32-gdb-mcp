# STM32L431 Peripheral-Not-Working Demo / STM32L431 外设不工作演示

English: PA5 is configured as an output, but the GPIOA clock is never enabled in
`RCC->AHB2ENR` — so the GPIO is clock-gated and dead. Used to validate the
`peripheral` playbook: `RCC.AHB2ENR.GPIOAEN` reads 0 (clock off) and `GPIOA.MODER`
does not take the configured value. The fix is `RCC_AHB2ENR |= (1u << 0);`.

中文：PA5 被配置为输出，但从未在 `RCC->AHB2ENR` 里使能 GPIOA 时钟——GPIO 被时钟门控、
失效。用于验证 `peripheral`（外设）playbook：`RCC.AHB2ENR.GPIOAEN` 读出为 0（时钟未开），
`GPIOA.MODER` 配置写不进去。修复方法是 `RCC_AHB2ENR |= (1u << 0);`。

## Build / 构建

```bash
cmake -S examples/firmware/stm32l431_periphdemo -B build/stm32l431_periphdemo -G Ninja -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l431_periphdemo/cmake/arm-none-eabi.cmake
cmake --build build/stm32l431_periphdemo
```

## Flashing / 烧录

English: Manual, **overwrites existing firmware** — back up first.
中文：需手动执行，会**覆盖原有固件**——请先备份。
