from cc_autopilot_notify import presence


def test_default_timeout_covers_powershell_startup(monkeypatch):
    """Add-Type cold start measures ~2s on WSL; a 2s default timed out."""
    seen = []

    def capture(_script, timeout, _powershell=None):
        seen.append(timeout)
        return "pebrel\n"

    monkeypatch.setattr(presence, "_run_ps", capture)
    assert presence.is_foreground() is True
    assert seen == [5.0]


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


def test_undecodable_output_is_undeterminable(monkeypatch):
    """powershell.exe on a Chinese Windows writes GBK, which text=True rejects.

    UnicodeDecodeError is not an OSError, so the existing handler misses it.
    """
    def bad(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(presence, "_run_ps", bad)
    assert presence.is_foreground() is None


def test_sample_parses_foreground_and_idle(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "pebrel|123.5\n")
    got = presence.sample()
    assert got.foreground is True
    assert got.idle_seconds == 123.5


def test_sample_reports_other_foreground_with_idle(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "chrome|42.25\n")
    got = presence.sample()
    assert got.foreground is False
    assert got.idle_seconds == 42.25


def test_sample_empty_foreground_is_unknown(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "|10.0\n")
    got = presence.sample()
    assert got.foreground is None
    assert got.idle_seconds == 10.0


def test_sample_err_idle_keeps_foreground(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "pebrel|ERR\n")
    got = presence.sample()
    assert got.foreground is True
    assert got.idle_seconds is None


def test_sample_non_numeric_idle_is_none(monkeypatch):
    for raw in ("pebrel|oops\n", "pebrel|\n", "pebrel|-3\n",
                "pebrel|nan\n", "pebrel|inf\n"):
        monkeypatch.setattr(presence, "_run_ps", lambda *a, _r=raw, **k: _r)
        got = presence.sample()
        assert got is not None, raw
        assert got.idle_seconds is None, raw


def test_sample_idle_over_seven_days_is_none(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "pebrel|604801\n")
    assert presence.sample().idle_seconds is None


def test_sample_unexpected_shape_is_none(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "just-a-name\n")
    assert presence.sample() is None


def test_sample_powershell_failure_is_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(presence, "_run_ps", boom)
    assert presence.sample() is None


def test_sample_timeout_is_none(monkeypatch):
    import subprocess

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=1.5)
    monkeypatch.setattr(presence, "_run_ps", slow)
    assert presence.sample() is None


def test_sample_script_is_single_call_and_culture_safe(monkeypatch):
    seen = []

    def capture(script, timeout, _powershell=None):
        seen.append((script, timeout))
        return "pebrel|1.0\n"

    monkeypatch.setattr(presence, "_run_ps", capture)
    assert presence.sample() is not None
    assert len(seen) == 1, "foreground and idle must share one PowerShell call"
    assert "InvariantCulture" in seen[0][0]
    assert "GetForegroundWindow" in seen[0][0]
    assert "GetLastInputInfo" in seen[0][0]
