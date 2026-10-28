#!/usr/bin/env python3
"""Probe Windows system idle time from WSL.

Answers the spike question in docs/superpowers/specs/2026-10-28-idle-aware-autoanswer-design.md:
does GetLastInputInfo return the interactive session's idle time when called
from (a) an interactive WSL shell, (b) a detached process, (c) a minimal env?

Read-only: prints JSON lines. Never modifies the system.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

DEFAULT_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

SCRIPT = r"""
Add-Type -Namespace W -Name I -MemberDefinition '
[DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
[DllImport("kernel32.dll")] public static extern uint GetTickCount();
public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
';
$li = New-Object W.I+LASTINPUTINFO;
$li.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf($li);
if (-not [W.I]::GetLastInputInfo([ref]$li) -or $li.dwTime -eq 0) { "ERR"; exit 0 }
$delta = [W.I]::GetTickCount() - $li.dwTime;
$secs = [string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:F3}", $delta / 1000.0)
"$secs|$($li.dwTime)|$([W.I]::GetTickCount())"
"""


def sample(powershell: str, timeout: float) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
            capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the failure is the observation
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.monotonic() - started, 3)}
    elapsed = round(time.monotonic() - started, 3)
    out = (result.stdout or "").strip()
    payload = {"ok": False, "raw": out, "returncode": result.returncode,
               "stderr": (result.stderr or "").strip()[:300], "elapsed_s": elapsed}
    if out == "ERR":
        payload["error"] = "dwTime is 0"
        return payload
    parts = out.split("|")
    if len(parts) != 3:
        payload["error"] = "unexpected output shape"
        return payload
    try:
        seconds = float(parts[0])
        tick = int(parts[1])
        now_tick = int(parts[2])
    except ValueError:
        payload["error"] = "non-numeric output"
        return payload
    if not (0 <= seconds < 604800) or not (0 <= tick < 2**32) or not (0 <= now_tick < 2**32):
        payload["error"] = "out of range"
        payload.update(seconds=seconds, tick=tick, now_tick=now_tick)
        return payload
    payload.update(ok=True, seconds=seconds, tick=tick, now_tick=now_tick)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--powershell", default=DEFAULT_POWERSHELL)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    for index in range(args.samples):
        if index:
            time.sleep(args.interval)
        payload = sample(args.powershell, args.timeout)
        payload["sample"] = index + 1
        if args.label:
            payload["label"] = args.label
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
