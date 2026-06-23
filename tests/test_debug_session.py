from mcp_server.debug_session import DebugSession, SessionManager


def test_session_bundles_independent_objects():
    a = DebugSession("a")
    b = DebugSession("b")
    assert a.gdb_client is not b.gdb_client
    assert a.debug_profile is not b.debug_profile
    assert a.memory_guard is not b.memory_guard
    assert a.last_session is not b.last_session


def test_manager_lazily_creates_and_reuses_sessions():
    m = SessionManager()
    s1 = m.get("rackA")
    s2 = m.get("rackA")
    assert s1 is s2                      # same id -> same session
    assert m.get("rackB") is not s1      # different id -> isolated session


def test_manager_assigns_distinct_ports_to_concurrent_sessions():
    m = SessionManager()
    assert m.get("default").gdb_port == 3333
    p_a = m.get("rackA").gdb_port
    p_b = m.get("rackB").gdb_port
    assert p_a != 3333 and p_b != 3333 and p_a != p_b


def test_manager_list_and_close():
    m = SessionManager()
    m.get("rackA")
    m.get("rackB")
    ids = {row["session"] for row in m.list()}
    assert ids == {"rackA", "rackB"}

    assert m.close("rackA") is True
    assert m.close("rackA") is False     # already gone
    assert {row["session"] for row in m.list()} == {"rackB"}
