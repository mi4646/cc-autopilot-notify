"""Pebrel native notification.

Pebrel already owns the Windows toast and its click-to-focus behaviour, so this
channel delegates rather than reimplementing a toast stack. When Pebrel is not
the host, it is a no-op: another channel covers the user.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

DEFAULT_CLI = "/mnt/c/Users/Anonymous/AppData/Local/Programs/Pebrel/pebrel.exe"


def _cli() -> str | None:
    return os.environ.get("PEBREL_CLI") or (
        DEFAULT_CLI if os.path.exists(DEFAULT_CLI) else None
    )


def send(n) -> None:
    cli = _cli()
    if not cli:
        log.debug("channels.pebrel: no Pebrel CLI; skipping")
        return
    # Pebrel's toast shows its own pane-scoped content; we only ask it to
    # surface attention. The title/body travel through the other channels.
    subprocess.run([cli, "notify-test"], capture_output=True, timeout=10)
