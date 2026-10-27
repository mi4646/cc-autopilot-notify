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
from .migrate import _is_our_hook

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
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        # A non-UTF-8 reply is not an OSError; powershell.exe on a Chinese
        # Windows writes GBK, which text=True cannot decode.
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


def _legacy_install() -> Check:
    """FAIL while legacy settings.json hooks remain: they double-fire with the plugin."""
    import json
    import os

    path = Path(os.environ.get("CC_AUTOPILOT_SETTINGS")
                or Path.home() / ".claude" / "settings.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return Check("legacy install", "SKIP", f"cannot read {path}")
    except ValueError:
        return Check("legacy install", "FAIL", f"{path} is not valid JSON")
    for items in (data.get("hooks") or {}).values():
        for item in items or []:
            for hook in item.get("hooks") or []:
                # Same predicate as migration: a foreign run_hook.py is neither
                # removed by /cc-autopilot-notify:migrate nor a reason to demand it.
                if _is_our_hook(hook):
                    return Check("legacy install", "FAIL",
                                 "legacy hooks in settings.json; run /cc-autopilot-notify:migrate")
    return Check("legacy install", "PASS", "no legacy entries in settings.json")


def _answers_probe(spike_path: Path | None = None) -> Check:
    """Path B's viability. SKIP until someone runs the recorded probe.

    A hook that answers questions cannot be tested from inside the session it
    would answer, so this reads the spike document's explicit verdict line. It
    must never report PASS on a "discarded" outcome: this check exists to
    re-detect exactly that failure after a Claude Code upgrade.
    """
    spike = spike_path or (Path(__file__).resolve().parent.parent / "docs" / "spikes"
                           / "2026-10-27-permission-request-answers.md")
    try:
        text = spike.read_text(encoding="utf-8")
    except OSError:
        return Check("updatedInput.answers accepted", "SKIP", "no spike document")
    except UnicodeDecodeError:
        return Check("updatedInput.answers accepted", "SKIP",
                     "spike document is not UTF-8")

    verdict = ""
    for line in text.splitlines():
        if line.startswith("VERDICT:"):
            verdict = line.split(":", 1)[1].strip().upper()
            break
    if verdict == "ACCEPTED":
        return Check("updatedInput.answers accepted", "PASS", "probe confirmed acceptance")
    if verdict == "DISCARDED":
        return Check("updatedInput.answers accepted", "FAIL",
                     "probe showed CC discards it; path B is unusable here")
    return Check("updatedInput.answers accepted", "SKIP",
                 "unverified on this CC version; see the spike document")


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
    checks.append(_legacy_install())
    checks.append(_answers_probe())
    return checks
