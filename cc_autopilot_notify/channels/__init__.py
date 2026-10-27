"""Notification dispatch.

Every channel is best-effort. A failure is reported to the caller and logged;
it never raises out of `send_all`, because a broken webhook must not stop a
Claude Code turn.
"""

from __future__ import annotations

import dataclasses
import logging

from .. import config as _config
from . import feishu, pebrel, terminal

log = logging.getLogger(__name__)

__all__ = ["Notification", "send_all", "feishu", "terminal", "pebrel"]


@dataclasses.dataclass
class Notification:
    title: str
    body: str
    project: str = ""
    session_id: str = ""
    event: str = ""


# Fixed order: the cheapest channel first, so a slow network channel cannot
# delay the one the user is most likely staring at.
_ORDER = ("terminal", "pebrel", "feishu")


def send_all(n: Notification, cfg: dict) -> list[str]:
    """Send to every enabled channel. Returns the names that failed."""
    channels_cfg = cfg.get("channels", {})

    failed: list[str] = []
    for name in _ORDER:
        spec = channels_cfg.get(name) or {}
        if not spec.get("enabled"):
            continue
        module = {"terminal": terminal, "pebrel": pebrel, "feishu": feishu}[name]
        try:
            if name == "feishu":
                webhook = _config.feishu_webhook(cfg)
                if not webhook:
                    log.debug("channels: feishu enabled but no webhook configured")
                    continue
                module.send(n, webhook)
            else:
                module.send(n)
        except Exception as exc:  # noqa: BLE001 - best effort by design
            log.warning("channels: %s failed (%s)", name, exc)
            failed.append(name)
    return failed
