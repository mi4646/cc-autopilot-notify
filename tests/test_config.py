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


def test_invalid_threshold_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"thresholds": {"foreground_seconds": "soon"}}))
    try:
        config.load(p)
    except config.ConfigError:
        return
    raise AssertionError("expected ConfigError")


def test_webhook_reads_named_env(monkeypatch):
    cfg = config.DEFAULTS
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://example.test/x")
    assert config.feishu_webhook(cfg) == "https://example.test/x"


def test_webhook_absent_env_is_none(monkeypatch):
    monkeypatch.delenv("CC_AUTOPILOT_FEISHU_WEBHOOK", raising=False)
    assert config.feishu_webhook(config.DEFAULTS) is None


def test_judge_model_defaults_to_none():
    assert config.judge_model(config.DEFAULTS) is None
