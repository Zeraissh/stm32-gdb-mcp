from mcp_server.session_journal import SessionJournal


def test_record_assigns_increasing_sequence_and_keeps_fields():
    journal = SessionJournal()

    e1 = journal.record("read_core_registers", {}, ok=True, summary="pc=0x8000058")
    e2 = journal.record("write_memory", {"address": "0x40003000"}, ok=False, error="blocked")

    assert e1["seq"] == 1
    assert e2["seq"] == 2
    assert e1["tool"] == "read_core_registers"
    assert e1["ok"] is True
    assert e2["ok"] is False
    assert e2["error"] == "blocked"
    assert isinstance(e1["ts"], float)


def test_get_respects_limit_and_run_id_is_stable():
    journal = SessionJournal()
    for i in range(5):
        journal.record(f"tool{i}", {}, ok=True)

    assert len(journal.get()) == 5
    assert len(journal.get(limit=2)) == 2
    assert journal.get(limit=2)[0]["tool"] == "tool3"
    assert journal.run_id == journal.run_id  # stable for the session


def test_timeline_is_human_readable():
    journal = SessionJournal()
    journal.record("set_breakpoint", {"location": "main"}, ok=True, summary="bp set")
    journal.record("run_and_wait", {}, ok=False, error="timeout")

    lines = journal.timeline()

    assert "set_breakpoint" in lines[0]
    assert "ok" in lines[0]
    assert "run_and_wait" in lines[1]
    assert "timeout" in lines[1]


def test_clear_empties_the_journal_but_keeps_run_id():
    journal = SessionJournal()
    journal.record("x", {}, ok=True)
    run_id = journal.run_id

    journal.clear()

    assert journal.get() == []
    assert journal.run_id == run_id
