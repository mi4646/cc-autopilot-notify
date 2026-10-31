"""PreToolUse hook for AskUserQuestion.

Registers the pending question and pushes a notification, then exits 0 after a
few milliseconds. It must NOT wait: a blocking PermissionRequest-style hook
would hide the dialog for the whole threshold, which is exactly the opposite of
"the user can still answer".

The daemon does the waiting.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from . import channels, hook_common, state

log = logging.getLogger(__name__)

TOOL = "AskUserQuestion"


def handle(event: dict, root: Path, cfg: dict) -> int:
    if event.get("tool_name") != TOOL:
        return 0

    questions = ((event.get("tool_input") or {}).get("questions")) or []
    if not questions:
        log.debug("hook_pre_tool: %s with no questions", TOOL)
        return 0

    session_id = event.get("session_id") or ""
    if not session_id:
        log.debug("hook_pre_tool: event without session_id")
        return 0

    now = time.time()
    # Claude Code's hook payload carries no pane identity, and the runtime
    # refuses pane 0 with `invalid_params`. Prefer the environment Pebrel sets
    # in the pane; the daemon resolves the window id (and re-resolves the pane)
    # from agents.list when either is unknown.
    try:
        pane_id = int(os.environ.get("PEBREL_PANE_ID") or 0)
    except ValueError:
        pane_id = 0
    try:
        state.record_questions(session_id, root, pane_id=pane_id,
                               questions=questions,
                               cwd=event.get("cwd") or "")
    except OSError as exc:
        log.warning("hook_pre_tool: cannot record pending (%s)", exc)

    first = questions[0] if isinstance(questions[0], dict) else {}
    labels = [o.get("label", "") for o in (first.get("options") or [])
              if isinstance(o, dict)]
    body = str(first.get("question") or "")[:300]

    # This hook runs before the dialog is drawn. The channel timeout must stay
    # well below the hook timeout, or a slow webhook delays the question.
    channels.send_all(
        channels.Notification(
            title=channels.title_with_project("Claude 在等你回答",
                                              event.get("cwd") or ""),
            body=body,
            project=event.get("cwd") or "",
            session_id=session_id,
            event=TOOL,
            question_count=len(questions),
            options=[str(x) for x in labels[:6]],
        ),
        cfg,
        timeout=2.0,
    )
    return 0


def main() -> int:
    return hook_common.run_hook(handle)


if __name__ == "__main__":
    sys.exit(main())
