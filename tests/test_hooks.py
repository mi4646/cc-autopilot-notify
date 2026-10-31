from cc_autopilot_notify import config, hook_pre_tool, hook_notify, state


def _auq_event(pane=2):
    return {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
            "session_id": "s1", "cwd": "/var/www/demo",
            "tool_input": {"questions": [{"question": "Q",
                                          "options": [{"label": "A"}, {"label": "B"}]}]}}


def test_registers_pending(tmp_path):
    rc = hook_pre_tool.handle(_auq_event(), tmp_path, {"channels": {}, "thresholds": {}})
    assert rc == 0
    pend = state.load_all(tmp_path)
    assert len(pend) == 1 and pend[0].session_id == "s1"


def test_ignores_non_auq_tools(tmp_path):
    ev = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s1"}
    assert hook_pre_tool.handle(ev, tmp_path, {"channels": {}, "thresholds": {}}) == 0
    assert state.load_all(tmp_path) == []


def test_malformed_event_returns_zero(tmp_path):
    assert hook_pre_tool.handle({}, tmp_path, {"channels": {}, "thresholds": {}}) == 0


def test_notification_channel_failure_still_returns_zero(tmp_path, monkeypatch):
    import cc_autopilot_notify.channels as channels
    monkeypatch.setattr(channels, "send_all", lambda n, c: ["feishu"])
    cfg = {"channels": {"terminal": {"enabled": True}}, "thresholds": {}}
    assert hook_pre_tool.handle(_auq_event(), tmp_path, cfg) == 0


def _capture(monkeypatch):
    import cc_autopilot_notify.channels as channels
    seen = []
    monkeypatch.setattr(channels, "send_all", lambda n, c: seen.append(n) or [])
    return seen


def test_question_notification_carries_all_questions_and_options(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    ev = _auq_event()
    ev["tool_input"]["questions"] = [
        {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]},
        {"question": "Q2", "options": [{"label": "C"}]},
    ]
    assert hook_pre_tool.handle(ev, tmp_path, {"channels": {}, "thresholds": {}}) == 0
    n = seen[0]
    assert n.question_count == 2
    assert n.options == ["A", "B"]
    assert n.body == "Q1", "options belong in their own card field, not the body"


def test_stop_event_does_not_notify(tmp_path, monkeypatch):
    """Stop fires every turn; a notification per turn is the bug being fixed."""
    seen = _capture(monkeypatch)
    ev = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/var/www/demo"}
    assert hook_notify.handle(ev, tmp_path, {"channels": {}}) == 0
    assert seen == []


def test_idle_prompt_notification_is_forwarded(tmp_path, monkeypatch):
    """CC's idle_prompt is the real "finished and waiting" signal."""
    seen = _capture(monkeypatch)
    ev = {"hook_event_name": "Notification", "session_id": "s1",
          "notification_type": "idle_prompt",
          "message": "Claude is waiting for your input"}
    cfg = {"channels": {}, "notify": {"message_translations": {
        "Claude is waiting for your input": "Claude 正在等待您的输入",
    }}}
    assert hook_notify.handle(ev, tmp_path, cfg) == 0
    assert [(n.title, n.body) for n in seen] == [
        ("Claude 等你", "Claude 正在等待您的输入"),
    ]


