#include <stdint.h>

#define RCC_AHB2ENR (*(volatile uint32_t *)0x4002104C)
#define GPIOA_MODER (*(volatile uint32_t *)0x48000000)
#define GPIOA_ODR (*(volatile uint32_t *)0x48000014)

#define GPIOAEN (1u << 0)
#define LED_PIN 5u

volatile uint32_t heartbeat;

static void delay(volatile uint32_t cycles)
{
    while (cycles-- > 0u) {
        __asm volatile ("nop");
    }
}

int main(void)
{
    RCC_AHB2ENR |= GPIOAEN;
    GPIOA_MODER &= ~(3u << (LED_PIN * 2u));
    GPIOA_MODER |= (1u << (LED_PIN * 2u));

    while (1) {
        heartbeat++;
        GPIOA_ODR ^= (1u << LED_PIN);
        delay(120000u);
    }
}
