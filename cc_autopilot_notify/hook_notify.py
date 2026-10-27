"""Notification policy for Claude Code events.

`Stop` fires once per assistant turn, and a turn can end while background work
is still running. Treating it as "the task is done" is what made every turn
push a notification. The completion signal is Claude Code's own `idle_prompt`
notification: it fires about 60 seconds after Claude finishes, only when the
user has not typed and no background agent is still running. So `Stop` no
longer notifies; `StopFailure` still does.

Notification types are filtered too: only ones that need a human (or mark the
end of a wait) are forwarded, and an identical message is suppressed for
`notify.dedupe_seconds`. `idle_prompt` is the only type held back by
`quiet_hours`; being blocked needs to reach the user even at night.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from pathlib import Path

from . import channels, config, hook_common, state

log = logging.getLogger(__name__)

TOOL = "AskUserQuestion"

# agent_completed and progress-style notifications are deliberately absent:
# they fire whether or not the user's task is finished.
_ALLOWED = {
    "idle_prompt",
    "permission_prompt",
    "agent_needs_input",
    "elicitation_dialog",
    "elicitation_url_dialog",
}
_QUIET_SUPPRESSIBLE = {"idle_prompt"}

_TITLES = {
    "idle_prompt": "Claude 等你",
    "permission_prompt": "Claude 需要审批",
    "agent_needs_input": "Claude 等你输入",
    "elicitation_dialog": "Claude 等你填写",
    "elicitation_url_dialog": "Claude 等你打开链接",
}


def _seen_key(session_id: str, kind: str, body: str) -> str:
    raw = f"{session_id}\0{kind}\0{body}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _send(event: dict, root: Path, cfg: dict, *, kind: str, title: str,
          body: str, quiet: bool) -> None:
    if quiet and config.in_quiet_hours(cfg):
        log.debug("notify: %s suppressed by quiet hours", kind)
        return
    window = config.notify_dedupe_seconds(cfg)
    if window > 0:
        key = _seen_key(event.get("session_id") or "", kind, body)
        now = time.time()
        if state.seen_recently(root, key, now=now, window=window):
            log.debug("notify: duplicate %s suppressed", kind)
            return
        state.mark_seen(root, key, now=now)
    channels.send_all(
        channels.Notification(
            title=title, body=body,
            project=event.get("cwd") or "",
            session_id=event.get("session_id") or "",
            event=kind,
        ),
        cfg,
    )


def handle(event: dict, root: Path, cfg: dict) -> int:
    name = event.get("hook_event_name") or ""

    if name == "PostToolUse" and event.get("tool_name") == TOOL:
        session_id = event.get("session_id") or ""
        if session_id:
            state.forget(session_id, root)
        return 0

    if name == "StopFailure":
        body = str(event.get("error") or "本轮以错误结束")[:300]
        _send(event, root, cfg, kind="StopFailure", title="Claude 失败",
              body=body, quiet=False)
        return 0

    if name == "Notification":
        kind = str(event.get("notification_type") or "")
        if kind not in _ALLOWED:
            log.debug("notify: ignoring notification_type %r", kind)
            return 0
        body = str(event.get("message") or event.get("title") or "")[:300]
        _send(event, root, cfg, kind=kind,
              title=_TITLES.get(kind, "Claude 通知"), body=body,
              quiet=kind in _QUIET_SUPPRESSIBLE)
        return 0

    # Stop fires per turn; it is not a completion signal. Nothing to send.
    return 0


def main() -> int:
    return hook_common.run_hook(handle)


if __name__ == "__main__":
    sys.exit(main())
