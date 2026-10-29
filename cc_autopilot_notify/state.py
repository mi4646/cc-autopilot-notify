"""Pending-question records and daemon heartbeat.

One JSON file per Claude Code session. Session ids come from Claude Code and
must never be trusted as path components, so they are sanitised before use.
Writes are atomic (temp file + os.replace) and serialised with flock.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import math
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_HEARTBEAT = "heartbeat"
_LOCK = ".lock"


@dataclasses.dataclass
class Pending:
    session_id: str
    window_id: int
    pane_id: int
    questions: list[dict]
    appeared_at: float
    last_activity: float
    revision: str | None = None
    choose_failures: int = 0
    cwd: str = ""


def _sanitise(session_id: str) -> str:
    return _UNSAFE.sub("_", session_id or "unknown")[:96] or "unknown"


def _record_path(session_id: str, root: Path) -> Path:
    return root / f"pending_{_sanitise(session_id)}.json"


@contextmanager
def _locked(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with open(root / _LOCK, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(pending: Pending, root: Path) -> None:
    root = Path(root)
    with _locked(root):
        _atomic_write(_record_path(pending.session_id, root),
                      dataclasses.asdict(pending))


def load_all(root: Path) -> list[Pending]:
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[Pending] = []
    for path in sorted(root.glob("pending_*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            out.append(Pending(**raw))
        except (OSError, ValueError, TypeError) as exc:
            log.warning("state: skipping unreadable record %s (%s)", path.name, exc)
    return out


def forget(session_id: str, root: Path) -> None:
    root = Path(root)
    if not root.is_dir():
        return
    with _locked(root):
        try:
            _record_path(session_id, root).unlink()
        except FileNotFoundError:
            pass


def _mutate(session_id: str, root: Path, change) -> Pending | None:
    """Read-modify-write one record under the lock. Returns None if absent."""
    root = Path(root)
    with _locked(root):
        path = _record_path(session_id, root)
        try:
            pending = Pending(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None
        change(pending)
        _atomic_write(path, dataclasses.asdict(pending))
        return pending


def touch(session_id: str, root: Path, when: float, revision: str | None = None) -> None:
    def change(p: Pending) -> None:
        p.last_activity = when
        if revision is not None:
            p.revision = revision
    _mutate(session_id, root, change)


def bump_choose_failures(session_id: str, root: Path) -> int:
    counter = {"n": 0}

    def change(p: Pending) -> None:
        p.choose_failures += 1
        counter["n"] = p.choose_failures

    _mutate(session_id, root, change)
    return counter["n"]


def write_heartbeat(root: Path, when: float, *, pid: int | None = None,
                    plugin_root: str | None = None,
                    version: str | None = None) -> None:
    root = Path(root)
    with _locked(root):
        _atomic_write(root / f"{_HEARTBEAT}.json",
                      {"at": float(when), "pid": pid,
                       "plugin_root": plugin_root, "version": version})


def read_heartbeat(root: Path) -> dict | None:
    path = Path(root) / f"{_HEARTBEAT}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("at"), (int, float)):
            return None
        at = float(data["at"])
    except (OSError, ValueError, OverflowError, TypeError):
        return None
    return {"at": at, "pid": data.get("pid"),
            "plugin_root": data.get("plugin_root"),
            "version": data.get("version")}


def heartbeat_age(root: Path, now: float) -> float | None:
    path = Path(root) / f"{_HEARTBEAT}.json"
    try:
        at = float(json.loads(path.read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        # OverflowError covers a JSON integer too large for a float (e.g. 10**400).
        return None
    if not math.isfinite(at):
        # JSON's Infinity/NaN cannot be a real heartbeat time.
        return None
    return now - at


_SEEN = "notify_seen.json"


def seen_recently(root: Path, key: str, now: float, window: float) -> bool:
    """True when `key` was marked within `window` seconds."""
    if window <= 0:
        return False
    try:
        data = json.loads((Path(root) / _SEEN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    last = data.get(key) if isinstance(data, dict) else None
    return isinstance(last, (int, float)) and now - last < window


def mark_seen(root: Path, key: str, now: float) -> None:
    root = Path(root)
    with _locked(root):
        path = root / _SEEN
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        cutoff = now - 86400  # one day of history is plenty for dedupe
        data = {k: v for k, v in data.items()
                if isinstance(v, (int, float)) and v >= cutoff}
        data[key] = float(now)
        _atomic_write(path, data)


_TURN_START = "turn_start.json"


def mark_turn_start(session_id: str, root: Path, now: float) -> None:
    """Remember when the current turn began, for the min_turn_seconds gate."""
    root = Path(root)
    with _locked(root):
        path = root / _TURN_START
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        cutoff = now - 86400
        data = {k: v for k, v in data.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                and v >= cutoff}
        data[_sanitise(session_id)] = float(now)
        _atomic_write(path, data)


def pop_turn_start(session_id: str, root: Path) -> float | None:
    """Read and clear the turn start, so a stale value cannot gate the next turn."""
    root = Path(root)
    with _locked(root):
        path = root / _TURN_START
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.pop(_sanitise(session_id), None)
        try:
            _atomic_write(path, data)
        except OSError:
            pass
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
