import json
import os
from collections.abc import Sequence

from mcp.types import CallToolResult, TextContent

# Reference schema for the response envelope. It is intentionally NOT advertised
# per-tool (that repeated ~460 chars on every tool); tool_help and the server
# instructions describe it once. Fields other than "ok" are omitted when empty.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {},
        "error": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "code": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["message", "code"],
            "additionalProperties": False,
        },
        "raw_response": {},
        "suggested_next_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["ok"],
    "additionalProperties": False,
}


def verbose_raw_responses() -> bool:
    """Raw GDB/MI records ride along on successful results only when opted in."""
    return bool(os.environ.get("STM32_GDB_MCP_VERBOSE"))


def _envelope(ok: bool, data=None, error=None, raw_response=None, suggested_next_actions=None) -> dict:
    response: dict = {"ok": ok}
    if data is not None:
        response["data"] = data
    if error is not None:
        response["error"] = error
    if raw_response is not None:
        response["raw_response"] = raw_response
    if suggested_next_actions:
        response["suggested_next_actions"] = list(suggested_next_actions)
    return response


def _dump(response: dict) -> str:
    return json.dumps(response, separators=(",", ":"))


def success_response(data=None, raw_response=None, suggested_next_actions=None):
    if not verbose_raw_responses():
        raw_response = None
    return _envelope(True, data=data, raw_response=raw_response,
                     suggested_next_actions=suggested_next_actions)


def content_success(data=None, raw_response=None, suggested_next_actions=None) -> TextContent:
    return TextContent(
        type="text",
        text=_dump(success_response(data, raw_response, suggested_next_actions)),
    )


def content_error(message: str, code: str | None = None, raw_response=None,
                  suggested_next_actions=None, data=None) -> TextContent:
    return TextContent(
        type="text",
        text=_dump(error_response(message, code, raw_response, suggested_next_actions, data)),
    )


class _CompatibleCallToolResult(CallToolResult):
    def __getitem__(self, index):
        return self.content[index]


def call_tool_result(content: Sequence[TextContent]) -> CallToolResult:
    payload = parse_content_text(content[0])
    return _CompatibleCallToolResult(
        content=list(content),
        structuredContent=payload,
        isError=not bool(payload.get("ok")),
    )


def parse_content_text(content: TextContent) -> dict:
    return json.loads(content.text)


def error_response(message: str, code: str | None = None, raw_response=None,
                   suggested_next_actions=None, data=None):
    # Errors always keep raw_response: it is the evidence needed to diagnose them.
    # ``data`` lets a partial failure (a batch where some steps worked) report
    # ok:false without throwing away the per-step results.
    return _envelope(
        False,
        data=data,
        error={"message": message, "code": code},
        raw_response=raw_response,
        suggested_next_actions=suggested_next_actions,
    )
