#include <stdint.h>

/*
 * Assert demo: a precondition (x > 0) is violated, firing assert_failed(), which
 * parks in an infinite loop — the classic STM32 HAL assert_failed(file, line)
 * pattern. Diagnosis (assert playbook): break on assert_failed, read the frame
 * variables (file/line), and read the backtrace to see who called it.
 */

volatile uint32_t g_assert_line;
volatile const char *g_assert_file;

void assert_failed(const char *file, uint32_t line)
{
    g_assert_file = file;
    g_assert_line = line;
    while (1) {              /* park here for the debugger */
    }
}

static int compute(int x)
{
    if (!(x > 0)) {
        assert_failed(__FILE__, (uint32_t)__LINE__);   /* precondition violated */
    }
    return 100 / x;
}

volatile int g_result;

int main(void)
{
    for (volatile uint32_t i = 0; i < 100000u; i++) {
    }

    g_result = compute(0);   /* BUG: passes 0 -> assert fires */

    while (1) {
    }
}
