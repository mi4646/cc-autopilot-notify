import re

import pytest

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


@pytest.fixture(autouse=True)
def _reset_watch():
    """The input watch is per-process global; tests must not inherit it."""
    continuation._INPUT_WATCH.clear()
    continuation._CAPPED_SENT.clear()
    yield
    continuation._INPUT_WATCH.clear()
    continuation._CAPPED_SENT.clear()


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
        cached_presence = staticmethod(lambda: presence.Presence(
            foreground=False, idle_seconds=500.0, locked=locked))
        fresh_presence = staticmethod(lambda: presence.Presence(
            foreground=False, idle_seconds=500.0, locked=locked))
        pane_owner_ok = staticmethod(lambda pending: owner_ok)
        agents = staticmethod(lambda: [{"pane_id": 2, "agent": {"session_id": "s1"}}])
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
    deps, presses, notified, _ = _counting_deps(
        [_pane_tail(""), _pane_tail("生成一下周报"), _pane_tail("生成一下周报")])
    for i in range(3):  # tick1 空（建立过渡），tick2 出现，tick3 稳定→按键
        _run(state.load_all(tmp_path)[0], deps, now=500.0 + i, tmp=tmp_path)
    assert presses == ["tab", "enter"]
    p = state.load_all(tmp_path)[0]
    assert p.continue_count == 1 and p.last_auto_submit is not None
    assert p.pressed_at is not None
    card = [n for n in notified if n.event == "auto_continue"][0]
    assert "自动续跑 1/10" in card.title and "生成一下周报" in card.body


def test_press_failure_before_marker_keeps_no_count(tmp_path, monkeypatch):
    # marker 先落盘：mark_auto_submit 必须发生在 press 之前（codex Critical #1）
    state.record(_pending(), tmp_path)
    import cc_autopilot_notify.continuation as cont
    order = []
    monkeypatch.setattr(cont.state, "mark_auto_submit",
                        lambda *a, **k: order.append("mark"))
    deps, presses, notified, _ = _counting_deps(
        [_pane_tail(""), _pane_tail("生成一下周报"), _pane_tail("生成一下周报")])
    orig_press = deps.press
    deps.press = lambda pending, key, repeat: (order.append(f"press:{key}"),
                                               orig_press(pending, key, repeat))[1]
    for i in range(3):
        _run(state.load_all(tmp_path)[0], deps, now=500.0 + i, tmp=tmp_path)
    assert order == ["mark", "press:tab", "press:enter"]


def test_cap_stops_with_single_capped_card(tmp_path):
    state.record(_pending(continue_count=10), tmp_path)
    deps, presses, notified, _ = _counting_deps(
        [_pane_tail(""), _pane_tail("生成一下周报"), _pane_tail("生成一下周报")])
    for i in range(3):
        _run(state.load_all(tmp_path)[0], deps, now=500.0 + i, tmp=tmp_path)
    assert presses == []
    assert len([n for n in notified if n.event == "continue_capped"]) == 1


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
    deps, presses, notified, _ = _counting_deps(
        [_pane_tail(""), _pane_tail("生成一下周报"), _pane_tail("生成一下周报"),
         _pane_tail("生成一下周报")], press_fails=True)
    for i in range(4):  # 32s 间隔越过 settle 窗；两次注入尝试都被拒
        _run(state.load_all(tmp_path)[0], deps, now=500.0 + i * 32, tmp=tmp_path)
    p = state.load_all(tmp_path)[0]
    assert p.finished_at is None  # 放弃：回合状态清零
    assert notified and any("放弃" in n.body for n in notified)


def _counting_deps(suggestion_texts, locked=True, owner=True, press_fails=False):
    """deps whose read() yields successive texts; counts fresh_presence calls."""
    seq = list(suggestion_texts)
    fresh_calls, presses, notified = [], [], []

    def do_read(pending):
        text = seq.pop(0) if seq else (seq_texts[-1] if seq_texts else "")
        return {"state": "finished", "text": text}

    seq_texts = suggestion_texts

    def do_press(pending, key, repeat):
        if press_fails:
            raise RuntimeError("refused")
        presses.append(key)
        return {"ok": True}

    class D:
        read = staticmethod(do_read)
        press = staticmethod(do_press)
        fresh_presence = staticmethod(lambda: (fresh_calls.append(1),
            presence.Presence(False, 500.0, locked))[1])
        cached_presence = staticmethod(lambda: presence.Presence(
            False, 500.0, locked))
        pane_owner_ok = staticmethod(lambda pending: owner)
        agents = staticmethod(lambda: [{"pane_id": 2, "agent": {"session_id": "s1"}}])
        notify = staticmethod(lambda n: notified.append(n))
        sleep = staticmethod(lambda s: None)
    return D(), presses, notified, fresh_calls


