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

## Later (subsequent plans)

- Productionization: container image, pinned toolchain, health/readiness, versioned API.
- Security/multi-tenancy: authn/authz, per-tenant isolation, command allowlisting.
- Integration: CI/test-orchestration hooks, result schemas, event/webhook surface.
- Concurrency hardening: per-probe locks, parallel-safe OpenOCD port allocation.
