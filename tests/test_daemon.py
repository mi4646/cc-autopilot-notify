from cc_autopilot_notify import daemon, state
from cc_autopilot_notify.presence import Presence


def _deps(revision="r1", foreground=True, idle_seconds=1000.0, choose_result=None,
          judge_index=1, choose_calls=None, prompt_id="p" * 64, epoch=1,
          options=None, locate=(1, 2), multi_indices=(0,), sleep=None,
          sample_seq=None, sample_calls=None):
    calls = choose_calls if choose_calls is not None else []
    seen = sample_calls if sample_calls is not None else []
    seq = list(sample_seq) if sample_seq is not None else None
    opts = options if options is not None else [{"label": "A"}, {"label": "B"}]

    def do_sample():
        seen.append(1)
        if seq is not None:
            return seq.pop(0)
        return Presence(foreground=foreground, idle_seconds=idle_seconds)

    return daemon.Deps(
        read=lambda p: {"revision": revision,
                        "prompt": {"prompt_id": prompt_id, "options": opts},
                        "identity": {"epoch": epoch}},
        sample=do_sample,
        judge_fn=lambda q: judge_index,
        notify=lambda msg: None,
        locate=lambda sid: locate,
        press=lambda p, key, repeat: {"ok": True},
        judge_multi_fn=lambda q: multi_indices,
        sleep=sleep or (lambda seconds: None),
    )


def _attention_deps(keys, **kwargs):
    deps = _deps(**kwargs)
    deps.read = lambda p: {"state": "attention",
                           "text": "  ❯ 1. A\n  2. B\n", "revision": "r1"}
    deps.press = lambda p, key, repeat: keys.append((p.session_id, key, repeat)) or {"ok": True}
    return deps


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
    keys = []
    deps = _deps(foreground=True)
    deps.read = lambda p: {"state": "attention",
                           "text": "  ❯ 1. A\n  2. B\n", "revision": "r1"}
    deps.press = lambda p, key, repeat: keys.append((key, repeat)) or {"ok": True}
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert keys == [("down", 1), ("enter", 1)], keys


