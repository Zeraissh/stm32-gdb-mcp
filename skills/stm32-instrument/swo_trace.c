/* swo_trace.c — implementation of the printf-style SWO_LOG text channel.
 * Compiled to nothing when SWO_TRACE_ENABLED == 0. */
#include "swo_trace.h"

#if SWO_TRACE_ENABLED

#include <stdarg.h>
#include <stdio.h>

/* Text on port 0. This MAY block briefly while the FIFO drains, so use SWO_LOG only
 * on non-hot paths (init, state changes, error/return-fail, fault handlers). For ISRs
 * and hot loops use SWO_EVENT (1 byte, drop-on-full) instead. */
void swo_log(const char *fmt, ...) {
    if (!swo__ready(SWO_PORT_TEXT)) {
        return;  /* no host attached — don't spin, don't cost anything */
    }
    char buf[128];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n < 0) {
        return;
    }
    if (n > (int)sizeof buf - 1) {
        n = (int)sizeof buf - 1;  /* truncated, but never overruns */
    }
    for (int i = 0; i < n; i++) {
        while (ITM->PORT[SWO_PORT_TEXT].u32 == 0u) {
            /* wait for FIFO space; only reached when a host is actively draining */
        }
        ITM->PORT[SWO_PORT_TEXT].u8 = (uint8_t)buf[i];
    }
}

#endif /* SWO_TRACE_ENABLED */
