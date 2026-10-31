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
# A numbered row's checkbox, kept separate from _CHECKBOX so the tick state
# survives parsing: pressing Enter on an already-ticked row unticks it.
_CHECKED_ROW = re.compile(r"^\s*(?:❯)?\s*(\d+)[.．、]\s*\[([^\]]*)\]")
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


def _checked_states(text: str) -> dict[int, bool]:
    """Row number -> whether its multi-select checkbox is ticked."""
    states: dict[int, bool] = {}
    for line in text.splitlines():
        match = _CHECKED_ROW.match(line)
        if match:
            states[int(match.group(1))] = bool(match.group(2).strip())
    return states


def _focused_index(rows: list[tuple[int, str, bool]], start: int, count: int) -> int:
    """The cursor's row index, searched only inside the matched dialog.

    The pane tail can still show an older dialog above this one; taking the
    first `❯` in the whole text would then compute key deltas from the wrong
    origin and submit a different row.
    """
    hits = [start + i for i, row in enumerate(rows[start:start + count]) if row[2]]
    if len(hits) != 1:
        raise ScreenMismatch(
            f"expected exactly one focused row in the dialog, found {len(hits)}")
    return hits[0]


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

    focused = _focused_index(rows, start, len(labels) + 1)

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

    focused = _focused_index(rows, start, len(labels) + 1)
    checked = _checked_states(screen_text)

    presses: list[tuple[str, int | str]] = []
    current = focused
    for target in sorted(set(targets)):
        delta = target - current
        if delta:
            presses.append(("down" if delta > 0 else "up", abs(delta)))
        # Enter toggles. A row the user already ticked must be left alone:
        # toggling it again would clear their choice.
        if not checked.get(rows[target][0], False):
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

    focused = _focused_index(rows, start, len(labels) + 1)

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


def _question_visible(question: dict, text: str) -> bool:
    """Whether the prompt itself appears in the pane, ignoring wrapping."""
    prompt = "".join(str(question.get("question") or "").split())
    if not prompt:
        return False
    return prompt[:24] in "".join(text.split())


def match_question(questions: list[dict], text: str) -> tuple[int, dict] | None:
    """Which recorded question the screen currently shows, if any.

    Two questions can share the same option labels (Yes/No is common). When
    more than one recorded question matches, the prompt text must name exactly
    one of them; otherwise we refuse rather than answer the wrong question.
    """
    candidates: list[tuple[int, dict]] = []
    for index, question in enumerate(questions):
        try:
            _locate(question, text)
        except ScreenMismatch:
            continue
        candidates.append((index, question))
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    visible = [c for c in candidates if _question_visible(c[1], text)]
    return visible[0] if len(visible) == 1 else None


_REVIEW_LINES = ("Review your answers", "Submit answers")


def is_review_screen(text: str) -> bool:
    """The final confirmation page shown after multi-question/multi-select.

    Matched per line: a transcript that merely mentions "Submit answers" in
    prose must not make us press Enter on whatever is focused.
    """
    return any(line.strip() in _REVIEW_LINES for line in text.splitlines())
