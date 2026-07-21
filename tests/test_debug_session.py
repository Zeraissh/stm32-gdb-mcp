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


def test_manager_does_not_reuse_a_live_sessions_port_after_middle_close():
    m = SessionManager()
    m.get("default")
    m.get("rackA")
    rack_b_port = m.get("rackB").gdb_port

    assert m.close("rackA") is True

    assert m.get("rackC").gdb_port != rack_b_port


def test_session_teardown_stops_debug_and_all_log_readers():
    session = DebugSession("rack")
    stopped = []

    class Stopper:
        def __init__(self, name):
            self.name = name

        def stop(self):
            stopped.append(self.name)

    class GdbStopper:
        def stop_gdb(self):
            stopped.append("gdb")

    session.gdb_client = GdbStopper()
    session.gdb_manager = Stopper("server")
    session.variable_tracker = Stopper("tracker")
    session.rtt_log_reader = Stopper("rtt")
    session.swo_log_reader = Stopper("swo")
    session.swo_file_reader = Stopper("swo_file")
    session.uart_log_reader = Stopper("uart")

    session.teardown()

    assert set(stopped) == {"gdb", "server", "tracker", "rtt", "swo", "swo_file", "uart"}
