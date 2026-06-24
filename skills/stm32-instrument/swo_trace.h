/* swo_trace.h — gated SWO/ITM tracing for AI-assisted debugging.
 *
 * Drop this (and swo_trace.c) into a firmware project so an agent can instrument
 * decision points cheaply and read them back over the single SWO pin with
 * stm32-gdb-mcp (setup_swo + logging(channel="swo")).
 *
 * Design goals (why this is safe to leave in the tree):
 *   - ZERO cost in release: with SWO_TRACE_ENABLED=0 every macro compiles to nothing.
 *   - Non-perturbing in hot paths/ISRs: SWO_EVENT/SWO_MARK DROP the sample if the ITM
 *     FIFO is full or trace is disabled — they never block, so timing is preserved.
 *   - Structured, not noisy: fixed port convention (text / events / your channels).
 *
 * Usage:
 *   - Build with -DSWO_TRACE_ENABLED=1 -DSWO_TRACE_INCLUDE="\"stm32l4xx.h\""
 *     (point SWO_TRACE_INCLUDE at your CMSIS device header that provides `ITM`).
 *   - SWO_LOG("state=%d\n", s);   // human text, NON-hot paths only (blocks briefly)
 *   - SWO_EVENT(EVT_ISR_UART);    // 1 byte, safe anywhere incl. ISRs (drop-on-full)
 *   - SWO_MARK(2, value);         // 32-bit value on your own stimulus port
 */
#ifndef SWO_TRACE_H
#define SWO_TRACE_H

#ifndef SWO_TRACE_ENABLED
#define SWO_TRACE_ENABLED 0
#endif

/* Port convention — keep instrumentation structured, not a wall of printf.
 *   port 0 : human-readable text   (SWO_LOG)   — non-hot paths only
 *   port 1 : 1-byte event codes     (SWO_EVENT) — ISR/hot-path safe, drop-on-full
 *   port 2+: your own structured channels (SWO_MARK) */
#define SWO_PORT_TEXT   0
#define SWO_PORT_EVENT  1

#if SWO_TRACE_ENABLED

#ifdef SWO_TRACE_INCLUDE
#include SWO_TRACE_INCLUDE   /* your CMSIS device header, e.g. "stm32l4xx.h" */
#endif
#include <stdint.h>

/* trace enabled AND this stimulus port enabled (host attached & configured) */
static inline int swo__ready(uint32_t port) {
    return (ITM->TCR & ITM_TCR_ITMENA_Msk) && (ITM->TER & (1UL << port));
}

/* Non-blocking 32-bit write: drop if the FIFO is full so timing is never perturbed. */
static inline void swo_mark(uint32_t port, uint32_t value) {
    if (swo__ready(port) && ITM->PORT[port].u32 != 0u) {
        ITM->PORT[port].u32 = value;
    }
}

/* Non-blocking 1-byte event — the right tool for ISRs and hot loops. */
static inline void swo_event(uint8_t code) {
    if (swo__ready(SWO_PORT_EVENT) && ITM->PORT[SWO_PORT_EVENT].u32 != 0u) {
        ITM->PORT[SWO_PORT_EVENT].u8 = code;
    }
}

void swo_log(const char *fmt, ...);  /* printf-style text on port 0 (see swo_trace.c) */

#define SWO_LOG(...)     swo_log(__VA_ARGS__)
#define SWO_EVENT(code)  swo_event((uint8_t)(code))
#define SWO_MARK(p, v)   swo_mark((uint32_t)(p), (uint32_t)(v))

#else  /* SWO_TRACE_ENABLED == 0 : compiles to nothing, zero cost in release */

#define SWO_LOG(...)     ((void)0)
#define SWO_EVENT(code)  ((void)0)
#define SWO_MARK(p, v)   ((void)0)

#endif /* SWO_TRACE_ENABLED */
#endif /* SWO_TRACE_H */
