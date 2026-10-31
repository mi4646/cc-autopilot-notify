import pytest

from cc_autopilot_notify import pebrel_runtime
from pathlib import Path


def _fake_appdata(tmp_path, port, token="a" * 32):
    d = tmp_path / "Pebrel"
    d.mkdir()
    (d / "runtime.port").write_text(f"{port} {token} 1\n")
    return d


def test_endpoint_parses_port_and_token(tmp_path):
    appdata = _fake_appdata(tmp_path, 54345)
    port, token = pebrel_runtime.endpoint(appdata)
    assert port == 54345
    assert token == "a" * 32


def test_endpoint_missing_raises(tmp_path):
    with pytest.raises(pebrel_runtime.RuntimeError):
        pebrel_runtime.endpoint(tmp_path)


def test_press_repeats_client_side(monkeypatch):
    """The runtime ignores the repeat field (live-verified); loop here."""
    calls = []

    def fake_call(appdata, method, params):
        calls.append(params)
        return {"ok": True}

    monkeypatch.setattr(pebrel_runtime, "_call", fake_call)
    pebrel_runtime.press(Path("/tmp"), 3, "down", 2)
    assert len(calls) == 2
    assert calls[0] == {"pane_id": 3, "key": "down"}


def test_paste_sends_text(monkeypatch):
    calls = []
    monkeypatch.setattr(pebrel_runtime, "_call",
                        lambda a, m, p: calls.append((m, p)) or {"ok": True})
    pebrel_runtime.paste(Path("/tmp"), 3, "按照你的建议来")
    assert calls == [("pane.paste", {"pane_id": 3, "text": "按照你的建议来"})]


def test_supports_conversation_true():
    desc = {"capabilities": ["conversation.read", "conversation.choose"]}
    assert pebrel_runtime.supports_conversation(desc) is True


def test_supports_conversation_false_when_choose_missing():
    desc = {"capabilities": ["conversation.read"]}
    assert pebrel_runtime.supports_conversation(desc) is False


def test_question_options_handles_null_prompt():
    assert pebrel_runtime.question_options(None) == []


def test_question_options_reads_labels_and_descriptions():
    prompt = {"options": [{"label": "A", "description": "first"},
                          {"label": "B", "description": "second"}]}
    assert pebrel_runtime.question_options(prompt) == [
        {"label": "A", "description": "first"},
        {"label": "B", "description": "second"},
    ]


def test_question_options_unknown_shape_raises():
    """Unobserved prompt shapes must fail loudly, never yield a wrong answer."""
    with pytest.raises(pebrel_runtime.RuntimeError):
        pebrel_runtime.question_options({"mystery": "payload"})
