"""Configuration loading, defaults, and credential lookup.

The config file is JSON so hooks can parse it quickly without extra
dependencies. A Feishu webhook may be filled in directly, which takes
precedence; otherwise the environment variable named by ``webhook_env`` is
used. Because the file can now hold a credential, it must stay private: the
default config this module writes is created with mode 0600.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when a config file is valid JSON but holds an unusable value."""


DEFAULTS: dict = {
    "enabled": True,
    "thresholds": {"foreground_seconds": 150, "background_seconds": 90},
    "afk": {"set_claude_afk_timeout_ms": 300000},
    "judge": {"enabled": True, "model": None, "timeout_ms": 8000, "fallback": "first_option"},
    # Terminal/Pebrel toasts were dropped at the owner's call; Feishu is the
    # only channel. Keeping phantom sections here made users configure things
    # that never ran.
    "channels": {
        "feishu": {"enabled": True, "webhook": None,
                   "webhook_env": "CC_AUTOPILOT_FEISHU_WEBHOOK"},
    },
    "path_b": {"enabled": False},
    "daemon": {"enabled": True},
    "autopilot": {"continue_enabled": False, "shadow_mode": True,
                  "max_continues": 10, "suggest_wait_seconds": 90,
                  "failure_continues": 2},
    "notify": {"dedupe_seconds": 300, "message_translations": {},
               "min_turn_seconds": 0},
    "quiet_hours": {"enabled": False, "start": "00:00", "end": "09:00"},
    "min_duration_seconds": 30,
}


