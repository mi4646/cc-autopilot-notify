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
import os
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

# How long to let the panel redraw between steps of a multi-question answer.
# The whole sequence runs inside one tick so our own keypresses cannot be
# mistaken for the user changing the dialog.
ANSWER_STEP_SECONDS = 0.3
MAX_ANSWER_STEPS = 12
MAX_STALLS = 2

# One PowerShell sample covers foreground + idle and costs ~0.5-0.9s. The
# idle threshold is measured in tens of seconds, so a 5s cache loses nothing.
IDLE_SAMPLE_INTERVAL_SECONDS = 5.0
FAILURE_BACKOFF_SECONDS = 5.0
OUTAGE_ALERT_SECONDS = 60.0
OUTAGE_MESSAGE = ("空闲探测失败，自动应答已停摆。"
                  "运行 cc-autopilot-notify doctor 检查 PowerShell。")
RECOVERY_MESSAGE = "空闲探测已恢复，自动应答重新生效。"


@dataclasses.dataclass
class Deps:
    read: Callable[[state.Pending], dict]
    press: Callable[[state.Pending, str, int], dict]        # (pending, key, repeat)
    sample: Callable[[], "presence.Presence | None"]
    judge_fn: Callable[[dict], int]
    notify: Callable[[str], None]
    locate: Callable[[str], "tuple[int, int] | None"] = lambda sid: None
    judge_multi_fn: Callable[[dict], "tuple[int, ...]"] = lambda q: (0,)
    sleep: Callable[[float], None] = time.sleep


@dataclasses.dataclass
class PresenceGate:
    """Cache a successful sample for 5s; back off 5s after a failure.

    The cache exists because one sample costs ~0.7s and the loops runs every
    second. A failure stops auto-answering and, after 60s, tells the user
    once. Recovery is announced only if an outage was announced first.
    """
    last_at: float | None = None
    last_presence: "presence.Presence | None" = None
    fail_since: float | None = None
    backoff_until: float = 0.0
    notified: bool = False

    def get(self, now: float, deps: Deps) -> "presence.Presence | None":
        if now < self.backoff_until:
            return None
        if (self.last_presence is not None and self.last_at is not None
                and now - self.last_at < IDLE_SAMPLE_INTERVAL_SECONDS):
            return self.last_presence

        presence = deps.sample()
        if presence is None or presence.idle_seconds is None:
            if self.fail_since is None:
                self.fail_since = now
            elif not self.notified and now - self.fail_since >= OUTAGE_ALERT_SECONDS:
                deps.notify(OUTAGE_MESSAGE)
                self.notified = True
            self.backoff_until = now + FAILURE_BACKOFF_SECONDS
            self.last_presence = None
            self.last_at = now
            return None

        if self.notified:
            deps.notify(RECOVERY_MESSAGE)
        self.fail_since = None
        self.notified = False
        self.backoff_until = 0.0
        self.last_at = now
        self.last_presence = presence
        return presence

    def invalidate(self) -> None:
        """Drop the cache after a press: our own input reset system idle."""
        self.last_at = None
        self.last_presence = None


class _PressRefused(Exception):
    pass


class _JudgeFailed(Exception):
    pass


def _press_or_raise(pending: state.Pending, key: str, repeat: int,
                    deps: Deps) -> None:
    try:
        result = deps.press(pending, key, repeat)
    except Exception as exc:  # noqa: BLE001
        raise _PressRefused(key) from exc
    if result.get("ok", True) is False:
        raise _PressRefused(key)


def _answer_sequence(pending: state.Pending, reading: dict,
                     deps: Deps) -> tuple[dict, list[int]]:
    """Answer every visible question, confirm the review, return the last read.

    Runs to completion inside one tick: between steps the panel redraws, and
    re-reading here keeps those self-inflicted revision changes out of the
    "user is interacting" check.
    """
    questions = pending.questions or []
    needs_review = len(questions) > 1 or any(q.get("multiSelect") for q in questions)
    answered: set[int] = set()
    chosen: list[int] = []
    review_confirmed = False
    stalls = 0

    for _ in range(MAX_ANSWER_STEPS):
        text = str(reading.get("text") or "")

        if screen.is_review_screen(text):
            if review_confirmed:
                raise screen.ScreenMismatch("review page did not close after confirming")
            review_confirmed = True
            _press_or_raise(pending, "enter", 1, deps)
            deps.sleep(ANSWER_STEP_SECONDS)
            reading = deps.read(pending)
            continue

        match = screen.match_question(questions, text)
        if match is None:
            if len(answered) < len(questions):
                if reading.get("state") == "attention" and text.strip():
                    raise screen.ScreenMismatch(
                        "visible question is not in the recorded prompt")
                if stalls >= MAX_STALLS:
                    raise screen.ScreenMismatch(
                        "panel closed before every question was answered")
                stalls += 1
                deps.sleep(ANSWER_STEP_SECONDS)
                reading = deps.read(pending)
                continue
            return reading, chosen

        index, question = match
        if index in answered:
            if len(answered) >= len(questions) and not needs_review:
                return reading, chosen
            if stalls >= MAX_STALLS:
                raise screen.ScreenMismatch("panel never advanced past a question")
            stalls += 1
            if len(answered) < len(questions):
                _press_or_raise(pending, "tab", 1, deps)
            deps.sleep(ANSWER_STEP_SECONDS)
            reading = deps.read(pending)
            continue

        try:
            if question.get("multiSelect"):
                indices = list(deps.judge_multi_fn(question))
                presses = screen.plan_multi(question, text, indices)
            else:
                index_choice = deps.judge_fn(question)
                indices = [index_choice]
                presses = screen.plan(question, text, index_choice)
        except screen.ScreenMismatch:
            raise
        except Exception as exc:  # noqa: BLE001 - judge failure ends the record
            raise _JudgeFailed(str(exc)) from exc

        for key, repeat in presses:
            _press_or_raise(pending, key, repeat, deps)
        answered.add(index)
        chosen.extend(indices)
        stalls = 0
        deps.sleep(ANSWER_STEP_SECONDS)
        reading = deps.read(pending)

    raise screen.ScreenMismatch("too many steps answering the panel")


