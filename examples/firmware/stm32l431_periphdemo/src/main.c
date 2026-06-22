#include <stdint.h>

/*
 * Peripheral-not-working demo: configure PA5 as an output, but FORGET to enable
 * the GPIOA clock in RCC. With the clock gated, writes to the GPIOA registers
 * have no effect (the peripheral is "dead"). This is the single most common
 * "my peripheral doesn't work" bug.
 *
 * Diagnosis (peripheral playbook): RCC->AHB2ENR.GPIOAEN reads 0 -> clock off.
 * Fix: RCC_AHB2ENR |= (1u << 0);  before touching GPIOA.
 */

#define RCC_AHB2ENR (*(volatile uint32_t *)0x4002104C)
#define GPIOA_MODER (*(volatile uint32_t *)0x48000000)
#define GPIOA_ODR   (*(volatile uint32_t *)0x48000014)

volatile uint32_t heartbeat;

int main(void)
{
    /* BUG: missing  RCC_AHB2ENR |= (1u << 0);  (GPIOAEN) */

    GPIOA_MODER &= ~(3u << (5u * 2u));
    GPIOA_MODER |=  (1u << (5u * 2u));   /* PA5 -> output; no effect, clock gated */

    while (1) {
        heartbeat++;
        GPIOA_ODR ^= (1u << 5u);
    }
}
