"""Retire a legacy direct install without touching unrelated entries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class MigrationError(Exception):
    """Abort the whole migration; nothing was written."""


WEBHOOK_ENV = "CC_AUTOPILOT_FEISHU_WEBHOOK"
AFK_KEY = "CLAUDE_AFK_TIMEOUT_MS"


def _read_json(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationError(f"refusing to touch {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MigrationError(f"refusing to touch {path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise MigrationError(f"refusing to touch {path}: not a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _without_our_hooks(data: dict) -> int:
    removed = 0
    hooks = data.get("hooks") or {}
    for event in list(hooks):
        kept = []
        for item in hooks[event] or []:
            inner = item.get("hooks") or []
            if any("run_hook.py" in str(h.get("command", "")) for h in inner):
                removed += 1
                continue
            kept.append(item)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    return removed


def _stop_unit(unit: Path, run_systemctl: bool) -> bool:
    if not unit.exists():
        return False
    try:
        text = unit.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if "cc_autopilot_notify" not in text:
        return False
    if run_systemctl and subprocess.run(
            ["systemctl", "--user", "disable", "--now", unit.name],
            capture_output=True).returncode not in (0,):
        # Not fatal: the unit file still has to go, or it restarts on login.
        print(f"warn: could not stop {unit.name}", file=sys.stderr)
    unit.unlink(missing_ok=True)
    return True


def migrate(settings: Path, config_path: Path, unit: Path,
            *, run_systemctl: bool = True) -> str:
    settings, config_path, unit = Path(settings), Path(config_path), Path(unit)
    data = _read_json(settings)            # aborts before any write
    env = data.get("env") or {}
    legacy_webhook = env.get(WEBHOOK_ENV)
    legacy_afk = env.get(AFK_KEY)

    removed = _without_our_hooks(data)
    prev = data.get("_ccAutopilotPrevious") or {}
    if isinstance(prev, dict):
        for key in (WEBHOOK_ENV, AFK_KEY):
            prev.pop(key, None)
        if prev:
            data["_ccAutopilotPrevious"] = prev
        else:
            data.pop("_ccAutopilotPrevious", None)

    moved = False
    if isinstance(legacy_webhook, str) and legacy_webhook:
        cfg = _read_json(config_path)
        channels = cfg.setdefault("channels", {})
        feishu = channels.setdefault("feishu", {})
        if not feishu.get("webhook"):
            feishu["webhook"] = legacy_webhook
            _write_json(config_path, cfg)
            os.chmod(config_path, 0o600)
            moved = True
        if moved:
            env.pop(WEBHOOK_ENV, None)

    if env:
        data["env"] = env
    else:
        data.pop("env", None)

    if removed or moved:
        backup = settings.with_name(
            f"{settings.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        if settings.exists():
            backup.write_bytes(settings.read_bytes())
        _write_json(settings, data)
    stopped = _stop_unit(unit, run_systemctl)

    parts = []
    if removed:
        parts.append(f"removed {removed} legacy hook entries")
    if moved:
        parts.append(f"moved {WEBHOOK_ENV} into config")
    if stopped:
        parts.append(f"removed {unit}")
    if legacy_afk is not None:
        parts.append(f"kept {AFK_KEY}={legacy_afk}")
    return "; ".join(parts) if parts else "nothing to migrate"


def main(argv: list[str] | None = None) -> int:
    settings = Path(os.environ.get(
        "CC_AUTOPILOT_SETTINGS", Path.home() / ".claude" / "settings.json"))
    config_path = Path(os.environ.get(
        "CC_AUTOPILOT_CONFIG",
        Path.home() / ".config" / "cc-autopilot-notify" / "config.json"))
    unit = Path(os.environ.get(
        "CC_AUTOPILOT_UNIT",
        Path.home() / ".config" / "systemd" / "user" / "cc-autopilot-notify.service"))
    try:
        print(migrate(settings, config_path, unit))
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
