from mcp_server.issue_reporter import build_issue_body, file_issue, issue_fingerprint


def test_build_issue_body_includes_context_journal_and_version():
    body = build_issue_body(
        description="flash then crash on flash write",
        env="Cursor / deepseek-v4-pro",
        version="bec8055",
        journal=[{"seq": 1, "tool": "start_debug_session", "ok": True, "summary": "started"},
                 {"seq": 2, "tool": "set_breakpoint", "ok": False, "error": {"code": "x"}, "summary": ""}],
        metrics={"totals": {"calls": 2, "ok": 1, "failed": 1}},
    )

    assert "flash then crash" in body
    assert "Cursor / deepseek-v4-pro" in body
    assert "bec8055" in body
    assert "start_debug_session" in body
    assert "set_breakpoint" in body
    assert "calls=2" in body


def test_fingerprint_is_stable_and_distinguishes_titles():
    assert issue_fingerprint("[agent] reset fails") == issue_fingerprint("[agent] Reset Fails ")
    assert issue_fingerprint("a") != issue_fingerprint("b")


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_file_issue_success_returns_url():
    captured = {}

    def runner(cmd, input=None, capture_output=False, text=False, timeout=None):
        captured["cmd"] = cmd
        captured["input"] = input
        return _Proc(0, stdout="https://github.com/Zeraissh/stm32-gdb-mcp/issues/7\n")

    result = file_issue("Zeraissh/stm32-gdb-mcp", "[agent] bug", "body text", runner=runner)

    assert result["ok"] is True
    assert result["url"] == "https://github.com/Zeraissh/stm32-gdb-mcp/issues/7"
    assert captured["cmd"][:3] == ["gh", "issue", "create"]
    assert "--repo" in captured["cmd"] and "Zeraissh/stm32-gdb-mcp" in captured["cmd"]
    assert captured["input"] == "body text"


def test_file_issue_reports_failure_with_body_for_manual_filing():
    def runner(cmd, **kw):
        return _Proc(1, stderr="gh: not authenticated")

    result = file_issue("r/x", "t", "the body", runner=runner)

    assert result["ok"] is False
    assert "not authenticated" in result["error"]
    assert result["body"] == "the body"


def test_file_issue_handles_missing_gh():
    def runner(cmd, **kw):
        raise FileNotFoundError()

    result = file_issue("r/x", "t", "b", runner=runner)
    assert result["ok"] is False
    assert "gh" in result["error"].lower()
