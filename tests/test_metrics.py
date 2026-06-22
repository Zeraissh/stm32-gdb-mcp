from mcp_server.metrics import compute_metrics


def _entry(tool, ok, ms):
    return {"tool": tool, "ok": ok, "duration_ms": ms}


def test_compute_metrics_aggregates_per_tool_and_totals():
    entries = [
        _entry("read_memory", True, 10.0),
        _entry("read_memory", True, 20.0),
        _entry("write_memory", False, 5.0),
    ]

    metrics = compute_metrics(entries)

    rm = metrics["by_tool"]["read_memory"]
    assert rm["calls"] == 2
    assert rm["ok"] == 2
    assert rm["failed"] == 0
    assert rm["avg_ms"] == 15.0

    wm = metrics["by_tool"]["write_memory"]
    assert wm["failed"] == 1

    assert metrics["totals"]["calls"] == 3
    assert metrics["totals"]["ok"] == 2
    assert metrics["totals"]["failed"] == 1
    assert metrics["totals"]["tools"] == 2


def test_compute_metrics_handles_missing_durations_and_empty():
    assert compute_metrics([])["totals"]["calls"] == 0

    metrics = compute_metrics([{"tool": "x", "ok": True, "duration_ms": None}])
    assert metrics["by_tool"]["x"]["avg_ms"] == 0
