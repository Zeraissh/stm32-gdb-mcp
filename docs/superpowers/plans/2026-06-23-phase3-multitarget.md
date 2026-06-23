# Phase 3 — Multi-target / concurrency & productionization / 多端与产线化

> Implement task-by-task with TDD. Phase 3 starts the work deferred during single-target
> excellence: concurrency/isolation first, then service/security/integration.

## P3-A: Per-target session isolation (de-singleton) [foundation]

Today every per-target object is a module-level singleton in `server.py` (one gdb_manager,
gdb_client, svd_parser, variable_tracker, debug_profile, freertos_inspector, log readers,
memory_guard, last_session). That allows exactly ONE target at a time — the blocker for a
test rack / CI debugging N boards concurrently.

- **DebugSession** (`debug_session.py`): bundles all per-target objects.
- **SessionManager**: `get(session_id)` (lazy-create), `list()`, `close(session_id)`.
- `_dispatch_tool` resolves the session from `arguments["session"]` (default `"default"`) and
  binds the per-target objects as locals — handlers stay textually unchanged.
- Backward compatible: the `"default"` session reads the existing module globals, so
  single-target users and all existing tests work unchanged with no `session` arg.
- New tools: `list_sessions`, `close_session`. e.g. `start_debug_session(session="rackA")`.
- The session journal stays global (entries tagged with the session id) for rack observability.

## P3-B: Concurrency hardening (distinct ports + per-board probes) [done]

State isolation (P3-A) is necessary but not sufficient for *concurrent* OpenOCD: two
instances both bind gdb_port 3333 and both grab "the first ST-Link". Fixed so N boards run
at once:

- `gdb_manager` parses `gdb_port N` out of the OpenOCD `-c` args (was hard-coded 3333), so the
  GDB client connects to whatever port that session's server actually bound.
- `start_debug_session` auto-appends, for OpenOCD:
  - `-c "gdb_port <session.gdb_port>"` for non-default sessions (3343, 3353, …) — distinct ports.
  - `-c "adapter serial <serial>"` when a `serial` arg is given — selects that board's probe.
  Both are skipped if the user already specified them (explicit args win). The serial is
  remembered on the session so reconnect/recover reuses it.
- HIL-validated on the L431 rig: `start_debug_session(session="rackHIL", …)` brings OpenOCD up
  on 3343 and the GDB client connects there; `list_sessions` shows default(3333)+rackHIL(3343).

## Later (subsequent plans)

- Productionization: container image, pinned toolchain, health/readiness, versioned API.
- Security/multi-tenancy: authn/authz, per-tenant isolation, command allowlisting.
- Integration: CI/test-orchestration hooks, result schemas, event/webhook surface.
- Further hardening: per-probe locks / telnet+tcl port allocation if OpenOCD's other ports collide.
