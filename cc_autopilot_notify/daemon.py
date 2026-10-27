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

from . import config, judge, pebrel_runtime, presence, screen, state

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 10.0

# A session that never shows up in any Pebrel pane cannot be answered, and the
# record is retried every second until it is dropped. Give it the time we would
# otherwise have spent waiting to answer, then forget it: keeping it costs one
# runtime round trip per second, forever.
LOCATE_GRACE_SECONDS = 300.0

# A failing locate is different: it may be a transient runtime outage, so the
# record is kept and only the log is throttled.
LOCATE_WARN_EVERY_SECONDS = 600.0
_locate_warned_at: dict[str, float] = {}


@dataclasses.dataclass
class Deps:
    read: Callable[[state.Pending], dict]
    press: Callable[[state.Pending, str, int], dict]        # (pending, key, repeat)
    foreground: Callable[[], bool | None]
    judge_fn: Callable[[dict], int]
    notify: Callable[[str], None]
    locate: Callable[[str], "tuple[int, int] | None"] = lambda sid: None


def should_fire(pending: state.Pending, now: float, threshold: float) -> bool:
    return (now - pending.last_activity) >= threshold


def _give_up(sid: str, root: Path, deps: "Deps", message: str) -> None:
    """Stop trying, tell the user once, then drop the record.

    Without the drop this would re-notify every tick forever, and each notify
    runs the whole channel fan-out inside the single-threaded loop.
    """
    state.forget(sid, root)
    log.warning("daemon: %s", message)
    deps.notify(message)


def _threshold(cfg: dict, foreground: bool | None) -> int:
    # Unknown counts as background: answering sooner is the safe direction,
    # because the alternative is a session stalled until morning.
    if foreground is True:
        return config.foreground_seconds(cfg)
    return config.background_seconds(cfg)


def _tick_one(pending: state.Pending, now: float, root: Path, cfg: dict,
              deps: Deps, actions: list[str]) -> None:
    sid = pending.session_id

    # The hook records PEBREL_PANE_ID directly. Only fall back to Pebrel's own
    # session association when that is missing: `agents.list` reports
    # session_id=None for panes started by `agent-start`, so depending on it
    # would make auto-answer silently skip those sessions.
    if not pending.pane_id:
        try:
            located = deps.locate(sid)
        except Exception as exc:  # noqa: BLE001
            # A runtime outage must not spam a line a second: log once, then at
            # most every LOCATE_WARN_EVERY_SECONDS while it persists.
            last = _locate_warned_at.get(sid)
            if last is None or now - last >= LOCATE_WARN_EVERY_SECONDS:
                _locate_warned_at[sid] = now
                log.warning("daemon: cannot locate a pane for %s (%s)", sid, exc)
            return
        if located is None:
            # No pane means the session is not running under Pebrel (a /tmp
            # session, a dead terminal). That will not change, so waiting past
            # the grace period is pure waste: drop the record once.
            if now - pending.appeared_at >= LOCATE_GRACE_SECONDS:
                state.forget(sid, root)
                log.info("daemon: %s never appeared in any Pebrel pane; dropping", sid)
            return
        pending.window_id, pending.pane_id = located
        state.record(pending, root)

    try:
        reading = deps.read(pending)
    except Exception as exc:  # noqa: BLE001 - one bad pane must not stop the loop
        log.warning("daemon: read failed for %s (%s)", sid, exc)
        return

    # The dialog gate. `conversation.read.prompt` never carries Claude Code's
    # AskUserQuestion (130s live probe: 0 hits even at attention), so Presence
    # of the dialog is what `task_state == "attention"` reports. Anything else
    # means the turn is running, finished, or the pane is gone: not our moment.
    if reading.get("state") != "attention":
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

    # Which option: the hook's structured data (judge), never the screen.
    question = (pending.questions or [{}])[0]
    try:
        choice = deps.judge_fn(question)
    except Exception as exc:  # noqa: BLE001 - one bad question must not stop the loop
        log.warning("daemon: judge failed for %s (%s)", sid, exc)
        _give_up(sid, root, deps, f"{sid}: judge failed ({exc})")
        return

    # Screen agreement: the recorded labels must appear as numbered rows with a
    # visible cursor, or we refuse. This is what stops a key press from landing
    # on a different dialog than the question we recorded.
    try:
        presses = screen.plan(question, str(reading.get("text") or ""), choice)
    except screen.ScreenMismatch as exc:
        # Unlike the prompt-shape case this is recoverable: a redraw, a second
        # question, a rendering delay. Do not drop the record; report once.
        failures = state.bump_choose_failures(sid, root)
        log.info("daemon: screen mismatch for %s (%s)", sid, exc)
        if failures >= 3:
            _give_up(sid, root, deps, f"{sid}: giving up, screen never matched ({exc})")
        return

    for key, repeat in presses:
        try:
            result = deps.press(pending, key, repeat)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": {"code": "raised", "message": str(exc)}}
        if result.get("ok", True) is False:
            failures = state.bump_choose_failures(sid, root)
            log.info("daemon: press refused for %s attempt %s (%s)",
                     sid, key, (result.get("error") or {}).get("code"))
            if failures >= 2:
                state.forget(sid, root)
                deps.notify("Gave up auto-answering this question; left it to the user")
            return

    actions.append(f"{sid}: auto-answered option {choice}")
    state.touch(sid, root, when=now, revision=revision)
    deps.notify(f"Auto-answered: option {choice}")


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
        # The dialog gate and the screen text both come from the pane, not from
        # conversation.read: `prompt` never carries Claude Code's question.
        return pebrel_runtime.dialog(appdata, pending.pane_id)

    def do_press(pending: state.Pending, key: str, repeat: int) -> dict:
        return pebrel_runtime.press(appdata, pending.pane_id, key, repeat)

    def do_locate(session_id: str):
        return pebrel_runtime.locate(appdata, session_id)

    def do_judge(question: dict) -> int:
        return judge.pick(question, model=config.judge_model(cfg)).index

    from . import channels

    def do_notify(message: str) -> None:
        channels.send_all(channels.Notification(title="cc-autopilot-notify",
                                                body=message), cfg)

    return Deps(read=do_read, press=do_press,
                foreground=presence.is_foreground,
                judge_fn=do_judge, notify=do_notify, locate=do_locate)


def run(root: Path, cfg: dict, appdata: Path | None = None) -> None:
    """Loop forever. Used by the systemd unit."""
    appdata = appdata or Path("/mnt/c/Users/Anonymous/AppData/Roaming")
    deps = build_deps(appdata, cfg)
    while True:
        try:
            for action in tick(time.time(), Path(root), cfg, deps):
                log.info("daemon: %s", action)
        except Exception:  # noqa: BLE001 - the daemon must outlive any one tick
            log.exception("daemon: tick raised")
        time.sleep(POLL_SECONDS)
