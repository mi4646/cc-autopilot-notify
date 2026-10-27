import json
import stat

import pytest

from cc_autopilot_notify import migrate


def _settings(tmp_path, payload):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload))
    return path


def _ours():
    return {"type": "command",
            "command": "PYTHONPATH=/x python3 /x/run_hook.py notify"}


def _foreign():
    return {"type": "command", "command": "other-tool"}


def test_removes_only_our_hooks_and_keeps_foreign(tmp_path):
    settings = _settings(tmp_path, {"hooks": {
        "Stop": [{"matcher": "", "hooks": [_ours()]},
                 {"matcher": "", "hooks": [_foreign()]}],
        "PreToolUse": [{"matcher": "x", "hooks": [_foreign()]}],
    }})
    migrate.migrate(settings, tmp_path / "cfg" / "config.json",
                    tmp_path / "unit.service", run_systemctl=False)
    data = json.loads(settings.read_text())
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "other-tool"
    assert data["hooks"]["Stop"] == [{"matcher": "", "hooks": [_foreign()]}]


def test_invalid_json_aborts_without_touching_files(tmp_path):
    bad = tmp_path / "settings.json"
    bad.write_text("{not json")
    before = bad.read_bytes()
    with pytest.raises(migrate.MigrationError):
        migrate.migrate(bad, tmp_path / "cfg.json", tmp_path / "unit.service",
                        run_systemctl=False)
    assert bad.read_bytes() == before


def test_moves_webhook_from_env_into_config(tmp_path):
    settings = _settings(tmp_path, {
        "env": {"CC_AUTOPILOT_FEISHU_WEBHOOK": "https://hook.example/x",
                "KEEP": "1"},
        "hooks": {"Stop": [{"matcher": "", "hooks": [_ours()]}]}})
    config_path = tmp_path / "cfg" / "config.json"
    migrate.migrate(settings, config_path, tmp_path / "unit.service",
                    run_systemctl=False)
    assert json.loads(config_path.read_text())["channels"]["feishu"]["webhook"] == \
        "https://hook.example/x"
    data = json.loads(settings.read_text())
    assert "CC_AUTOPILOT_FEISHU_WEBHOOK" not in data["env"]
    assert data["env"]["KEEP"] == "1"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_keeps_existing_direct_webhook(tmp_path):
    settings = _settings(tmp_path, {
        "env": {"CC_AUTOPILOT_FEISHU_WEBHOOK": "https://old/x"},
        "hooks": {}})
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"channels": {"feishu": {
        "webhook": "https://new/x"}}}))
    migrate.migrate(settings, config_path, tmp_path / "unit.service",
                    run_systemctl=False)
    assert json.loads(config_path.read_text())["channels"]["feishu"]["webhook"] == \
        "https://new/x"
    assert json.loads(settings.read_text())["env"]["CC_AUTOPILOT_FEISHU_WEBHOOK"] == \
        "https://old/x"


def test_unit_removed_only_with_our_marker(tmp_path):
    unit = tmp_path / "unit.service"
    unit.write_text("ExecStart=python3 -m cc_autopilot_notify.cli daemon\n")
    settings = _settings(tmp_path, {"hooks": {}})
    migrate.migrate(settings, tmp_path / "cfg.json", unit, run_systemctl=False)
    assert not unit.exists()


def test_foreign_unit_is_kept(tmp_path):
    unit = tmp_path / "unit.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/other\n")
    settings = _settings(tmp_path, {"hooks": {}})
    migrate.migrate(settings, tmp_path / "cfg.json", unit, run_systemctl=False)
    assert unit.exists()


def test_second_run_is_a_no_op(tmp_path):
    settings = _settings(tmp_path, {"hooks": {"Stop": [
        {"matcher": "", "hooks": [_ours()]}]}})
    migrate.migrate(settings, tmp_path / "cfg.json", tmp_path / "u.service",
                    run_systemctl=False)
    first = settings.read_bytes()
    summary = migrate.migrate(settings, tmp_path / "cfg.json",
                              tmp_path / "u.service", run_systemctl=False)
    assert "nothing to migrate" in summary
    assert settings.read_bytes() == first
