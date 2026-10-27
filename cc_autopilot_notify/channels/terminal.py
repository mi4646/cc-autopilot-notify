"""Terminal notification via OSC 9.

Inside a real Claude Code hook, stdout is a pipe that Claude Code captures for
the hook's output — OSC bytes written there are never seen by anyone. The
notification has to go to the controlling terminal instead, so this opens
`/dev/tty` whenever stdout is not a tty.

If neither is available (no controlling terminal, dumb TERM), it stays silent:
corrupting a captured output stream is worse than one missed notification.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

_ESC = "\x1b]9;"
_BEL = "\x07"


def _open_tty():
    """Return (stream, opened_here). `stream` is None when nothing is writable."""
    if sys.stdout.isatty():
        return sys.stdout, False
    try:
        return open("/dev/tty", "w"), True
    except OSError as exc:
        log.debug("terminal: no controlling tty (%s)", exc)
        return None, False


def send(n, tty=None) -> None:
    term = os.environ.get("TERM", "")
    if not term or term == "dumb":
        return
    text = f"{n.title}: {n.body}".replace("\x07", " ").replace("\x1b", " ")
    if tty is not None:
        stream, opened_here = tty, False
    else:
        stream, opened_here = _open_tty()
    if stream is None:
        return
    try:
        stream.write(f"{_ESC}{text}{_BEL}\n")
        stream.flush()
    except OSError as exc:
        log.debug("terminal: write failed (%s)", exc)
    finally:
        if opened_here:
            stream.close()
