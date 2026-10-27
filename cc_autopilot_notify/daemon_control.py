"""Liveness decisions and process control for the polling daemon."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import state

log = logging.getLogger(__name__)

POLL_STALE_SECONDS = 15.0
LOCK_NAME = "daemon.lock"
LOG_NAME = "daemon.log"
LOG_MAX_BYTES = 5 * 1024 * 1024


@dataclasses.dataclass
class Decision:
    action: str            # skip | spawn | replace
    reason: str
    stale_pid: int | None = None


def valid_pid(value) -> int | None:
    """A pid worth returning or signalling: positive int, never bool/0/negative.

    ``bool`` is an ``int`` subclass and ``0`` means "the caller's process
    group" to ``os.kill``; both must be rejected before any liveness decision.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _proc_cmdline(pid: int) -> list[str] | None:
    """Linux/WSL only: the process's argv, or None when it cannot be read."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def owns_pid(pid: int) -> bool:
    """Confirm `pid` is our daemon before signalling it.

    A heartbeat can outlive its process, and the kernel may have handed the
    number to an unrelated process. Identity is read from /proc rather than
    trusted from the file; when it cannot be confirmed we fail closed and
    signal nothing.
    """
    if valid_pid(pid) is None:
        return False
    args = _proc_cmdline(pid)
    if not args:
        return False
    joined = " ".join(args)
    return "cc_autopilot_notify" in joined and "daemon" in args


def should_spawn(root: Path, plugin_root: str, now: float | None = None) -> Decision:
    now = time.time() if now is None else now
    hb = state.read_heartbeat(root)
    if hb is None:
        return Decision("spawn", "no heartbeat")
    age = now - hb["at"]
    if age >= POLL_STALE_SECONDS:
        return Decision("spawn", f"heartbeat stale by {age:.0f}s")
    pid = valid_pid(hb.get("pid"))
    if hb.get("plugin_root") != plugin_root:
        # Only a heartbeat that names its process can be replaced safely. A
        # legacy (or pid-less) daemon cannot be stopped, and spawning anyway
        # would leave two loops answering the same questions.
        if pid is not None:
            return Decision("replace", "plugin root changed", stale_pid=pid)
        return Decision(
            "skip", "unmanaged daemon alive with no pid; run /cc-autopilot-notify:migrate")
    return Decision("skip", f"daemon alive ({age:.0f}s old)")


def acquire_lock(root: Path):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / LOCK_NAME, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def start(root: Path, plugin_root: str) -> int:
    root = Path(root)
    # A fresh machine has no state root yet; the log cannot be opened without it.
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / LOG_NAME
    if log_path.exists() and log_path.stat().st_size > LOG_MAX_BYTES:
        os.truncate(log_path, 0)
    env = dict(os.environ, PYTHONPATH=plugin_root)
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "cc_autopilot_notify.cli", "daemon"],
            cwd=plugin_root, env=env, stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=log_file,
            start_new_session=True, close_fds=True)
    return proc.pid


def stop(root: Path) -> bool:
    hb = state.read_heartbeat(root)
    pid = valid_pid((hb or {}).get("pid"))
    if pid is None:
        return False
    if not owns_pid(pid):
        log.warning("daemon_control: refusing to signal pid %s: "
                    "process identity not confirmed", pid)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log.warning("daemon_control: cannot stop pid %s (%s)", pid, exc)
        return False
    return True


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    """Wait for `pid` to exit, so a replacement never overlaps the old daemon."""
    pid = valid_pid(pid)
    if pid is None:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def plugin_version(plugin_root: str) -> str | None:
    try:
        data = json.loads((Path(plugin_root) / ".claude-plugin" / "plugin.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def package_present(plugin_root: str) -> bool:
    return (Path(plugin_root) / "cc_autopilot_notify").is_dir()
