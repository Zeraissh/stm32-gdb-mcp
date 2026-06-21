def success_response(data=None, raw_response=None, suggested_next_actions=None):
    return {
        "ok": True,
        "data": data,
        "error": None,
        "raw_response": raw_response,
        "suggested_next_actions": suggested_next_actions or [],
    }


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
