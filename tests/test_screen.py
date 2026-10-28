from cc_autopilot_notify import screen


def _q(labels):
    return {"question": "Q?", "options": [{"label": l} for l in labels]}


def test_parse_rows_reads_a_real_dialog():
    text = ("  执行方式\n"
            "❯ 1. Native：本会话直接实现（推荐）\n"
            "  2. Subagent-driven：逐任务子代理 + 评审\n"
            "  3. Type something.\n"
            "  4. Chat about this\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n")
    rows = screen.parse_rows(text)
    assert [r[1] for r in rows][:2] == ["Native：本会话直接实现（推荐）",
                                        "Subagent-driven：逐任务子代理 + 评审"]
    assert [r[2] for r in rows] == [True, False, False, False]
    assert rows[0][0] == 1


def test_parse_rows_ignores_prose_numbered_lists():
    text = ("  - 熔断必须放在 tail 计算之后\n"
            "  1. 这行是选项 A\n"
            "结论：两个方案 3. 都会被忽略因为不是行首结构\n")
    rows = screen.parse_rows(text)
    assert [r[1] for r in rows] == ["这行是选项 A"]


def test_plan_zero_moves_when_focus_already_on_target():
    text = "❯ 1. A\n  2. B\n"
    assert screen.plan(_q(["A", "B"]), text, 0) == [("enter", 1)]


def test_plan_moves_down_then_enter():
    text = "❯ 1. A\n  2. B\n"
    assert screen.plan(_q(["A", "B"]), text, 1) == [("down", 1), ("enter", 1)]


def test_plan_moves_up_then_enter():
    text = "  1. A\n❯ 2. B\n"
    assert screen.plan(_q(["A", "B"]), text, 0) == [("up", 1), ("enter", 1)]


def test_plan_skips_unrecorded_rows():
    """The dialog also offers 'Type something.' rows; matching anchors on ours."""
    text = "❯ 1. A\n  2. B\n  3. Type something.\n"
    assert screen.plan(_q(["A", "B"]), text, 1) == [("down", 1), ("enter", 1)]


def test_plan_supports_chinese_dot():
    text = "❯ 1．甲\n  2．乙\n"
    assert screen.plan(_q(["甲", "乙"]), text, 1) == [("down", 1), ("enter", 1)]


def test_plan_raises_when_labels_absent():
    text = "❯ 1. 完全不同的一\n  2. 完全不同的二\n"
    try:
        screen.plan(_q(["A", "B"]), text, 0)
    except screen.ScreenMismatch:
        return
    raise AssertionError("pressing keys against a foreign dialog must not happen")


def test_plan_raises_when_no_visible_focus():
    text = "  1. A\n  2. B\n"
    try:
        screen.plan(_q(["A", "B"]), text, 0)
    except screen.ScreenMismatch:
        return
    raise AssertionError("without a cursor Enter submits the emulator's guess")


def test_plan_raises_when_index_out_of_range():
    text = "❯ 1. A\n  2. B\n"
    try:
        screen.plan(_q(["A", "B"]), text, 5)
    except screen.ScreenMismatch:
        return
    raise AssertionError("out-of-range index must never be pressed")


def test_plan_matches_recommended_suffix_rendering():
    """The hook records 'B (Recommended)'; the dialog draws top marks instead."""
    text = "❯ 1. A\n  2. B\n"
    assert screen.plan(_q(["A", "B (Recommended)"]), text, 1) == \
        [("down", 1), ("enter", 1)]


def test_plan_matches_chinese_recommended_suffix():
    text = "❯ 1. 甲\n  2. 乙\n"
    assert screen.plan(_q(["甲", "乙（推荐）"]), text, 1) == [("down", 1), ("enter", 1)]


def _multi_q(labels, question="Q?"):
    return {"question": question, "multiSelect": True,
            "options": [{"label": l} for l in labels]}


def test_parse_rows_strips_multiselect_checkbox():
    text = "❯ 1. [ ] A\n  2. [✓] B\n"
    assert [r[1] for r in screen.parse_rows(text, multi=True)] == ["A", "B"]


def test_plan_multi_toggles_each_target_then_submits():
    text = ("❯ 1. [ ] A\n  2. [ ] B\n  3. [✓] C\n"
            "  4. [ ] Type something\n")
    assert screen.plan_multi(_multi_q(["A", "B", "C"]), text, (0, 2)) == \
        [("space", 1), ("down", 2), ("space", 1), ("down", 2), ("enter", 1)]


def test_plan_multi_single_target():
    text = "❯ 1. [ ] A\n  2. [ ] B\n  3. [ ] Type something\n"
    assert screen.plan_multi(_multi_q(["A", "B"]), text, (1,)) == \
        [("down", 1), ("space", 1), ("down", 2), ("enter", 1)]


def test_plan_multi_refuses_a_foreign_screen():
    text = "❯ 1. [ ] X\n  2. [ ] Y\n"
    try:
        screen.plan_multi(_multi_q(["A", "B"]), text, (0,))
    except screen.ScreenMismatch:
        return
    raise AssertionError("must not toggle options recorded for another dialog")


def test_match_question_finds_the_visible_one():
    q1 = _q(["A", "B"])
    q2 = _q(["C", "D"])
    assert screen.match_question([q1, q2], "❯ 1. C\n  2. D\n")[0] == 1


def test_match_question_returns_none_for_foreign_screen():
    assert screen.match_question([_q(["A", "B"])], "❯ 1. X\n  2. Y\n") is None


def test_review_screen_detection():
    assert screen.is_review_screen("Review your answers\nSubmit answers\n")
    assert not screen.is_review_screen("❯ 1. A\n  2. B\n")
