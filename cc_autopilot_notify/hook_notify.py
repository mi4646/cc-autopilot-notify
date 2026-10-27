"""Notification and lifecycle hook.

Two jobs: tell the user a turn finished or failed, and clear the pending record
the moment the question is over — whether we answered it or they did.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import channels, hook_common, state

log = logging.getLogger(__name__)

TOOL = "AskUserQuestion"
_DONE = {"Stop", "StopFailure", "Notification"}


def handle(event: dict, root: Path, cfg: dict) -> int:
    name = event.get("hook_event_name") or ""

    if name == "PostToolUse" and event.get("tool_name") == TOOL:
        session_id = event.get("session_id") or ""
        if session_id:
            state.forget(session_id, root)
        return 0

    if name not in _DONE:
        return 0

    session_id = event.get("session_id") or ""
    if name == "Stop":
        title, body = "Claude 已完成", "本轮结束"
    elif name == "StopFailure":
        title = "Claude 失败"
        body = str(event.get("error") or "本轮以错误结束")[:300]
    else:
        title = "Claude 通知"
        body = str(event.get("message") or event.get("notification_type") or "")[:300]

    channels.send_all(
        channels.Notification(
            title=title, body=body,
            project=event.get("cwd") or "",
            session_id=session_id, event=name,
        ),
        cfg,
    )
    return 0


def main() -> int:
    return hook_common.run_hook(handle)


if __name__ == "__main__":
    sys.exit(main())
