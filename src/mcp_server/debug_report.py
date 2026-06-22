"""Bundle a self-contained, reproducible debug report.

Phase 2 priority #1 (determinism & reproducibility): collapse a whole session
into one shareable artifact — the action journal, aggregated metrics, the active
profile, and optionally a final state snapshot and a coredump path — keyed by
run-id. Hand someone this file and they can see exactly what was done and what
the target looked like.
"""

import json
import time

from .metrics import compute_metrics


def build_report(run_id, journal_entries, profile=None, snapshot=None, coredump_path=None) -> dict:
    return {
        "run_id": run_id,
        "generated_at": time.time(),
        "profile": profile or {},
        "metrics": compute_metrics(journal_entries),
        "journal": journal_entries,
        "snapshot": snapshot,
        "coredump": coredump_path,
    }


def write_report(path: str, report: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    return path
