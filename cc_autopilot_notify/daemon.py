"""The polling loop that answers unanswered questions.

Runs as a long-lived process. Each tick reads every pending question from
Pebrel, decides whether the user is still working on it, and submits an answer
when the idle threshold has passed.

All external effects live behind `Deps`, so a test never opens a socket or
touches Windows.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from pathlib import Path
from typing import Callable

from . import config, judge, pebrel_runtime, presence, state

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 10.0


@dataclasses.dataclass
class Deps:
    read: Callable[[state.Pending], dict]
    choose: Callable[[state.Pending, str, int], dict]
    foreground: Callable[[], bool | None]
    judge_fn: Callable[[dict], int]
    notify: Callable[[str], None]


def should_fire(pending: state.Pending, now: float, threshold: float) -> bool:
    return (now - pending.last_activity) >= threshold


def _threshold(cfg: dict, foreground: bool | None) -> int:
    # Unknown counts as background: answering sooner is the safe direction,
    # because the alternative is a session stalled until morning.
    if foreground is True:
        return config.foreground_seconds(cfg)
    return config.background_seconds(cfg)


def _tick_one(pending: state.Pending, now: float, root: Path, cfg: dict,
              deps: Deps, actions: list[str]) -> None:
    sid = pending.session_id
    try:
        reading = deps.read(pending)
    except Exception as exc:  # noqa: BLE001 - one bad pane must not stop the loop
        log.warning("daemon: read failed for %s (%s)", sid, exc)
        return

    prompt = reading.get("prompt")
    if prompt is None:
        actions.append(f"{sid}: answered or cancelled; clearing")
        state.forget(sid, root)
        return

    revision = reading.get("revision")
    if pending.revision is None:
        # First observation: establish the baseline, do not treat it as movement.
        state.touch(sid, root, when=pending.last_activity, revision=revision)
    elif revision is not None and revision != pending.revision:
        actions.append(f"{sid}: user is interacting; timer reset")
        state.touch(sid, root, when=now, revision=revision)
        return

    threshold = _threshold(cfg, deps.foreground())
    if not should_fire(pending, now, threshold):
        return

    try:
        options = pebrel_runtime.question_options(prompt)
    except pebrel_runtime.RuntimeError as exc:
        log.warning("daemon: cannot parse prompt for %s (%s)", sid, exc)
        deps.notify(f"Could not auto-answer: unrecognised question shape ({exc})")
        return

    question = (pending.questions or [{}])[0]
    question = {**question, "options": options or question.get("options", [])}
    choice = deps.judge_fn(question)
    prompt_id = reading.get("prompt_id") or ""

    for attempt in (1, 2):
        try:
            result = deps.choose(pending, prompt_id, choice)
        except Exception as exc:  # noqa: BLE001 - refusal and transport alike
            result = {"ok": False, "error": {"code": "raised", "message": str(exc)}}
        if result.get("ok", True) is not False:
            actions.append(f"{sid}: auto-answered option {choice}")
            state.touch(sid, root, when=now, revision=revision)
            deps.notify(f"Auto-answered: option {choice}")
            return
        log.info("daemon: choose refused for %s attempt %d (%s)",
                 sid, attempt, (result.get("error") or {}).get("code"))
        if attempt == 2:
            break
        # The user may have answered between our read and our choose.
        try:
            again = deps.read(pending)
        except Exception:  # noqa: BLE001
            break
        if again.get("prompt") is None:
            state.forget(sid, root)
            return
        prompt_id = again.get("prompt_id") or prompt_id

    failures = state.bump_choose_failures(sid, root)
    actions.append(f"{sid}: choose refused ({failures})")
    if failures >= 2:
        state.forget(sid, root)
        deps.notify("Gave up auto-answering this question; left it to the user")


def tick(now: float, root: Path, cfg: dict, deps: Deps) -> list[str]:
    """Run one iteration. Returns human-readable action lines."""
    root = Path(root)
    state.write_heartbeat(root, when=now)
    actions: list[str] = []
    for pending in state.load_all(root):
        _tick_one(pending, now, root, cfg, deps, actions)
    return actions


def build_deps(appdata: Path, cfg: dict) -> Deps:
    def do_read(pending: state.Pending) -> dict:
        return pebrel_runtime.read(appdata, pending.window_id, pending.pane_id,
                                   pending.session_id)

    def do_choose(pending: state.Pending, prompt_id: str, option: int) -> dict:
        return pebrel_runtime.choose(appdata, pending.window_id, pending.pane_id,
                                     pending.session_id, prompt_id, option)

    def do_judge(question: dict) -> int:
        return judge.pick(question, model=config.judge_model(cfg)).index

    from . import channels
    from .config import _merge  # local import keeps module import order simple

    def do_notify(message: str) -> None:
        channels.send_all(channels.Notification(title="cc-autopilot-notify",
                                                body=message), cfg)

    return Deps(read=do_read, choose=do_choose, foreground=presence.is_foreground,
                judge_fn=do_judge, notify=do_notify)


def run(root: Path, cfg: dict, appdata: Path | None = None) -> None:
    """Loop forever. Used by the systemd unit."""
    appdata = appdata or Path("/mnt/c/Users/Anonymous/AppData/Roaming")
    deps = build_deps(appdata, cfg)
    while True:
        try:
            tick(time.time(), Path(root), cfg, deps)
        except Exception:  # noqa: BLE001 - the daemon must outlive any one tick
            log.exception("daemon: tick raised")
        time.sleep(POLL_SECONDS)
