"""Lockscreen autopilot continuation: suggestion matching and the round loop.

Everything about how a "next-step suggestion" is recognised and how a
continuation round is walked lives here; daemon.py only calls `handle` once
per tick (spec D1). Fail-closed throughout: anything unrecognised means no
keypress.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from pathlib import Path
from typing import Callable

from . import channels, config, presence, state

log = logging.getLogger(__name__)

# The pane tail separates UI regions with long box-drawing rules; the CURRENT
# input box sits between the last two. A suggestion renders as plain text on
# the `❯ ` line inside it (spike 2026-10-31; the lock gate is what tells a
# suggestion from user-typed text — a locked console cannot type).
_SEP_RE = re.compile("─{10,}")

PRESS_STEP_SECONDS = 0.3
PRESSED_SETTLE_SECONDS = 15.0
STAGE_WAIT_MARGIN_SECONDS = 30.0
AUTO_SUBMIT_WINDOW_SECONDS = 10.0


def match_suggestion(text: str) -> str | None:
    """The suggestion text shown in the pane's current input box, or None."""
    if not text:
        return None
    parts = _SEP_RE.split(text)
    if len(parts) < 2:
        return None
    for line in parts[-2].splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("❯"):
            continue
        suggestion = stripped[1:].strip()
        return suggestion or None
    return None


def _stage_deadline(pending: state.Pending, cfg: dict) -> float:
    return (pending.finished_at or 0.0) + config.suggest_wait_seconds(cfg) \
        + STAGE_WAIT_MARGIN_SECONDS


@dataclasses.dataclass
class ContinueDeps:
    read: Callable[[state.Pending], dict]
    press: Callable[[state.Pending, str, int], dict]
    fresh_presence: Callable[[], "presence.Presence | None"]
    pane_owner_ok: Callable[[state.Pending], bool]
    notify: Callable[[object], None]
    sleep: Callable[[float], None]


class PressRefused(Exception):
    pass


def _press_or_raise(pending: state.Pending, key: str, repeat: int,
                    deps: ContinueDeps) -> None:
    try:
        result = deps.press(pending, key, repeat)
    except Exception as exc:  # noqa: BLE001
        raise PressRefused(key) from exc
    if result.get("ok", True) is False:
        raise PressRefused(key)


def _card(pending: state.Pending, now: float, event: str, title: str,
          body: str) -> "channels.Notification":
    return channels.Notification(
        title=channels.title_with_project(title, pending.cwd), body=body,
        project=pending.cwd, session_id=pending.session_id, event=event,
        created_at=now)


def handle(pending: state.Pending, now: float, root: Path, cfg: dict,
           deps: ContinueDeps) -> list[str]:
    """One continuation decision for a finished session. Spec §3.3; the lock
    gate is the unattended switch — anything uncertain here means no keys."""
    if not config.continue_enabled(cfg):
        return []

    fresh = deps.fresh_presence()
    if fresh is None or fresh.locked is not True:
        return []

    try:
        reading = deps.read(pending)
    except Exception as exc:  # noqa: BLE001 - one bad pane must not stop the loop
        log.warning("continuation: read failed for %s (%s)", pending.session_id, exc)
        return []
    if pending.questions or reading.get("state") != "finished":
        return []  # an open question outranks continuation; a running turn is not ours

    suggestion = match_suggestion(str(reading.get("text") or ""))
    shadow = config.shadow_mode(cfg)

    if suggestion and shadow:
        log.info("continuation: SHADOW would press tab+enter for %s: %s",
                 pending.session_id, channels.first_line(suggestion))
        return [f"{pending.session_id}: SHADOW would continue: "
                f"{channels.first_line(suggestion)}"]
    if shadow:
        return []

    if not suggestion:
        if "tab" in str(reading.get("text") or "").lower():
            state.bump_canary(root, now)
        if (not pending.stage_card_sent and pending.finished_at is not None
                and now >= _stage_deadline(pending, cfg)):
            state.mark_stage_card(pending.session_id, root, now)
            deps.notify(_card(pending, now, "continue_stage", "续跑告一段落",
                              f"本次共自动续跑 {pending.continue_count} 条，无更多建议"))
            return [f"{pending.session_id}: continuation stage complete "
                    f"({pending.continue_count} runs)"]
        return []

    if (pending.pressed_at is not None
            and now - pending.pressed_at < PRESSED_SETTLE_SECONDS):
        return []  # UI may not have caught up; never double-press
    if not deps.pane_owner_ok(pending):
        return []

    cap = config.failure_continues(cfg) if pending.finished_failed \
        else config.max_continues(cfg)
    used = pending.failure_count if pending.finished_failed \
        else pending.continue_count
    if used >= cap:
        if not pending.stage_card_sent:
            state.mark_stage_card(pending.session_id, root, now)
            deps.notify(_card(pending, now, "continue_capped", "续跑已达上限",
                              f"已自动执行 {used} 条，剩余建议留待主人处理"))
            return [f"{pending.session_id}: continuation cap reached ({used}/{cap})"]
        return []

    # The marker MUST hit disk before the Enter key: our own submit fires
    # UserPromptSubmit, and the hook must see the marker to leave the round
    # state alone (codex Critical #1).
    state.mark_auto_submit(pending.session_id, root, now)
    state.mark_pressed(pending.session_id, root, now)
    count = state.bump_continue(pending.session_id, root,
                                failed=pending.finished_failed)
    try:
        _press_or_raise(pending, "tab", 1, deps)
        deps.sleep(PRESS_STEP_SECONDS)
        _press_or_raise(pending, "enter", 1, deps)
    except PressRefused as exc:
        failures = state.bump_choose_failures(pending.session_id, root)
        log.info("continuation: press refused for %s (%s)", pending.session_id, exc)
        if failures >= 2:
            state.clear_finished(pending.session_id, root)
            deps.notify(channels.Notification(
                title="cc-autopilot-notify",
                body=f"{pending.session_id}: 续跑按键连续被拒，放弃本轮，留待主人处理"))
        return []

    label = f"失败重试 {count}/{cap}" if pending.finished_failed \
        else f"第 {count} 次 / 上限 {cap}"
    title = f"自动续跑（失败重试）{count}/{cap}" if pending.finished_failed \
        else f"自动续跑 {count}/{cap}"
    deps.notify(_card(pending, now, "auto_continue", title,
                      f"→ {channels.first_line(suggestion)}\n{label}"))
    return [f"{pending.session_id}: auto-continued ({count}/{cap}): "
            f"{channels.first_line(suggestion)}"]
