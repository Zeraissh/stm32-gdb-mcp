# STM32L431 Assert Demo / STM32L431 断言演示

English: `compute(0)` violates the `x > 0` precondition, firing `assert_failed()`
which parks in an infinite loop (the STM32 HAL `assert_failed(file, line)` pattern).
Validates the `assert` playbook: break on `assert_failed`, `read_frame_variables`
(file/line), `read_call_stack` (compute → main).

中文：`compute(0)` 违反 `x > 0` 前置条件，触发 `assert_failed()` 并停在死循环
（STM32 HAL `assert_failed(file, line)` 模式）。验证 `assert` playbook：断点
`assert_failed`、`read_frame_variables`（file/line）、`read_call_stack`（compute → main）。

## Build / 构建

```bash
cmake -S examples/firmware/stm32l431_assertdemo -B build/stm32l431_assertdemo -G Ninja -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l431_assertdemo/cmake/arm-none-eabi.cmake
cmake --build build/stm32l431_assertdemo
```

Flashing overwrites existing firmware — back up first. / 烧录会覆盖原固件，请先备份。
