"""The mechanical *hands* of the acceptance loop (Pillar C).

``run_iteration`` performs one deterministic pass — build → flash+run-to-state → evaluate — and
records the outcome through the pure control core. A build or run-to failure is captured as a
``phase_error`` iteration (the loop advances and counts toward the bound) rather than raising, so
the loop degrades gracefully instead of crashing the agent.

The mechanical steps are injected as a ``steps`` object so the loop is unit-testable with a fake.
``GdbLoopSteps`` is the real adapter, reusing the existing build / flash / acceptance primitives
rather than reimplementing them.
"""

from . import build as build_mod
from .acceptance_eval import GdbAcceptanceReader, evaluate_acceptance
from .composites import flash_and_run
from .loop_control import loop_decision, record_iteration


def run_iteration(state: dict, steps) -> dict:
    """Run one build→flash→evaluate pass through *steps*; record + decide. Returns
    ``{"iteration", "decision"}``."""
    if getattr(steps, "has_build", False):
        result = steps.build()
        if not result.get("success"):
            detail = result.get("log_tail") or f"build returned {result.get('returncode')}"
            entry = record_iteration(state, phase_error={"phase": "build", "detail": detail})
            return {"iteration": entry, "decision": loop_decision(state)}

    if getattr(steps, "has_flash", False):
        result = steps.flash()
        if not result.get("stopped"):
            detail = result.get("detail") or "target did not reach the run-to point within timeout"
            entry = record_iteration(state, phase_error={"phase": "flash_run", "detail": detail})
            return {"iteration": entry, "decision": loop_decision(state)}

    verdict = steps.evaluate()
    entry = record_iteration(state, verdict=verdict)
    return {"iteration": entry, "decision": loop_decision(state)}


class GdbLoopSteps:
    """Real mechanical steps: build via ``build.py``, flash+run via ``flash_and_run``, evaluate
    via the Pillar B1 acceptance evaluator against the live ``gdb_client``."""

    def __init__(self, gdb_client, spec: dict, build_cfg: dict | None = None, flash_cfg: dict | None = None):
        self._gdb = gdb_client
        self._spec = spec
        self._build_cfg = build_cfg
        self._flash_cfg = flash_cfg
        self.has_build = bool(build_cfg)
        self.has_flash = bool(flash_cfg)

    def build(self) -> dict:
        cfg = self._build_cfg
        assert cfg is not None  # callers gate on has_build
        kind = cfg["kind"]
        uv4_path = cfg.get("uv4_path")
        if kind == "keil":
            uv4_path = uv4_path or build_mod.find_uv4()
        command = build_mod.resolve_build_command(
            kind,
            project=cfg.get("project"),
            build_dir=cfg.get("build_dir"),
            directory=cfg.get("directory"),
            target=cfg.get("target"),
            config=cfg.get("config"),
            rebuild=cfg.get("rebuild", False),
            uv4_path=uv4_path,
            command=cfg.get("command"),
        )
        result = build_mod.run_build(command, timeout=cfg.get("timeout_sec", 600), cwd=cfg.get("cwd"))
        success = build_mod.is_build_success(kind, result["returncode"])
        return {"success": success, "returncode": result["returncode"], "log_tail": result["output"][-2000:]}

    def flash(self) -> dict:
        cfg = self._flash_cfg
        assert cfg is not None  # callers gate on has_flash
        result = flash_and_run(
            self._gdb,
            file_path=cfg["file_path"],
            run_to=cfg.get("run_to", "main"),
            timeout_sec=cfg.get("timeout_sec", 10.0),
        )
        return {"stopped": bool(result.get("stopped")), "detail": result.get("note")}

    def evaluate(self) -> dict:
        return evaluate_acceptance(self._spec, GdbAcceptanceReader(self._gdb))