def test_unmapped_notification_message_keeps_original_text(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    ev = {"hook_event_name": "Notification", "session_id": "s1",
          "notification_type": "permission_prompt", "message": "approve?"}
    cfg = {"channels": {}, "notify": {"message_translations": {
        "Claude is waiting for your input": "Claude 正在等待您的输入",
    }}}
    hook_notify.handle(ev, tmp_path, cfg)
    assert [n.body for n in seen] == ["approve?"]


def test_unlisted_notification_types_are_dropped(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    ev = {"hook_event_name": "Notification", "session_id": "s1",
          "notification_type": "agent_completed", "message": "agent done"}
    assert hook_notify.handle(ev, tmp_path, {"channels": {}}) == 0
    assert seen == []


def test_permission_prompt_notification_is_forwarded(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    ev = {"hook_event_name": "Notification", "session_id": "s1",
          "notification_type": "permission_prompt", "message": "needs approval"}
    hook_notify.handle(ev, tmp_path, {"channels": {}})
    assert [n.title for n in seen] == ["Claude 需要审批"]


def test_duplicate_notifications_are_suppressed(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    ev = {"hook_event_name": "Notification", "session_id": "s1",
          "notification_type": "idle_prompt", "message": "waiting"}
    hook_notify.handle(ev, tmp_path, {"channels": {}})
    hook_notify.handle(ev, tmp_path, {"channels": {}})
    assert len(seen) == 1


def test_idle_prompt_is_suppressed_during_quiet_hours(tmp_path, monkeypatch):
    import datetime

    seen = _capture(monkeypatch)
    now = datetime.datetime.now()
    start = (now - datetime.timedelta(minutes=2)).strftime("%H:%M")
    end = (now + datetime.timedelta(minutes=2)).strftime("%H:%M")
    cfg = {"channels": {},
           "quiet_hours": {"enabled": True, "start": start, "end": end}}
    ev = {"hook_event_name": "Notification", "session_id": "s1",
          "notification_type": "idle_prompt", "message": "waiting"}
    hook_notify.handle(ev, tmp_path, cfg)
    assert seen == []


def test_permission_prompt_ignores_quiet_hours(tmp_path, monkeypatch):
    import datetime

    seen = _capture(monkeypatch)
    now = datetime.datetime.now()
    start = (now - datetime.timedelta(minutes=2)).strftime("%H:%M")
    end = (now + datetime.timedelta(minutes=2)).strftime("%H:%M")
    cfg = {"channels": {},
           "quiet_hours": {"enabled": True, "start": start, "end": end}}
    ev = {"hook_event_name": "Notification", "session_id": "s1",
          "notification_type": "permission_prompt", "message": "approve?"}
    hook_notify.handle(ev, tmp_path, cfg)
    assert [n.title for n in seen] == ["Claude 需要审批"]


def test_stop_failure_still_notifies(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    ev = {"hook_event_name": "StopFailure", "session_id": "s1",
          "error": "boom"}
    hook_notify.handle(ev, tmp_path, {"channels": {}})
    assert [n.title for n in seen] == ["Claude 失败"]


def test_post_tool_use_clears_pending(tmp_path):
    hook_pre_tool.handle(_auq_event(), tmp_path, {"channels": {}, "thresholds": {}})
    ev = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
          "session_id": "s1"}
    assert hook_notify.handle(ev, tmp_path, {"channels": {}}) == 0
    assert state.load_all(tmp_path) == []


def _idle_prompt(cwd=None):
    ev = {"hook_event_name": "Notification", "session_id": "s1",
          "notification_type": "idle_prompt", "message": "waiting"}
    if cwd:
        ev["cwd"] = cwd
    return ev


def test_title_names_the_project(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    hook_notify.handle(_idle_prompt(cwd="/var/www/demo"), tmp_path, {"channels": {}})
    assert [n.title for n in seen] == ["demo · Claude 等你"]


def test_question_title_names_the_project(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    hook_pre_tool.handle(_auq_event(), tmp_path, {"channels": {}, "thresholds": {}})
    assert seen[0].title == "demo · Claude 在等你回答"


def test_user_prompt_submit_records_the_turn_start(tmp_path):
    ev = {"hook_event_name": "UserPromptSubmit", "session_id": "s1"}
    assert hook_notify.handle(ev, tmp_path, {"channels": {}}) == 0
    assert state.pop_turn_start("s1", tmp_path) is not None


def test_short_turn_is_not_notified_when_a_threshold_is_set(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    cfg = {"channels": {}, "notify": {"min_turn_seconds": 60}}
    hook_notify.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1"},
                       tmp_path, cfg)
    hook_notify.handle(_idle_prompt(), tmp_path, cfg)
    assert seen == []


def test_long_turn_reports_its_duration(tmp_path, monkeypatch):
    import time

    seen = _capture(monkeypatch)
    state.mark_turn_start("s1", tmp_path, time.time() - 125)
    cfg = {"channels": {}, "notify": {"min_turn_seconds": 60}}
    hook_notify.handle(_idle_prompt(), tmp_path, cfg)
    assert [n.body for n in seen] == ["waiting · 耗时 2m05s"]


def test_turn_gate_fails_open_without_a_recorded_start(tmp_path, monkeypatch):
    """A session that started before this plugin must not lose its notification."""
    seen = _capture(monkeypatch)
    cfg = {"channels": {}, "notify": {"min_turn_seconds": 60}}
    hook_notify.handle(_idle_prompt(), tmp_path, cfg)
    assert len(seen) == 1


def test_turn_gate_leaves_other_events_alone(tmp_path, monkeypatch):
    seen = _capture(monkeypatch)
    cfg = {"channels": {}, "notify": {"min_turn_seconds": 60}}
    state.mark_turn_start("s1", tmp_path, 0)
    ev = {"hook_event_name": "Notification", "session_id": "s1",
          "notification_type": "permission_prompt", "message": "approve?"}
    hook_notify.handle(ev, tmp_path, cfg)
    assert [n.body for n in seen] == ["approve?"]


def test_pending_records_the_cwd(tmp_path):
    """The auto-answer record needs the cwd to name the project in Feishu."""
    hook_pre_tool.handle(_auq_event(), tmp_path, {"channels": {}, "thresholds": {}})
    pend = state.load_all(tmp_path)
    assert pend[0].cwd == "/var/www/demo"


def _cfg():
    return {"channels": {"terminal": {"enabled": False}}, "thresholds": {}}


def _bare(sid):
    return state.Pending(session_id=sid, window_id=1, pane_id=2, questions=[],
                         appeared_at=1.0, last_activity=1.0)


def test_stop_marks_finished(tmp_path):
    rc = hook_notify.handle({"hook_event_name": "Stop", "session_id": "s1",
                             "cwd": "/var/www/demo"}, tmp_path, _cfg())
    assert rc == 0
    p = state.load_all(tmp_path)[0]
    assert p.finished_at is not None and p.finished_failed is False
    assert p.cwd == "/var/www/demo"


def test_stop_failure_marks_finished_failed(tmp_path, monkeypatch):
    monkeypatch.setattr("cc_autopilot_notify.channels.send_all", lambda n, c: [])
    rc = hook_notify.handle({"hook_event_name": "StopFailure", "session_id": "s1",
                             "error": "boom"}, tmp_path, _cfg())
    assert rc == 0
    p = state.load_all(tmp_path)[0]
    assert p.finished_failed is True and p.finished_at is not None


def test_human_prompt_clears_finished(tmp_path):
    state.record(_bare("s1"), tmp_path)
    state.set_finished("s1", tmp_path, when=100.0, pane_id=2)
    hook_notify.handle({"hook_event_name": "UserPromptSubmit",
                        "session_id": "s1"}, tmp_path, _cfg())
    p = state.load_all(tmp_path)[0]
    assert p.finished_at is None and p.continue_count == 0


def test_prompt_within_auto_submit_window_keeps_state(tmp_path, monkeypatch):
    state.record(_bare("s1"), tmp_path)
    state.set_finished("s1", tmp_path, when=100.0, pane_id=2)
    state.bump_continue("s1", tmp_path, failed=False)
    monkeypatch.setattr(hook_notify.time, "time", lambda: 103.0)
    state.mark_auto_submit("s1", tmp_path, when=102.5)
    hook_notify.handle({"hook_event_name": "UserPromptSubmit",
                        "session_id": "s1"}, tmp_path, _cfg())
    p = state.load_all(tmp_path)[0]
    assert p.continue_count == 1 and p.finished_at == 100.0  # 未清


def test_auq_record_preserves_continuation_state(tmp_path):
    """I2: 面板记录更新不得清掉续跑计数与 finished 标记（D3 护栏）。"""
    state.record(_bare("s1"), tmp_path)
    state.bump_continue("s1", tmp_path, failed=False)
    state.set_finished("s1", tmp_path, when=100.0, pane_id=2)
    rc = hook_pre_tool.handle(_auq_event(), tmp_path, {"channels": {}, "thresholds": {}})
    assert rc == 0
    p = state.load_all(tmp_path)[0]
    assert p.continue_count == 1 and p.finished_at == 100.0
    assert p.questions and p.questions[0].get("question") == "Q"  # 面板内容已更新


def test_stop_failure_suppressed_in_quiet_window(tmp_path, monkeypatch):
    """凌晨全静默：StopFailure 也走 send_all 的静默闸（穿过真实通道层）。"""
    import cc_autopilot_notify.channels as channels
    sent = []
    monkeypatch.setattr(channels.feishu, "send", lambda n, w: sent.append(n))
    monkeypatch.setattr(config, "in_quiet_hours", lambda cfg: True)
    cfg = {"channels": {"feishu": {"enabled": True, "webhook": "https://x"}},
           "quiet_hours": {"enabled": True, "start": "00:00", "end": "09:00"}}
    rc = hook_notify.handle({"hook_event_name": "StopFailure", "session_id": "s1",
                             "error": "boom"}, tmp_path, cfg)
    assert rc == 0 and sent == []
