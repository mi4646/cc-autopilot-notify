from cc_autopilot_notify import hook_pre_tool, hook_notify, state


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
