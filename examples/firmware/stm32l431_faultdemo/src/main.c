#include <stdint.h>

/* Cortex-M Configuration and Control Register: bit 4 = DIV_0_TRP. */
#define SCB_CCR (*(volatile uint32_t *)0xE000ED14)
#define DIV_0_TRP (1u << 4)

volatile uint32_t heartbeat;
volatile int g_divisor = 0;   /* volatile so the compiler cannot fold the divide */
volatile int g_result;

/* Strong handler so a HardFault parks here in a tight loop for the debugger. */
void HardFault_Handler(void)
{
    while (1) {
    }
}

/* The faulting instruction (an integer divide by zero) lives on this line. */
static void trigger_divzero(void)
{
    g_result = 100 / g_divisor;
}

int main(void)
{
    /* Trap divide-by-zero as a UsageFault; it escalates to HardFault. */
    SCB_CCR |= DIV_0_TRP;

    for (volatile uint32_t i = 0; i < 200000u; i++) {
        heartbeat++;
    }

    trigger_divzero();

    while (1) {
    }
}
