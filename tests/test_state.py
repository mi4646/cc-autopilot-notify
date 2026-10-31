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


def test_set_pane_preserves_fields_written_concurrently(tmp_path):
    state.record(_pending(), tmp_path)
    state.bump_choose_failures("s1", tmp_path)
    updated = state.set_pane("s1", tmp_path, window_id=9, pane_id=10)
    assert updated is not None
    got = state.load_all(tmp_path)[0]
    assert (got.window_id, got.pane_id) == (9, 10)
    assert got.choose_failures == 1
    assert got.questions == _pending().questions


def test_set_pane_does_not_resurrect_a_forgotten_record(tmp_path):
    assert state.set_pane("missing", tmp_path, window_id=1, pane_id=2) is None
    assert state.load_all(tmp_path) == []


def test_corrupt_record_is_dropped(tmp_path):
    bad = tmp_path / "pending_bad.json"
    bad.write_text("{not json")
    assert state.load_all(tmp_path) == []
    assert not bad.exists(), "an unparseable record would re-log every tick"


def test_purge_stale_keeps_a_record_alive_via_finished_at(tmp_path):
    p = _pending()
    p.last_activity = 1000.0
    p.finished_at = 5000.0
    state.record(p, tmp_path)
    state.purge_stale(tmp_path, now=1000.0 + 86401, max_age_seconds=86400)
    assert state.load_all(tmp_path), "a recent finished round keeps the record"


def test_clear_questions_keeps_continuation_counts(tmp_path):
    state.record(_pending(), tmp_path)
    state.bump_continue("s1", tmp_path, failed=False)
    state.clear_questions("s1", tmp_path)
    got = state.load_all(tmp_path)[0]
    assert got.questions == []
    assert got.continue_count == 1


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


def test_heartbeat_age_returns_none_for_overflow_and_non_finite(tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    for payload in (f'{{"at": {10 ** 400}}}', '{"at": Infinity}', '{"at": NaN}'):
        heartbeat.write_text(payload)
        assert state.heartbeat_age(tmp_path, now=1.0) is None, payload


def test_heartbeat_age_keeps_working_for_normal_values(tmp_path):
    (tmp_path / "heartbeat.json").write_text('{"at": 100.5}')
    assert state.heartbeat_age(tmp_path, now=101.0) == 0.5



def _bare(sid):
    return state.Pending(session_id=sid, window_id=1, pane_id=2, questions=[],
                         appeared_at=1.0, last_activity=1.0)


def test_old_record_loads_with_defaults(tmp_path):
    (tmp_path / "pending_s1.json").write_text(
        '{"session_id":"s1","window_id":1,"pane_id":2,"questions":[],'
        '"appeared_at":1.0,"last_activity":1.0}')
    p = state.load_all(tmp_path)[0]
    assert p.finished_at is None and p.continue_count == 0
    assert p.failure_count == 0 and p.finished_failed is False
    assert p.last_auto_submit is None and p.pressed_at is None
    assert p.stage_card_sent is False


def test_set_finished_keeps_counts_and_clears_markers(tmp_path):
    state.record(_bare("s1"), tmp_path)
    state.bump_continue("s1", tmp_path, failed=False)
    state.mark_stage_card("s1", tmp_path, when=50.0)
    state.mark_pressed("s1", tmp_path, when=51.0)
    state.set_finished("s1", tmp_path, when=100.0, pane_id=2, failed=False)
    p = state.load_all(tmp_path)[0]
    assert p.finished_at == 100.0 and p.continue_count == 1
    assert p.stage_card_sent is False and p.pressed_at is None
    assert p.finished_failed is False and p.pane_id == 2


def test_clear_finished_zeroes_counts(tmp_path):
    state.record(_bare("s1"), tmp_path)
    state.bump_continue("s1", tmp_path, failed=False)
    state.bump_continue("s1", tmp_path, failed=True)
    state.clear_finished("s1", tmp_path)
    p = state.load_all(tmp_path)[0]
    assert p.finished_at is None
    assert p.continue_count == 0 and p.failure_count == 0


def test_mark_auto_submit_and_bump_failed(tmp_path):
    state.record(_bare("s1"), tmp_path)
    state.mark_auto_submit("s1", tmp_path, when=7.0)
    n = state.bump_continue("s1", tmp_path, failed=True)
    p = state.load_all(tmp_path)[0]
    assert n == 1 and p.failure_count == 1 and p.continue_count == 0
    assert p.last_auto_submit == 7.0


def test_purge_stale_drops_old_records(tmp_path):
    state.record(_bare("old"), tmp_path)
    state.record(_bare("new"), tmp_path)
    state.touch("new", tmp_path, when=99900.0)
    state.purge_stale(tmp_path, now=100000.0)
    names = [p.session_id for p in state.load_all(tmp_path)]
    assert "new" in names and "old" not in names