def test_background_threshold_is_used_when_not_foreground(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    keys = []
    deps = _deps(foreground=False)
    deps.read = lambda p: {"state": "attention",
                           "text": "  ❯ 1. A\n  2. B\n", "revision": "r1"}
    deps.press = lambda p, key, repeat: keys.append((key, repeat)) or {"ok": True}
    daemon.tick(1000.0 + 90, tmp_path, _cfg(), deps)
    assert keys, "background threshold must already have fired"


def test_undeterminable_foreground_uses_background_threshold(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    keys = []
    deps = _deps(foreground=None)
    deps.read = lambda p: {"state": "attention",
                           "text": "  ❯ 1. A\n  2. B\n", "revision": "r1"}
    deps.press = lambda p, key, repeat: keys.append((key, repeat)) or {"ok": True}
    daemon.tick(1000.0 + 90, tmp_path, _cfg(), deps)
    assert keys, "unknown presence must fall to the background threshold"


def test_panel_redraw_alone_does_not_block(tmp_path):
    """The footer/status line can redraw every second; that is not user input.

    System idle is the authoritative signal. A changing revision alone must
    not stop an answer while the user is away.
    """
    _seed(tmp_path, appeared=1000.0)
    state.touch("s1", tmp_path, when=1000.0, revision="r1")
    keys = []
    deps = _attention_deps(keys, idle_seconds=1000.0)
    deps.read = lambda p: {"state": "attention",
                           "text": "  ❯ 1. A\n  2. B\n", "revision": "r2"}
    daemon.tick(1000.0 + 200, tmp_path, _cfg(), deps)
    assert keys == [("s1", "down", 1), ("s1", "enter", 1)]





def test_tick_writes_heartbeat(tmp_path):
    daemon.tick(1234.0, tmp_path, _cfg(), _deps())
    assert state.heartbeat_age(tmp_path, now=1234.0) == 0.0


def test_tick_writes_identity_into_heartbeat(tmp_path):
    daemon.tick(1234.5, tmp_path, _cfg(), _deps(),
                pid=55, plugin_root="/p", version="0.2.0")
    hb = state.read_heartbeat(tmp_path)
    assert (hb["pid"], hb["plugin_root"], hb["version"]) == (55, "/p", "0.2.0")







def test_unlocatable_session_is_dropped_after_grace(tmp_path, caplog):
    """The every-second "not running in any Pebrel pane yet" line must stop.

    A session that never appears in a pane (a /tmp session, a dead terminal)
    cannot be answered. Once the grace period passes, the record is dropped
    once with one final line, not retried forever.
    """
    import logging

    # window/pane 0 is what a hook writes before locate has run: the branch
    # under test only fires for such records.
    p = state.Pending(session_id="s1", window_id=0, pane_id=0,
                      questions=[{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}],
                      appeared_at=1000.0, last_activity=1000.0)
    state.record(p, tmp_path)
    deps = _deps()
    deps.locate = lambda sid: None
    for i in range(3):
        daemon.tick(1000.0 + i, tmp_path, _cfg(), deps)
        assert state.load_all(tmp_path), "dropping before the grace period is a bug"
    with caplog.at_level(logging.INFO):
        daemon.tick(1000.0 + daemon.LOCATE_GRACE_SECONDS + 1, tmp_path, _cfg(), deps)
    assert state.load_all(tmp_path) == [], "record must be dropped after the grace"
    assert caplog.text.count("never appeared") == 1, "exactly one final line"
    daemon.tick(1000.0 + daemon.LOCATE_GRACE_SECONDS + 2, tmp_path, _cfg(), deps)
    assert caplog.text.count("never appeared") == 1, "no repeat after drop"


def test_locate_failure_is_logged_at_most_once_per_window(tmp_path):
    """A transient runtime outage may persist for minutes; one line is enough."""
    import logging

    deps = _deps()
    calls = {"n": 0}
    def boom(sid):
        calls["n"] += 1
        raise RuntimeError("runtime unreachable")
    deps.locate = boom
    p = state.Pending(session_id="s1", window_id=0, pane_id=0,
                      questions=[{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}],
                      appeared_at=1000.0, last_activity=1000.0)
    state.record(p, tmp_path)
    lines = []
    class _H(logging.Handler):
        def emit(self, r):
            lines.append(r.getMessage())
    h = _H()
    logging.getLogger("cc_autopilot_notify.daemon").addHandler(h)
    try:
        for i in range(600):
            daemon.tick(1000.0 + i, tmp_path, _cfg(), deps)
        assert calls["n"] == 600, "locate must keep being retried"
        warnings = [ln for ln in lines if "cannot locate" in ln]
        assert len(warnings) == 1, f"logged {len(warnings)} times; expected 1"
    finally:
        logging.getLogger("cc_autopilot_notify.daemon").removeHandler(h)


def _keys_sent(tmp_path, answer_state=("attention", "attention", "done"),
               options_line='  ❯ 1. A\n  2. B\n'):
    """Deps built around the A' mechanism: attention gate + send-key."""
    import itertools
    states = itertools.islice(itertools.cycle(answer_state), 3)
    keys = []

    def do_read(pending):
        st = next(states)
        return {"state": st,
                "text": options_line if st == "attention" else "",
                "revision": "r1"}

    def do_press(pending, key, repeat=1):
        keys.append((key, repeat))
        return {"ok": True}

    deps = _deps(foreground=True)
    deps.read = do_read
    deps.press = do_press
    deps._keys = keys
    return deps


def test_prime_answers_via_send_key(tmp_path):
    """A' mechanism: attention gate then arrow keys, never conversation.choose."""
    state.record(state.Pending(session_id="s1", window_id=1, pane_id=2,
                               questions=[{"question": "Q",
                                           "options": [{"label": "A"},
                                                       {"label": "B (Recommended)"}]}],
                               appeared_at=1000.0, last_activity=1000.0), tmp_path)
    deps = _keys_sent(tmp_path)
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    # Options are "1. A" / "2. B": picking index 1 means one `down` then `enter`.
    assert deps._keys == [("down", 1), ("enter", 1)], deps._keys


def test_prime_only_fires_when_dialog_is_waiting(tmp_path):
    """Not in attention: wait, never press keys at a running prompt."""
    state.record(state.Pending(session_id="s1", window_id=1, pane_id=2,
                               questions=[{"question": "Q",
                                           "options": [{"label": "A"}, {"label": "B"}]}],
                               appeared_at=1000.0, last_activity=1000.0), tmp_path)
    deps = _keys_sent(tmp_path, answer_state=("running",))
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert deps._keys == [], "pressed keys into a running (non-dialog) turn"


def test_prime_refuses_foreign_screen_text(tmp_path):
    """The screen must agree with the recorded options, or nothing is pressed."""
    state.record(state.Pending(session_id="s1", window_id=1, pane_id=2,
                               questions=[{"question": "Q",
                                           "options": [{"label": "A"},
                                                       {"label": "B (Recommended)"}]}],
                               appeared_at=1000.0, last_activity=1000.0), tmp_path)
    deps = _keys_sent(tmp_path, options_line="  ❯ 1. yes\n  2. no\n")
    daemon.tick(1000.0 + 190, tmp_path, _cfg(), deps)
    assert deps._keys == [], \
        "a screen that does not show the recorded labels must never be pressed"


def _record_questions(tmp_path, questions, appeared=1000.0):
    state.record(state.Pending(session_id="s1", window_id=1, pane_id=2,
                               questions=questions, appeared_at=appeared,
                               last_activity=appeared), tmp_path)


def _screen_deps(screens, keys, judge_index=0, multi_indices=(0,)):
    screens = list(screens)
    deps = _deps(foreground=True, judge_index=judge_index,
                 multi_indices=multi_indices)
    deps.read = lambda p: screens.pop(0) if len(screens) > 1 else screens[0]
    deps.press = lambda p, key, repeat: keys.append((key, repeat)) or {"ok": True}
    return deps


def test_answers_every_question_then_confirms_review(tmp_path):
    _record_questions(tmp_path, [
        {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]},
        {"question": "Q2", "options": [{"label": "C"}, {"label": "D"}]},
    ])
    keys = []
    deps = _screen_deps([
        {"state": "attention", "text": "❯ 1. A\n  2. B\n", "revision": "r1"},
        {"state": "attention", "text": "❯ 1. C\n  2. D\n", "revision": "r2"},
        {"state": "attention", "text": "Review your answers\nSubmit answers\n",
         "revision": "r3"},
        {"state": "done", "text": "", "revision": "r4"},
    ], keys)
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert keys == [("enter", 1), ("enter", 1), ("enter", 1)], keys


def test_multiselect_toggles_each_chosen_option(tmp_path):
    _record_questions(tmp_path, [
        {"question": "Pick", "multiSelect": True,
         "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}]},
    ])
    keys = []
    deps = _screen_deps([
        {"state": "attention",
         "text": ("❯ 1. [ ] A\n  2. [ ] B\n  3. [ ] C\n"
                  "  4. [ ] Type something\n"), "revision": "r1"},
        {"state": "done", "text": "", "revision": "r2"},
    ], keys, multi_indices=(0, 2))
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert keys == [("space", 1), ("down", 2), ("space", 1),
                    ("down", 2), ("enter", 1)], keys


def test_unanswered_question_keeps_the_record_when_screen_goes_foreign(tmp_path):
    _record_questions(tmp_path, [
        {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]},
        {"question": "Q2", "options": [{"label": "C"}, {"label": "D"}]},
    ])
    keys = []
    deps = _screen_deps([
        {"state": "attention", "text": "❯ 1. A\n  2. B\n", "revision": "r1"},
        {"state": "attention", "text": "❯ 1. X\n  2. Y\n", "revision": "r2"},
        {"state": "attention", "text": "❯ 1. X\n  2. Y\n", "revision": "r2"},
    ], keys)
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert keys == [("enter", 1)], keys
    got = state.load_all(tmp_path)
    assert got, "partial success must keep the record for the remaining question"
    assert got[0].choose_failures == 1


def test_stalled_panel_advances_with_tab(tmp_path):
    """A single-select question that does not auto-advance is left with Tab."""
    _record_questions(tmp_path, [
        {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]},
        {"question": "Q2", "options": [{"label": "C"}, {"label": "D"}]},
    ])
    keys = []
    deps = _screen_deps([
        {"state": "attention", "text": "❯ 1. A\n  2. B\n", "revision": "r1"},
        {"state": "attention", "text": "❯ 1. A\n  2. B\n", "revision": "r1"},
        {"state": "attention", "text": "❯ 1. C\n  2. D\n", "revision": "r2"},
        {"state": "attention", "text": "Review your answers\nSubmit answers\n",
         "revision": "r3"},
        {"state": "done", "text": "", "revision": "r4"},
    ], keys)
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert keys == [("enter", 1), ("tab", 1), ("enter", 1), ("enter", 1)], keys


