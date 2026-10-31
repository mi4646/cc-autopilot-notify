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

from . import config, continuation, judge, pebrel_runtime, presence, screen, state
from .channels import Notification, title_with_project

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
    judge_fn: Callable[[dict], "judge.Decision"]
    notify: Callable[[object], None]  # str or channels.Notification
    locate: Callable[[str], "tuple[int, int] | None"] = lambda sid: None
    agents: Callable[[], list] = lambda: []
    judge_multi_fn: Callable[[dict], "judge.Decision"] = lambda q: (0,)
    sleep: Callable[[float], None] = time.sleep
    paste: Callable[[state.Pending, str], dict] = lambda p, text: {"ok": True}


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
        if key == "paste":
            result = deps.paste(pending, str(repeat))
        else:
            result = deps.press(pending, key, repeat)
    except Exception as exc:  # noqa: BLE001
        raise _PressRefused(key) from exc
    if result.get("ok", True) is False:
        raise _PressRefused(key)


def _reason_text(decision: "judge.Decision") -> str:
    if getattr(decision, "delegate", False):
        return "无推荐，代答『按照你的建议来』"
    if decision.fallback:
        return "无推荐，选了第一项"
    if "marked" in decision.reason:
        return "Claude 标记推荐"
    return "模型判断"


def _answer_summary(answers: list[dict]) -> str:
    """One line per question: what was chosen and why, for the Feishu record."""
    lines = []
    for i, answer in enumerate(answers, 1):
        lines.append(f"Q{i}: {answer['question']}\n→ {'、'.join(answer['labels'])}"
                     f"（{_reason_text(answer['decision'])}）")
    return "\n\n".join(lines)


def _answer_sequence(pending: state.Pending, reading: dict,
                     deps: Deps) -> tuple[dict, list[int], list[dict]]:
    """Answer every visible question, confirm the review, return the last read.

    Runs to completion inside one tick: between steps the panel redraws, and
    re-reading here keeps those self-inflicted revision changes out of the
    "user is interacting" check. Returns the readings plus one record per
    question: question text, chosen label(s), and the judge's decision.
    """
    questions = pending.questions or []
    needs_review = len(questions) > 1 or any(q.get("multiSelect") for q in questions)
    answered: set[int] = set()
    chosen: list[int] = []
    answers: list[dict] = []
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
            return reading, chosen, answers

        index, question = match
        if index in answered:
            if len(answered) >= len(questions) and not needs_review:
                return reading, chosen, answers
            if stalls >= MAX_STALLS:
                raise screen.ScreenMismatch("panel never advanced past a question")
            stalls += 1
            if len(answered) < len(questions):
                _press_or_raise(pending, "tab", 1, deps)
            deps.sleep(ANSWER_STEP_SECONDS)
            reading = deps.read(pending)
            continue

        labels: list[str]
        try:
            if question.get("multiSelect"):
                decision = deps.judge_multi_fn(question)
                if decision.delegate:
                    indices = []
                    labels = [judge.DELEGATE_TEXT]
                    presses = screen.plan_free_text(question, text,
                                                    judge.DELEGATE_TEXT)
                else:
                    indices = list(decision.indices)
                    labels = [str(x) for x in decision.labels]
                    presses = screen.plan_multi(question, text, indices)
            else:
                decision = deps.judge_fn(question)
                indices = [decision.index]
                labels = [decision.label]
                presses = screen.plan(question, text, decision.index)
        except screen.ScreenMismatch:
            raise
        except Exception as exc:  # noqa: BLE001 - judge failure ends the record
            raise _JudgeFailed(str(exc)) from exc

        for key, repeat in presses:
            _press_or_raise(pending, key, repeat, deps)
        answered.add(index)
        chosen.extend(indices)
        answers.append({"question": str(question.get("question") or ""),
                        "labels": labels, "decision": decision})
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

    if pending.finished_at is not None:
        actions.extend(continuation.handle(
            pending, now, root, cfg, _continue_deps(deps, gate, now)))
        return

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

    # No revision gate here. The pane's status line can redraw every second
    # (timer/spinner), which changes the 60-line tail without any user input.
    # System idle is the authoritative "is the user here" signal: any real
    # keypress or mouse movement resets it to zero.

    presence_now = gate.get(now, deps)
    if presence_now is None or presence_now.idle_seconds is None:
        return
    threshold = _threshold(cfg, presence_now)
    if presence_now.idle_seconds < threshold:
        return
    if now - pending.appeared_at < int(cfg.get("min_duration_seconds", 30)):
        return
    log.info("daemon: %s: answering idle=%.1fs foreground=%s threshold=%ss",
             sid, presence_now.idle_seconds, presence_now.foreground, threshold)

    # Answer every question in the panel, then confirm the review if one is
    # shown. The revision stored is the one read after the last keypress, so
    # the next tick does not mistake our own presses for user activity.
    answered[0] = True
    try:
        reading, chosen, answers = _answer_sequence(pending, reading, deps)
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
    summary = _answer_summary(answers)
    deps.notify(Notification(
        title=title_with_project("自动应答", pending.cwd),
        body=summary,
        project=pending.cwd,
        session_id=sid,
        event="auto_answer",
        question_count=len(answers),
        created_at=now,
    ))


