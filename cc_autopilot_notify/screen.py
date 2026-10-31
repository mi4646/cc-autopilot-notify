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
# The free-text row's rendered placeholder.
_OTHER = "Type something"


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
        # Numbered chrome below the dialog ("4. Chat about this") does match,
        # but planning anchors on the recorded labels and never walks onto it.
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
               option_indices: tuple[int, ...] | list[int]) -> list[tuple[str, int | str]]:
    """Key presses to tick every option in `option_indices`, then submit.

    Multi-select rows are toggled with Enter — verified live (2026-10-31):
    the runtime refuses a `space` key entirely, and in the multi-select
    dialog Enter toggles the focused row ([ ] → [✔]). The submit control is
    the header tab past the last row, so the sequence walks down past the
    end (an extra step is absorbed there) and presses Enter, which opens the
    review screen the caller then confirms.
    """
    rows, start = _locate(question, screen_text)
    options = question.get("options") or []
    labels = [str(o.get("label", "")) for o in options]
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

    presses: list[tuple[str, int | str]] = []
    current = focused
    for target in sorted(set(targets)):
        delta = target - current
        if delta:
            presses.append(("down" if delta > 0 else "up", abs(delta)))
        presses.append(("enter", 1))
        current = target

    # One row past the free-text row: walking down from there wraps onto the
    # header Submit tab (verified live 2026-10-31), where Enter opens the
    # review screen. The count comes from the recorded geometry, not the
    # parsed rows — numbered chrome below the dialog would overcount.
    remaining = start + len(labels) + 1 - current
    presses.append(("down", remaining))
    presses.append(("enter", 1))
    return presses


def plan_free_text(question: dict, screen_text: str,
                   text: str) -> list[tuple[str, int | str]]:
    """Key presses to answer via the free-text row, for a delegated decision.

    Verified live (2026-10-31) against a real multi-select dialog: Enter on
    the "Type something" row activates it, a paste replaces the placeholder
    with the text, Enter confirms (the row shows [✔] with the text), one more
    down wraps onto the header Submit tab, and Enter opens the review screen
    the caller confirms. Anything but the recorded placeholder on that row is
    refused: pasting into an unknown row is how a wrong answer gets submitted.
    """
    rows, start = _locate(question, screen_text)
    labels = [str(o.get("label", "")) for o in (question.get("options") or [])]
    other = start + len(labels)
    if other >= len(rows) or _stem(rows[other][1]) != _OTHER:
        raise ScreenMismatch(
            f"no free-text row after the recorded options in {rows!r}")

    focused = next((i for i, row in enumerate(rows) if row[2]), None)
    if focused is None:
        raise ScreenMismatch("no focused row in the dialog")

    presses: list[tuple[str, int | str]] = []
    delta = other - focused
    if delta:
        presses.append(("down" if delta > 0 else "up", abs(delta)))
    presses.append(("enter", 1))
    presses.append(("paste", text))
    presses.append(("enter", 1))
    presses.append(("down", 1))
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