def _merge(base: dict, over: dict) -> dict:
    """Recursively merge `over` onto a copy of `base`."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _warn_unknown_keys(raw: dict, known: dict, prefix: str = "") -> None:
    """Name every key the file carries that this version does not read.

    A typo like `foreground_second` otherwise loads cleanly and silently uses
    150, which is the exact "hide a config mistake" outcome this module refuses.
    Unknown keys are only warned about, not rejected: a config written for a
    newer version must not break an older one.
    """
    for key, value in raw.items():
        path = f"{prefix}{key}"
        if key not in known:
            log.warning("config: unknown key %r ignored", path)
        elif isinstance(value, dict) and isinstance(known[key], dict):
            _warn_unknown_keys(value, known[key], prefix=path + ".")


def _section(cfg: dict, name: str) -> dict:
    """Return a section as a dict, or {} when it is absent or not an object."""
    value = cfg.get(name)
    return value if isinstance(value, dict) else {}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(cfg: dict, name: str) -> dict:
    value = cfg.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a JSON object, got {value!r}")
    return value


def _require_positive_int(section: dict, name: str, path: str) -> None:
    value = section.get(name)
    if not _is_int(value) or value <= 0:
        raise ConfigError(f"{path} must be a positive integer, got {value!r}")


def _require_bool(section: dict, name: str, path: str) -> None:
    value = section.get(name)
    if not isinstance(value, bool):
        raise ConfigError(f"{path} must be true or false, got {value!r}")


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate(cfg: dict) -> None:
    if not isinstance(cfg.get("enabled"), bool):
        raise ConfigError(f"enabled must be true or false, got {cfg.get('enabled')!r}")

    thresholds = _require_object(cfg, "thresholds")
    for name in ("foreground_seconds", "background_seconds"):
        _require_positive_int(thresholds, name, f"thresholds.{name}")

    afk = _require_object(cfg, "afk")
    _require_positive_int(afk, "set_claude_afk_timeout_ms", "afk.set_claude_afk_timeout_ms")

    judge = _require_object(cfg, "judge")
    _require_bool(judge, "enabled", "judge.enabled")
    _require_positive_int(judge, "timeout_ms", "judge.timeout_ms")
    model = judge.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ConfigError(f"judge.model must be a model name or null, got {model!r}")
    fallback = judge.get("fallback")
    if not isinstance(fallback, str) or not fallback:
        raise ConfigError(f"judge.fallback must be a non-empty string, got {fallback!r}")

    channels = _require_object(cfg, "channels")
    feishu = _require_object(channels, "feishu")
    _require_bool(feishu, "enabled", "channels.feishu.enabled")
    env = feishu.get("webhook_env")
    if not isinstance(env, str) or not env:
        raise ConfigError(
            f"channels.feishu.webhook_env must be an env var name, got {env!r}")
    webhook = feishu.get("webhook")
    if webhook is not None and not isinstance(webhook, str):
        # Never echo the value: a mis-shaped webhook IS the credential.
        raise ConfigError(
            "channels.feishu.webhook must be a URL string or null, got "
            f"{type(webhook).__name__}")

    quiet = _require_object(cfg, "quiet_hours")
    _require_bool(quiet, "enabled", "quiet_hours.enabled")
    for name in ("start", "end"):
        value = quiet.get(name)
        if not isinstance(value, str) or not _HHMM.match(value):
            raise ConfigError(f"quiet_hours.{name} must be HH:MM, got {value!r}")

    path_b = _require_object(cfg, "path_b")
    _require_bool(path_b, "enabled", "path_b.enabled")
    daemon = _require_object(cfg, "daemon")
    _require_bool(daemon, "enabled", "daemon.enabled")

    autopilot = _require_object(cfg, "autopilot")
    _require_bool(autopilot, "continue_enabled", "autopilot.continue_enabled")
    _require_bool(autopilot, "shadow_mode", "autopilot.shadow_mode")
    for name in ("max_continues", "suggest_wait_seconds", "failure_continues"):
        _require_positive_int(autopilot, name, f"autopilot.{name}")

    notify = _require_object(cfg, "notify")
    dedupe = notify.get("dedupe_seconds")
    if not _is_int(dedupe) or dedupe < 0:
        raise ConfigError(
            f"notify.dedupe_seconds must be a non-negative integer, got {dedupe!r}")
    translations = notify.get("message_translations")
    if not isinstance(translations, dict) or not all(
            isinstance(source, str) and isinstance(target, str)
            for source, target in translations.items()):
        raise ConfigError(
            "notify.message_translations must be an object of string pairs, "
            f"got {translations!r}")
    min_turn = notify.get("min_turn_seconds")
    if not _is_int(min_turn) or min_turn < 0:
        raise ConfigError(
            "notify.min_turn_seconds must be a non-negative integer, "
            f"got {min_turn!r}")

    if not _is_int(cfg.get("min_duration_seconds")) or cfg["min_duration_seconds"] < 0:
        raise ConfigError(
            f"min_duration_seconds must be a non-negative integer, "
            f"got {cfg.get('min_duration_seconds')!r}")


def load(path: Path | None = None) -> dict:
    """Load config, merging over DEFAULTS.

    A missing file yields DEFAULTS. A corrupt file logs and yields DEFAULTS.
    A file with an unusable value raises ConfigError: silently substituting a
    default would hide a config mistake the user needs to see.
    """
    if path is None:
        path = default_path()
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("config: cannot read %s (%s); using defaults", path, exc)
        return copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        log.warning("config: %s is not a JSON object; using defaults", path)
        return copy.deepcopy(DEFAULTS)
    cfg = _merge(DEFAULTS, raw)
    _warn_unknown_keys(raw, DEFAULTS)
    _validate(cfg)
    return cfg


def foreground_seconds(cfg: dict) -> int:
    return int(_section(cfg, "thresholds").get("foreground_seconds", 150))


def background_seconds(cfg: dict) -> int:
    return int(_section(cfg, "thresholds").get("background_seconds", 90))


def feishu_webhook(cfg: dict) -> str | None:
    """Direct URL first; otherwise the env var the config names."""
    feishu = _section(_section(cfg, "channels"), "feishu")
    direct = feishu.get("webhook")
    # Whitespace-only is a typo, not a webhook: treat it like "" and fall back.
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    env_name = feishu.get("webhook_env")
    if not env_name:
        return None
    # `$(cat file)` commonly leaves a trailing newline; it makes the URL
    # invalid and the send fail silently.
    return (os.environ.get(env_name) or "").strip() or None


def path_b_enabled(cfg: dict) -> bool:
    return _section(cfg, "path_b").get("enabled") is True


def continue_enabled(cfg: dict) -> bool:
    return _section(cfg, "autopilot").get("continue_enabled") is True


def shadow_mode(cfg: dict) -> bool:
    return _section(cfg, "autopilot").get("shadow_mode", True) is True


def max_continues(cfg: dict) -> int:
    return int(_section(cfg, "autopilot").get("max_continues", 10))


def suggest_wait_seconds(cfg: dict) -> int:
    return int(_section(cfg, "autopilot").get("suggest_wait_seconds", 90))


def failure_continues(cfg: dict) -> int:
    return int(_section(cfg, "autopilot").get("failure_continues", 2))


def daemon_enabled(cfg: dict) -> bool:
    return _section(cfg, "daemon").get("enabled", True) is True


def enabled(cfg: dict) -> bool:
    """Top-level switch: false disables every hook and the daemon."""
    return cfg.get("enabled", True) is True


def judge_enabled(cfg: dict) -> bool:
    return _section(cfg, "judge").get("enabled", True) is True


def judge_timeout_seconds(cfg: dict) -> float:
    return max(1, int(_section(cfg, "judge").get("timeout_ms", 8000))) / 1000.0


def judge_fallback(cfg: dict) -> str:
    value = _section(cfg, "judge").get("fallback", "first_option")
    return value if value in ("first_option", "delegate") else "first_option"


def afk_timeout_ms(cfg: dict) -> int:
    return int(_section(cfg, "afk").get("set_claude_afk_timeout_ms", 300000))


def appdata_root() -> Path:
    """Where Pebrel keeps runtime.port, without hardcoding a Windows user."""
    override = os.environ.get("CC_AUTOPILOT_APPDATA")
    if override:
        return Path(override)
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "Anonymous"
    return Path("/mnt/c/Users") / user / "AppData/Roaming"


def default_path() -> Path:
    override = os.environ.get("CC_AUTOPILOT_CONFIG")
    return Path(override) if override else (
        Path.home() / ".config" / "cc-autopilot-notify" / "config.json")


def write_default(path: Path) -> None:
    """Create the default config with mode 0600; never overwrite an existing file.

    SessionStart can fire from several sessions at once, so the file is written
    to a private temp file and moved into place instead of being truncated in
    place. If the target already exists it is left exactly as the user wrote it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(copy.deepcopy(DEFAULTS), handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        try:
            # link() fails if the target appeared meanwhile, so a config the
            # user wrote in that window is never overwritten.
            os.link(tmp, path)
        except FileExistsError:
            pass
        except OSError:
            # Filesystems without hard links: atomic replace is still better
            # than a half-written file, at the cost of the tiny race window.
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def judge_model(cfg: dict) -> str | None:
    if not judge_enabled(cfg):
        return None
    return _section(cfg, "judge").get("model") or None


def notify_dedupe_seconds(cfg: dict) -> int:
    """Suppress an identical notification repeated within this many seconds."""
    return int(_section(cfg, "notify").get("dedupe_seconds", 300))


def notification_message_translations(cfg: dict) -> dict[str, str]:
    translations = _section(cfg, "notify").get("message_translations")
    return translations if isinstance(translations, dict) else {}


def notify_min_turn_seconds(cfg: dict) -> int:
    """Suppress `idle_prompt` for turns shorter than this. 0 disables the gate."""
    return int(_section(cfg, "notify").get("min_turn_seconds", 0))


def in_quiet_hours(cfg: dict, now: float | None = None) -> bool:
    """True when local time falls inside the configured quiet window.

    Windows may wrap midnight (start > end). Bounds that are equal, malformed
    or missing allow the notification: a mistyped window must cost at most an
    extra alert, never a whole day of silence.
    """
    quiet = _section(cfg, "quiet_hours")
    if quiet.get("enabled") is not True:
        return False
    start, end = quiet.get("start"), quiet.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return False
    try:
        sh, sm = (int(part) for part in start.split(":"))
        eh, em = (int(part) for part in end.split(":"))
    except ValueError:
        return False
    # 25:00 parses as an int but is not a time; letting it through would turn a
    # typo into a silent window that covers most of the day.
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        return False
    clock = time.localtime(time.time() if now is None else now)
    minutes = clock.tm_hour * 60 + clock.tm_min
    start_min, end_min = sh * 60 + sm, eh * 60 + em
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= minutes < end_min
    return minutes >= start_min or minutes < end_min
