"""Foreground-window detection.

Answers one question: is the terminal that runs Claude Code the window the
user is looking at? Anything else — no powershell, a timeout, an unreadable
reply — is `None`, and the daemon then applies the stricter background
threshold. Failing toward "they are away" means we answer sooner; failing
toward "they are here" would leave a session stalled all night.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

PROCESS_NAME = "pebrel"

DEFAULT_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# Prints the foreground window's owning process name, lowercased.
_SCRIPT = (
    "Add-Type -Namespace W -Name U -MemberDefinition '"
    '[DllImport(\"user32.dll\")] public static extern System.IntPtr GetForegroundWindow();'
    '[DllImport(\"user32.dll\")] public static extern int GetWindowThreadProcessId('
    'System.IntPtr h, out uint p);'
    "';"
    "$fg=[W.U]::GetForegroundWindow();"
    "$p=0; [void][W.U]::GetWindowThreadProcessId($fg,[ref]$p);"
    "(Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName"
)


def _run_ps(script: str, timeout: float, powershell: str | None = None) -> str:
    exe = powershell or DEFAULT_POWERSHELL
    result = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return (result.stdout or "").strip()


def is_foreground(powershell: str | None = None, timeout: float = 2.0) -> bool | None:
    """True if the Claude Code terminal is foreground, False if not, None if unknown."""
    try:
        name = _run_ps(_SCRIPT, timeout, powershell)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is not an OSError: powershell.exe on a Chinese
        # Windows writes GBK to stderr, which text=True cannot decode.
        log.debug("presence: powershell unusable (%s)", exc)
        return None
    name = name.strip().lower()
    if not name:
        return None
    return name == PROCESS_NAME
