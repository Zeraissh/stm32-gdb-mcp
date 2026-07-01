from mcp_server.loop_control import (
    loop_decision,
    new_loop_state,
    record_iteration,
    summarize_loop,
)


def _verdict(ok, results):
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    errored = sum(1 for r in results if r["status"] == "error")
    return {"ok": ok, "results": results,
            "stats": {"total": len(results), "passed": passed, "failed": failed, "errored": errored}}


def _r(id, status):
    return {"id": id, "kind": "memory_u32", "status": status}


def test_new_state_fills_bound_defaults():
    state = new_loop_state({"acceptance_name": "x"})
    assert state["plan"]["max_iterations"] == 10
    assert state["plan"]["stall_patience"] == 3
    assert state["status"] == "active"
    assert state["iterations"] == []


def test_converges_when_verdict_ok():
    state = new_loop_state({})
    record_iteration(state, verdict=_verdict(True, [_r("a", "pass")]))

    decision = loop_decision(state)
    assert state["status"] == "converged"
    assert decision["converged"] is True
    assert decision["should_continue"] is False
    assert "plan_framework" in decision["next_actions"]


def test_active_while_checks_fail_and_reports_ids():
    state = new_loop_state({})
    record_iteration(state, verdict=_verdict(False, [_r("usart1", "fail"), _r("clk", "error"), _r("sp", "pass")]))

    entry = state["iterations"][0]
    assert entry["unsatisfied_ids"] == ["usart1", "clk"]  # fail ∪ error
    decision = loop_decision(state)
    assert decision["status"] == "active"
    assert decision["should_continue"] is True
    assert any("usart1" in a for a in decision["next_actions"])


def test_trajectory_diff_tracks_newly_satisfied_and_broken():
    state = new_loop_state({})
    record_iteration(state, verdict=_verdict(False, [_r("a", "fail"), _r("b", "fail")]))
    # fix a, but break c
    record_iteration(state, verdict=_verdict(False, [_r("a", "pass"), _r("b", "fail"), _r("c", "fail")]))

    entry = state["iterations"][1]
    assert entry["newly_satisfied"] == ["a"]
    assert entry["newly_broken"] == ["c"]
    assert set(entry["unsatisfied_ids"]) == {"b", "c"}


def test_exhausts_at_max_iterations():
    state = new_loop_state({"max_iterations": 3})
    for _ in range(3):
        record_iteration(state, verdict=_verdict(False, [_r("a", "fail")]))

    # 3 identical failures would also be a stall, but exhaustion is checked first here because
    # count >= max_iterations. Either way the loop must stop.
    decision = loop_decision(state)
    assert decision["should_continue"] is False
    assert state["status"] in ("exhausted", "stalled")
    assert decision["exhausted"] is True


def test_detects_stall_before_exhaustion():
    state = new_loop_state({"max_iterations": 10, "stall_patience": 3})
    for _ in range(3):
        record_iteration(state, verdict=_verdict(False, [_r("a", "fail"), _r("b", "fail")]))

    decision = loop_decision(state)
    assert state["status"] == "stalled"
    assert decision["stalled"] is True
    assert decision["should_continue"] is False


def test_no_stall_when_failing_set_changes():
    state = new_loop_state({"max_iterations": 10, "stall_patience": 3})
    record_iteration(state, verdict=_verdict(False, [_r("a", "fail"), _r("b", "fail")]))
    record_iteration(state, verdict=_verdict(False, [_r("a", "fail")]))            # progress: b fixed
    record_iteration(state, verdict=_verdict(False, [_r("a", "fail"), _r("c", "fail")]))  # different set

    # Making progress (the failing set keeps changing) must NOT count as a stall.
    assert state["status"] == "active"
    assert loop_decision(state)["should_continue"] is True


def test_phase_error_iteration_is_non_ok_and_not_a_stall():
    state = new_loop_state({"max_iterations": 10, "stall_patience": 3})
    for _ in range(3):
        record_iteration(state, phase_error={"phase": "build", "detail": "cmake: 2 errors"})

    entry = state["iterations"][-1]
    assert entry["ok"] is False
    assert entry["phase"] == "build"
    assert entry["unsatisfied_ids"] == []
    # Build failures have an empty unsatisfied set, so they are not a check-stall; the loop
    # stays active until it exhausts.
    assert state["status"] == "active"
    decision = loop_decision(state)
    assert "build" in decision["reason"]
    assert any("build" in a for a in decision["next_actions"])


def test_summarize_loop_reports_trajectory():
    state = new_loop_state({"acceptance_name": "blinky", "max_iterations": 5})
    record_iteration(state, verdict=_verdict(False, [_r("a", "fail")]))
    record_iteration(state, verdict=_verdict(True, [_r("a", "pass")]))

    summary = summarize_loop(state)
    assert summary["status"] == "converged"
    assert summary["iteration_count"] == 2
    assert summary["acceptance_name"] == "blinky"
    assert [t["ok"] for t in summary["trajectory"]] == [False, True]
    assert summary["last_unsatisfied_ids"] == []
