"""Low-rate debugger-side sampling helpers.

This is best-effort polling through the existing GDB expression path. It does
not halt the target itself, and it is deliberately scoped to slow trend capture
rather than high-speed oscilloscope-style acquisition.
"""

import math
import time
from typing import Any

from .debug_experiments import capture_expressions


def sample_expressions(
    gdb_client,
    duration_sec: float,
    interval_sec: float,
    expressions: list[str] | None = None,
    table: dict | None = None,
    max_samples: int = 10_000,
    sleep=None,
    monotonic=None,
) -> dict:
    """Poll expressions at a fixed interval and return series + summaries."""
    duration = _validate_duration(duration_sec)
    interval = _validate_interval(interval_sec)
    if not expressions and table is None:
        raise ValueError("sample expressions or table is required")
    _validate_sample_budget(duration, interval, max_samples)
    sleep_fn = sleep or time.sleep
    monotonic_fn = monotonic or time.monotonic

    started = monotonic_fn()
    deadline = started + duration
    series = []
    index = 0
    while True:
        now = started if index == 0 else monotonic_fn()
        capture = capture_expressions(gdb_client, expressions=expressions, table=table)
        series.append(_sample_row(index, now - started, capture))
        if now >= deadline:
            break

        index += 1
        next_at = min(started + index * interval, deadline)
        sleep_fn(max(0.0, next_at - now))

    return {
        "mode": "debugger_polling",
        "note": (
            "Best-effort low-rate polling through GDB expressions. It does not halt the target, "
            "but running-target reads may fail on probes/targets that do not support background access."
        ),
        "series": series,
        "summary": _summarize_series(series),
        "timing": {
            "duration_sec": duration,
            "requested_interval_sec": interval,
            "actual_elapsed_sec": round(series[-1]["t_sec"], 6) if series else 0.0,
            "sample_count": len(series),
        },
    }


def sample_interval_from_config(sample: dict) -> float:
    """Accept either seconds or milliseconds in tool arguments."""
    if "interval_sec" in sample:
        return _validate_interval(sample["interval_sec"])
    if "interval_ms" in sample:
        interval_ms = sample["interval_ms"]
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int | float):
            raise ValueError("sample.interval_ms must be a positive number")
        return _validate_interval(float(interval_ms) / 1000.0)
    raise ValueError("sample.interval_sec or sample.interval_ms is required")


def _validate_duration(value) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("duration_sec must be a number >= 0")
    duration = float(value)
    if duration < 0:
        raise ValueError("duration_sec must be >= 0")
    return duration


def _validate_interval(value) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("interval_sec must be a positive number")
    interval = float(value)
    if interval <= 0:
        raise ValueError("interval_sec must be > 0")
    return interval


def _validate_sample_budget(duration: float, interval: float, max_samples: int) -> None:
    if isinstance(max_samples, bool) or not isinstance(max_samples, int) or max_samples < 1:
        raise ValueError("max_samples must be an integer >= 1")
    expected = math.ceil(duration / interval) + 1
    if expected > max_samples:
        raise ValueError(f"sample budget exceeded: {expected} samples requested, max_samples={max_samples}")


def _sample_row(index: int, elapsed_sec: float, capture: dict) -> dict:
    row: dict[str, Any] = {
        "index": index,
        "t_sec": round(max(0.0, elapsed_sec), 6),
        "values": {},
        "raw": {},
        "errors": {},
    }
    for item in capture["values"]:
        expression = item["expression"]
        if "error" in item:
            row["errors"][expression] = item["error"]
            continue
        row["values"][expression] = item.get("value")
        row["raw"][expression] = item.get("raw")
    if "tables" in capture:
        row["tables"] = capture["tables"]
    return row


def _summarize_series(series: list[dict]) -> dict:
    expressions = set()
    for row in series:
        expressions.update(row["values"])
        expressions.update(row["errors"])

    summary = {}
    for expression in sorted(expressions):
        values = [row["values"][expression] for row in series if expression in row["values"]]
        error_count = sum(1 for row in series if expression in row["errors"])
        item = {
            "sample_count": len(series),
            "error_count": error_count,
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
        }
        if values and all(isinstance(value, int | float) and not isinstance(value, bool) for value in values):
            item["min"] = min(values)
            item["max"] = max(values)
            item["delta"] = values[-1] - values[0]
        summary[expression] = item
    return summary
