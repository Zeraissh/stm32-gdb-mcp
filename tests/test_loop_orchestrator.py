from mcp_server.loop_control import new_loop_state
from mcp_server.loop_orchestrator import run_iteration


def _verdict(ok, results):
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    errored = sum(1 for r in results if r["status"] == "error")
    return {"ok": ok, "results": results,
            "stats": {"total": len(results), "passed": passed, "failed": failed, "errored": errored}}


def _r(id, status):
    return {"id": id, "kind": "memory_u32", "status": status}


class FakeSteps:
    def __init__(self, *, has_build=False, has_flash=False, builds=None, flashes=None, verdicts=None):
        self.has_build = has_build
        self.has_flash = has_flash
        self._builds = list(builds or [])
        self._flashes = list(flashes or [])
        self._verdicts = list(verdicts or [])
        self.calls = []

    def build(self):
        self.calls.append("build")
        return self._builds.pop(0)

    def flash(self):
        self.calls.append("flash")
        return self._flashes.pop(0)

    def evaluate(self):
        self.calls.append("evaluate")
        return self._verdicts.pop(0)


def test_build_failure_short_circuits_before_flash_and_evaluate():
    state = new_loop_state({})
    steps = FakeSteps(has_build=True, has_flash=True,
                      builds=[{"success": False, "returncode": 2, "log_tail": "cmake: 2 errors"}])
    result = run_iteration(state, steps)

    assert steps.calls == ["build"]  # flash / evaluate never run
    assert result["iteration"]["phase"] == "build"
    assert "cmake" in result["iteration"]["phase_error"]
    assert result["decision"]["should_continue"] is True  # still active, agent fixes the build


def test_flash_not_stopped_is_a_phase_error():
    state = new_loop_state({})
    steps = FakeSteps(has_build=True, has_flash=True,
                      builds=[{"success": True, "returncode": 0, "log_tail": ""}],
                      flashes=[{"stopped": False, "detail": "never reached main"}])
    result = run_iteration(state, steps)

    assert steps.calls == ["build", "flash"]
    assert result["iteration"]["phase"] == "flash_run"
    assert "reached main" in result["iteration"]["phase_error"]


def test_full_pass_converges():
    state = new_loop_state({})
    steps = FakeSteps(has_build=True, has_flash=True,
                      builds=[{"success": True, "returncode": 0, "log_tail": ""}],
                      flashes=[{"stopped": True, "detail": None}],
                      verdicts=[_verdict(True, [_r("a", "pass"), _r("b", "pass")])])
    result = run_iteration(state, steps)

    assert steps.calls == ["build", "flash", "evaluate"]
    assert result["decision"]["converged"] is True
    assert state["status"] == "converged"


def test_evaluate_only_loop_without_build_or_flash():
    # No build/flash configured: the agent flashed manually; the iteration is evaluate-only.
    state = new_loop_state({})
    steps = FakeSteps(verdicts=[_verdict(False, [_r("a", "fail")])])
    result = run_iteration(state, steps)

    assert steps.calls == ["evaluate"]
    assert result["iteration"]["unsatisfied_ids"] == ["a"]
    assert result["decision"]["should_continue"] is True


def test_multi_iteration_fix_then_converge():
    state = new_loop_state({})
    steps = FakeSteps(
        has_flash=True,
        flashes=[{"stopped": True, "detail": None}, {"stopped": True, "detail": None}],
        verdicts=[_verdict(False, [_r("a", "fail")]), _verdict(True, [_r("a", "pass")])],
    )
    first = run_iteration(state, steps)
    assert first["decision"]["should_continue"] is True

    second = run_iteration(state, steps)
    assert second["decision"]["converged"] is True
    assert [e["ok"] for e in state["iterations"]] == [False, True]
    assert state["iterations"][1]["newly_satisfied"] == ["a"]