def test_draft_growing_never_presses(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, _, _ = _counting_deps(["❯草稿", _pane_tail("推"), _pane_tail("推到"), _pane_tail("推到远")])
    for i in range(4):
        _run(_pending(), deps, now=500.0 + i, tmp=tmp_path)
    assert presses == []


def test_suggestion_after_empty_transition_presses_once(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, _, _ = _counting_deps(
        [_pane_tail(""), _pane_tail("生成一下周报"), _pane_tail("生成一下周报")])
    for i in range(3):
        _run(_pending(), deps, now=500.0 + i, tmp=tmp_path)
    assert presses == ["tab", "enter"]  # 第 3 tick：空→出现→稳定


def test_unknown_first_sighting_never_presses(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, _, _ = _counting_deps(
        [_pane_tail("生成一下周报")] * 4)  # daemon 重启即见非空，无空过渡
    for i in range(4):
        _run(_pending(), deps, now=500.0 + i, tmp=tmp_path)
    assert presses == []


def test_fresh_presence_only_before_press(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, _, fresh = _counting_deps([_pane_tail(""), _pane_tail("生成一下周报"), _pane_tail("生成一下周报")])
    for i in range(3):
        _run(_pending(), deps, now=500.0 + i, tmp=tmp_path)
    assert len(fresh) == 1  # 只有按键那个 tick 强采


def test_locked_none_zero_press(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, _, _ = _counting_deps([_pane_tail("生成一下周报")], locked=None)
    _run(_pending(), deps, tmp=tmp_path)
    assert presses == []


def test_owner_false_no_press(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, _, _ = _counting_deps([_pane_tail(""), _pane_tail("生成一下周报"), _pane_tail("生成一下周报")], owner=False)
    for i in range(3):
        _run(_pending(), deps, now=500.0 + i, tmp=tmp_path)
    assert presses == []


def test_failure_cap_stops_with_card(tmp_path):
    state.record(_pending(finished_failed=True, failure_count=2), tmp_path)
    deps, presses, notified, _ = _counting_deps(
        [_pane_tail(""), _pane_tail("重试吧"), _pane_tail("重试吧")])
    for i in range(3):
        _run(_pending(finished_failed=True, failure_count=2), deps, now=500.0 + i, tmp=tmp_path)
    assert presses == []
    assert any(n.event == "continue_capped" for n in notified)


def test_canary_once_per_segment_on_blind_tab_hint(tmp_path):
    state.record(_pending(), tmp_path)
    sep = "─" * 60
    blind = f"done\n{sep}\n❯ \n  ⏵⏵ tab to use: 生成一下周报\n{sep}\n  status\n"
    deps, presses, notified, _ = _counting_deps([blind, blind])
    _run(state.load_all(tmp_path)[0], deps, now=500.0, tmp=tmp_path)
    _run(state.load_all(tmp_path)[0], deps, now=501.0, tmp=tmp_path)
    assert notified and notified[0].event == "continue_stage"
    assert state.read_canary(tmp_path, when=500.0) == 1  # 每段一次


def test_interference_tails_match_none():
    assert continuation.match_suggestion(
        _pane_tail("") + "  ! tab to toggle mcp\n") is None
    sep = "─" * 60
    perm = f"Allow Bash?\n{sep}\n❯ \n{sep}\n  Yes / No\n"
    assert continuation.match_suggestion(perm) is None


def test_single_separator_fails_closed():
    assert continuation.match_suggestion("历史 ❯ 旧提示\n" + "─" * 60 + "\n❯ 生成一下周报") is None


def test_press_refused_does_not_consume_cap(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, notified, _ = _counting_deps(
        [_pane_tail(""), _pane_tail("生成一下周报"), _pane_tail("生成一下周报")],
        press_fails=True)
    for i in range(3):
        _run(_pending(), deps, now=500.0 + i, tmp=tmp_path)
    p = state.load_all(tmp_path)[0]
    assert p.continue_count == 0  # 注入失败不占预算（spec 机制 7）


def test_give_up_assertion_is_real(tmp_path):
    state.record(_pending(), tmp_path)
    deps, presses, notified, _ = _counting_deps(
        [_pane_tail(""), _pane_tail("生成一下周报"), _pane_tail("生成一下周报"),
         _pane_tail("生成一下周报")], press_fails=True)
    for i in range(4):  # 32s 间隔越过 settle 窗；两次注入尝试都被拒
        _run(state.load_all(tmp_path)[0], deps, now=500.0 + i * 32, tmp=tmp_path)
    p = state.load_all(tmp_path)[0]
    assert p.finished_at is None
    assert notified and any("放弃" in n.body for n in notified)


def test_read_failure_with_dead_pane_forgets_record(tmp_path):
    """pane 已不在 agents 快照 = 会话死亡，记录必须清掉而不是每秒重试。"""
    state.record(_pending(), tmp_path)
    deps, presses, notified, _ = _counting_deps([_pane_tail("")])

    def dead_read(pending):
        raise RuntimeError("target_not_found pane 2 does not exist")

    deps.read = dead_read
    deps.agents = staticmethod(lambda: [{"pane_id": 9, "agent": None}])  # 本 pane 不在
    out = _run(state.load_all(tmp_path)[0], deps, tmp=tmp_path)
    assert state.load_all(tmp_path) == []  # 记录被清


def test_read_failure_during_outage_keeps_record(tmp_path):
    """agents 快照本身取不到 = 运行时故障，不得误清记录。"""
    state.record(_pending(), tmp_path)
    deps, presses, notified, _ = _counting_deps([_pane_tail("")])

    def dead_read(pending):
        raise RuntimeError("target_not_found pane 2 does not exist")

    def outage_agents():
        raise RuntimeError("runtime unreachable")

    deps.read = dead_read
    deps.agents = outage_agents
    _run(state.load_all(tmp_path)[0], deps, tmp=tmp_path)
    assert state.load_all(tmp_path) != []  # 故障期间保留
