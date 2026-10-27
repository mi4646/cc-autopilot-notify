"""Path B: answer from the PermissionRequest hook when the daemon cannot.

This hook BLOCKS — Claude Code does not draw the dialog until it returns. So it
is only wired in when path A is unavailable. Waiting here is the whole point: it
gives a human the same threshold to answer first, after which we answer.
"""

from __future__ import annotations

import logging
import os
import sys
import time

from . import hook_common, judge

log = logging.getLogger(__name__)

TOOL = "AskUserQuestion"


def answer(event: dict, wait_seconds: float, judge_fn) -> dict | None:
    """Return the hook payload, or None to leave the question to the terminal."""
    if event.get("tool_name") != TOOL:
        return None
    questions = ((event.get("tool_input") or {}).get("questions")) or []
    if not questions:
        return None

    if wait_seconds > 0:
        time.sleep(wait_seconds)

    answers: dict[str, str] = {}
    for question in questions:
        if not isinstance(question, dict) or not question.get("question"):
            continue
        options = [o for o in (question.get("options") or []) if isinstance(o, dict)]
        if not options:
            continue
        index = judge_fn(question)
        if not 0 <= index < len(options):
            index = 0
        if question.get("multiSelect"):
            # Multi-select legitimately holds several labels, and the plan's
            # contract is a comma-joined answer. Union the judged option with
            # every option Claude marked, so both signals survive. With no
            # marks this collapses to the judged option alone.
            marked = [i for i, o in enumerate(options)
                      if judge.marker_index({"options": [o]}) == 0]
            picks = sorted({index, *marked})
        else:
            picks = [index]
        labels = [str(options[i].get("label", "")) for i in picks]
        labels = [label for label in labels if label]
        if not labels:
            continue
        answers[question["question"]] = ", ".join(labels)

    if not answers:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow",
                         "updatedInput": {"answers": answers}},
        }
    }


def handle(event: dict, root, cfg: dict) -> int:
    import json

    from . import config

    # Claude Code kills a hook at its configured timeout, so the wait must end
    # before that or the hook is killed mid-block having answered nothing.
    hook_timeout = float(os.environ.get("CC_AUTOPILOT_HOOK_TIMEOUT", "180"))
    wait = min(config.background_seconds(cfg), max(hook_timeout - 10.0, 0.0))
    payload = answer(event, wait_seconds=wait,
                     judge_fn=lambda q: judge.pick(q, model=config.judge_model(cfg)).index)
    if payload is not None:
        print(json.dumps(payload))
    return 0


def main() -> int:
    from . import config

    try:
        cfg = config.load()
    except Exception:  # noqa: BLE001 - a hook must never wedge Claude Code
        log.exception("permission hook: cannot load config; leaving the dialog alone")
        return 0
    if not config.path_b_enabled(cfg):
        return 0
    return hook_common.run_hook(handle)


if __name__ == "__main__":
    sys.exit(main())