def _give_up(sid: str, root: Path, deps: "Deps", message: str) -> None:
    """Stop trying, tell the user once, then drop the record.

    Without the drop this would re-notify every tick forever, and each notify
    runs the whole channel fan-out inside the single-threaded loop.
    """
    state.forget(sid, root)
    log.warning("daemon: %s", message)
    deps.notify(message)


def _threshold(cfg: dict, presence_now: "presence.Presence") -> int:
    # Unknown foreground counts as background. It still requires the system
    # idle threshold, so a user who is typing is never answered early.
    if presence_now.foreground is True:
        return config.foreground_seconds(cfg)
    return config.background_seconds(cfg)


def _tick_one(pending: state.Pending, now: float, root: Path, cfg: dict,
              deps: Deps, actions: list[str], gate: PresenceGate,
              answered: list[bool]) -> None:
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

    presence_now = gate.get(now, deps)
    if presence_now is None or presence_now.idle_seconds is None:
        return
    if presence_now.idle_seconds < _threshold(cfg, presence_now):
        return
    if now - pending.appeared_at < int(cfg.get("min_duration_seconds", 30)):
        return

    # Answer every question in the panel, then confirm the review if one is
    # shown. The revision stored is the one read after the last keypress, so
    # the next tick does not mistake our own presses for user activity.
    answered[0] = True
    try:
        reading, chosen = _answer_sequence(pending, reading, deps)
    except screen.ScreenMismatch as exc:
        failures = state.bump_choose_failures(sid, root)
        log.info("daemon: screen mismatch for %s (%s)", sid, exc)
        if failures >= 3:
            _give_up(sid, root, deps, f"{sid}: giving up, screen never matched ({exc})")
        return
    except _PressRefused as exc:
        failures = state.bump_choose_failures(sid, root)
        log.info("daemon: press refused for %s attempt %s", sid, exc)
        if failures >= 2:
            state.forget(sid, root)
            deps.notify("Gave up auto-answering this question; left it to the user")
        return
    except _JudgeFailed as exc:
        log.warning("daemon: judge failed for %s (%s)", sid, exc)
        _give_up(sid, root, deps, f"{sid}: judge failed ({exc})")
        return
    except Exception as exc:  # noqa: BLE001 - a read outage keeps the record
        log.warning("daemon: answer failed for %s (%s)", sid, exc)
        return

    choice = ",".join(str(i) for i in chosen)
    actions.append(f"{sid}: auto-answered option {choice}")
    state.touch(sid, root, when=now, revision=reading.get("revision"))
    deps.notify(f"Auto-answered: option {choice}")


def tick(now: float, root: Path, cfg: dict, deps: Deps, *, pid: int | None = None,
         plugin_root: str | None = None, version: str | None = None,
         gate: PresenceGate | None = None) -> list[str]:
    """Run one iteration. Returns human-readable action lines."""
    root = Path(root)
    state.write_heartbeat(root, when=now, pid=pid,
                          plugin_root=plugin_root, version=version)
    actions: list[str] = []
    pendings = state.load_all(root)
    if not pendings:
        return actions
    gate = gate if gate is not None else PresenceGate()
    answered = [False]
    for pending in pendings:
        _tick_one(pending, now, root, cfg, deps, actions, gate, answered)
    if answered[0]:
        # Our own keypresses reset system idle; never reuse the sample taken
        # before those presses for the next tick.
        gate.invalidate()
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

    def do_judge_multi(question: dict) -> tuple[int, ...]:
        return judge.pick_multi(question, model=config.judge_model(cfg)).indices

    from . import channels

    def do_notify(message: str) -> None:
        channels.send_all(channels.Notification(title="cc-autopilot-notify",
                                                body=message), cfg)

    return Deps(read=do_read, press=do_press,
                sample=presence.sample,
                judge_fn=do_judge, judge_multi_fn=do_judge_multi,
                notify=do_notify, locate=do_locate)


def run(root: Path, cfg: dict, appdata: Path | None = None) -> None:
    """Loop forever. Used by the systemd unit."""
    from . import daemon_control

    root = Path(root)
    lock = daemon_control.acquire_lock(root)
    if lock is None:
        log.warning("daemon: another instance holds the lock; exiting")
        return
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(
        Path(__file__).resolve().parent.parent)
    version = daemon_control.plugin_version(plugin_root)
    appdata = appdata or Path("/mnt/c/Users/Anonymous/AppData/Roaming")
    deps = build_deps(appdata, cfg)
    gate = PresenceGate()
    ticks = 0
    while True:
        try:
            for action in tick(time.time(), root, cfg, deps, pid=os.getpid(),
                               plugin_root=plugin_root, version=version,
                               gate=gate):
                log.info("daemon: %s", action)
        except Exception:  # noqa: BLE001 - the daemon must outlive any one tick
            log.exception("daemon: tick raised")
        ticks += 1
        if ticks % 60 == 0 and not daemon_control.package_present(plugin_root):
            log.warning("daemon: package root %s is gone; exiting", plugin_root)
            return
        time.sleep(POLL_SECONDS)
