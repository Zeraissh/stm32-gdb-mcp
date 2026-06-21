import json

from mcp.types import TextContent


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
