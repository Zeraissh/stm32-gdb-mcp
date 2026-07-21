import json

from mcp.types import CallToolResult, TextContent

import mcp_server.tool_response as tool_response
from mcp_server.tool_response import (
    content_error,
    content_success,
    error_response,
    parse_content_text,
    success_response,
)


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


def test_content_success_returns_textcontent_with_json_envelope():
    content = content_success({"message": "ok"}, raw_response=[{"message": "done"}])

    assert isinstance(content, TextContent)
    payload = json.loads(content.text)
    assert payload["ok"] is True
    assert payload["data"] == {"message": "ok"}
    assert payload["raw_response"] == [{"message": "done"}]
    assert payload["error"] is None


def test_content_error_returns_textcontent_with_json_envelope():
    content = content_error("Unknown tool", code="unknown_tool", suggested_next_actions=["list_tools"])

    payload = parse_content_text(content)
    assert payload["ok"] is False
    assert payload["error"] == {"message": "Unknown tool", "code": "unknown_tool"}
    assert payload["suggested_next_actions"] == ["list_tools"]


def test_call_tool_result_preserves_text_and_adds_native_structured_content():
    content = content_success({"message": "ok"})

    result = tool_response.call_tool_result([content])

    assert isinstance(result, CallToolResult)
    assert json.loads(result.content[0].text) == result.structuredContent
    assert result.isError is False

    error = tool_response.call_tool_result([content_error("bad", code="test_error")])
    assert error.structuredContent["error"]["code"] == "test_error"
    assert error.isError is True
