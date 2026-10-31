import re

from cc_autopilot_notify import presence, continuation as continue_


def _pane_tail(current_input: str) -> str:
    """A realistic pane tail: history, input box, status line."""
    sep = "─" * 60
    return (f"✻ Brewed for 1m · done 10:27 AM\n\n"
            f"{sep}\n❯ 清掉浏览器吧\n\n  Thought for 9s\n{sep}\n"
            f"❯ {current_input}\n{sep}\n"
            f"  [DeepSeek] │ demo │ task │ ⏱️  9h\n  ⏵⏵ bypass permissions on")


def test_extracts_suggestion_from_current_input_box():
    assert continue_.match_suggestion(_pane_tail("生成一下周报")) == "生成一下周报"


def test_empty_input_box_is_no_suggestion():
    assert continue_.match_suggestion(_pane_tail("")) is None


def test_no_separators_fails_closed():
    assert continue_.match_suggestion("普通输出，没有输入框") is None
    assert continue_.match_suggestion("") is None


def test_whitespace_only_input_is_no_suggestion():
    assert continue_.match_suggestion(_pane_tail("   ")) is None


def test_multiline_suggestion_takes_first_line():
    assert continue_.match_suggestion(_pane_tail("第一行\n第二行")) == "第一行"


import dataclasses

from cc_autopilot_notify import continuation, state


def _pending(sid="s1", **over):
    p = state.Pending(session_id=sid, window_id=1, pane_id=2, questions=[],
                      appeared_at=1.0, last_activity=1.0, finished_at=100.0)
    for k, v in over.items():
        setattr(p, k, v)
    return p


def _deps(suggestion=True, locked=True, state_name="finished", presses=None,
          owner_ok=True, questions=None):
    press_calls = presses if presses is not None else []
    notified = []

    def do_read(pending):
        text = _pane_tail("生成一下周报") if suggestion else _pane_tail("")
        return {"state": state_name, "text": text}

    def do_press(pending, key, repeat):
        press_calls.append((key, repeat))
        return {"ok": True}

    class D:
        read = staticmethod(do_read)
        press = staticmethod(do_press)
        fresh_presence = staticmethod(lambda: presence.Presence(
            foreground=False, idle_seconds=500.0, locked=locked))
        pane_owner_ok = staticmethod(lambda pending: owner_ok)
        notify = staticmethod(lambda n: notified.append(n))
        sleep = staticmethod(lambda s: None)
    return D(), press_calls, notified


_CFG = {"autopilot": {"continue_enabled": True, "shadow_mode": False,
                      "max_continues": 10, "suggest_wait_seconds": 90,
                      "failure_continues": 2}}


def _run(pending, deps, now=500.0, cfg=_CFG, tmp=None):
    import pathlib
    return continuation.handle(pending, now, pathlib.Path(tmp or "/tmp/x"),
                               cfg, deps)


def test_disabled_returns_without_anything(tmp_path):
    pending = _pending()
    deps, presses, notified = _deps()
    cfg = {"autopilot": {"continue_enabled": False, "shadow_mode": False,
                         "max_continues": 10, "suggest_wait_seconds": 90,
                         "failure_continues": 2}}
    out = _run(pending, deps, cfg=cfg, tmp=tmp_path)
    assert out == [] and presses == [] and notified == []


def test_unlocked_zero_presses(tmp_path):
    deps, presses, notified = _deps(locked=False)
    out = _run(_pending(), deps, tmp=tmp_path)
    assert presses == [] and notified == []


def test_unanswered_questions_take_priority(tmp_path):
    pending = _pending(questions=[{"question": "Q"}])
    deps, presses, notified = _deps()
    _run(pending, deps, tmp=tmp_path)
    assert presses == [] and notified == []


def test_running_state_is_skipped(tmp_path):
    deps, presses, notified = _deps(state_name="running")
    _run(_pending(), deps, tmp=tmp_path)
    assert presses == [] and notified == []


def test_locked_suggestion_presses_tab_then_enter_and_counts(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, notified = _deps()
    out = _run(_pending(), deps, tmp=tmp_path)
    assert presses == [("tab", 1), ("enter", 1)]
    p = state.load_all(tmp_path)[0]
    assert p.continue_count == 1 and p.last_auto_submit is not None
    assert p.pressed_at is not None
    card = notified[0]
    assert card.event == "auto_continue" and "自动续跑 1/10" in card.title
    assert "生成一下周报" in card.body


def test_press_failure_before_marker_keeps_no_count(tmp_path, monkeypatch):
    # marker 先落盘：mark_auto_submit 必须发生在 press 之前（codex Critical #1）
    state.record(_pending(), tmp_path)
    import cc_autopilot_notify.continuation as cont
    order = []
    monkeypatch.setattr(cont.state, "mark_auto_submit",
                        lambda *a, **k: order.append("mark"))
    deps, presses, notified = _deps()
    orig_press = deps.press
    deps.press = lambda pending, key, repeat: (order.append(f"press:{key}"),
                                               orig_press(pending, key, repeat))[1]
    _run(_pending(), deps, tmp=tmp_path)
    assert order == ["mark", "press:tab", "press:enter"]


def test_cap_stops_with_single_capped_card(tmp_path):
    state.record(_pending(continue_count=10), tmp_path)
    deps, presses, notified = _deps()
    out = _run(_pending(continue_count=10), deps, tmp=tmp_path)
    assert presses == []
    assert notified and notified[0].event == "continue_capped"
    out2 = _run(state.load_all(tmp_path)[0], deps, tmp=tmp_path)  # 下一个 tick 重载记录
    assert len(notified) == 1  # 不重发


def test_stage_card_once_after_deadline(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, notified = _deps(suggestion=False)
    out = _run(_pending(), deps, now=500.0, tmp=tmp_path)  # 100+90+30 < 500
    assert presses == []
    assert notified and notified[0].event == "continue_stage"
    assert "共自动续跑 0 条" in notified[0].body
    _run(state.load_all(tmp_path)[0], deps, now=501.0, tmp=tmp_path)
    assert len(notified) == 1  # 不重发


def test_shadow_mode_logs_without_pressing(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, notified = _deps()
    cfg = {"autopilot": {"continue_enabled": True, "shadow_mode": True,
                         "max_continues": 10, "suggest_wait_seconds": 90,
                         "failure_continues": 2}}
    out = _run(_pending(), deps, cfg=cfg, tmp=tmp_path)
    assert presses == [] and notified == []
    assert any("SHADOW" in line for line in out)


def test_press_refused_twice_gives_up(tmp_path):
    state.record(_pending(), tmp_path)

    def boom(pending, key, repeat):
        raise RuntimeError("refused")

    deps, presses, notified = _deps()
    deps.press = boom
    _run(_pending(), deps, tmp=tmp_path)
    _run(_pending(), deps, tmp=tmp_path)
    p = state.load_all(tmp_path)[0]
    assert p.finished_at is None  # 放弃：回合状态清零
    assert notified and "放弃" in notified[-1].body or notified[-1].title
