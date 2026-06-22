import pytest

from mcp_server.error_taxonomy import classify_error
from mcp_server.reliability import retry_call


def test_returns_immediately_on_success():
    sleeps = []
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert retry_call(fn, attempts=3, sleep=sleeps.append) == "ok"
    assert len(calls) == 1
    assert sleeps == []


def test_retries_retryable_errors_then_succeeds():
    sleeps = []
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("Did not get response from gdb after 1.0 seconds")
        return "recovered"

    result = retry_call(fn, attempts=4, backoff_base=0.1, sleep=sleeps.append)

    assert result == "recovered"
    assert state["n"] == 3
    assert len(sleeps) == 2  # two backoffs before the third, successful try


def test_does_not_retry_non_retryable_errors():
    sleeps = []
    calls = []

    def fn():
        calls.append(1)
        raise RuntimeError('No symbol "x" in current context.')

    with pytest.raises(RuntimeError):
        retry_call(fn, attempts=5, sleep=sleeps.append)

    assert len(calls) == 1
    assert sleeps == []


def test_gives_up_after_attempts_on_persistent_retryable_error():
    sleeps = []

    def fn():
        raise RuntimeError("open failed")

    assert classify_error("open failed")["retryable"] is True
    with pytest.raises(RuntimeError):
        retry_call(fn, attempts=3, backoff_base=0.1, sleep=sleeps.append)

    assert len(sleeps) == 2  # attempts-1 backoffs
