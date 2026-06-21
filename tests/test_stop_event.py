from mcp_server.stop_event import TIMEOUT_REASON, parse_stop_event


def _stopped_record(reason, frame=None, **payload):
    record = {"type": "notify", "message": "stopped", "payload": {"reason": reason}}
    if frame is not None:
        record["payload"]["frame"] = frame
    record["payload"].update(payload)
    return record


def test_parse_breakpoint_hit_extracts_frame_and_breakpoint():
    records = [
        {"type": "result", "message": "running", "payload": None},
        _stopped_record(
            "breakpoint-hit",
            frame={"func": "main", "file": "main.c", "line": "42", "addr": "0x08001000"},
            bkptno="2",
        ),
    ]

    event = parse_stop_event(records)

    assert event["stopped"] is True
    assert event["reason"] == "breakpoint-hit"
    assert event["breakpoint_id"] == "2"
    assert event["frame"] == {
        "func": "main",
        "file": "main.c",
        "line": 42,
        "addr": "0x08001000",
    }


def test_parse_signal_received_captures_signal_name():
    records = [_stopped_record("signal-received", **{"signal-name": "SIGTRAP"})]

    event = parse_stop_event(records)

    assert event["reason"] == "signal-received"
    assert event["signal"] == "SIGTRAP"
    assert event["stopped"] is True


def test_parse_end_stepping_range_returns_frame_without_breakpoint():
    records = [
        _stopped_record(
            "end-stepping-range",
            frame={"func": "loop", "file": "main.c", "line": "55", "addr": "0x08001100"},
        )
    ]

    event = parse_stop_event(records)

    assert event["reason"] == "end-stepping-range"
    assert event["breakpoint_id"] is None
    assert event["frame"]["line"] == 55


def test_parse_no_stop_record_reports_timeout():
    records = [
        {"type": "console", "message": None, "payload": "Continuing.\n"},
        {"type": "result", "message": "running", "payload": None},
    ]

    event = parse_stop_event(records)

    assert event["stopped"] is False
    assert event["reason"] == TIMEOUT_REASON
    assert event["frame"] is None


def test_parse_empty_records_reports_timeout():
    event = parse_stop_event([])

    assert event["stopped"] is False
    assert event["reason"] == TIMEOUT_REASON
