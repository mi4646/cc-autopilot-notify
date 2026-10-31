import json
import time

from cc_autopilot_notify import daemon_control, state


def test_spawns_when_no_heartbeat(tmp_path):
    d = daemon_control.should_spawn(tmp_path, "/p", now=100.0)
    assert d.action == "spawn"


def test_skips_fresh_heartbeat_same_root(tmp_path):
    state.write_heartbeat(tmp_path, when=100.0, pid=7, plugin_root="/p")
    assert daemon_control.should_spawn(tmp_path, "/p", now=105.0).action == "skip"


def test_replaces_fresh_heartbeat_from_another_root(tmp_path):
    state.write_heartbeat(tmp_path, when=100.0, pid=7, plugin_root="/old")
    d = daemon_control.should_spawn(tmp_path, "/new", now=105.0)
    assert (d.action, d.stale_pid) == ("replace", 7)


def test_skips_legacy_heartbeat_without_root(tmp_path):
    """A live daemon with no pid cannot be stopped; spawning would double it."""
    (tmp_path / "heartbeat.json").write_text('{"at": 100.0}')
    d = daemon_control.should_spawn(tmp_path, "/p", now=105.0)
    assert d.action == "skip"
    assert "no pid" in d.reason


def test_skips_other_root_when_pid_is_missing(tmp_path):
    state.write_heartbeat(tmp_path, when=100.0, pid=None, plugin_root="/old")
    d = daemon_control.should_spawn(tmp_path, "/new", now=105.0)
    assert (d.action, d.stale_pid) == ("skip", None)


def test_should_spawn_rejects_non_positive_and_bool_pids(tmp_path):
    for bad in (True, False, 0, -7, "7", 3.0):
        state.write_heartbeat(tmp_path, when=100.0, pid=bad, plugin_root="/old")
        d = daemon_control.should_spawn(tmp_path, "/new", now=105.0)
        assert (d.action, d.stale_pid) == ("skip", None), bad


def test_valid_pid_predicate():
    assert daemon_control.valid_pid(7) == 7
    for bad in (True, False, 0, -7, "7", 3.0, None):
        assert daemon_control.valid_pid(bad) is None, bad


def test_spawns_when_heartbeat_stale(tmp_path):
    state.write_heartbeat(tmp_path, when=100.0, pid=7, plugin_root="/p")
    now = 100.0 + daemon_control.POLL_STALE_SECONDS + 1
    assert daemon_control.should_spawn(tmp_path, "/p", now=now).action == "spawn"


def test_lock_is_exclusive(tmp_path):
    first = daemon_control.acquire_lock(tmp_path)
    assert first is not None
    try:
        assert daemon_control.acquire_lock(tmp_path) is None
    finally:
        first.close()


def test_start_launches_detached_process(tmp_path, monkeypatch):
    seen = {}

    class FakeProc:
        pid = 4321

    def fake_popen(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return FakeProc()

    monkeypatch.setattr(daemon_control.subprocess, "Popen", fake_popen)
    assert daemon_control.start(tmp_path, "/plugin") == 4321
    assert seen["argv"][1:] == ["-m", "cc_autopilot_notify.cli", "daemon"]
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["env"]["PYTHONPATH"] == "/plugin"


def test_start_creates_missing_state_root(tmp_path, monkeypatch):
    """A fresh machine has no state root; the log open must not fail."""
    root = tmp_path / "deep" / "state"

    class FakeProc:
        pid = 4321

    monkeypatch.setattr(daemon_control.subprocess, "Popen",
                        lambda argv, **kwargs: FakeProc())
    assert daemon_control.start(root, "/plugin") == 4321
    assert (root / "daemon.log").exists()


def test_ensure_daemon_swallows_unexpected_errors(tmp_path, monkeypatch, capsys):
    from cc_autopilot_notify import cli

    cfg = tmp_path / "config.json"
    cfg.write_text('{"daemon": {"enabled": true}}')
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(cfg))
    monkeypatch.setenv("CC_AUTOPILOT_STATE", str(tmp_path / "state"))

    def boom(root, plugin_root):
        raise OSError("state root unwritable")

    monkeypatch.setattr(daemon_control, "should_spawn", boom)
    assert cli._ensure_daemon() == 0
    captured = capsys.readouterr()
    assert captured.out == "", "SessionStart must not inject this into context"
    assert "unexpected error" in captured.err


