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

# One PowerShell launch returns three facts: "<process name>|<idle seconds>|<locked>".
# "ERR" means the field failed; the caller must not treat it as a value.
# InvariantCulture keeps the decimal separator stable. `locked` comes from
# WTS QuerySessionInformation(WTSSessionInfoEx) on the console session:
# SessionFlags 0=LOCKED, 1=UNLOCKED, anything else (incl. 0xFFFFFFFF UNKNOWN)
# reports ERR — callers treat that as "not locked" and stand down.
_WTS_SCRIPT = (
    "Add-Type -Namespace W -Name S -MemberDefinition '"
    '[DllImport(\"wtsapi32.dll\")] public static extern bool WTSQuerySessionInformation('
    'System.IntPtr hServer, int sessionId, int infoClass, '
    'out System.IntPtr ppBuffer, out int pBytesReturned);'
    '[DllImport(\"wtsapi32.dll\")] public static extern void WTSFreeMemory(System.IntPtr pMemory);'
    '[DllImport(\"kernel32.dll\")] public static extern int WTSGetActiveConsoleSessionId();'
    "';"
    "$locked=\"ERR\";"
    "$csid=[W.S]::WTSGetActiveConsoleSessionId();"
    "$buf=[System.IntPtr]::Zero;$n=0;"
    "if ([W.S]::WTSQuerySessionInformation([System.IntPtr]::Zero,$csid,25,[ref]$buf,[ref]$n)) {"
    "$qsid=[System.Runtime.InteropServices.Marshal]::ReadInt32($buf,8);"
    "if ($qsid -eq $csid) {"
    "$flags=[System.Runtime.InteropServices.Marshal]::ReadInt32($buf,16);"
    "if ($flags -eq 0) { $locked=\"0\" } elseif ($flags -eq 1) { $locked=\"1\" }"
    "};"
    "[void][W.S]::WTSFreeMemory($buf);"
    "};"
)
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
    + _WTS_SCRIPT +
    '"$name|$idle|$locked"'
)

MAX_IDLE_SECONDS = 604800.0


@dataclasses.dataclass
class Presence:
    foreground: bool | None
    idle_seconds: float | None
    locked: bool | None = None


def _run_ps(script: str, timeout: float, powershell: str | None = None) -> str:
    exe = powershell or DEFAULT_POWERSHELL
    result = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return (result.stdout or "").strip()


def _parse_sample(raw: str) -> tuple[str, float | None, bool | None]:
    """Split one "name|idle|locked" line. Any unreadable field is None."""
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1 or "|" not in lines[0]:
        return "", None, None
    parts = lines[0].split("|")
    name = parts[0].strip().lower()
    idle_raw = parts[1].strip() if len(parts) > 1 else "ERR"
    locked_raw = parts[2].strip() if len(parts) > 2 else ""

    idle = None
    if idle_raw != "ERR":
        try:
            value = float(idle_raw)
        except ValueError:
            value = None
        if value is not None and 0.0 <= value < MAX_IDLE_SECONDS:
            idle = value

    locked = {"0": False, "1": True}.get(locked_raw)
    return name, idle, locked


def sample(powershell: str | None = None,
           timeout: float = 1.5) -> Presence | None:
    """Foreground ownership, idle time and lock state from one PowerShell call.

    Returns None when PowerShell is unusable or the output has the wrong
    shape. A field the script could not read is None on the Presence.
    """
    try:
        raw = _run_ps(SAMPLE_SCRIPT, timeout, powershell)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        log.debug("presence: sample failed (%s)", exc)
        return None

    name, idle, locked = _parse_sample(raw)
    if not name and idle is None and locked is None:
        return None
    foreground = None if not name else name == PROCESS_NAME
    return Presence(foreground=foreground, idle_seconds=idle, locked=locked)



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
