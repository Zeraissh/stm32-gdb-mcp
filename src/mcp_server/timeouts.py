"""Centralized, overridable timeouts.

Phase 2 priority #1 (determinism) + #2 (reliability): one named place for every
GDB operation timeout, instead of magic numbers scattered across call sites. The
agent can widen them once for a slow/flaky probe, and because ``set_timeouts`` is
a journaled tool call, a replayed session uses the same values — deterministically.
"""

DEFAULTS = {
    "default": 1.0,
    "connect": 5.0,
    "reset": 5.0,
    "halt": 5.0,
    "memory": 2.0,
    "registers": 2.0,
    "source": 2.0,
    "run": 10.0,
    "download": 60.0,
    "symbols": 2.0,
    "monitor": 2.0,
    "breakpoint": 2.0,
    "step": 1.0,
    "finish": 2.0,
    "stack": 2.0,
    "symbol_list": 3.0,
    "evaluate": 2.0,
    "coredump": 30.0,
    "coredump_load": 10.0,
    "verify": 30.0,
}


class TimeoutConfig:
    def __init__(self):
        self.values = dict(DEFAULTS)

    def get(self, name: str) -> float:
        return self.values.get(name, self.values["default"])

    def set(self, overrides: dict) -> dict:
        validated = {}
        for name, value in (overrides or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"timeout '{name}' must be a positive number, got {value!r}")
            validated[name] = float(value)
        self.values.update(validated)
        return self.as_dict()

    def as_dict(self) -> dict:
        return dict(self.values)
