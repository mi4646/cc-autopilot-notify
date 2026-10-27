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


def test_stop_event_notifies(tmp_path, monkeypatch):
    import cc_autopilot_notify.channels as channels
    seen = []
    monkeypatch.setattr(channels, "send_all", lambda n, c: seen.append(n.title) or [])
    ev = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/var/www/demo"}
    assert hook_notify.handle(ev, tmp_path, {"channels": {}}) == 0
    assert seen


def test_post_tool_use_clears_pending(tmp_path):
    hook_pre_tool.handle(_auq_event(), tmp_path, {"channels": {}, "thresholds": {}})
    ev = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
          "session_id": "s1"}
    assert hook_notify.handle(ev, tmp_path, {"channels": {}}) == 0
    assert state.load_all(tmp_path) == []
