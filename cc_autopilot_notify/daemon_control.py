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


def should_spawn(root: Path, plugin_root: str, now: float | None = None) -> Decision:
    now = time.time() if now is None else now
    hb = state.read_heartbeat(root)
    if hb is None:
        return Decision("spawn", "no heartbeat")
    age = now - hb["at"]
    if age >= POLL_STALE_SECONDS:
        return Decision("spawn", f"heartbeat stale by {age:.0f}s")
    if hb.get("plugin_root") != plugin_root:
        return Decision("replace", "plugin root changed", stale_pid=hb.get("pid"))
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
    pid = (hb or {}).get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log.warning("daemon_control: cannot stop pid %s (%s)", pid, exc)
        return False
    return True


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    """Wait for `pid` to exit, so a replacement never overlaps the old daemon."""
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
