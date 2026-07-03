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


def capture_expressions(gdb_client, expressions: list[str] | None = None, table: dict | None = None) -> dict:
    expression_list = list(expressions or [])
    table_plan = _build_table_plan(table)
    seen = set(expression_list)
    for expression in table_plan["expressions"]:
        if expression not in seen:
            expression_list.append(expression)
            seen.add(expression)

    values = [_read_expression(gdb_client, expression) for expression in expression_list]
    result = {"values": values}
    if table_plan["table"] is not None:
        result["tables"] = [_build_table_result(table_plan["table"], values)]
    return result


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
    for before_item, after_item in zip(before, after, strict=True):
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
    try:
        response = gdb_client.read_variable(expression)
    except Exception as exc:
        return {"expression": expression, "error": str(exc)}
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


def _build_table_plan(table):
    if table is None:
        return {"table": None, "expressions": []}
    start, end = _validate_index_range(table.get("index_range"))
    columns = table.get("columns")
    if not isinstance(columns, list) or not columns or not all(isinstance(column, str) and column for column in columns):
        raise ValueError("table.columns must be a non-empty list of expression prefixes")
    expressions = [f"{column}[{index}]" for index in range(start, end + 1) for column in columns]
    return {
        "table": {"kind": "indexed", "index_range": [start, end], "columns": columns},
        "expressions": expressions,
    }


def _validate_index_range(value):
    if (
        not isinstance(value, list)
        or len(value) != 2
        or isinstance(value[0], bool)
        or isinstance(value[1], bool)
        or not isinstance(value[0], int)
        or not isinstance(value[1], int)
    ):
        raise ValueError("table.index_range must be [start, end] integers")
    start, end = value
    if end < start:
        raise ValueError("table.index_range end must be >= start")
    return start, end


def _build_table_result(table, values):
    by_expression = {item["expression"]: item for item in values}
    start, end = table["index_range"]
    rows = []
    for index in range(start, end + 1):
        row = {"index": index, "values": {}, "raw": {}, "errors": {}}
        for column in table["columns"]:
            expression = f"{column}[{index}]"
            item = by_expression[expression]
            if "error" in item:
                row["errors"][column] = item["error"]
                continue
            row["values"][column] = item.get("value")
            row["raw"][column] = item.get("raw")
        rows.append(row)
    return {**table, "rows": rows}
