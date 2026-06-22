import json

from mcp_server.debug_report import build_report, write_report


def _entries():
    return [
        {"seq": 1, "tool": "self_check", "ok": True, "duration_ms": 5.0},
        {"seq": 2, "tool": "write_memory", "ok": False, "duration_ms": 2.0, "error": "blocked"},
    ]


def test_build_report_bundles_journal_metrics_and_metadata():
    report = build_report(
        run_id="abc123",
        journal_entries=_entries(),
        profile={"mcu": "STM32L431"},
    )

    assert report["run_id"] == "abc123"
    assert report["profile"]["mcu"] == "STM32L431"
    assert report["journal"] == _entries()
    assert report["metrics"]["totals"]["calls"] == 2
    assert report["metrics"]["totals"]["failed"] == 1
    assert "generated_at" in report
    assert report["snapshot"] is None
    assert report["coredump"] is None


def test_build_report_includes_snapshot_and_coredump_when_given():
    report = build_report(
        run_id="abc123",
        journal_entries=[],
        snapshot={"registers": {"pc": "0x8000000"}},
        coredump_path="dump.core",
    )

    assert report["snapshot"]["registers"]["pc"] == "0x8000000"
    assert report["coredump"] == "dump.core"


def test_write_report_writes_json_file(tmp_path):
    path = tmp_path / "report.json"
    report = build_report(run_id="abc123", journal_entries=_entries())

    write_report(str(path), report)

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["run_id"] == "abc123"
    assert loaded["metrics"]["totals"]["calls"] == 2
