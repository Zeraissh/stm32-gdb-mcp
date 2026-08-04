from mcp_server.debug_experiments import (
    _parse_value,
    assert_expressions,
    capture_expressions,
    compare_expressions_after_action,
)


class FakeGdbClient:
    def __init__(self):
        self.values = {
            "counter": "0x2a",
            "state": '"RUN"',
            "temperature": "37",
            "s_diag_cb_count[2]": "10",
            "s_diag_ack_count[2]": "8",
            "s_diag_cb_count[3]": "11",
            "s_diag_ack_count[3]": "9",
        }
        self.queries = []

    def read_variable(self, expression):
        self.queries.append(expression)
        if expression not in self.values:
            return [{"type": "result", "message": "error", "payload": {"msg": "No symbol"}}]
        return [{"type": "result", "message": "done", "payload": {"value": self.values[expression]}}]


def test_capture_expressions_parses_values_and_errors():
    client = FakeGdbClient()

    result = capture_expressions(client, ["counter", "state", "missing"])

    assert result == {
        "values": [
            {"expression": "counter", "raw": "0x2a", "value": 42, "kind": "int"},
            {"expression": "state", "raw": '"RUN"', "value": "RUN", "kind": "string"},
            {"expression": "missing", "error": "No value returned"},
        ]
    }


def test_capture_expressions_builds_indexed_table_and_preserves_values():
    client = FakeGdbClient()

    result = capture_expressions(
        client,
        table={"index_range": [2, 3], "columns": ["s_diag_cb_count", "s_diag_ack_count"]},
    )

    assert [item["expression"] for item in result["values"]] == [
        "s_diag_cb_count[2]",
        "s_diag_ack_count[2]",
        "s_diag_cb_count[3]",
        "s_diag_ack_count[3]",
    ]
    assert result["tables"] == [
        {
            "kind": "indexed",
            "index_range": [2, 3],
            "columns": ["s_diag_cb_count", "s_diag_ack_count"],
            "rows": [
                {
                    "index": 2,
                    "values": {"s_diag_cb_count": 10, "s_diag_ack_count": 8},
                    "raw": {"s_diag_cb_count": "10", "s_diag_ack_count": "8"},
                    "errors": {},
                },
                {
                    "index": 3,
                    "values": {"s_diag_cb_count": 11, "s_diag_ack_count": 9},
                    "raw": {"s_diag_cb_count": "11", "s_diag_ack_count": "9"},
                    "errors": {},
                },
            ],
        }
    ]


def test_assert_expressions_evaluates_numeric_and_string_conditions():
    client = FakeGdbClient()

    result = assert_expressions(client, [
        {"expression": "counter", "operator": ">=", "expected": 40},
        {"expression": "state", "operator": "==", "expected": "RUN"},
        {"expression": "temperature", "operator": "<", "expected": 30},
    ])

    assert result["passed"] is False
    assert [item["passed"] for item in result["assertions"]] == [True, True, False]


def test_compare_expressions_after_action_reports_changes():
    client = FakeGdbClient()

    def action():
        client.values["counter"] = "0x2b"
        return [{"message": "done"}]

    result = compare_expressions_after_action(client, ["counter", "state"], "step_over", action)

    assert result["action"] == "step_over"
    assert result["action_response"] == [{"message": "done"}]
    assert result["changes"] == [
        {
            "expression": "counter",
            "before": 42,
            "after": 43,
            "changed": True,
        },
        {
            "expression": "state",
            "before": "RUN",
            "after": "RUN",
            "changed": False,
        },
    ]


def test_a_char_value_is_read_as_a_number_not_a_decorated_string():
    # GDB prints a char as "161 '\241'"; when its charset conversion fails the
    # quoted half becomes an error string glued onto the value (issue #34).
    broken = "161 '<error reading variable: Converting character sets: Invalid argument.>"

    assert _parse_value(broken) == (161, "int")
    assert _parse_value("0 '<error reading variable: Converting character sets: Invalid argument.>") == (0, "int")
    assert _parse_value(r"65 'A'") == (65, "int")


def test_a_decorated_pointer_is_read_as_a_number():
    assert _parse_value("0x20000010 <g_state>") == (0x20000010, "int")


def test_plain_numbers_and_strings_are_unchanged():
    assert _parse_value("57") == (57, "int")
    assert _parse_value("0x1f") == (0x1F, "int")
    assert _parse_value('"hello"') == ("hello", "string")


def test_struct_dumps_and_enum_names_stay_strings():
    # The space-then-quote/angle requirement is what keeps these off the int path.
    assert _parse_value("{a = 1, b = 2}") == ("{a = 1, b = 2}", "string")
    assert _parse_value("OTA_STATE_CONFIRMED") == ("OTA_STATE_CONFIRMED", "string")
    assert _parse_value("1 2 3") == ("1 2 3", "string")
