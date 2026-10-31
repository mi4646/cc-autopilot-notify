"""Shared plumbing for hook entry points.

Claude Code runs a hook by executing a command and piping the event as JSON on
stdin. A hook that exits non-zero can block the turn, so every failure inside a
hook is swallowed and turned into exit 0: losing a notification is bad, wedging
someone's session is worse.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)


def read_event() -> dict:
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        event = json.loads(raw)
    except ValueError:
        log.warning("hook: stdin was not JSON")
        return {}
    return event if isinstance(event, dict) else {}


def state_root() -> Path:
    import os

    override = os.environ.get("CC_AUTOPILOT_STATE")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "cc-autopilot-notify"


def _record_config_error(exc: Exception) -> None:
    """Leave a marker doctor can read: a hook's stderr is invisible to users."""
    try:
        root = state_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "config_error.json").write_text(
            json.dumps({"at": time.time(), "error": str(exc)}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def run_hook(handler) -> int:
    """Run `handler(event, root, cfg)`, never letting an exception escape."""
    from . import config

    try:
        event = read_event()
        cfg = config.load()
        if not config.enabled(cfg):
            return 0
        handler(event, state_root(), cfg)
    except config.ConfigError as exc:
        # Without a marker this silently turns every hook into a no-op.
        _record_config_error(exc)
        log.exception("hook: invalid config; hooks are disabled until it is fixed")
    except Exception:  # noqa: BLE001 - a hook must never wedge Claude Code
        log.exception("hook: unexpected failure")
    return 0
