"""Self-checks that answer "is this still working on the machine it runs on?".

Every check returns PASS, FAIL or SKIP plus the evidence it used, so a version
bump in Claude Code or Pebrel produces one command to re-run rather than a
re-investigation.
"""

from __future__ import annotations

import dataclasses
import logging
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import pebrel_runtime, state

log = logging.getLogger(__name__)

HEARTBEAT_STALE_SECONDS = 60.0
DEFAULT_APPDATA = Path("/mnt/c/Users/Anonymous/AppData/Roaming")


@dataclasses.dataclass
class Check:
    name: str
    status: str          # PASS | FAIL | SKIP
    detail: str


def _cc_version() -> Check:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True,
                             text=True, timeout=5)
        text = (out.stdout or out.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("claude version", "SKIP", f"cannot run claude: {exc}")
    return Check("claude version", "PASS" if text else "SKIP", text or "no output")


def _runtime(probe) -> list[Check]:
    try:
        desc = probe()
    except Exception as exc:  # noqa: BLE001 - any failure is the finding
        return [
            Check("pebrel runtime", "FAIL", f"unreachable: {exc}"),
            Check("pebrel protocol version", "SKIP", "runtime unreachable"),
            Check("pebrel conversation capabilities", "SKIP", "runtime unreachable"),
        ]
    if not isinstance(desc, dict) or not desc:
        return [
            Check("pebrel runtime", "FAIL", "runtime returned no description"),
            Check("pebrel protocol version", "SKIP", "no description"),
            Check("pebrel conversation capabilities", "SKIP", "no description"),
        ]
    version = desc.get("protocol_version")
    caps = desc.get("capabilities") or []
    return [
        Check("pebrel runtime", "PASS", f"app {desc.get('app_version', 'unknown')}"),
        Check("pebrel protocol version", "PASS" if version == 1 else "FAIL",
              f"protocol_version={version} (expected 1)"),
        Check("pebrel conversation capabilities",
              "PASS" if pebrel_runtime.supports_conversation(desc) else "FAIL",
              "conversation.read+choose" if pebrel_runtime.supports_conversation(desc)
              else "missing conversation.read or conversation.choose"),
    ]


def _powershell() -> Check:
    from . import presence

    value = presence.is_foreground()
    if value is None:
        return Check("powershell foreground", "FAIL", "cannot read the foreground window")
    return Check("powershell foreground", "PASS", f"foreground={value}")


def _heartbeat(root: Path, now: float) -> Check:
    age = state.heartbeat_age(root, now=now)
    if age is None:
        return Check("daemon heartbeat", "FAIL", "no heartbeat file; daemon not running")
    if age > HEARTBEAT_STALE_SECONDS:
        return Check("daemon heartbeat", "FAIL", f"stale by {age:.0f}s")
    return Check("daemon heartbeat", "PASS", f"{age:.0f}s old")


def _feishu(cfg: dict) -> Check:
    from . import config

    webhook = config.feishu_webhook(cfg)
    if not webhook:
        return Check("feishu webhook", "SKIP", "not configured")
    return Check("feishu webhook", "PASS", "configured (not probed)")


def _settings_clean() -> Check:
    import json
    import os

    path = Path(os.environ.get("CC_AUTOPILOT_SETTINGS")
                or Path.home() / ".claude" / "settings.json")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Check("settings residue", "SKIP", f"cannot read {path}: {exc}")
    try:
        data = json.loads(text)
    except ValueError:
        return Check("settings residue", "FAIL", f"{path} is not valid JSON")
    present = "cc_autopilot_notify" in text
    return Check("settings residue", "PASS" if not present else "FAIL",
                 "no cc_autopilot_notify entries" if not present
                 else "settings.json still references cc_autopilot_notify")


def run_all(root: Path, cfg: dict, probe: Callable[[], dict] | None = None,
            now: float | None = None) -> list[Check]:
    now = time.time() if now is None else now
    probe = probe or (lambda: pebrel_runtime.describe(
        Path(cfg.get("_appdata") or DEFAULT_APPDATA)))
    checks = [_cc_version()]
    checks.extend(_runtime(probe))
    checks.append(_powershell())
    checks.append(_heartbeat(Path(root), now))
    checks.append(_feishu(cfg))
    checks.append(_settings_clean())
    return checks
