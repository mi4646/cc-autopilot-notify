import json
import stat
from pathlib import Path

import pytest

from cc_autopilot_notify import migrate


def _settings(tmp_path, payload):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload))
    return path


def _ours():
    return {"type": "command",
            "command": "PYTHONPATH=/x python3 /x/cc_autopilot_notify/run_hook.py notify"}


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


def test_keeps_foreign_hook_in_the_same_group(tmp_path):
    settings = _settings(tmp_path, {"hooks": {"Stop": [
        {"matcher": "", "hooks": [_ours(), _foreign()]}]}})
    migrate.migrate(settings, tmp_path / "cfg.json", tmp_path / "u.service",
                    run_systemctl=False)
    data = json.loads(settings.read_text())
    assert data["hooks"]["Stop"] == [{"matcher": "", "hooks": [_foreign()]}]


def test_keeps_another_projects_run_hook(tmp_path):
    foreign = {"type": "command", "command": "python3 /other/run_hook.py notify"}
    settings = _settings(tmp_path, {"hooks": {"Stop": [
        {"matcher": "", "hooks": [foreign]}]}})
    migrate.migrate(settings, tmp_path / "cfg.json", tmp_path / "u.service",
                    run_systemctl=False)
    data = json.loads(settings.read_text())
    assert data["hooks"]["Stop"] == [{"matcher": "", "hooks": [foreign]}]


def test_backup_is_private_because_it_holds_the_secret(tmp_path):
    settings = _settings(tmp_path, {
        "env": {"CC_AUTOPILOT_FEISHU_WEBHOOK": "https://hook.example/x"},
        "hooks": {"Stop": [{"matcher": "", "hooks": [_ours()]}]}})
    migrate.migrate(settings, tmp_path / "cfg" / "config.json",
                    tmp_path / "u.service", run_systemctl=False)
    backups = list(tmp_path.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


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


def test_moves_legacy_webhook_even_when_config_has_one(tmp_path):
    """Re-running the migration must finish the credential move."""
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
    data = json.loads(settings.read_text())
    assert "CC_AUTOPILOT_FEISHU_WEBHOOK" not in data.get("env", {})


def test_refuses_a_non_object_hooks_section(tmp_path):
    settings = _settings(tmp_path, {"hooks": "not-an-object"})
    with pytest.raises(migrate.MigrationError):
        migrate.migrate(settings, tmp_path / "config.json",
                        tmp_path / "unit.service", run_systemctl=False)
    assert json.loads(settings.read_text()) == {"hooks": "not-an-object"}


def test_refuses_a_non_list_hook_event(tmp_path):
    settings = _settings(tmp_path, {"hooks": {"Stop": "nope"}})
    with pytest.raises(migrate.MigrationError):
        migrate.migrate(settings, tmp_path / "config.json",
                        tmp_path / "unit.service", run_systemctl=False)


def test_follows_a_symlinked_settings_file(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(
        {"env": {"CC_AUTOPILOT_FEISHU_WEBHOOK": "https://old/x"}}))
    link = tmp_path / "settings.json"
    link.symlink_to(real)

    migrate.migrate(link, tmp_path / "config.json",
                    tmp_path / "unit.service", run_systemctl=False)

    assert link.is_symlink(), "replacing the link would orphan the real file"
    data = json.loads(real.read_text())
    assert "CC_AUTOPILOT_FEISHU_WEBHOOK" not in data.get("env", {})


def test_does_not_touch_systemd_for_a_throwaway_unit(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(migrate.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    unit = tmp_path / "unit.service"
    unit.write_text("[Service]\n# cc_autopilot_notify\n")
    settings = _settings(tmp_path, {"hooks": {}})

    migrate.migrate(settings, tmp_path / "config.json", unit,
                    run_systemctl=True)

    assert calls == [], "a unit outside the systemd user dir must not reach systemctl"


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


def _failing_replace(monkeypatch, fail_on):
    real_replace = migrate.os.replace

    def replace(src, dst):
        if Path(dst) == fail_on:
            raise OSError("simulated write failure")
        real_replace(src, dst)

    monkeypatch.setattr(migrate.os, "replace", replace)


def test_settings_write_failure_is_controlled_and_keeps_env(tmp_path, monkeypatch):
    """The later write can fail: config is already correct, settings is intact.

    Cross-file replacement is not transactional, so the migration replaces the
    config first. A failing settings write therefore leaves a duplicate (safe)
    rather than dropping the only webhook copy, and the error is controlled.
    """
    settings = _settings(tmp_path, {
        "env": {"CC_AUTOPILOT_FEISHU_WEBHOOK": "https://hook.example/x",
                "KEEP": "1"},
        "hooks": {"Stop": [{"matcher": "", "hooks": [_ours()]}]}})
    config_path = tmp_path / "cfg" / "config.json"
    original = settings.read_bytes()
    _failing_replace(monkeypatch, settings)

    with pytest.raises(migrate.MigrationError) as excinfo:
        migrate.migrate(settings, config_path, tmp_path / "u.service",
                        run_systemctl=False)

    assert settings.read_bytes() == original
    assert json.loads(settings.read_text())["env"]["CC_AUTOPILOT_FEISHU_WEBHOOK"] == \
        "https://hook.example/x"
    assert json.loads(config_path.read_text())["channels"]["feishu"]["webhook"] == \
        "https://hook.example/x"
    assert "already replaced" in str(excinfo.value)
    backups = list(tmp_path.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert not list(tmp_path.rglob(".tmp-*"))


def test_config_write_failure_leaves_settings_untouched(tmp_path, monkeypatch):
    settings = _settings(tmp_path, {
        "env": {"CC_AUTOPILOT_FEISHU_WEBHOOK": "https://hook.example/x"},
        "hooks": {"Stop": [{"matcher": "", "hooks": [_ours()]}]}})
    config_path = tmp_path / "cfg" / "config.json"
    original = settings.read_bytes()
    _failing_replace(monkeypatch, config_path)

    with pytest.raises(migrate.MigrationError) as excinfo:
        migrate.migrate(settings, config_path, tmp_path / "u.service",
                        run_systemctl=False)

    assert settings.read_bytes() == original
    assert not config_path.exists()
    assert "no file was replaced" in str(excinfo.value)
    assert not list(tmp_path.rglob(".tmp-*"))


def test_staging_failure_writes_nothing(tmp_path, monkeypatch):
    settings = _settings(tmp_path, {
        "env": {"CC_AUTOPILOT_FEISHU_WEBHOOK": "https://hook.example/x"},
        "hooks": {"Stop": [{"matcher": "", "hooks": [_ours()]}]}})
    config_path = tmp_path / "config.json"
    original = settings.read_bytes()
    real_stage = migrate._stage_json

    def stage(path, data, **kwargs):
        if Path(path) == settings:
            raise TypeError("not serializable")
        return real_stage(path, data, **kwargs)

    monkeypatch.setattr(migrate, "_stage_json", stage)

    with pytest.raises(migrate.MigrationError):
        migrate.migrate(settings, config_path, tmp_path / "u.service",
                        run_systemctl=False)

    assert settings.read_bytes() == original
    assert not config_path.exists()
    assert not list(tmp_path.glob(".tmp-*"))
