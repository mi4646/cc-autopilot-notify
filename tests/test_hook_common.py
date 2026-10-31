import io
import json

from cc_autopilot_notify import hook_common


def _env(tmp_path, monkeypatch, payload):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(config_path))
    monkeypatch.setenv("CC_AUTOPILOT_STATE", str(tmp_path / "state"))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))


def test_run_hook_honours_the_top_level_switch(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, {"enabled": False})
    called = []
    hook_common.run_hook(lambda event, root, cfg: called.append(1))
    assert called == []


def test_run_hook_records_a_config_error_marker(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, {"thresholds": {"foreground_seconds": "soon"}})
    hook_common.run_hook(lambda event, root, cfg: None)
    marker = tmp_path / "state" / "config_error.json"
    assert marker.exists(), "a hook's stderr is invisible; leave a doctor marker"
    assert "foreground_seconds" in marker.read_text(encoding="utf-8")
