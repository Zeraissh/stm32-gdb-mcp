"""Stack headroom / overflow analysis for Cortex-M.

The missing primitive for diagnosing stack overflows: instead of the agent
hand-computing SP against linker bounds, this reports used/free bytes and a clear
overflow verdict. The stack grows DOWN from ``stack_top`` (the initial MSP, i.e.
the first word of the vector table) toward ``stack_limit``; SP below the limit
means the stack has overflowed into whatever lives beneath it.
"""


def stack_report(sp, stack_top, stack_limit=None) -> dict:
    used = stack_top - sp if stack_top is not None else None
    free = (sp - stack_limit) if stack_limit is not None else None
    size = (stack_top - stack_limit) if (stack_top is not None and stack_limit is not None) else None
    overflow = bool(stack_limit is not None and sp < stack_limit)
    pct = round(100.0 * used / size, 1) if (used is not None and size) else None

    if used is not None and used < 0:
        summary = (f"SP {hex(sp)} is ABOVE stack_top {hex(stack_top)} — wrong stack pointer? "
                   f"check $msp vs $psp")
    elif overflow:
        assert free is not None  # overflow requires stack_limit, which makes free known
        summary = (f"STACK OVERFLOW: SP {hex(sp)} is {abs(free)} bytes below the stack limit "
                   f"{hex(stack_limit)} (size {size} B)")
    elif free is not None:
        summary = (f"stack OK: used {used} / {size} B ({pct}%), {free} B free "
                   f"(SP {hex(sp)}, limit {hex(stack_limit)})")
    else:
        summary = (f"SP {hex(sp)}, stack_top {hex(stack_top)}, used {used} B — "
                   f"provide stack_size or stack_limit for an overflow verdict")

    return {
        "sp": hex(sp),
        "stack_top": hex(stack_top) if stack_top is not None else None,
        "stack_limit": hex(stack_limit) if stack_limit is not None else None,
        "used_bytes": used,
        "free_bytes": free,
        "size_bytes": size,
        "pct_used": pct,
        "overflow": overflow,
        "summary": summary,
    }
