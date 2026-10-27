from cc_autopilot_notify import state


def _pending(sid="s1", pane=3):
    return state.Pending(
        session_id=sid, window_id=1, pane_id=pane,
        questions=[{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}],
        appeared_at=1000.0, last_activity=1000.0,
    )


def test_roundtrip(tmp_path):
    state.record(_pending(), tmp_path)
    got = state.load_all(tmp_path)
    assert len(got) == 1
    assert got[0].session_id == "s1"
    assert got[0].pane_id == 3
    assert got[0].last_activity == 1000.0


def test_two_sessions_are_independent(tmp_path):
    state.record(_pending("s1", 3), tmp_path)
    state.record(_pending("s2", 4), tmp_path)
    got = {p.session_id: p for p in state.load_all(tmp_path)}
    assert got["s1"].pane_id == 3
    assert got["s2"].pane_id == 4


def test_forget_removes_only_one(tmp_path):
    state.record(_pending("s1"), tmp_path)
    state.record(_pending("s2"), tmp_path)
    state.forget("s1", tmp_path)
    assert [p.session_id for p in state.load_all(tmp_path)] == ["s2"]


def test_touch_updates_last_activity(tmp_path):
    state.record(_pending(), tmp_path)
    state.touch("s1", tmp_path, when=2000.0, revision="r9")
    got = state.load_all(tmp_path)[0]
    assert got.last_activity == 2000.0
    assert got.revision == "r9"


def test_bump_counts(tmp_path):
    state.record(_pending(), tmp_path)
    assert state.bump_choose_failures("s1", tmp_path) == 1
    assert state.bump_choose_failures("s1", tmp_path) == 2


def test_corrupt_record_is_skipped_not_fatal(tmp_path):
    (tmp_path / "pending_s1.json").write_text("{broken")
    state.record(_pending("s2"), tmp_path)
    assert [p.session_id for p in state.load_all(tmp_path)] == ["s2"]


def test_heartbeat_roundtrip(tmp_path):
    state.write_heartbeat(tmp_path, when=500.0)
    assert state.heartbeat_age(tmp_path, now=560.0) == 60.0


def test_heartbeat_missing_is_none(tmp_path):
    assert state.heartbeat_age(tmp_path, now=1.0) is None


def test_session_id_is_sanitised_for_filenames(tmp_path):
    state.record(_pending("../../etc/passwd"), tmp_path)
    names = [p.name for p in tmp_path.glob("pending_*")]
    assert all("/" not in n for n in names)
    assert len(state.load_all(tmp_path)) == 1


def test_heartbeat_carries_process_identity(tmp_path):
    state.write_heartbeat(tmp_path, when=1000.0, pid=4242,
                          plugin_root="/plugin/root", version="0.2.0")
    hb = state.read_heartbeat(tmp_path)
    assert hb == {"at": 1000.0, "pid": 4242,
                  "plugin_root": "/plugin/root", "version": "0.2.0"}
    assert state.heartbeat_age(tmp_path, now=1010.0) == 10.0


def test_read_heartbeat_returns_none_when_missing(tmp_path):
    assert state.read_heartbeat(tmp_path) is None


def test_legacy_heartbeat_without_pid_still_reads(tmp_path):
    (tmp_path / "heartbeat.json").write_text('{"at": 5.0}')
    hb = state.read_heartbeat(tmp_path)
    assert hb["at"] == 5.0 and hb["pid"] is None


def test_read_heartbeat_returns_none_for_malformed_payloads(tmp_path):
    (tmp_path / "heartbeat.json").write_text("{broken")
    assert state.read_heartbeat(tmp_path) is None

    (tmp_path / "heartbeat.json").write_text("[]")
    assert state.read_heartbeat(tmp_path) is None

    (tmp_path / "heartbeat.json").write_text('{"at": ' + str(10**400) + "}")
    assert state.read_heartbeat(tmp_path) is None
