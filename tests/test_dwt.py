from mcp_server.dwt import (
    DEMCR,
    DEMCR_TRCENA,
    DWT_CYCCNT,
    PCSR_UNSAMPLEABLE,
    build_pc_profile,
    enable_cycle_counter_writes,
    enable_pc_sampling_writes,
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


def test_enable_pc_sampling_turns_on_trace_clock():
    assert (DEMCR, DEMCR_TRCENA) in enable_pc_sampling_writes()


def test_build_pc_profile_aggregates_by_function_with_percentages():
    # 0x100 -> foo (x3), 0x200 -> bar (x1), plus one unsampleable read
    samples = [0x100, 0x100, 0x200, 0x100, PCSR_UNSAMPLEABLE]
    names = {0x100: "foo", 0x200: "bar"}

    profile = build_pc_profile(samples, symbolize=lambda pc: names.get(pc, ""))

    assert profile["total_samples"] == 5
    assert profile["sampled"] == 4
    assert profile["unsampleable"] == 1
    # hottest first, with percentages over the *sampled* total
    assert profile["hotspots"][0] == {"function": "foo", "samples": 3, "percent": 75.0}
    assert profile["hotspots"][1] == {"function": "bar", "samples": 1, "percent": 25.0}
    assert profile["hot_addresses"][0]["address"] == "0x00000100"


def test_build_pc_profile_falls_back_to_hex_when_unsymbolized():
    profile = build_pc_profile([0xDEAD, 0xDEAD], symbolize=lambda pc: "")
    assert profile["hotspots"][0]["function"] == "0x0000dead"


def test_build_pc_profile_all_unsampleable_means_core_not_running():
    profile = build_pc_profile([PCSR_UNSAMPLEABLE] * 8, symbolize=lambda pc: "x")
    assert profile["sampled"] == 0
    assert profile["unsampleable"] == 8
    assert profile["hotspots"] == []
