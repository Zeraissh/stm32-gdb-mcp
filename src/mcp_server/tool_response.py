import json
from collections.abc import Sequence

from mcp.types import CallToolResult, TextContent

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "data": {},
        "error": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "code": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": ["message", "code"],
                    "additionalProperties": False,
                },
            ],
        },
        "raw_response": {},
        "suggested_next_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["ok", "data", "error", "raw_response", "suggested_next_actions"],
    "additionalProperties": False,
}


class _CompatibleCallToolResult(CallToolResult):
    def __getitem__(self, index):
        return self.content[index]


def success_response(data=None, raw_response=None, suggested_next_actions=None):
    return {
        "ok": True,
        "data": data,
        "error": None,
        "raw_response": raw_response,
        "suggested_next_actions": suggested_next_actions or [],
    }


def content_success(data=None, raw_response=None, suggested_next_actions=None) -> TextContent:
    return TextContent(
        type="text",
        text=json.dumps(success_response(data, raw_response, suggested_next_actions), indent=2),
    )


def content_error(message: str, code: str | None = None, raw_response=None, suggested_next_actions=None) -> TextContent:
    return TextContent(
        type="text",
        text=json.dumps(error_response(message, code, raw_response, suggested_next_actions), indent=2),
    )


def call_tool_result(content: Sequence[TextContent]) -> CallToolResult:
    payload = parse_content_text(content[0])
    return _CompatibleCallToolResult(
        content=list(content),
        structuredContent=payload,
        isError=not bool(payload.get("ok")),
    )


def parse_content_text(content: TextContent) -> dict:
    return json.loads(content.text)


def error_response(message: str, code: str | None = None, raw_response=None, suggested_next_actions=None):
    return {
        "ok": False,
        "data": None,
        "error": {
            "message": message,
            "code": code,
        },
        "raw_response": raw_response,
        "suggested_next_actions": suggested_next_actions or [],
    }
