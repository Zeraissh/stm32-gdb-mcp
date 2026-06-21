from mcp_server.gdb_client import build_break_insert_command


def test_plain_breakpoint_is_unchanged():
    assert build_break_insert_command("main.c:42") == "-break-insert main.c:42"


def test_conditional_breakpoint_quotes_condition():
    assert build_break_insert_command("foo", condition="count > 5") == (
        '-break-insert -c "count > 5" foo'
    )


def test_temporary_and_ignore_count_flags():
    assert build_break_insert_command("foo", temporary=True, ignore_count=3) == (
        "-break-insert -t -i 3 foo"
    )


def test_all_options_compose_in_order():
    assert build_break_insert_command(
        "*0x08001000", condition="x==1", temporary=True, ignore_count=2
    ) == '-break-insert -t -c "x==1" -i 2 *0x08001000'


def test_blank_condition_is_ignored():
    assert build_break_insert_command("foo", condition="   ") == "-break-insert foo"
