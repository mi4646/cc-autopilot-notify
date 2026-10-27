import pytest

from cc_autopilot_notify import pebrel_runtime


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
