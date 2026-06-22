# STM32L431 Heap-Leak Demo / STM32L431 堆泄漏演示

English: A bump allocator (`leaky_alloc`) hands out 64 B per loop and never frees,
so `g_free_bytes` declines monotonically until the heap is exhausted. Validates the
`heap` playbook: track `g_free_bytes` over time → a steady decline is a leak.

中文：bump 分配器（`leaky_alloc`）每轮分配 64 字节且从不释放，`g_free_bytes` 单调
下降直到堆耗尽。验证 `heap` playbook：跟踪 `g_free_bytes` 趋势 → 持续下降即泄漏。

## Build / 构建

```bash
cmake -S examples/firmware/stm32l431_heapdemo -B build/stm32l431_heapdemo -G Ninja -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l431_heapdemo/cmake/arm-none-eabi.cmake
cmake --build build/stm32l431_heapdemo
```

Flashing overwrites existing firmware — back up first. / 烧录会覆盖原固件，请先备份。