# --- system-idle thresholds -------------------------------------------------

def test_foreground_idle_below_threshold_does_not_fire(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    keys = []
    daemon.tick(1000.0 + 150, tmp_path, _cfg(),
                _attention_deps(keys, foreground=True, idle_seconds=149.0))
    assert keys == []


def test_foreground_idle_at_threshold_fires(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    keys = []
    daemon.tick(1000.0 + 150, tmp_path, _cfg(),
                _attention_deps(keys, foreground=True, idle_seconds=150.0))
    assert keys == [("s1", "down", 1), ("s1", "enter", 1)]


def test_background_idle_below_threshold_does_not_fire(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    keys = []
    daemon.tick(1000.0 + 90, tmp_path, _cfg(),
                _attention_deps(keys, foreground=False, idle_seconds=89.0))
    assert keys == []


def test_background_idle_at_threshold_fires(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    keys = []
    daemon.tick(1000.0 + 90, tmp_path, _cfg(),
                _attention_deps(keys, foreground=False, idle_seconds=90.0))
    assert keys == [("s1", "down", 1), ("s1", "enter", 1)]


def test_answer_logs_idle_foreground_and_threshold(tmp_path, caplog):
    """The answer log must say which threshold was actually used."""
    import logging

    _seed(tmp_path, appeared=1000.0)
    keys = []
    deps = _attention_deps(keys, foreground=True, idle_seconds=150.0)
    with caplog.at_level(logging.INFO, logger="cc_autopilot_notify.daemon"):
        daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert keys
    assert "idle=150.0s" in caplog.text
    assert "foreground=True" in caplog.text
    assert "threshold=150s" in caplog.text


def test_unknown_foreground_uses_background_threshold(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    keys = []
    daemon.tick(1000.0 + 90, tmp_path, _cfg(),
                _attention_deps(keys, foreground=None, idle_seconds=90.0))
    assert keys == [("s1", "down", 1), ("s1", "enter", 1)]


def test_unknown_idle_never_fires(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    keys = []
    daemon.tick(1000.0 + 500, tmp_path, _cfg(),
                _attention_deps(keys, idle_seconds=None))
    assert keys == []


def test_min_duration_blocks_a_fresh_question(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    keys = []
    deps = _attention_deps(keys, idle_seconds=1000.0)
    daemon.tick(1000.0 + 10, tmp_path, _cfg(), deps)
    assert keys == []
    daemon.tick(1000.0 + 30, tmp_path, _cfg(), deps)
    assert keys == [("s1", "down", 1), ("s1", "enter", 1)]


def test_no_pending_means_no_presence_call(tmp_path):
    calls = []
    daemon.tick(1234.0, tmp_path, _cfg(), _deps(sample_calls=calls))
    assert calls == []


def _seed_as(tmp_path, sid, appeared=1000.0):
    state.record(state.Pending(
        session_id=sid, window_id=1, pane_id=2,
        questions=[{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}],
        appeared_at=appeared, last_activity=appeared), tmp_path)


def test_batch_answers_every_pending_in_one_tick(tmp_path):
    for sid in ("s1", "s2", "s3"):
        _seed_as(tmp_path, sid)
    keys = []
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), _attention_deps(keys))
    assert {sid for sid, _key, _n in keys} == {"s1", "s2", "s3"}
    assert len(keys) == 6


def test_own_press_resets_the_next_window(tmp_path):
    _seed_as(tmp_path, "s1")
    keys = []
    deps = _attention_deps(keys, sample_seq=[
        Presence(True, 1000.0), Presence(True, 1.0)])
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps)
    assert len(keys) == 2, "first pending must be answered"
    _seed_as(tmp_path, "s2")
    daemon.tick(1000.0 + 152, tmp_path, _cfg(), deps)
    assert len(keys) == 2, "idle was reset by our own press; nothing else may fire"


def test_shared_gate_resamples_after_an_answer(tmp_path):
    """A cached pre-press sample must not answer the same pending again."""
    _seed_as(tmp_path, "s1")
    keys = []
    calls = []
    deps = _attention_deps(keys, sample_calls=calls, sample_seq=[
        Presence(True, 1000.0), Presence(True, 1.0)])
    gate = daemon.PresenceGate()
    daemon.tick(1000.0 + 150, tmp_path, _cfg(), deps, gate=gate)
    assert len(keys) == 2
    daemon.tick(1000.0 + 151, tmp_path, _cfg(), deps, gate=gate)
    assert calls == [1, 1], "a press must invalidate the cached sample"
    assert len(keys) == 2, "the stale high-idle sample must not fire again"


# --- PresenceGate -----------------------------------------------------------

class _GateDeps:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.notes = []

    def sample(self):
        self.calls += 1
        return self.results.pop(0) if self.results else None

    def notify(self, msg):
        self.notes.append(msg)


def test_gate_caches_a_successful_sample_for_five_seconds():
    gate = daemon.PresenceGate()
    presence = Presence(True, 1000.0)
    deps = _GateDeps([presence, presence])
    assert gate.get(1000.0, deps) is presence
    assert gate.get(1002.0, deps) is presence
    assert deps.calls == 1
    assert gate.get(1006.0, deps) is presence
    assert deps.calls == 2


def test_gate_backs_off_five_seconds_after_failure():
    gate = daemon.PresenceGate()
    deps = _GateDeps([None, Presence(True, 1.0)])
    assert gate.get(1000.0, deps) is None
    assert gate.get(1002.0, deps) is None
    assert deps.calls == 1, "backoff must suppress the second probe"
    assert gate.get(1006.0, deps) == Presence(True, 1.0)
    assert deps.calls == 2


def test_gate_warns_once_after_sixty_seconds_of_failure():
    gate = daemon.PresenceGate()
    deps = _GateDeps([None] * 20)
    for now in range(1000, 1061, 6):
        assert gate.get(float(now), deps) is None
    assert deps.notes == [daemon.OUTAGE_MESSAGE]


def test_gate_sends_recovery_only_after_a_warned_outage():
    gate = daemon.PresenceGate()
    deps = _GateDeps([None, Presence(True, 1.0)])
    gate.get(1000.0, deps)
    gate.get(1006.0, deps)
    assert deps.notes == [], "a short blip must not produce two notifications"

    gate = daemon.PresenceGate()
    deps = _GateDeps([None] * 11 + [Presence(True, 1.0)])
    for now in range(1000, 1061, 6):
        gate.get(float(now), deps)
    gate.get(1066.0, deps)
    assert deps.notes == [daemon.OUTAGE_MESSAGE, daemon.RECOVERY_MESSAGE]
