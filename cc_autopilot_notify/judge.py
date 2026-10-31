"""Choose which option to submit when nobody is there to choose.

Two tiers, cheapest first:

1. Read the recommendation marker Claude itself put in the options. This costs
   nothing and reflects Claude's own judgment, which is what the user asked for.
2. Ask a model, only when no marker is present.

Anything that goes wrong ends at option 0 with `fallback=True`, and the caller
says so in the notification. A silent wrong answer is worse than a stated guess.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_SUFFIXES = ("(recommended)", "（推荐）", "(推荐)", "（recommended）")

# A description only marks a recommendation when it says so affirmatively:
# "not recommended for production" must never win the vote.
_DESC_MARKER = re.compile(r"\brecommended\b|推荐", re.IGNORECASE)
_DESC_NEGATED = re.compile(
    r"\b(?:not|never)\b[^.]{0,30}?\brecommended\b|不推荐|不建议", re.IGNORECASE)

# When nothing distinguishes an option, the answer sent back is this free
# text: it delegates the choice to Claude itself, who has the context the
# daemon lacks. Verified live (2026-10-31) that the AskUserQuestion dialog's
# "Type something" row accepts pasted text and submits it as the answer.
DELEGATE_TEXT = "按照你的建议来"


@dataclasses.dataclass
class Decision:
    indices: tuple[int, ...]
    labels: tuple[str, ...]
    reason: str
    fallback: bool
    delegate: bool = False

    @property
    def index(self) -> int:
        return self.indices[0]

    @property
    def label(self) -> str:
        return self.labels[0]


def _labels(question: dict) -> list[dict]:
    return [o for o in (question.get("options") or []) if isinstance(o, dict)]


def marker_indices(question: dict) -> list[int]:
    """Every option Claude marked recommended, in screen order."""
    hits: list[int] = []
    for index, option in enumerate(_labels(question)):
        label = str(option.get("label") or "").strip()
        lowered = label.lower()
        if any(lowered.endswith(suffix) for suffix in _SUFFIXES):
            hits.append(index)
            continue
        description = str(option.get("description") or "")
        if _DESC_MARKER.search(description) and not _DESC_NEGATED.search(description):
            hits.append(index)
    return hits


def marker_index(question: dict) -> int | None:
    """Return the index of the single recommended option, or None.

    None covers both "no marker" and "more than one marker": when Claude did not
    express one preference, guessing which of two it meant is not our call.
    """
    hits = marker_indices(question)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        log.debug("judge: %d options marked recommended; declining to pick", len(hits))
    return None


def _ask_model(question: dict, model: str, timeout: float = 8.0) -> int:
    """Ask the gateway which option index to submit. Raises on any failure."""
    base = os.environ.get("ANTHROPIC_BASE_URL")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not base or not token:
        raise RuntimeError("no ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL configured")

    options = _labels(question)
    listing = "\n".join(
        f"{i}. {o.get('label', '')} — {o.get('description', '')}"
        for i, o in enumerate(options)
    )
    prompt = (
        "Pick the single best option for this question.\n"
        f"Question: {question.get('question', '')}\n"
        f"Options:\n{listing}\n\n"
        f"Reply with only the option number (0-{len(options) - 1})."
    )
    body = json.dumps({
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + "/v1/messages", data=body,
        headers={"Content-Type": "application/json",
                 "anthropic-version": "2023-06-01",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    text = "".join(
        block.get("text", "") for block in payload.get("content", [])
        if isinstance(block, dict)
    ).strip()
    # Only a bare integer counts. Concatenating digits turned "1 (not 0)" into
    # 10 and "-1" into 1: a valid-looking index pointing at the wrong option.
    match = re.fullmatch(r"\s*(\d{1,3})\s*", text)
    if not match:
        raise RuntimeError(f"model reply was not a single number: {text[:80]!r}")
    return int(match.group(1))


def pick(question: dict, model: str | None, call_model=None, *,
         timeout: float = 8.0, fallback: str = "first_option") -> Decision:
    options = _labels(question)
    if not options:
        raise ValueError("question has no options to choose from")

    marked = marker_index(question)
    if marked is not None:
        return Decision(indices=(marked,),
                        labels=(str(options[marked].get("label", "")),),
                        reason="claude marked it recommended", fallback=False)

    if model:
        ask = call_model or _ask_model

        def call() -> int:
            # A caller-supplied stub keeps the two-argument shape used by tests.
            return ask(question, model) if call_model is not None else ask(
                question, model, timeout=timeout)

        index = -1
        for attempt in range(2):  # one retry, as the spec allows
            try:
                index = call()
                break
            except Exception as exc:  # noqa: BLE001 - every failure ends the same way
                log.warning("judge: model call failed (attempt %d/2: %s)",
                            attempt + 1, exc)
                index = -1
        if 0 <= index < len(options):
            return Decision(indices=(index,),
                            labels=(str(options[index].get("label", "")),),
                            reason=f"model {model}", fallback=False)
        if index != -1:
            log.warning("judge: model returned out-of-range index %s", index)

    if fallback == "delegate":
        return Decision(indices=(), labels=(DELEGATE_TEXT,),
                        reason="no recommendation; delegated the choice back to Claude",
                        fallback=True, delegate=True)
    return Decision(indices=(0,), labels=(str(options[0].get("label", "")),),
                    reason="no recommendation available; fell back to first option",
                    fallback=True)


def pick_multi(question: dict, model: str | None = None, call_model=None) -> Decision:
    """Choose the set of options to tick in a multi-select question.

    All recommended markers are ticked. With no marker there is nothing to
    guess from: multi-select means the asker wants a set, and ticking one
    arbitrary option is the worst-shaped answer — so the decision is delegated
    back to Claude as free text (DELEGATE_TEXT) instead. The model is not
    asked: there is no verified prompt shape for "a set of options", and
    guessing a set is worse than delegating.
    """
    options = _labels(question)
    if not options:
        raise ValueError("question has no options to choose from")

    hits = marker_indices(question)
    if hits:
        return Decision(indices=tuple(hits),
                        labels=tuple(str(options[i].get("label", "")) for i in hits),
                        reason="claude marked them recommended", fallback=False)
    return Decision(indices=(), labels=(DELEGATE_TEXT,),
                    reason="no recommendation; delegated the choice back to Claude",
                    fallback=True, delegate=True)