def test_ensure_daemon_writes_status_to_stderr(tmp_path, monkeypatch, capsys):
    from cc_autopilot_notify import cli

    cfg = tmp_path / "config.json"
    cfg.write_text('{"daemon": {"enabled": false}}')
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(cfg))
    monkeypatch.setenv("CC_AUTOPILOT_STATE", str(tmp_path / "state"))
    assert cli._ensure_daemon() == 0
    captured = capsys.readouterr()
    assert captured.out == "", "SessionStart must not inject this into context"
    assert "disabled by config" in captured.err


def test_stop_signals_heartbeat_pid(tmp_path, monkeypatch):
    import errno

    state.write_heartbeat(tmp_path, when=time.time(), pid=99, plugin_root="/p")
    killed = []
    monkeypatch.setattr(daemon_control, "owns_pid", lambda pid: True)

    def no_pidfd(pid):
        raise OSError(errno.ENOSYS, "pidfd unsupported")

    monkeypatch.setattr(daemon_control.os, "pidfd_open", no_pidfd, raising=False)
    monkeypatch.setattr(daemon_control.os, "kill", lambda pid, sig: killed.append(pid))
    assert daemon_control.stop(tmp_path) is True
    assert killed == [99]


def test_stop_uses_pidfd_when_available(tmp_path, monkeypatch):
    state.write_heartbeat(tmp_path, when=time.time(), pid=99, plugin_root="/p")
    calls = []
    monkeypatch.setattr(daemon_control, "owns_pid", lambda pid: True)
    monkeypatch.setattr(daemon_control.os, "pidfd_open", lambda pid: 7, raising=False)
    monkeypatch.setattr(
        daemon_control.signal, "pidfd_send_signal",
        lambda fd, sig: calls.append(("signal", fd, sig)), raising=False)
    monkeypatch.setattr(daemon_control.os, "close",
                        lambda fd: calls.append(("close", fd)))
    assert daemon_control.stop(tmp_path) is True
    assert calls == [("signal", 7, daemon_control.signal.SIGTERM), ("close", 7)]


def test_stop_refuses_bool_zero_and_negative_pids(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(daemon_control, "owns_pid", lambda pid: True)
    monkeypatch.setattr(daemon_control.os, "kill", lambda pid, sig: killed.append(pid))
    for bad in (True, False, 0, -7):
        state.write_heartbeat(tmp_path, when=time.time(), pid=bad, plugin_root="/p")
        assert daemon_control.stop(tmp_path) is False, bad
    assert killed == []


def test_stop_fails_closed_when_process_identity_is_unconfirmed(tmp_path, monkeypatch):
    state.write_heartbeat(tmp_path, when=time.time(), pid=99, plugin_root="/p")
    killed = []
    monkeypatch.setattr(daemon_control, "owns_pid", lambda pid: False)
    monkeypatch.setattr(daemon_control.os, "kill", lambda pid, sig: killed.append(pid))
    assert daemon_control.stop(tmp_path) is False
    assert killed == []


def test_owns_pid_rejects_unreadable_or_foreign_processes():
    # A pid that cannot be read from /proc is never assumed to be ours.
    assert daemon_control.owns_pid(999_999_999) is False
    assert daemon_control.owns_pid(0) is False
    assert daemon_control.owns_pid(True) is False


def test_wait_gone_ignores_invalid_pids(monkeypatch):
    called = []
    monkeypatch.setattr(daemon_control.os, "kill", lambda pid, sig: called.append(pid))
    assert daemon_control.wait_gone(0) is True
    assert daemon_control.wait_gone(-1) is True
    assert daemon_control.wait_gone(True) is True
    assert called == []


def test_stop_returns_false_without_pid(tmp_path):
    assert daemon_control.stop(tmp_path) is False


def test_package_present(tmp_path):
    (tmp_path / "cc_autopilot_notify").mkdir()
    assert daemon_control.package_present(str(tmp_path)) is True
    assert daemon_control.package_present(str(tmp_path / "gone")) is False
