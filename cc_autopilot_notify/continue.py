"""Lockscreen autopilot continuation: suggestion matching and the round loop.

Everything about how a "next-step suggestion" is recognised and how a
continuation round is walked lives here; daemon.py only calls `handle` once
per tick (spec D1). Fail-closed throughout: anything unrecognised means no
keypress.
"""

from __future__ import annotations

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
