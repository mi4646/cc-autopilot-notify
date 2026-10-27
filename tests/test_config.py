import json

from cc_autopilot_notify import config


def test_missing_file_yields_defaults(tmp_path):
    cfg = config.load(tmp_path / "absent.json")
    assert config.foreground_seconds(cfg) == 150
    assert config.background_seconds(cfg) == 90


def test_partial_file_merges_over_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"thresholds": {"foreground_seconds": 30}}))
    cfg = config.load(p)
    assert config.foreground_seconds(cfg) == 30
    assert config.background_seconds(cfg) == 90


def test_corrupt_json_falls_back_to_defaults(tmp_path, caplog):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    cfg = config.load(p)
    assert config.foreground_seconds(cfg) == 150
    assert any("config" in r.message.lower() for r in caplog.records)


def test_undecodable_bytes_fall_back_to_defaults(tmp_path, caplog):
    p = tmp_path / "c.json"
    p.write_bytes(b'{"enabled": true}\xff\xfe')
    cfg = config.load(p)
    assert config.foreground_seconds(cfg) == 150
    assert any("config" in r.message.lower() for r in caplog.records)


def test_invalid_threshold_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"thresholds": {"foreground_seconds": "soon"}}))
    try:
        config.load(p)
    except config.ConfigError:
        return
    raise AssertionError("expected ConfigError")


def _load(tmp_path, payload):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(payload))
    return config.load(p)


def _assert_config_error(tmp_path, payload):
    try:
        _load(tmp_path, payload)
    except config.ConfigError:
        return
    raise AssertionError(f"expected ConfigError for {payload!r}")


def test_non_object_thresholds_raise_config_error(tmp_path):
    """`thresholds: null` used to reach thresholds.get and raise AttributeError."""
    for value in (None, "fast", [], 3):
        _assert_config_error(tmp_path, {"thresholds": value})


def test_non_object_sections_raise_config_error(tmp_path):
    for name, value in (("judge", None), ("channels", None),
                        ("afk", "300"), ("quiet_hours", []), ("thresholds", None)):
        _assert_config_error(tmp_path, {name: value})


def test_unusable_section_values_raise_config_error(tmp_path):
    cases = [
        {"judge": {"name_typo": 1}, "channels": {"feishu": {"webhook_env": 123}}},
        {"judge": {"timeout_ms": "8s"}},
        {"judge": {"enabled": "yes"}},
        {"judge": {"model": 5}},
        {"judge": {"fallback": ""}},
        {"afk": {"set_claude_afk_timeout_ms": -1}},
        {"channels": {"feishu": None}},
        {"channels": {"feishu": {"enabled": "true"}}},
        {"quiet_hours": {"start": "9am"}},
        {"quiet_hours": {"start": "25:00"}},
        {"quiet_hours": {"enabled": "no"}},
        {"min_duration_seconds": -5},
        {"enabled": "maybe"},
    ]
    for payload in cases:
        _assert_config_error(tmp_path, payload)


def test_misspelled_key_warns_instead_of_silently_ignoring(tmp_path, caplog):
    """A typo must not vanish: the value stays 150 and a warning names the key."""
    cfg = _load(tmp_path, {"thresholds": {"foreground_second": 30}})
    assert config.foreground_seconds(cfg) == 150
    assert "thresholds.foreground_second" in caplog.text


def test_misspelled_nested_key_warns(tmp_path, caplog):
    _load(tmp_path, {"channels": {"feishu": {"webhook_envn": "X"}}})
    assert "channels.feishu.webhook_envn" in caplog.text


def test_accessors_tolerate_non_dict_sections():
    """A hand-built cfg (tests, older callers) must not raise AttributeError."""
    broken = {"judge": None, "channels": None, "thresholds": None}
    assert config.judge_model(broken) is None
    assert config.feishu_webhook(broken) is None



def test_webhook_reads_named_env(monkeypatch):
    cfg = config.DEFAULTS
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://example.test/x")
    assert config.feishu_webhook(cfg) == "https://example.test/x"


def test_webhook_absent_env_is_none(monkeypatch):
    monkeypatch.delenv("CC_AUTOPILOT_FEISHU_WEBHOOK", raising=False)
    assert config.feishu_webhook(config.DEFAULTS) is None


def test_judge_model_defaults_to_none():
    assert config.judge_model(config.DEFAULTS) is None
