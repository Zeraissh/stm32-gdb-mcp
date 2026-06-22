# STM32L431 Stack-Overflow Demo / STM32L431 栈溢出演示

English: A tiny bare-metal STM32L431CCTx firmware that reproduces a classic stack
overflow — an 8 KiB buffer placed on the stack in `flash_write_buggy()`, far beyond
a typical ~2 KiB stack budget. Used to validate the `analyze_stack` tool and the
`stack-overflow` playbook. The fix is to make the buffer `static` (move it to `.bss`).

中文：一个很小的 STM32L431CCTx 裸机固件，复现典型栈溢出——在 `flash_write_buggy()` 里
把 8 KiB 缓冲区放在**栈上**，远超约 2 KiB 的栈预算。用于验证 `analyze_stack` 工具与
`stack-overflow` playbook。修复方法是把缓冲区改为 `static`（移入 `.bss`）。

## Build / 构建

```bash
cmake -S examples/firmware/stm32l431_stackdemo -B build/stm32l431_stackdemo -G Ninja -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l431_stackdemo/cmake/arm-none-eabi.cmake
cmake --build build/stm32l431_stackdemo
```

## Expected tool output / 预期工具输出

Break on `flash_write_buggy` (after the array is written), then:

- `analyze_stack(stack_size="0x800")` → `overflow: true`, with SP several KiB below
  the stack limit.
- `read_call_stack` → shows `flash_write_buggy` as the frame consuming the stack.

## Flashing / 烧录

English: Flashing is manual and **overwrites existing firmware** — back up first.

中文：烧录需手动执行，会**覆盖原有固件**——请先备份。
