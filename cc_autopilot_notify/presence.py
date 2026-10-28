"""Foreground-window detection.

Answers one question: is the terminal that runs Claude Code the window the
user is looking at? Anything else — no powershell, a timeout, an unreadable
reply — is `None`, and the daemon then applies the stricter background
threshold. Failing toward "they are away" means we answer sooner; failing
toward "they are here" would leave a session stalled all night.
"""

from __future__ import annotations

import dataclasses
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

# One PowerShell launch returns both facts: "<process name>|<idle seconds>".
# "ERR" means GetLastInputInfo failed; the caller must not treat it as an
# idle value. InvariantCulture keeps the decimal separator stable.
SAMPLE_SCRIPT = (
    "Add-Type -Namespace W -Name I -MemberDefinition '"
    '[DllImport(\"user32.dll\")] public static extern System.IntPtr GetForegroundWindow();'
    '[DllImport(\"user32.dll\")] public static extern int GetWindowThreadProcessId('
    'System.IntPtr h, out uint p);'
    '[DllImport(\"user32.dll\")] public static extern bool GetLastInputInfo('
    'ref LASTINPUTINFO plii);'
    '[DllImport(\"kernel32.dll\")] public static extern uint GetTickCount();'
    'public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }'
    "';"
    "$fg=[W.I]::GetForegroundWindow();"
    "$p=0; [void][W.I]::GetWindowThreadProcessId($fg,[ref]$p);"
    "$name=(Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName;"
    "$li=New-Object W.I+LASTINPUTINFO;"
    "$li.cbSize=[System.Runtime.InteropServices.Marshal]::SizeOf($li);"
    '$idle="ERR";'
    "if ([W.I]::GetLastInputInfo([ref]$li) -and $li.dwTime -ne 0) {"
    "$delta=[W.I]::GetTickCount()-$li.dwTime;"
    '$idle=[string]::Format([Globalization.CultureInfo]::InvariantCulture,'
    '"{0:F3}",$delta/1000.0)'
    "};"
    '"$name|$idle"'
)

MAX_IDLE_SECONDS = 604800.0


@dataclasses.dataclass
class Presence:
    foreground: bool | None
    idle_seconds: float | None


def _run_ps(script: str, timeout: float, powershell: str | None = None) -> str:
    exe = powershell or DEFAULT_POWERSHELL
    result = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return (result.stdout or "").strip()


def sample(powershell: str | None = None,
           timeout: float = 1.5) -> Presence | None:
    """Foreground ownership and system idle time from one PowerShell call.

    Returns None when PowerShell is unusable or the output has the wrong
    shape. A field the script could not read is None on the Presence.
    """
    try:
        raw = _run_ps(SAMPLE_SCRIPT, timeout, powershell)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        log.debug("presence: sample failed (%s)", exc)
        return None

    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1 or "|" not in lines[0]:
        return None
    name, idle_raw = lines[0].split("|", 1)

    name = name.strip().lower()
    foreground = None if not name else name == PROCESS_NAME

    idle = None
    if idle_raw.strip() != "ERR":
        try:
            value = float(idle_raw.strip())
        except ValueError:
            value = None
        if value is not None and 0.0 <= value < MAX_IDLE_SECONDS:
            idle = value

    return Presence(foreground=foreground, idle_seconds=idle)



def is_foreground(powershell: str | None = None, timeout: float = 5.0) -> bool | None:
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
