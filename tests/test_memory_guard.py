import pytest

from mcp_server.memory_guard import MemoryWriteGuard


def test_normal_ram_write_is_allowed_and_audited():
    guard = MemoryWriteGuard()

    decision = guard.evaluate(0x20000000, width_bits=32)
    assert decision["action"] == "write"

    entry = guard.audit("write_memory", "0x20000000", "0x1", decision)
    assert entry["action"] == "write"
    assert guard.audit_log[-1] is entry


def test_protected_region_write_is_blocked_by_default():
    guard = MemoryWriteGuard()

    decision = guard.evaluate(0x40003000, width_bits=32)  # IWDG

    assert decision["action"] == "blocked"
    assert "iwdg" in decision["region"]["name"]


def test_option_bytes_write_is_blocked():
    guard = MemoryWriteGuard()

    decision = guard.evaluate(0x1FFF7800, width_bits=32)

    assert decision["action"] == "blocked"


def test_explicit_allow_overrides_protected_region():
    guard = MemoryWriteGuard()
    guard.set_policy(add_allow=[{"name": "iwdg_ok", "start": 0x40003000, "end": 0x400033FF}])

    decision = guard.evaluate(0x40003000, width_bits=32)

    assert decision["action"] == "write"
    assert decision["region"]["name"] == "iwdg_ok"


def test_dry_run_mode_simulates_all_writes():
    guard = MemoryWriteGuard()
    guard.set_policy(mode="dry_run")

    decision = guard.evaluate(0x20000000, width_bits=32)

    assert decision["action"] == "simulated"


def test_width_spanning_into_protected_region_is_blocked():
    guard = MemoryWriteGuard()

    # 64-bit write starting just below IWDG spills into the protected range.
    decision = guard.evaluate(0x40002FFC, width_bits=64)

    assert decision["action"] == "blocked"


def test_evaluate_range_blocks_an_erase_that_clips_a_protected_region():
    guard = MemoryWriteGuard()

    # 0x1FFF0000 is option bytes / system memory; an erase that reaches it is at
    # least as destructive as a write into it (issue #42).
    decision = guard.evaluate_range(0x1FFEF000, 0x2000)

    assert decision["action"] == "blocked"
    assert decision["region"]["name"] == "option_bytes_system_memory"


def test_evaluate_range_allows_an_ordinary_application_flash_range():
    guard = MemoryWriteGuard()

    assert guard.evaluate_range(0x08016000, 0x1000)["action"] == "write"


def test_evaluate_range_rejects_a_non_positive_length():
    guard = MemoryWriteGuard()

    with pytest.raises(ValueError):
        guard.evaluate_range(0x08016000, 0)


def test_evaluate_range_honours_dry_run_like_evaluate():
    guard = MemoryWriteGuard()
    guard.set_policy(mode="dry_run")

    assert guard.evaluate_range(0x1FFF0000, 0x1000)["action"] == "simulated"


def test_an_allow_that_only_clips_a_range_does_not_unlock_a_protected_region_inside_it():
    guard = MemoryWriteGuard()
    guard.set_policy(add_allow=[{"name": "scratch", "start": 0x1FFEF000, "end": 0x1FFEFFFF}])

    # The range touches the allowed scratch area AND the protected option bytes.
    # Overlap-wins-first would return "write" and never consult the protected list.
    decision = guard.evaluate_range(0x1FFEF800, 0x2000)

    assert decision["action"] == "blocked"
    assert decision["region"]["name"] == "option_bytes_system_memory"


def test_an_allow_that_contains_the_whole_range_still_wins():
    guard = MemoryWriteGuard()
    guard.set_policy(add_allow=[{"name": "option_bytes_ok", "start": 0x1FFF0000, "end": 0x1FFFFFFF}])

    assert guard.evaluate_range(0x1FFF0000, 0x100)["action"] == "write"


def test_a_single_word_write_inside_an_allowed_region_is_unaffected():
    guard = MemoryWriteGuard()
    guard.set_policy(add_allow=[{"name": "iwdg_ok", "start": 0x40003000, "end": 0x400033FF}])

    assert guard.evaluate(0x40003000, width_bits=32)["action"] == "write"


def test_read_memory_decodes_a_word_as_little_endian():
    # Cortex-M is little-endian, so RCC_CSR = 0x0C000000 comes back as "0000000c".
    # Reversing that by hand is where bit positions get misread -- and on RCC_CSR that
    # is the difference between "a sticky reset flag cleared" (a proven power cycle)
    # and "nothing happened".
    from mcp_server.tools.memory_tools import _little_endian_word

    assert _little_endian_word("0000000c", 4) == "0x0c000000"
    assert _little_endian_word("0000001c", 4) == "0x1c000000"
    assert _little_endian_word("34122301", 4) == "0x01231234"
    assert _little_endian_word("cd", 1) == "0xcd"
    assert _little_endian_word("3412", 2) == "0x1234"


def test_read_memory_omits_the_word_when_the_bytes_are_not_one():
    # A 3-byte or 12-byte read has no single scalar reading; inventing one would be an
    # interpretation nobody asked for, and `bytes` still carries the raw order.
    from mcp_server.tools.memory_tools import _little_endian_word

    assert _little_endian_word("001122", 3) is None
    assert _little_endian_word("0011", 4) is None, "length must match the bytes returned"
    assert _little_endian_word("zzzz", 2) is None
    assert _little_endian_word(None, 4) is None
