import pytest

from mcp_server.acceptance_model import (
    summarize_acceptance,
    validate_acceptance_spec,
)


def test_validate_normalizes_ids_and_default_op():
    spec = {
        "name": "blinky",
        "checks": [
            {"kind": "memory_u32", "address": "0x40013800", "expect": "0x1"},
            {"id": "sysclk", "kind": "variable", "name": "SystemCoreClock", "expect": 80000000, "op": "eq"},
            {"kind": "no_fault"},
        ],
    }
    normalized = validate_acceptance_spec(spec)

    assert [c["id"] for c in normalized["checks"]] == ["check_0", "sysclk", "check_2"]
    assert normalized["checks"][0]["op"] == "eq"  # default filled for a comparing kind
    assert "op" not in normalized["checks"][2]  # no_fault does not take an op
    assert normalized["name"] == "blinky"


def test_validate_rejects_non_dict_spec():
    with pytest.raises(ValueError, match="must be an object"):
        validate_acceptance_spec([1, 2, 3])


def test_validate_rejects_missing_checks_list():
    with pytest.raises(ValueError, match="'checks' list"):
        validate_acceptance_spec({"name": "x"})


def test_validate_rejects_empty_checks():
    with pytest.raises(ValueError, match="no checks"):
        validate_acceptance_spec({"checks": []})


def test_validate_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown kind"):
        validate_acceptance_spec({"checks": [{"kind": "waveform", "expect": 1}]})


def test_validate_rejects_missing_required_field():
    with pytest.raises(ValueError, match="missing required field 'address'"):
        validate_acceptance_spec({"checks": [{"kind": "memory_u32", "expect": "0x1"}]})


def test_validate_rejects_invalid_op():
    with pytest.raises(ValueError, match="invalid op"):
        validate_acceptance_spec({"checks": [{"kind": "variable", "name": "x", "expect": 1, "op": "approx"}]})


def test_validate_rejects_duplicate_ids():
    spec = {"checks": [
        {"id": "dup", "kind": "no_fault"},
        {"id": "dup", "kind": "no_fault"},
    ]}
    with pytest.raises(ValueError, match="duplicate check id"):
        validate_acceptance_spec(spec)


def test_summarize_counts_kinds():
    spec = validate_acceptance_spec({"name": "s", "checks": [
        {"kind": "memory_u32", "address": "0x0", "expect": 0},
        {"kind": "memory_u32", "address": "0x4", "expect": 0},
        {"kind": "no_fault"},
    ]})
    summary = summarize_acceptance(spec)

    assert summary["check_count"] == 3
    assert summary["kinds"] == {"memory_u32": 2, "no_fault": 1}
