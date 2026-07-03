from mcp_server.debug_experiments import (
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
