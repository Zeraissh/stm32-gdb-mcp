"""Cortex-M DWT cycle counter and PC sampling helpers.

The Data Watchpoint and Trace unit gives cheap, on-chip timing: ``DWT_CYCCNT``
is a free-running cycle counter useful for "how long did this take" without any
external tooling, and ``DWT_PCSR`` periodically samples the program counter
while the core runs — a poor-man's statistical profiler for locating hangs or
hot spots.

Addresses are the architectural Cortex-M debug addresses (same across the M3/M4/
M7 families this project targets).
"""

DEMCR = 0xE000EDFC      # Debug Exception and Monitor Control Register
DWT_CTRL = 0xE0001000   # DWT Control Register
DWT_CYCCNT = 0xE0001004  # DWT Cycle Count Register
DWT_PCSR = 0xE000101C   # DWT Program Counter Sample Register

DEMCR_TRCENA = 1 << 24
DWT_CTRL_CYCCNTENA = 1 << 0

PCSR_UNSAMPLEABLE = 0xFFFFFFFF  # DWT_PCSR reads this when the PC can't be sampled

# A zero PCSR read is never a program counter, and has two very different causes,
# both measured on hardware:
#   * the trace unit is not clocked (DEMCR.TRCENA clear), or
#   * the debug link cannot service memory reads while the core runs at all -- on
#     OpenOCD + ST-Link + STM32L151 EVERY read returns 0x00000000 while running,
#     DEMCR included, even though it reads 0x01000000 the moment the core halts.
# Either way nothing was sampled. Counting these as PCs produced a 100% hot spot
# at address 0 -- a confident wrong answer that would have an agent conclude the
# firmware executes at the vector table.
PCSR_ZERO = 0x00000000

# Never a PC on a Cortex-M executing real code.
PCSR_INVALID = (PCSR_UNSAMPLEABLE, PCSR_ZERO)


def enable_pc_sampling_writes():
    """Writes that turn on the trace clock so ``DWT_PCSR`` samples while the core runs.

    PC sampling is read non-intrusively over SWD (the debug AP reads the memory-mapped
    DWT register while the core keeps running) — no SWO pin or firmware change needed.
    """
    return [(DEMCR, DEMCR_TRCENA)]


def build_pc_profile(samples, symbolize):
    """Aggregate raw ``DWT_PCSR`` samples into a symbolized hot-spot histogram.

    ``samples``  : list[int] of PCSR reads (``0xFFFFFFFF`` = core not running code,
                   ``0x00000000`` = nothing readable; neither is a PC).
    ``symbolize``: callable(pc:int) -> function/symbol name ("" if unknown).

    Returns total/sampled/unsampleable counts, a ``hotspots`` list (by function,
    descending %), and the hottest raw addresses — so the agent reads "78% in
    foo()" instead of a wall of hex it has to symbolize itself.

    Note what is deliberately NOT rejected: a sample set that is entirely ONE
    address. That is the signature of a core spinning in a tight loop or parked on
    a ``b .`` — precisely the "what is it stuck in" question this profiler exists
    to answer — so only the two architecturally impossible values are dropped.
    """
    from collections import Counter

    total = len(samples)
    normalized = [s & 0xFFFFFFFF for s in samples]
    valid = [s for s in normalized if s not in PCSR_INVALID]
    unsampleable = sum(1 for s in normalized if s == PCSR_UNSAMPLEABLE)
    zero_samples = sum(1 for s in normalized if s == PCSR_ZERO)

    addr_counts = Counter(valid)
    sym = {addr: (symbolize(addr) or "") for addr in addr_counts}

    func_counts = Counter()
    for addr, n in addr_counts.items():
        func_counts[sym[addr] or f"0x{addr:08x}"] += n

    sampled = len(valid)
    hotspots = [
        {"function": name, "samples": n,
         "percent": round(100.0 * n / sampled, 1) if sampled else 0.0}
        for name, n in func_counts.most_common()
    ]
    hot_addresses = [
        {"address": f"0x{addr:08x}", "samples": n, "function": sym[addr]}
        for addr, n in addr_counts.most_common(8)
    ]
    return {
        "total_samples": total,
        "sampled": sampled,
        "unsampleable": unsampleable,  # high => core often halted / in WFI / sleeping
        # high => nothing was profiled: trace unit off, OR the link cannot
        # read memory while the core runs (see PCSR_ZERO).
        "zero_samples": zero_samples,
        "hotspots": hotspots,
        "hot_addresses": hot_addresses,
    }


def enable_cycle_counter_writes():
    """Return the (address, value) writes that enable and zero ``DWT_CYCCNT``."""
    return [
        (DEMCR, DEMCR_TRCENA),
        (DWT_CTRL, DWT_CTRL_CYCCNTENA),
        (DWT_CYCCNT, 0),
    ]


def read_cycle_count(read_word) -> int:
    return read_word(DWT_CYCCNT)


def sample_pc(read_word, count: int = 64):
    """Sample ``DWT_PCSR`` ``count`` times, returning the list of PC values.

    Note: PCSR returns 0xFFFFFFFF when the core is in a state where the PC is not
    sampleable (e.g. halted or in a low-power mode); callers should treat those
    as "no sample".
    """
    samples = []
    for _ in range(max(1, count)):
        samples.append(read_word(DWT_PCSR))
    return samples
