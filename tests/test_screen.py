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


_REAL_MULTI_SCREEN = (
    "Ran 1 shell command\n"
    "──────────────────────────────\n"
    "←  ☒ 探针问题  ✔ Submit  →\n"
    "\n"
    "│ 【探针】请勿碰面板\n"
    "\n"
    "❯ 1. [ ] 选项A\n"
    "         占位选项 A\n"
    "  2. [ ] 选项B\n"
    "         占位选项 B\n"
    "  3. [ ] Type something\n"
    "     Submit\n"
    "──────────────────────────────\n"
    "  4. Chat about this\n"
    "\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    "❯ \n")


def test_real_shape_multiselect_locates_and_plans():
    q = _multi_q(["选项A", "选项B"])
    rows, start = None, None
    # _locate is internal; exercise it through plan_multi on the live shape.
    presses = screen.plan_multi(q, _REAL_MULTI_SCREEN, (0,))
    assert presses[0] == ("enter", 1), "cursor starts on row 1"


def test_plan_free_text_walks_the_verified_sequence():
    q = _multi_q(["选项A", "选项B"])
    presses = screen.plan_free_text(q, _REAL_MULTI_SCREEN, "按照你的建议来")
    assert presses == [
        ("down", 2),      # focused row 1 -> free-text row 3
        ("enter", 1),     # activate the free-text row
        ("paste", "按照你的建议来"),
        ("enter", 1),     # confirm: row shows [✔] with the text
        ("down", 1),      # past the last row -> header Submit tab
        ("enter", 1),     # -> review screen
    ]


def test_plan_free_text_refuses_a_foreign_screen():
    text = "❯ 1. [ ] X\n  2. [ ] Y\n  3. [ ] Something else\n"
    try:
        screen.plan_free_text(_multi_q(["X", "Y"]), text, "按照你的建议来")
    except screen.ScreenMismatch:
        return
    raise AssertionError("must not paste into an unverified row")


def test_plan_multi_leaves_an_already_ticked_option_alone():
    """Pressing Enter on a ticked row would clear the user's choice."""
    text = ("❯ 1. [ ] A\n  2. [ ] B\n  3. [✓] C\n"
            "  4. [ ] Type something\n")
    assert screen.plan_multi(_multi_q(["A", "B", "C"]), text, (0, 2)) == \
        [("enter", 1), ("down", 2), ("down", 2), ("enter", 1)]


def test_plan_multi_single_target():
    text = "❯ 1. [ ] A\n  2. [ ] B\n  3. [ ] Type something\n"
    assert screen.plan_multi(_multi_q(["A", "B"]), text, (1,)) == \
        [("down", 1), ("enter", 1), ("down", 2), ("enter", 1)]


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


def test_plan_ignores_a_focus_marker_from_an_older_dialog():
    """The pane tail may still show yesterday's cursor above today's dialog."""
    text = "❯ 1. Old\n  2. Old2\n  1. A\n❯ 2. B\n"
    assert screen.plan(_q(["A", "B"]), text, 1) == [("enter", 1)]


def test_match_question_refuses_duplicate_labels_without_a_prompt():
    q1 = {"question": "Pick one", "options": [{"label": "Yes"}, {"label": "No"}]}
    q2 = {"question": "Pick two", "options": [{"label": "Yes"}, {"label": "No"}]}
    assert screen.match_question([q1, q2], "❯ 1. Yes\n  2. No\n") is None


def test_match_question_uses_the_prompt_to_disambiguate():
    q1 = {"question": "Pick one", "options": [{"label": "Yes"}, {"label": "No"}]}
    q2 = {"question": "Pick two", "options": [{"label": "Yes"}, {"label": "No"}]}
    text = "Pick two\n❯ 1. Yes\n  2. No\n"
    assert screen.match_question([q1, q2], text)[0] == 1


def test_review_screen_detection():
    assert screen.is_review_screen("Review your answers\nSubmit answers\n")
    assert not screen.is_review_screen("❯ 1. A\n  2. B\n")
