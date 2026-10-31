import json

import pytest

from cc_autopilot_notify import channels


def _n():
    return channels.Notification(title="Needs input", body="Pick one",
                                 project="demo", session_id="s1", event="AskUserQuestion")


def test_feishu_rejects_http_200_with_error_code():
    """Feishu returns 200 with a non-zero code on some failures."""
    body = json.dumps({"code": 19024, "msg": "key not found"})
    with pytest.raises(channels.feishu.FeishuError):
        channels.feishu.check_response(200, body)


def test_feishu_accepts_code_zero():
    channels.feishu.check_response(200, json.dumps({"code": 0}))


def test_feishu_rejects_non_200():
    with pytest.raises(channels.feishu.FeishuError):
        channels.feishu.check_response(500, "boom")


def test_feishu_card_neutralises_at_tags_and_links():
    n = channels.Notification(
        title="t", body="<at id=all></at> see [x](https://evil.example)",
        project="demo", event="AskUserQuestion",
        options=["<at id=all></at>"])
    card = channels.feishu._card(n)
    content = card["card"]["elements"][0]["text"]["content"]
    assert "<at" not in content
    assert "](" not in content


def test_feishu_error_never_echoes_the_webhook():
    """The token lives in the URL path; an exception must not leak it."""
    secret = "https://open.feishu.cn/open-apis/bot/v2/hook/SECRET TOKEN"
    with pytest.raises(channels.feishu.FeishuError) as exc:
        channels.feishu.send(_n(), secret)
    assert "SECRET" not in str(exc.value)
    assert "open.feishu.cn" not in str(exc.value)


def test_send_all_reports_failures_without_raising(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(channels.feishu, "send", boom)
    cfg = {"channels": {"feishu": {"enabled": True, "webhook_env": "W"}}}
    monkeypatch.setenv("W", "https://example.test/h")
    assert channels.send_all(_n(), cfg) == ["feishu"]


def test_disabled_channel_is_not_called(monkeypatch):
    called = []
    monkeypatch.setattr(channels.feishu, "send", lambda n, w: called.append("f"))
    cfg = {"channels": {"feishu": {"enabled": False}}}
    assert channels.send_all(_n(), cfg) == []
    assert called == []


def test_feishu_missing_code_is_a_failure():
    """A gateway error body has no code; treating it as success hides the loss."""
    with pytest.raises(channels.feishu.FeishuError):
        channels.feishu.check_response(200, json.dumps({"msg": "no code here", "data": {}}))


def test_feishu_string_zero_code_is_accepted():
    channels.feishu.check_response(200, json.dumps({"code": "0", "msg": "ok"}))


def _rich(**over):
    base = dict(title="Needs input", body="Pick one",
                project="/var/www/demo", session_id="abcdef1234567890",
                event="AskUserQuestion", question_count=2, options=["A", "B"])
    base.update(over)
    return channels.Notification(**base)


def test_feishu_card_shows_project_count_options_and_session():
    card = channels.feishu._card(_rich())
    fields = json.dumps(card["card"]["elements"][1]["fields"], ensure_ascii=False)
    assert "**项目**\\ndemo" in fields, "project must be the basename"
    assert "**问题数**\\n2" in fields
    assert "**选项**\\nA / B" in fields
    assert "**Session**\\nabcdef12" in fields


def test_feishu_card_omits_empty_extra_fields():
    card = channels.feishu._card(_rich(question_count=0, options=[], session_id=""))
    fields = json.dumps(card["card"]["elements"][1]["fields"], ensure_ascii=False)
    assert "问题数" not in fields
    assert "选项" not in fields
    assert "Session" not in fields


def test_feishu_card_color_follows_event():
    assert channels.feishu._card(_rich())["card"]["header"]["template"] == "orange"
    assert channels.feishu._card(
        _rich(event="idle_prompt"))["card"]["header"]["template"] == "blue"
    assert channels.feishu._card(
        _rich(event="StopFailure"))["card"]["header"]["template"] == "red"


def test_feishu_card_ends_with_a_timestamp_note():
    import datetime
    import time

    stamp = time.mktime(datetime.datetime(2026, 10, 29, 12, 34, 56).timetuple())
    note = channels.feishu._card(_rich(created_at=stamp))["card"]["elements"][-1]
    assert note["tag"] == "note"
    assert note["elements"][0]["content"] == "2026-10-29 12:34:56"


def test_title_carries_the_project_so_the_preview_does():
    assert channels.title_with_project("Claude 等你", "/var/www/demo") == \
        "demo · Claude 等你"
    assert channels.title_with_project("Claude 等你", "") == "Claude 等你"




def test_feishu_card_color_for_auto_answer_is_green():
    assert channels.feishu._card(
        _rich(event="auto_answer"))["card"]["header"]["template"] == "green"


def test_feishu_card_renders_answer_lines_in_body():
    body = "Q: Pick one\n→ A（Claude 标记推荐）"
    card = channels.feishu._card(_rich(event="auto_answer", body=body))
    content = card["card"]["elements"][0]["text"]["content"]
    assert "Q: Pick one" in content
    assert "→ A（Claude 标记推荐）" in content


def test_first_line_truncates():
    assert channels.first_line("abc\ndef") == "abc"
    assert channels.first_line("x" * 80) == "x" * 60 + "…"
    assert channels.first_line("") == "(无文本建议)"
    assert channels.first_line("   \n  ") == "(无文本建议)"


def test_new_event_colors():
    for event, color in (("auto_continue", "turquoise"),
                         ("continue_stage", "blue"),
                         ("continue_capped", "orange")):
        card = channels.feishu._card(channels.Notification(
            title="t", body="b", event=event))
        assert card["card"]["header"]["template"] == color


def test_send_all_suppresses_everything_in_quiet_window(monkeypatch):
    """凌晨静默：窗口内所有通知（含审批/续跑/错误）都不出通道。"""
    sent = []
    monkeypatch.setattr(channels.feishu, "send", lambda n, w: sent.append(n))
    cfg = {"channels": {"feishu": {"enabled": True, "webhook": "https://x"}},
           "quiet_hours": {"enabled": True, "start": "00:00", "end": "09:00"}}
    monkeypatch.setattr(channels._config, "in_quiet_hours", lambda cfg: True)
    out = channels.send_all(channels.Notification(title="t", body="b"), cfg)
    assert out == [] and sent == []


def test_send_all_sends_outside_quiet_window(monkeypatch):
    sent = []
    monkeypatch.setattr(channels.feishu, "send", lambda n, w: sent.append(n))
    cfg = {"channels": {"feishu": {"enabled": True, "webhook": "https://x"}},
           "quiet_hours": {"enabled": True, "start": "00:00", "end": "09:00"}}
    monkeypatch.setattr(channels._config, "in_quiet_hours", lambda cfg: False)
    channels.send_all(channels.Notification(title="t", body="b"), cfg)
    assert len(sent) == 1
