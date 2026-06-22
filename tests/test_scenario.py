import asyncio

from mcp_server.scenario import replay_scenario


def _run(payloads):
    """Build an async run_step that returns queued payloads per tool call."""
    calls = []

    async def run_step(tool, args):
        calls.append((tool, args))
        return payloads.pop(0)

    return run_step, calls


def test_replay_runs_each_step_and_reports_pass():
    steps = [
        {"tool": "reset_target", "args": {"halt": True}},
        {"tool": "set_breakpoint", "args": {"location": "main"}},
    ]
    run_step, calls = _run([
        {"ok": True, "data": {"message": "reset"}},
        {"ok": True, "data": {"summary": "bp set"}},
    ])

    report = asyncio.run(replay_scenario(steps, run_step))

    assert report["ok"] is True
    assert report["passed"] == 2
    assert report["total"] == 2
    assert calls[0] == ("reset_target", {"halt": True})
    assert report["steps"][1]["summary"] == "bp set"


def test_replay_stops_on_first_failure_by_default():
    steps = [
        {"tool": "flash_firmware", "args": {"file_path": "fw.elf"}},
        {"tool": "set_breakpoint", "args": {"location": "main"}},
    ]
    run_step, calls = _run([
        {"ok": False, "error": {"message": "flash failed"}},
        {"ok": True, "data": {}},
    ])

    report = asyncio.run(replay_scenario(steps, run_step))

    assert report["ok"] is False
    assert report["passed"] == 0
    assert report["completed"] == 1          # stopped after the failure
    assert len(calls) == 1                     # second step never ran
    assert report["steps"][0]["error"] == {"message": "flash failed"}


def test_replay_can_continue_past_failures():
    steps = [{"tool": "a", "args": {}}, {"tool": "b", "args": {}}]
    run_step, calls = _run([{"ok": False, "error": "x"}, {"ok": True, "data": {}}])

    report = asyncio.run(replay_scenario(steps, run_step, stop_on_failure=False))

    assert report["completed"] == 2
    assert report["passed"] == 1
    assert len(calls) == 2
