"""Notification dispatch.

Every channel is best-effort. A failure is reported to the caller and logged;
it never raises out of `send_all`, because a broken webhook must not stop a
Claude Code turn.
"""

from __future__ import annotations

import dataclasses
import logging

from .. import config as _config
from ..project import project_name
from . import feishu

log = logging.getLogger(__name__)

__all__ = ["Notification", "send_all", "feishu", "title_with_project"]


@dataclasses.dataclass
class Notification:
    title: str
    body: str
    project: str = ""
    session_id: str = ""
    event: str = ""
    question_count: int = 0
    options: list[str] = dataclasses.field(default_factory=list)
    created_at: float = 0.0


def first_line(text: str, limit: int = 60) -> str:
    """The suggestion text as a card shows it: first line, hard-truncated.

    The full text lives in the terminal/session; the card only needs to be
    scannable on a phone. Blank input (matcher gave nothing readable) gets a
    visible placeholder instead of an empty body.
    """
    line = (text or "").strip().splitlines()
    first = line[0].strip() if line else ""
    if not first:
        return "(无文本建议)"
    if len(first) > limit:
        return first[:limit] + "…"
    return first


def title_with_project(title: str, cwd: str) -> str:
    """Prefix the event title with the project name when the cwd is known.

    The notification centre shows the title alone, so the project has to be in
    it; an empty cwd adds "unknown · " noise and is left out.
    """
    if not (cwd or "").strip():
        return title
    return f"{project_name(cwd)} · {title}"


# Fixed order: the cheapest channel first, so a slow network channel cannot
# delay the one the user is most likely staring at.
# Pebrel owns the Windows toast natively; our scope here is Feishu plus
# unattended auto-answer. Terminal toasts were dropped at the owner's call:
# too many popups alongside Pebrel's own.
_ORDER = ("feishu",)


def send_all(n: Notification, cfg: dict) -> list[str]:
    """Send to every enabled channel. Returns the names that failed."""
    channels_cfg = cfg.get("channels", {})

    failed: list[str] = []
    for name in _ORDER:
        spec = channels_cfg.get(name) or {}
        if not spec.get("enabled"):
            continue
        module = {"feishu": feishu}.get(name)
        if module is None:
            continue
        try:
            if name == "feishu":
                webhook = _config.feishu_webhook(cfg)
                if not webhook:
                    log.debug("channels: feishu enabled but no webhook configured")
                    continue
                module.send(n, webhook)
        except Exception as exc:  # noqa: BLE001 - best effort by design
            log.warning("channels: %s failed (%s)", name, exc)
            failed.append(name)
    return failed