def tick(now: float, root: Path, cfg: dict, deps: Deps, *, pid: int | None = None,
         plugin_root: str | None = None, version: str | None = None,
         gate: PresenceGate | None = None) -> list[str]:
    """Run one iteration. Returns human-readable action lines."""
    root = Path(root)
    state.write_heartbeat(root, when=now, pid=pid,
                          plugin_root=plugin_root, version=version)
    actions: list[str] = []
    state.purge_stale(root, now)
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


def _continue_deps(deps: Deps, gate: "PresenceGate", now: float) -> "continuation.ContinueDeps":
    def owner_ok(pending: state.Pending) -> bool:
        """The record's session must still own the pane we are about to press
        (codex 复核 #16: panes get reused). Lookup is by pane in the agents
        snapshot: `locate` matches by session and returns None for agent-start
        panes, which would kill continuation for exactly the sessions the
        answering path already tolerates (eng review I4)."""
        try:
            for agent in deps.agents():
                if int(agent.get("pane_id", 0)) == pending.pane_id:
                    info = agent.get("agent") or {}
                    return info.get("session_id") in (None, pending.session_id)
        except Exception:  # noqa: BLE001 - an outage must not press keys
            return False
        return False

    return continuation.ContinueDeps(
        read=deps.read, press=deps.press,
        cached_presence=lambda: gate.get(now, deps),
        fresh_presence=deps.sample,  # bypasses the 5s gate cache: fresh lock
        pane_owner_ok=owner_ok, agents=deps.agents,
        notify=deps.notify, sleep=deps.sleep)


def build_deps(appdata: Path, cfg: dict) -> Deps:
    def do_read(pending: state.Pending) -> dict:
        # The dialog gate and the screen text both come from the pane, not from
        # conversation.read: `prompt` never carries Claude Code's question.
        return pebrel_runtime.dialog(appdata, pending.pane_id)

    def do_press(pending: state.Pending, key: str, repeat: int) -> dict:
        return pebrel_runtime.press(appdata, pending.pane_id, key, repeat)

    def do_paste(pending: state.Pending, text: str) -> dict:
        return pebrel_runtime.paste(appdata, pending.pane_id, text)

    def do_locate(session_id: str):
        return pebrel_runtime.locate(appdata, session_id)

    def do_agents() -> list:
        return pebrel_runtime.agents(appdata)

    def do_judge(question: dict) -> "judge.Decision":
        return judge.pick(question, model=config.judge_model(cfg))

    def do_judge_multi(question: dict) -> "judge.Decision":
        return judge.pick_multi(question, model=config.judge_model(cfg))

    from . import channels

    def do_notify(message: "str | channels.Notification") -> None:
        if not isinstance(message, channels.Notification):
            message = channels.Notification(title="cc-autopilot-notify",
                                            body=str(message))
        channels.send_all(message, cfg)

    return Deps(read=do_read, press=do_press,
                sample=presence.sample,
                judge_fn=do_judge, judge_multi_fn=do_judge_multi,
                notify=do_notify, locate=do_locate, agents=do_agents,
                paste=do_paste)


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
