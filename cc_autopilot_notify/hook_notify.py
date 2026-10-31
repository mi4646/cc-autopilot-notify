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
import os
import sys
import time
from pathlib import Path

from . import channels, config, continuation, hook_common, state

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


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _with_turn_duration(event: dict, root: Path, cfg: dict, kind: str,
                        body: str) -> str | None:
    """Append the turn's duration, or return None to suppress a short turn.

    The start is written by UserPromptSubmit and read-and-cleared here. A
    missing start fails open: a session that began before this plugin was
    installed must not silently lose its completion notification.
    """
    threshold = config.notify_min_turn_seconds(cfg)
    if kind != "idle_prompt" or threshold <= 0:
        return body
    start = state.pop_turn_start(event.get("session_id") or "", root)
    if start is None:
        return body
    elapsed = max(0, int(time.time() - start))
    if elapsed < threshold:
        log.debug("notify: idle_prompt after %ss < %ss", elapsed, threshold)
        return None
    return f"{body} · 耗时 {_format_duration(elapsed)}"


def _send(event: dict, root: Path, cfg: dict, *, kind: str, title: str,
          body: str, quiet: bool) -> None:
    if quiet and config.in_quiet_hours(cfg):
        log.debug("notify: %s suppressed by quiet hours", kind)
        return
    body = _with_turn_duration(event, root, cfg, kind, body)
    if body is None:
        return
    title = channels.title_with_project(title, event.get("cwd") or "")
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


def _pane_id() -> int:
    try:
        return int(os.environ.get("PEBREL_PANE_ID") or 0)
    except ValueError:
        return 0


def _finished(event: dict, root: Path, *, failed: bool) -> None:
    session_id = event.get("session_id") or ""
    if not session_id:
        return
    try:
        state.set_finished(session_id, root, time.time(), _pane_id(),
                           failed=failed, cwd=str(event.get("cwd") or ""))
    except OSError as exc:
        log.warning("notify: cannot record finished turn (%s)", exc)


def _human_or_own_prompt(session_id: str, root: Path) -> None:
    """UserPromptSubmit: clear the finished marker only for a human prompt.

    Our own continuation presses Enter, which fires this hook too; the
    last_auto_submit marker (written before the press) tells them apart.
    A marker younger than 10s means our own submit — keep the round state.
    """
    if not session_id:
        return
    for pending in state.load_all(root):
        if pending.session_id != session_id:
            continue
        mark = pending.last_auto_submit
        if mark is not None and 0 <= time.time() - mark < \
                continuation.AUTO_SUBMIT_WINDOW_SECONDS:
            return
        state.clear_finished(session_id, root)
        return


def handle(event: dict, root: Path, cfg: dict) -> int:
    name = event.get("hook_event_name") or ""

    if name == "UserPromptSubmit":
        session_id = event.get("session_id") or ""
        if session_id:
            try:
                state.mark_turn_start(session_id, root, time.time())
                _human_or_own_prompt(session_id, root)
            except OSError as exc:
                log.warning("notify: cannot record turn start (%s)", exc)
        return 0

    if name == "PostToolUse" and event.get("tool_name") == TOOL:
        session_id = event.get("session_id") or ""
        if session_id:
            state.forget(session_id, root)
        return 0

    if name == "StopFailure":
        _finished(event, root, failed=True)
        body = str(event.get("error") or "本轮以错误结束")[:300]
        _send(event, root, cfg, kind="StopFailure", title="Claude 失败",
              body=body, quiet=False)
        return 0

    if name == "Notification":
        kind = str(event.get("notification_type") or "")
        if kind not in _ALLOWED:
            log.debug("notify: ignoring notification_type %r", kind)
            return 0
        body = str(event.get("message") or event.get("title") or "")
        body = config.notification_message_translations(cfg).get(body, body)[:300]
        _send(event, root, cfg, kind=kind,
              title=_TITLES.get(kind, "Claude 通知"), body=body,
              quiet=kind in _QUIET_SUPPRESSIBLE)
        return 0

    # Stop fires per turn; it is not a completion signal. It only marks the
    # turn ended so the continuation loop knows when to look for suggestions.
    _finished(event, root, failed=False)
    return 0


def main() -> int:
    return hook_common.run_hook(handle)


if __name__ == "__main__":
    sys.exit(main())
