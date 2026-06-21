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
