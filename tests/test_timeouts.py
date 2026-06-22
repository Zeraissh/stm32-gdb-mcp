import pytest

from mcp_server.timeouts import DEFAULTS, TimeoutConfig


def test_defaults_are_available_by_name():
    cfg = TimeoutConfig()
    assert cfg.get("connect") == DEFAULTS["connect"]
    assert cfg.get("memory") == DEFAULTS["memory"]


def test_unknown_name_falls_back_to_default():
    cfg = TimeoutConfig()
    assert cfg.get("nonexistent") == DEFAULTS["default"]


def test_set_overrides_and_returns_full_config():
    cfg = TimeoutConfig()
    result = cfg.set({"memory": 4.0, "connect": 8.0})

    assert cfg.get("memory") == 4.0
    assert cfg.get("connect") == 8.0
    assert result["memory"] == 4.0
    # untouched values remain at their defaults
    assert result["reset"] == DEFAULTS["reset"]


def test_set_rejects_non_positive_or_non_numeric():
    cfg = TimeoutConfig()
    with pytest.raises(ValueError):
        cfg.set({"memory": 0})
    with pytest.raises(ValueError):
        cfg.set({"memory": "fast"})
    # config unchanged after a rejected update
    assert cfg.get("memory") == DEFAULTS["memory"]


def test_as_dict_is_a_copy():
    cfg = TimeoutConfig()
    snapshot = cfg.as_dict()
    snapshot["memory"] = 999
    assert cfg.get("memory") == DEFAULTS["memory"]
