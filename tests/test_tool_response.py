from mcp_server.tool_response import error_response, success_response


def test_success_response_wraps_data_and_next_actions():
    response = success_response(
        {"value": 42},
        raw_response=[{"message": "done"}],
        suggested_next_actions=["capture_debug_snapshot"],
    )

    assert response == {
        "ok": True,
        "data": {"value": 42},
        "error": None,
        "raw_response": [{"message": "done"}],
        "suggested_next_actions": ["capture_debug_snapshot"],
    }


def test_error_response_uses_stable_shape():
    response = error_response("GDB is not running", code="gdb_not_running")

    assert response == {
        "ok": False,
        "data": None,
        "error": {"message": "GDB is not running", "code": "gdb_not_running"},
        "raw_response": None,
        "suggested_next_actions": [],
    }
