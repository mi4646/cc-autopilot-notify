import json

from cc_autopilot_notify import hook_permission


class _ExplodingStdin:
    def read(self, *args, **kwargs):
        raise AssertionError("stdin must not be read when path B is disabled")


def test_main_returns_without_reading_stdin_when_disabled(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"thresholds": {"foreground_seconds": 150,
                                              "background_seconds": 90}}))
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(cfg))
    monkeypatch.setattr("sys.stdin", _ExplodingStdin())
    assert hook_permission.main() == 0


def test_main_reads_stdin_when_enabled(tmp_path, monkeypatch, capsys):
    import io
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"path_b": {"enabled": True}}))
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(cfg))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert hook_permission.main() == 0
