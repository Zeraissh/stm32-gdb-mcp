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
