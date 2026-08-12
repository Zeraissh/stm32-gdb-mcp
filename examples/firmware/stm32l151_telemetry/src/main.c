#include <stdint.h>

/* --------------------------------------------------------------------------
 * Register definitions
 * -------------------------------------------------------------------------- */

/* RCC */
#define RCC_BASE      0x40023800u
#define RCC_CR        (*(volatile uint32_t *)(RCC_BASE + 0x00))
#define RCC_CFGR      (*(volatile uint32_t *)(RCC_BASE + 0x08))

#define RCC_CR_HSION   (1u << 0)
#define RCC_CR_HSIRDY  (1u << 1)
#define RCC_CR_MSION   (1u << 8)
#define RCC_CR_MSIRDY  (1u << 9)

#define RCC_CFGR_SW_HSI    (1u << 0)   /* SW = 01 */
#define RCC_CFGR_SWS_HSI   (1u << 2)   /* SWS = 01 */

/* SysTick */
typedef struct {
    volatile uint32_t CTRL;
    volatile uint32_t LOAD;
    volatile uint32_t VAL;
    volatile uint32_t CALIB;
} SysTick_Type;

#define SysTick_BASE  0xE000E010u
#define SysTick       ((SysTick_Type *)SysTick_BASE)

#define SysTick_CTRL_ENABLE    (1u << 0)
#define SysTick_CTRL_TICKINT   (1u << 1)
#define SysTick_CTRL_CLKSOURCE (1u << 2)
#define SysTick_CTRL_VALUE_7   (SysTick_CTRL_ENABLE | SysTick_CTRL_TICKINT | SysTick_CTRL_CLKSOURCE)

/* SCB */
#define SCB_CCR       (*(volatile uint32_t *)0xE000ED14u)
#define SCB_CFSR      (*(volatile uint32_t *)0xE000ED28u)
#define SCB_HFSR      (*(volatile uint32_t *)0xE000ED2Cu)

#define SCB_CCR_DIV_0_TRP  (1u << 4)

/* --------------------------------------------------------------------------
 * Magic constants
 * -------------------------------------------------------------------------- */
#define TELEM_MAGIC  0x54454C4Du  /* "TELM" */
#define FAULT_MAGIC  0x464C5448u  /* "FLTH" */

#define HCLK_HZ      16000000u
#define SYSTICK_LOAD (HCLK_HZ / 1000u - 1u)  /* 1 ms tick */

/* --------------------------------------------------------------------------
 * .noinit section — survives warm resets
 * -------------------------------------------------------------------------- */

/* Startup validation token — placed in .noinit_head so the linker
 * positions it first, guaranteeing *(&_snoinit) == g_noinit_magic. */
__attribute__((section(".noinit_head")))
uint32_t g_noinit_magic;

/* Boot counter: incremented once per reset, survives resets. */
__attribute__((section(".noinit")))
uint32_t g_boot_count;

/* Fault latch: written once by HardFault_Handler, survives resets for post-
 * mortem inspection. */
__attribute__((section(".noinit")))
struct {
    uint32_t magic;
    uint32_t cfsr;
    uint32_t hfsr;
    uint32_t stacked_pc;
    uint32_t stacked_lr;
} g_fault_latch;

/* --------------------------------------------------------------------------
 * Regular .bss / .data variables
 * -------------------------------------------------------------------------- */

/* Telemetry: reset on every boot. */
volatile struct {
    uint32_t magic;
    uint32_t uptime_ms;
    uint32_t hclk_hz;
} g_telemetry;

/* Fault injection request. Write 1 to trigger a divide-by-zero HardFault. */
volatile uint32_t g_fault_request;

/* --------------------------------------------------------------------------
 * Clock: switch SYSCLK from MSI (default) to HSI 16 MHz
 * -------------------------------------------------------------------------- */
static void system_clock_hsi16(void)
{
    /* ① Enable HSI */
    RCC_CR |= RCC_CR_HSION;
    while (!(RCC_CR & RCC_CR_HSIRDY)) {
        /* wait for HSIRDY */
    }

    /* ② Switch SYSCLK to HSI (SW = 01) */
    RCC_CFGR = (RCC_CFGR & ~0x3u) | RCC_CFGR_SW_HSI;
    while ((RCC_CFGR & 0xCu) != RCC_CFGR_SWS_HSI) {
        /* wait for SWS to confirm HSI */
    }

    /* ③ Optionally disable MSI to save power */
    RCC_CR &= ~RCC_CR_MSION;
    while (RCC_CR & RCC_CR_MSIRDY) {
        /* wait for MSIRDY to clear */
    }
}

/* --------------------------------------------------------------------------
 * SysTick: 1 ms tick using HCLK (16 MHz)
 * -------------------------------------------------------------------------- */
static void systick_init(void)
{
    SysTick->LOAD = SYSTICK_LOAD;
    SysTick->VAL  = 0;
    SysTick->CTRL = SysTick_CTRL_VALUE_7;
}

/* --------------------------------------------------------------------------
 * ISR: SysTick_Handler (strong symbol, defined in startup)
 * -------------------------------------------------------------------------- */
void SysTick_Handler(void)
{
    g_telemetry.uptime_ms++;
}

/* --------------------------------------------------------------------------
 * ISR: HardFault_Handler (strong symbol, defined in startup)
 * -------------------------------------------------------------------------- */
void HardFault_Handler(void)
{
    uint32_t msp;
    uint32_t *frame;

    /* Read MSP (Main Stack Pointer) from the exception frame */
    __asm__ volatile ("MRS %0, MSP" : "=r" (msp));
    frame = (uint32_t *)msp;

    /* Latch fault context into .noinit for post-mortem */
    g_fault_latch.magic      = FAULT_MAGIC;
    g_fault_latch.cfsr       = SCB_CFSR;
    g_fault_latch.hfsr       = SCB_HFSR;
    g_fault_latch.stacked_pc = frame[6];  /* offset 0x18 = word 6 */
    g_fault_latch.stacked_lr = frame[5];  /* offset 0x14 = word 5 */

    /* Park here forever — let the debugger inspect the state */
    while (1) {
    }
}

/* --------------------------------------------------------------------------
 * main()
 * -------------------------------------------------------------------------- */
int main(void)
{
    /* a) Switch to HSI 16 MHz */
    system_clock_hsi16();

    /* b) Start 1 ms SysTick */
    systick_init();

    /* c) Boot count: first power-on (magic invalid → .noinit zeroed) → 1;
     *    each subsequent warm reset → +1.  g_boot_count lives in .noinit
     *    and survives resets; Reset_Handler zeros it on the very first boot. */
    if (g_boot_count == 0) {
        g_boot_count = 1;
    } else {
        g_boot_count++;
    }

    /* d) Initialize telemetry */
    g_telemetry.magic     = TELEM_MAGIC;
    g_telemetry.uptime_ms = 0;
    g_telemetry.hclk_hz   = HCLK_HZ;

    /* e) Fault request initially 0 */
    g_fault_request = 0;

    /* Main loop */
    while (1) {
        if (g_fault_request == 1) {
            /* Enable divide-by-zero UsageFault trapping */
            SCB_CCR |= SCB_CCR_DIV_0_TRP;

            /* Trigger a divide-by-zero → UsageFault → escalates to HardFault */
            volatile int zero = 0;
            volatile int boom = 1 / zero;
            (void)boom;
        }
    }
}
