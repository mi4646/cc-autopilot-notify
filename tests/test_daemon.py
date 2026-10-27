from cc_autopilot_notify import daemon, state


def _deps(revision="r1", foreground=True, choose_result=None, judge_index=1,
          choose_calls=None, prompt_id="p" * 64, epoch=1, options=None,
          locate=(1, 2)):
    calls = choose_calls if choose_calls is not None else []
    opts = options if options is not None else [{"label": "A"}, {"label": "B"}]
    return daemon.Deps(
        read=lambda p: {"revision": revision,
                        "prompt": {"prompt_id": prompt_id, "options": opts},
                        "identity": {"epoch": epoch}},
        choose=lambda p, ep, pid, option: calls.append(
            {"epoch": ep, "prompt_id": pid, "option": option}) or (
            choose_result if choose_result is not None else {"ok": True}),
        foreground=lambda: foreground,
        judge_fn=lambda q: judge_index,
        notify=lambda msg: None,
        locate=lambda sid: locate,
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
    assert calls, "expected a choose call at the threshold"
    assert calls[0]["option"] == 1


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


def test_choose_sends_the_fields_the_live_server_requires(tmp_path):
    """The live runtime rejects a choose without epoch or prompt_id.

    This asserts the wire shape, not that the lambda was called: a test that
    only counts calls stays green when the client sends nothing usable.
    """
    _seed(tmp_path, appeared=1000.0)
    calls = []
    daemon.tick(1000.0 + 150, tmp_path, _cfg(),
                _deps(foreground=True, choose_calls=calls, epoch=7, prompt_id="a" * 64))
    assert calls[0]["epoch"] == 7
    assert calls[0]["prompt_id"] == "a" * 64


def test_missing_prompt_id_gives_up_instead_of_guessing(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    calls = []
    deps = _deps(foreground=True, choose_calls=calls)
    deps.read = lambda p: {"revision": "r1",
                           "prompt": {"options": [{"label": "A"}, {"label": "B"}]},
                           "identity": {"epoch": 1}}
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert calls == [], "must not choose without a prompt_id"
    assert state.load_all(tmp_path) == [], "must drop rather than retry forever"


def test_unparseable_prompt_notifies_once_not_every_tick(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    seen = []
    deps = _deps(foreground=True)
    deps.notify = lambda m: seen.append(m)
    deps.read = lambda p: {"revision": "r1", "prompt": {"mystery": 1},
                           "identity": {"epoch": 1}}
    for i in range(5):
        daemon.tick(1000.0 + 150 + i, tmp_path, _cfg(), deps)
    assert len(seen) == 1, f"notified {len(seen)} times; expected once"
    assert state.load_all(tmp_path) == []


def test_judge_raising_does_not_stop_other_sessions(tmp_path):
    from cc_autopilot_notify import state as st

    for sid in ("a", "b"):
        st.record(st.Pending(session_id=sid, window_id=1, pane_id=2,
                             questions=[{"question": "Q"}],
                             appeared_at=1000.0, last_activity=1000.0), tmp_path)
    answered = []

    def judge(question):
        raise ValueError("boom")

    deps = _deps(foreground=True)
    deps.judge_fn = judge
    deps.notify = lambda m: answered.append(m)
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert state.load_all(tmp_path) == [], "both bad records must be dropped"
    assert len(answered) == 2


def test_unlocatable_prompt_id_logs_the_real_shape(tmp_path, caplog):
    """The prompt shape has never been observed non-null.

    When the id cannot be found, the log must carry what the server actually
    sent, or the next debugging round starts from zero again.
    """
    import logging

    _seed(tmp_path, appeared=1000.0)
    deps = _deps(foreground=True)
    deps.read = lambda p: {"revision": "r1",
                           "prompt": {"mystery_key": 1, "options": [{"label": "A"}]},
                           "identity": {"epoch": 1}}
    with caplog.at_level(logging.WARNING):
        daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "mystery_key" in joined, f"prompt keys not logged: {joined}"
