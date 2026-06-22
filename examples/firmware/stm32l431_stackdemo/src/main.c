#include <stdint.h>

/*
 * Stack-overflow demo: mirrors the real-world bug where a large buffer is placed
 * on the stack (here 8 KiB) and blows a small stack budget. Build, flash, set a
 * breakpoint on `flash_write_buggy` (or the pageBuf write line), run_and_wait,
 * then analyze_stack(stack_size=0x800) -> overflow: true. The fix is to make the
 * buffer `static` (move it off the stack into .bss).
 */

volatile uint8_t g_sink;

void flash_write_buggy(void)
{
    /* 8 KiB on the stack — far beyond a typical 2 KiB stack budget. */
    volatile uint8_t pageBuf[8192];

    for (int i = 0; i < 8192; i++) {
        pageBuf[i] = (uint8_t)i;   /* SP is now ~8 KiB below _estack */
    }

    g_sink = pageBuf[4095];        /* break here: SP is at its lowest */
}

int main(void)
{
    for (volatile uint32_t i = 0; i < 100000u; i++) {
    }

    flash_write_buggy();

    while (1) {
    }
}
