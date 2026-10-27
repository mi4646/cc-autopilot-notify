"""PreToolUse hook for AskUserQuestion.

Registers the pending question and pushes a notification, then exits 0 after a
few milliseconds. It must NOT wait: a blocking PermissionRequest-style hook
would hide the dialog for the whole threshold, which is exactly the opposite of
"the user can still answer".

The daemon does the waiting.
"""

from __future__ import annotations

import logging
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
    pending = state.Pending(
        session_id=session_id,
        window_id=int(event.get("window_id") or 1),
        pane_id=int(event.get("pane_id") or 0),
        questions=questions,
        appeared_at=now,
        last_activity=now,
    )
    try:
        state.record(pending, root)
    except OSError as exc:
        log.warning("hook_pre_tool: cannot record pending (%s)", exc)

    first = questions[0] if isinstance(questions[0], dict) else {}
    labels = [o.get("label", "") for o in (first.get("options") or [])
              if isinstance(o, dict)]
    body = str(first.get("question") or "")[:300]
    if labels:
        body += "\n选项: " + " / ".join(str(x) for x in labels[:6])

    channels.send_all(
        channels.Notification(
            title="Claude 在等你回答",
            body=body,
            project=event.get("cwd") or "",
            session_id=session_id,
            event=TOOL,
        ),
        cfg,
    )
    return 0


def main() -> int:
    return hook_common.run_hook(handle)


if __name__ == "__main__":
    sys.exit(main())
