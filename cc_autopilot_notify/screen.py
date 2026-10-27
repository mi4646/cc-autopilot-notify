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


def _stem(label: str) -> str:
    return _DECOR.sub("", label).strip()


class ScreenMismatch(Exception):
    """The dialog on screen does not match the recorded question."""


def parse_rows(text: str) -> list[tuple[int, str, bool]]:
    """(choice_number, label, focused) for each numbered row, screen order."""
    rows: list[tuple[int, str, bool]] = []
    for line in text.splitlines():
        # Rows look like "❯ 1. Native：…" or "  2. Subagent-driven：…". Prose
        # lines (paragraphs, numbered lists in the transcript) do not match
        # because they carry a separator and the marker only appears on rows.
        match = _ROW.match(line)
        if match:
            rows.append((int(match.group(1)), match.group(2), _MARKER in line))
    return rows


def plan(question: dict, screen_text: str, option_index: int) -> list[tuple[str, int]]:
    """Key presses to select `option_index`, anchored on the recorded labels.

    Arrows move the cursor, Enter submits. Raises ScreenMismatch when the
    recorded labels are not on screen in order, when there is no visible cursor,
    or when the option index is out of range: pressing keys against a wrong or
    unverifiable screen is how a wrong answer gets submitted.
    """
    rows = parse_rows(screen_text)
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
            break
    else:
        raise ScreenMismatch(
            f"recorded options {labels!r} not found in screen rows "
            f"{[row[1] for row in rows]!r}")

    target = start + option_index
    if target >= len(rows):
        raise ScreenMismatch(f"option index {option_index} out of screen range")

    focused = next((i for i, row in enumerate(rows) if row[2]), None)
    if focused is None:
        raise ScreenMismatch("no focused row in the dialog")

    delta = target - focused
    if delta == 0:
        return [("enter", 1)]
    key = "down" if delta > 0 else "up"
    return [(key, abs(delta)), ("enter", 1)]
