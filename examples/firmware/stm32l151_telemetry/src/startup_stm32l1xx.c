#include <stdint.h>

extern uint32_t _estack;
extern uint32_t _etext;
extern uint32_t _sdata;
extern uint32_t _edata;
extern uint32_t _sbss;
extern uint32_t _ebss;
extern uint32_t _snoinit;
extern uint32_t _enoinit;

#define NOINIT_MAGIC 0x4E4F494Eu

int main(void);
void Reset_Handler(void);
void Default_Handler(void);

/* Strong handlers — NOT weak-aliased to Default_Handler. */
void HardFault_Handler(void);
void SysTick_Handler(void);

/* All other exceptions remain weak-aliased to Default_Handler. */
void NMI_Handler(void)                 __attribute__((weak, alias("Default_Handler")));
void MemManage_Handler(void)           __attribute__((weak, alias("Default_Handler")));
void BusFault_Handler(void)            __attribute__((weak, alias("Default_Handler")));
void UsageFault_Handler(void)          __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void)                 __attribute__((weak, alias("Default_Handler")));
void DebugMon_Handler(void)            __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void)              __attribute__((weak, alias("Default_Handler")));

/* Cortex-M3 vector table */
__attribute__((section(".isr_vector")))
void (*const vector_table[])(void) = {
    (void (*)(void))(&_estack),
    Reset_Handler,
    NMI_Handler,
    HardFault_Handler,
    MemManage_Handler,
    BusFault_Handler,
    UsageFault_Handler,
    0,
    0,
    0,
    0,
    SVC_Handler,
    DebugMon_Handler,
    0,
    PendSV_Handler,
    SysTick_Handler,
};

void Reset_Handler(void)
{
    uint32_t *src;
    uint32_t *dst;

    /* Copy .data from flash to RAM */
    src = &_etext;
    dst = &_sdata;
    while (dst < &_edata) {
        *dst++ = *src++;
    }

    /* Zero-fill .bss */
    dst = &_sbss;
    while (dst < &_ebss) {
        *dst++ = 0u;
    }

    /* --- .noinit initialization ---
     * If the magic token at the start of .noinit is missing, this is the first
     * boot (cold boot / power-on). Zero the entire .noinit region and plant the
     * magic so subsequent warm resets preserve the data.
     */
    if (*(uint32_t *)&_snoinit != NOINIT_MAGIC) {
        dst = &_snoinit;
        while (dst < &_enoinit) {
            *dst++ = 0u;
        }
        *(uint32_t *)&_snoinit = NOINIT_MAGIC;
    }

    (void)main();

    while (1) {
    }
}

void Default_Handler(void)
{
    while (1) {
    }
}
