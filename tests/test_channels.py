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


