"""Terminal notification via OSC 9.

Works in any emulator that supports it (Pebrel does). Silent on a dumb or
missing TERM: printing escape codes into a pipe would corrupt the output
someone is capturing.
"""

from __future__ import annotations

import os
import sys

_ESC = "\x1b]9;"
_BEL = "\x07"


def send(n) -> None:
    term = os.environ.get("TERM", "")
    if not term or term == "dumb":
        return
    text = f"{n.title}: {n.body}".replace("\x07", " ").replace("\x1b", " ")
    sys.stdout.write(f"{_ESC}{text}{_BEL}\n")
    sys.stdout.flush()
