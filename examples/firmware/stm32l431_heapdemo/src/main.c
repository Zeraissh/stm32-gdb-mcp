#include <stdint.h>

/*
 * Heap-leak demo: a bump allocator hands out memory but NEVER frees it, so the
 * free-heap metric g_free_bytes declines on every loop until the heap is
 * exhausted (and goes negative). Diagnosis (heap playbook): track g_free_bytes
 * over time -> a monotonic decline is a leak. The fix is to free what you alloc.
 */

#define HEAP_SIZE 4096u

static uint8_t heap[HEAP_SIZE];
static uint32_t heap_used;

volatile int32_t g_free_bytes = (int32_t)HEAP_SIZE;   /* the free-heap metric to track */

static void *leaky_alloc(uint32_t n)
{
    void *p = &heap[heap_used % HEAP_SIZE];
    heap_used += n;                                    /* BUG: never freed -> leak */
    g_free_bytes = (int32_t)HEAP_SIZE - (int32_t)heap_used;
    return p;
}

volatile uint8_t *g_last;

int main(void)
{
    while (1) {
        g_last = (volatile uint8_t *)leaky_alloc(64u); /* leaks 64 B per loop */
        *g_last = 0xAA;
        for (volatile uint32_t i = 0; i < 200000u; i++) {
        }
    }
}
