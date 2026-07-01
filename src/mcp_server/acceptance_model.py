"""Validate and normalize an **AcceptanceSpec** — a machine-checked contract derived from a
product spec.

An AcceptanceSpec is a plain dict (JSON-native, no dataclasses) listing deterministic checks
that a later stage evaluates against live silicon state (see ``acceptance_eval``). This module
is the *schema*: it validates structure and fills defaults so the evaluator can trust its
input. Authoring the spec (product-spec text → this JSON) is out of scope — B1 consumes the
structured contract only, so the pass/fail verdict is never a hallucination.

Spec shape::

    {"name": "...", "description": "...",
     "checks": [{"id": "...", "kind": "...", "description": "...", <kind fields>}]}
"""

# Comparison operators shared with the evaluator. Kept here so validation can reject an
# unknown op up-front (fail fast at load, not mid-run).
VALID_OPS = ("eq", "ne", "lt", "le", "gt", "ge", "bits_set", "bits_clear")

# Required fields (beyond id/kind/description/op) per check kind.
_REQUIRED_FIELDS = {
    "memory_u32": ("address", "expect"),
    "variable": ("name", "expect"),
    "core_register": ("register", "expect"),
    "no_fault": (),
    "stopped_at": ("symbol",),
}
# Kinds that compare against a value and therefore accept an ``op`` (default "eq").
_COMPARING_KINDS = ("memory_u32", "variable", "core_register")

VALID_KINDS = tuple(_REQUIRED_FIELDS)


def validate_acceptance_spec(spec: dict) -> dict:
    """Return a normalized copy of *spec* or raise ``ValueError`` on any structural problem.

    Normalization: auto-assign ``id`` = ``check_<index>`` when missing, default ``op`` to
    ``"eq"`` for comparing kinds, and default ``description`` to "".
    """
    if not isinstance(spec, dict):
        raise ValueError("acceptance spec must be an object")
    checks = spec.get("checks")
    if not isinstance(checks, list):
        raise ValueError("acceptance spec must have a 'checks' list")
    if not checks:
        raise ValueError("acceptance spec has no checks")

    normalized_checks = []
    seen_ids: set = set()
    for index, check in enumerate(checks):
        normalized_checks.append(_validate_check(check, index, seen_ids))

    return {
        "name": spec.get("name") or "acceptance",
        "description": spec.get("description") or "",
        "checks": normalized_checks,
    }


def _validate_check(check: dict, index: int, seen_ids: set) -> dict:
    if not isinstance(check, dict):
        raise ValueError(f"check #{index} must be an object")

    kind = check.get("kind")
    if kind not in _REQUIRED_FIELDS:
        raise ValueError(
            f"check #{index} has unknown kind {kind!r}; expected one of {', '.join(VALID_KINDS)}"
        )

    check_id = check.get("id") or f"check_{index}"
    if check_id in seen_ids:
        raise ValueError(f"duplicate check id {check_id!r}")
    seen_ids.add(check_id)

    for field in _REQUIRED_FIELDS[kind]:
        if check.get(field) is None:
            raise ValueError(f"check {check_id!r} (kind {kind}) is missing required field {field!r}")

    normalized = dict(check)
    normalized["id"] = check_id
    normalized["description"] = check.get("description") or ""

    if kind in _COMPARING_KINDS:
        op = check.get("op") or "eq"
        if op not in VALID_OPS:
            raise ValueError(f"check {check_id!r} has invalid op {op!r}; expected one of {', '.join(VALID_OPS)}")
        normalized["op"] = op

    return normalized


def summarize_acceptance(spec: dict) -> dict:
    """A compact view of a (normalized or raw) spec for describe_acceptance."""
    checks = spec.get("checks") or []
    kinds: dict = {}
    for check in checks:
        kind = check.get("kind")
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "name": spec.get("name") or "acceptance",
        "description": spec.get("description") or "",
        "check_count": len(checks),
        "kinds": kinds,
    }
