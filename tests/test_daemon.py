from cc_autopilot_notify import daemon, state


def _deps(revision="r1", foreground=True, choose_result=None, judge_index=1, choose_calls=None):
    calls = choose_calls if choose_calls is not None else []
    return daemon.Deps(
        read=lambda p: {"revision": revision, "prompt": {"options": [
            {"label": "A"}, {"label": "B"}]}, "identity": {"epoch": 1}},
        choose=lambda p, prompt_id, option: calls.append((option, prompt_id)) or (
            choose_result if choose_result is not None else {"ok": True}),
        foreground=lambda: foreground,
        judge_fn=lambda q: judge_index,
        notify=lambda msg: None,
    )


def _seed(tmp_path, appeared, last_activity=None):
    p = state.Pending(session_id="s1", window_id=1, pane_id=2,
                      questions=[{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}],
                      appeared_at=appeared, last_activity=last_activity or appeared)
    state.record(p, tmp_path)
    return p


def _cfg():
    return {"thresholds": {"foreground_seconds": 150, "background_seconds": 90}}


def test_does_not_fire_before_foreground_threshold(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    assert daemon.tick(1000.0 + 149, tmp_path, _cfg(), _deps(foreground=True)) == []


def test_fires_at_foreground_threshold(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    calls = []
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), _deps(foreground=True, choose_calls=calls))
    assert calls and calls[0][0] == 1


def test_background_threshold_is_used_when_not_foreground(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    calls = []
    daemon.tick(1000.0 + 90, tmp_path, _cfg(), _deps(foreground=False, choose_calls=calls))
    assert len(calls) == 1


def test_undeterminable_foreground_uses_background_threshold(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    calls = []
    daemon.tick(1000.0 + 90, tmp_path, _cfg(), _deps(foreground=None, choose_calls=calls))
    assert len(calls) == 1


def test_revision_change_resets_activity(tmp_path):
    """A revision change means the user is interacting: no fire, timer moves."""
    p = _seed(tmp_path, appeared=1000.0)
    state.touch("s1", tmp_path, when=1000.0, revision="r1")
    calls = []
    daemon.tick(1000.0 + 200, tmp_path, _cfg(),
                _deps(revision="r2", foreground=True, choose_calls=calls))
    got = state.load_all(tmp_path)[0]
    assert calls == [], "must not answer while the user is interacting"
    assert got.last_activity == 1200.0
    assert got.revision == "r2"


def test_prompt_null_means_answered_and_record_is_cleared(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    deps = _deps()
    deps.read = lambda p: {"revision": "r", "prompt": None, "identity": {"epoch": 1}}
    daemon.tick(1000.0 + 500, tmp_path, _cfg(), deps)
    assert state.load_all(tmp_path) == []


def test_prompt_changed_is_retried_once(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    calls = []
    deps = _deps(choose_result={"ok": False, "error": {"code": "prompt_changed"}},
                 choose_calls=calls)
    daemon.tick(1000.0 + 200, tmp_path, _cfg(), deps)
    assert len(calls) == 2


def test_two_choose_failures_drop_record(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    deps = _deps(choose_result={"ok": False, "error": {"code": "prompt_changed"}})
    daemon.tick(1000.0 + 200, tmp_path, _cfg(), deps)
    daemon.tick(1000.0 + 260, tmp_path, _cfg(), deps)
    assert state.load_all(tmp_path) == []


def test_tick_writes_heartbeat(tmp_path):
    daemon.tick(1234.0, tmp_path, _cfg(), _deps())
    assert state.heartbeat_age(tmp_path, now=1234.0) == 0.0
