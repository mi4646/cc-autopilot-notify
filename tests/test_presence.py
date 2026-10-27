from cc_autopilot_notify import presence


def test_parses_foreground_match(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "pebrel\n")
    assert presence.is_foreground() is True


def test_parses_foreground_mismatch(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "chrome\n")
    assert presence.is_foreground() is False


def test_empty_output_is_undeterminable(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "")
    assert presence.is_foreground() is None


def test_powershell_failure_is_undeterminable(monkeypatch):
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(presence, "_run_ps", boom)
    assert presence.is_foreground() is None


def test_timeout_is_undeterminable(monkeypatch):
    import subprocess

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=2)
    monkeypatch.setattr(presence, "_run_ps", slow)
    assert presence.is_foreground() is None
