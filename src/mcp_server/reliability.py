"""Retry with exponential backoff for transient probe/link failures.

Phase 2 priority #2 (reliability). Probes are flaky — USB contention, a busy or
just-replugged ST-Link, a momentary link drop. ``retry_call`` retries only the
failures the error taxonomy marks ``retryable`` (USB contention, lost connections,
``open failed``), backing off between attempts, and re-raises anything fatal
(e.g. missing symbols) immediately so real errors are not masked.
"""

import time

from .error_taxonomy import classify_error


def retry_call(fn, attempts: int = 3, backoff_base: float = 0.5, classify=classify_error, sleep=time.sleep):
    """Call ``fn`` up to ``attempts`` times, retrying only retryable failures."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below if not retryable
            last_exc = exc
            info = classify(str(exc))
            if not info.get("retryable") or attempt == attempts - 1:
                raise
            sleep(backoff_base * (2 ** attempt))
    assert last_exc is not None  # only reachable when attempts < 1, an invalid call
    raise last_exc
