import json

from cc_autopilot_notify import hook_permission


class _RecordingStdin:
    def __init__(self):
        self.read_calls = 0

    def read(self, *args, **kwargs):
        self.read_calls += 1
        return ""


def test_main_returns_without_reading_stdin_when_disabled(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"thresholds": {"foreground_seconds": 150,
                                              "background_seconds": 90}}))
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(cfg))
    fake = _RecordingStdin()
    monkeypatch.setattr("sys.stdin", fake)
    assert hook_permission.main() == 0
    assert fake.read_calls == 0


def test_main_reads_stdin_when_enabled(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"path_b": {"enabled": True}}))
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(cfg))
    fake = _RecordingStdin()
    monkeypatch.setattr("sys.stdin", fake)
    assert hook_permission.main() == 0
    assert fake.read_calls == 1
