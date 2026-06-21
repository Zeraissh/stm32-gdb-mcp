import operator as operator_module
import re


OPERATORS = {
    "==": operator_module.eq,
    "!=": operator_module.ne,
    ">": operator_module.gt,
    ">=": operator_module.ge,
    "<": operator_module.lt,
    "<=": operator_module.le,
}


def capture_expressions(gdb_client, expressions: list[str]) -> dict:
    return {
        "values": [_read_expression(gdb_client, expression) for expression in expressions]
    }


def assert_expressions(gdb_client, assertions: list[dict]) -> dict:
    results = []
    for assertion in assertions:
        expression = assertion["expression"]
        op = assertion.get("operator", "==")
        expected = assertion.get("expected")
        value = _read_expression(gdb_client, expression)
        result = {
            "expression": expression,
            "operator": op,
            "expected": expected,
            "actual": value.get("value"),
            "raw": value.get("raw"),
        }
        if "error" in value:
            result["passed"] = False
            result["error"] = value["error"]
        elif op not in OPERATORS:
            result["passed"] = False
            result["error"] = f"Unsupported operator: {op}"
        else:
            result["passed"] = OPERATORS[op](value.get("value"), _normalize_expected(expected))
        results.append(result)

    return {
        "passed": all(item["passed"] for item in results),
        "assertions": results,
    }


def compare_expressions_after_action(gdb_client, expressions: list[str], action_name: str, action_callable) -> dict:
    before = capture_expressions(gdb_client, expressions)["values"]
    action_response = action_callable()
    after = capture_expressions(gdb_client, expressions)["values"]

    changes = []
    for before_item, after_item in zip(before, after):
        expression = before_item["expression"]
        before_value = before_item.get("value")
        after_value = after_item.get("value")
        change = {
            "expression": expression,
            "before": before_value,
            "after": after_value,
            "changed": before_value != after_value,
        }
        if "error" in before_item:
            change["before_error"] = before_item["error"]
        if "error" in after_item:
            change["after_error"] = after_item["error"]
        changes.append(change)

    return {
        "action": action_name,
        "action_response": action_response,
        "before": before,
        "after": after,
        "changes": changes,
    }


def _read_expression(gdb_client, expression: str) -> dict:
    response = gdb_client.read_variable(expression)
    raw_value = _extract_value(response)
    if raw_value is None:
        return {"expression": expression, "error": "No value returned"}
    parsed_value, kind = _parse_value(raw_value)
    return {
        "expression": expression,
        "raw": raw_value,
        "value": parsed_value,
        "kind": kind,
    }


def _extract_value(response):
    for record in response:
        if record.get("message") == "error":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and "value" in payload:
            return payload["value"]
        if isinstance(payload, str):
            return payload
    return None


def _parse_value(value):
    text = str(value).strip()
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1], "string"
    if re.fullmatch(r"[-+]?0x[0-9a-fA-F]+", text) or re.fullmatch(r"[-+]?\d+", text):
        return int(text, 0), "int"
    return text, "string"


def _normalize_expected(value):
    if isinstance(value, str):
        parsed, _ = _parse_value(value)
        return parsed
    return value
