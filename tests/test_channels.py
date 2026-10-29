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


