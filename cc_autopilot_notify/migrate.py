"""Retire a legacy direct install without touching unrelated entries.

Cross-file replacement is not transactional on a plain filesystem, so the order
below is deliberate and is the best recovery available:

1. Read both input files and build both output documents in memory. An
   unreadable or invalid ``settings.json`` aborts before anything is written.
2. Stage every output as a private temp file in its destination directory and
   create the settings backup (mode 0600) before replacing either original.
3. Replace ``config.json`` first, ``settings.json`` second.

A failure before the first replace leaves both originals untouched. A failure
between the two replaces leaves the webhook in both places: the config is
already correct and settings still carries the env fallback. That is a
duplicate, never a loss, and re-running the migration finishes the job. The
settings document is only committed with the env key removed after the config
output has been staged and replaced, so a config write failure can never remove
the only copy of the webhook.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class MigrationError(Exception):
    """Abort the migration; the message names what was and was not written."""


WEBHOOK_ENV = "CC_AUTOPILOT_FEISHU_WEBHOOK"
AFK_KEY = "CLAUDE_AFK_TIMEOUT_MS"
BACKUP_ATTEMPTS = 100


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


def _unlink(path: str | Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _stage_json(path: Path, data: dict, *, mode: int | None = None) -> str:
    """Serialize `data` to a private temp file next to `path`; return its path.

    Serialization happens here, before any original is replaced: a document
    that cannot be written leaves every input file untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, mode)
    except BaseException:
        _unlink(tmp)
        raise
    return tmp


def _backup_settings(settings: Path) -> Path:
    """Copy the original settings aside, private, before any replacement."""
    base = f"{settings.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    for attempt in range(BACKUP_ATTEMPTS):
        candidate = settings.with_name(base if attempt == 0 else f"{base}-{attempt}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise MigrationError(f"cannot create backup {candidate}: {exc}") from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(settings.read_bytes())
        except OSError as exc:
            _unlink(candidate)
            raise MigrationError(f"cannot write backup {candidate}: {exc}") from exc
        return candidate
    raise MigrationError(f"cannot create a unique backup next to {settings}")


def _replace_all(staged: list[tuple[str, Path]]) -> None:
    """Commit staged files in order; convert OSError into MigrationError."""
    for index, (tmp, path) in enumerate(staged):
        try:
            os.replace(tmp, path)
        except OSError as exc:
            _unlink(tmp)
            for leftover, _ in staged[index + 1:]:
                _unlink(leftover)
            done = [str(p) for _, p in staged[:index]]
            detail = (f"; already replaced: {', '.join(done)}" if done else
                      "; no file was replaced")
            raise MigrationError(f"cannot write {path}: {exc}{detail}") from exc


def _is_our_hook(hook: dict) -> bool:
    """Only OUR runner: another project's run_hook.py is not ours to delete."""
    command = str(hook.get("command", ""))
    return "run_hook.py" in command and "cc_autopilot_notify" in command


def _without_our_hooks(data: dict) -> int:
    removed = 0
    hooks = data.get("hooks") or {}
    for event in list(hooks):
        kept = []
        for item in hooks[event] or []:
            inner = item.get("hooks") or []
            kept_inner = [h for h in inner if not _is_our_hook(h)]
            removed_here = len(inner) - len(kept_inner)
            removed += removed_here
            if removed_here and not kept_inner:
                continue
            if removed_here:
                item = {**item, "hooks": kept_inner}
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
    if run_systemctl:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "disable", "--now", unit.name],
                capture_output=True)
        except OSError as exc:
            print(f"warn: could not run systemctl ({exc})", file=sys.stderr)
        else:
            if result.returncode != 0:
                # Not fatal: the unit file still has to go, or it restarts on login.
                print(f"warn: could not stop {unit.name}", file=sys.stderr)
    try:
        unit.unlink(missing_ok=True)
    except OSError as exc:
        # The settings/config migration already succeeded; report, don't undo.
        print(f"warn: could not remove {unit}: {exc}", file=sys.stderr)
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

    config_data = None
    moved = False
    env_changed = False
    if isinstance(legacy_webhook, str) and legacy_webhook:
        cfg = _read_json(config_path)      # also aborts before any write
        channels = cfg.setdefault("channels", {})
        feishu = channels.setdefault("feishu", {})
        if not feishu.get("webhook"):
            feishu["webhook"] = legacy_webhook
            config_data = cfg
            moved = True
        # Move the credential out of settings.json even when the config
        # already has a webhook: leaving it behind contradicts the promise
        # that re-running the migration finishes the job.
        env.pop(WEBHOOK_ENV, None)
        env_changed = True

    if env:
        data["env"] = env
    else:
        data.pop("env", None)

    # Build every output before touching either original. A serialization or
    # staging failure therefore leaves settings and config exactly as they were.
    staged: list[tuple[str, Path]] = []
    try:
        if config_data is not None:
            # A file that now holds a credential is private regardless of what
            # mode the pre-existing config had.
            staged.append((_stage_json(config_path, config_data, mode=0o600),
                           config_path))
        if removed or moved or env_changed:
            staged.append((_stage_json(settings, data), settings))
    except (OSError, TypeError, ValueError) as exc:
        for tmp, _ in staged:
            _unlink(tmp)
        raise MigrationError(f"cannot stage migration output: {exc}") from exc

    backup = None
    if removed or moved or env_changed:
        if settings.exists():
            try:
                backup = _backup_settings(settings)
            except MigrationError:
                for tmp, _ in staged:
                    _unlink(tmp)
                raise
    _replace_all(staged)
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
    if backup is not None:
        parts.append(f"backup {backup}")
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
