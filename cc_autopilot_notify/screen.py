"""Turn a recorded question into the key presses that answer it.

The Pebrel dialog renders one numbered row per option with the focused row
marked "❯". The hook already knows which option to pick (judge.index), so the
screen is only checked for agreement; we never re-derive the answer from pixels.
"""

from __future__ import annotations

import re

_MARKER = "❯"
_ROW = re.compile(r"^\s*(?:❯)?\s*(\d+)[.．、]\s*(.+?)\s*$")
# AskUserQuestion renders "(Recommended)" markers by decoration or strips them:
# the screen shows "B" where the hook recorded "B (Recommended)". Anchoring on
# the rendered text means comparing the stem before any parenthesised suffix.
_DECOR = re.compile(r"\s*[（(](?:推荐|recommended)[）)]\s*$", re.IGNORECASE)
# Multi-select rows render a checkbox between the number and the label.
_CHECKBOX = re.compile(r"^\[[^\]]?\]\s*")


def _stem(label: str) -> str:
    return _DECOR.sub("", label).strip()


class ScreenMismatch(Exception):
    """The dialog on screen does not match the recorded question."""


def parse_rows(text: str, multi: bool = False) -> list[tuple[int, str, bool]]:
    """(choice_number, label, focused) for each numbered row, screen order."""
    rows: list[tuple[int, str, bool]] = []
    for line in text.splitlines():
        # Rows look like "❯ 1. Native：…" or "  2. Subagent-driven：…". Prose
        # lines (paragraphs, numbered lists in the transcript) do not match
        # because they carry a separator and the marker only appears on rows.
        match = _ROW.match(line)
        if match:
            label = match.group(2)
            if multi:
                label = _CHECKBOX.sub("", label).strip()
            rows.append((int(match.group(1)), label, _MARKER in line))
    return rows


def _locate(question: dict, text: str) -> tuple[list[tuple[int, str, bool]], int]:
    """Screen rows for `question`, plus the row index where its labels start."""
    rows = parse_rows(text, multi=bool(question.get("multiSelect")))
    labels = [str(o.get("label", "")) for o in (question.get("options") or [])]
    if not labels:
        raise ScreenMismatch("recorded question has no options")

    # Find the recorded sequence in the screen's rows, in order. A dialog may
    # also render rows we did not record ("Type something."), and a multi-
    # question dialog renders the next question's rows behind ours; matching the
    # full recorded sequence is what keeps both apart. Labels compare on their
    # rendered stem: "B (Recommended)" on the hook side shows as "B" on screen.
    wanted = [_stem(label) for label in labels]
    for start in range(len(rows) - len(labels) + 1):
        window = [_stem(row[1]) for row in rows[start:start + len(labels)]]
        if window == wanted:
            return rows, start
    raise ScreenMismatch(
        f"recorded options {labels!r} not found in screen rows "
        f"{[row[1] for row in rows]!r}")


def plan(question: dict, screen_text: str, option_index: int) -> list[tuple[str, int]]:
    """Key presses to select `option_index`, anchored on the recorded labels.

    Arrows move the cursor, Enter submits. Raises ScreenMismatch when the
    recorded labels are not on screen in order, when there is no visible cursor,
    or when the option index is out of range: pressing keys against a wrong or
    unverifiable screen is how a wrong answer gets submitted.
    """
    rows, start = _locate(question, screen_text)
    labels = [str(o.get("label", "")) for o in (question.get("options") or [])]

    target = start + option_index
    if option_index < 0 or option_index >= len(labels) or target >= len(rows):
        raise ScreenMismatch(f"option index {option_index} out of screen range")

    focused = next((i for i, row in enumerate(rows) if row[2]), None)
    if focused is None:
        raise ScreenMismatch("no focused row in the dialog")

    delta = target - focused
    if delta == 0:
        return [("enter", 1)]
    key = "down" if delta > 0 else "up"
    return [(key, abs(delta)), ("enter", 1)]


def plan_multi(question: dict, screen_text: str,
               option_indices: tuple[int, ...] | list[int]) -> list[tuple[str, int]]:
    """Key presses to tick every option in `option_indices`, then submit.

    Multi-select rows are toggled with Space. The submit control is the row
    after the last option (the dialog always renders it for multi-select), so
    the sequence walks down to it and presses Enter.
    """
    rows, start = _locate(question, screen_text)
    options = question.get("options") or []
    targets: list[int] = []
    for index in option_indices:
        if index < 0 or index >= len(options):
            raise ScreenMismatch(f"option index {index} out of recorded range")
        targets.append(start + index)
    if not targets:
        raise ScreenMismatch("multi-select answer has no options")

    focused = next((i for i, row in enumerate(rows) if row[2]), None)
    if focused is None:
        raise ScreenMismatch("no focused row in the dialog")

    presses: list[tuple[str, int]] = []
    current = focused
    for target in sorted(set(targets)):
        delta = target - current
        if delta:
            presses.append(("down" if delta > 0 else "up", abs(delta)))
        presses.append(("space", 1))
        current = target

    # Rows after the last target, plus one for the submit control itself.
    remaining = len(rows) - current
    if remaining:
        presses.append(("down", remaining))
    presses.append(("enter", 1))
    return presses


def match_question(questions: list[dict], text: str) -> tuple[int, dict] | None:
    """Which recorded question the screen currently shows, if any."""
    for index, question in enumerate(questions):
        try:
            _locate(question, text)
        except ScreenMismatch:
            continue
        return index, question
    return None


def is_review_screen(text: str) -> bool:
    """The final confirmation page shown after multi-question/multi-select."""
    return "Review your answers" in text or "Submit answers" in text
