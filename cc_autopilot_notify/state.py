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
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("at"), (int, float)):
        return None
    return {"at": float(data["at"]), "pid": data.get("pid"),
            "plugin_root": data.get("plugin_root"),
            "version": data.get("version")}


def heartbeat_age(root: Path, now: float) -> float | None:
    path = Path(root) / f"{_HEARTBEAT}.json"
    try:
        at = float(json.loads(path.read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return now - at
