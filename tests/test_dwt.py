from mcp_server.dwt import (
    DWT_CYCCNT,
    enable_cycle_counter_writes,
    read_cycle_count,
)


def test_enable_sequence_sets_trace_and_cyccnt_enable_and_zeroes_counter():
    writes = enable_cycle_counter_writes()

    # (address, value) pairs: enable DWT/ITM trace, enable CYCCNT, then zero it.
    assert (0xE000EDFC, 1 << 24) in writes      # DEMCR.TRCENA
    assert (0xE0001000, 1 << 0) in writes       # DWT_CTRL.CYCCNTENA
    assert (DWT_CYCCNT, 0) in writes            # reset the counter


def test_read_cycle_count_uses_read_word_at_cyccnt():
    seen = []

    def read_word(address):
        seen.append(address)
        return 12345

    assert read_cycle_count(read_word) == 12345
    assert seen == [DWT_CYCCNT]
