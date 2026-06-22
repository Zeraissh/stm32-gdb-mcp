from mcp_server.stack_analysis import stack_report


def test_normal_usage_reports_used_free_and_no_overflow():
    # 8 KiB stack from 0x2000A000 down to 0x20008000; SP halfway.
    r = stack_report(sp=0x20009000, stack_top=0x2000A000, stack_limit=0x20008000)

    assert r["used_bytes"] == 0x1000
    assert r["free_bytes"] == 0x1000
    assert r["size_bytes"] == 0x2000
    assert r["pct_used"] == 50.0
    assert r["overflow"] is False
    assert r["sp"] == "0x20009000"


def test_sp_below_limit_is_flagged_as_overflow():
    r = stack_report(sp=0x20007F00, stack_top=0x2000A000, stack_limit=0x20008000)

    assert r["overflow"] is True
    assert r["free_bytes"] < 0
    assert "OVERFLOW" in r["summary"].upper()


def test_partial_when_limit_unknown_still_reports_used():
    r = stack_report(sp=0x20009000, stack_top=0x2000A000, stack_limit=None)

    assert r["used_bytes"] == 0x1000
    assert r["free_bytes"] is None
    assert r["overflow"] is False
    assert "stack_size" in r["summary"] or "limit" in r["summary"]


def test_sp_above_top_is_noted_not_negative_usage():
    # SP above stack_top (e.g. wrong stack pointer / PSP vs MSP mismatch).
    r = stack_report(sp=0x2000B000, stack_top=0x2000A000, stack_limit=0x20008000)

    assert r["used_bytes"] < 0
    assert "above" in r["summary"].lower() or "check" in r["summary"].lower()
