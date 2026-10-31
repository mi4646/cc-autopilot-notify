import re

from cc_autopilot_notify import continuation as continue_


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
