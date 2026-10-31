import datetime
import json
import os
import stat

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


def test_direct_webhook_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://env.example/x")
    cfg = _load(tmp_path, {"channels": {"feishu": {"webhook": "https://direct.example/y"}}})
    assert config.feishu_webhook(cfg) == "https://direct.example/y"


def test_env_fallback_when_webhook_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://env.example/x")
    cfg = _load(tmp_path, {"thresholds": {"foreground_seconds": 150,
                                         "background_seconds": 90}})
    assert config.feishu_webhook(cfg) == "https://env.example/x"


def test_empty_direct_webhook_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://env.example/x")
    cfg = _load(tmp_path, {"channels": {"feishu": {"webhook": ""}}})
    assert config.feishu_webhook(cfg) == "https://env.example/x"


def test_whitespace_direct_webhook_is_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://env.example/x")
    cfg = _load(tmp_path, {"channels": {"feishu": {"webhook": "   "}}})
    assert config.feishu_webhook(cfg) == "https://env.example/x"

    monkeypatch.delenv("CC_AUTOPILOT_FEISHU_WEBHOOK", raising=False)
    assert config.feishu_webhook(cfg) is None


def test_bad_webhook_type_raises(tmp_path):
    _assert_config_error(tmp_path, {"channels": {"feishu": {"webhook": 5}}})


def test_path_b_and_daemon_flags_validate(tmp_path):
    assert config.path_b_enabled(_load(tmp_path, {})) is False
    assert config.daemon_enabled(_load(tmp_path, {})) is True
    cfg = _load(tmp_path, {"path_b": {"enabled": True}, "daemon": {"enabled": False}})
    assert config.path_b_enabled(cfg) is True
    assert config.daemon_enabled(cfg) is False
    _assert_config_error(tmp_path, {"path_b": {"enabled": "yes"}})
    _assert_config_error(tmp_path, {"daemon": {"enabled": 1}})


def test_config_path_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom.json"
    target.write_text(json.dumps({"thresholds": {"foreground_seconds": 111,
                                                 "background_seconds": 22}}))
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(target))
    assert config.foreground_seconds(config.load()) == 111


def test_write_default_is_private(tmp_path):
    target = tmp_path / "cfg" / "config.json"
    config.write_default(target)
    assert json.loads(target.read_text())["daemon"]["enabled"] is True
    assert (target.stat().st_mode & 0o777) == 0o600


def test_write_default_never_overwrites_an_existing_file(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"enabled": false}\n')
    os.chmod(target, 0o644)
    config.write_default(target)
    assert target.read_text() == '{"enabled": false}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_notify_dedupe_seconds_default_and_validation(tmp_path):
    assert config.notify_dedupe_seconds(config.load(tmp_path / "absent.json")) == 300
    assert config.notify_dedupe_seconds(_load(tmp_path, {"notify": {"dedupe_seconds": 0}})) == 0
    _assert_config_error(tmp_path, {"notify": {"dedupe_seconds": -1}})
    _assert_config_error(tmp_path, {"notify": {"dedupe_seconds": True}})


def test_notification_message_translations_load_and_validate(tmp_path):
    translations = {"Claude is waiting for your input": "Claude 正在等待您的输入"}
    cfg = _load(tmp_path, {"notify": {"message_translations": translations}})
    assert config.notification_message_translations(cfg) == translations
    _assert_config_error(tmp_path, {"notify": {"message_translations": []}})
    _assert_config_error(tmp_path, {"notify": {"message_translations": {"message": 1}}})


def test_notify_min_turn_seconds_default_and_validation(tmp_path):
    assert config.notify_min_turn_seconds(config.load(tmp_path / "absent.json")) == 0
    assert config.notify_min_turn_seconds(
        _load(tmp_path, {"notify": {"min_turn_seconds": 60}})) == 60
    _assert_config_error(tmp_path, {"notify": {"min_turn_seconds": -1}})
    _assert_config_error(tmp_path, {"notify": {"min_turn_seconds": True}})


def _at(hour, minute):
    return datetime.datetime(2026, 10, 27, hour, minute).timestamp()


def test_quiet_hours_same_day_window():
    cfg = {"quiet_hours": {"enabled": True, "start": "01:00", "end": "05:00"}}
    assert config.in_quiet_hours(cfg, now=_at(3, 0)) is True
    assert config.in_quiet_hours(cfg, now=_at(6, 0)) is False


def test_quiet_hours_overnight_window():
    cfg = {"quiet_hours": {"enabled": True, "start": "22:00", "end": "06:00"}}
    assert config.in_quiet_hours(cfg, now=_at(23, 0)) is True
    assert config.in_quiet_hours(cfg, now=_at(5, 59)) is True
    assert config.in_quiet_hours(cfg, now=_at(6, 0)) is False
    assert config.in_quiet_hours(cfg, now=_at(12, 0)) is False


def test_quiet_hours_disabled():
    cfg = {"quiet_hours": {"enabled": False, "start": "00:00", "end": "23:59"}}
    assert config.in_quiet_hours(cfg, now=_at(12, 0)) is False


def test_quiet_hours_equal_bounds_allow_rather_than_silence():
    """An unset/mistyped window must not cost the user a whole day of alerts."""
    cfg = {"quiet_hours": {"enabled": True, "start": "00:00", "end": "00:00"}}
    assert config.in_quiet_hours(cfg, now=_at(3, 0)) is False
    assert config.in_quiet_hours(cfg, now=_at(23, 0)) is False


def test_quiet_hours_invalid_bounds_allow():
    for start, end in (("9am", "17:00"), ("25:00", "17:00"), (None, "17:00")):
        cfg = {"quiet_hours": {"enabled": True, "start": start, "end": end}}
        assert config.in_quiet_hours(cfg, now=_at(3, 0)) is False


def test_autopilot_defaults(tmp_path):
    cfg = config.load(tmp_path / "absent.json")
    assert config.continue_enabled(cfg) is False
    assert config.shadow_mode(cfg) is True
    assert config.max_continues(cfg) == 10
    assert config.suggest_wait_seconds(cfg) == 90
    assert config.failure_continues(cfg) == 2


def test_autopilot_overrides(tmp_path):
    cfg = _load(tmp_path, {"autopilot": {"continue_enabled": True,
                                         "shadow_mode": False,
                                         "max_continues": 3,
                                         "suggest_wait_seconds": 120,
                                         "failure_continues": 1}})
    assert config.continue_enabled(cfg) is True
    assert config.shadow_mode(cfg) is False
    assert config.max_continues(cfg) == 3
    assert config.suggest_wait_seconds(cfg) == 120
    assert config.failure_continues(cfg) == 1


def test_autopilot_invalid_values_raise(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"autopilot": {"max_continues": 0}}))
    try:
        config.load(p)
    except config.ConfigError:
        return
    raise AssertionError("expected ConfigError")


def test_autopilot_accessors_tolerate_non_dict_section():
    """A hand-built cfg (tests, older callers) must not raise AttributeError."""
    broken = {"autopilot": None}
    assert config.continue_enabled(broken) is False
    assert config.max_continues(broken) == 10
