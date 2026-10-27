"""Configuration loading, defaults, and credential lookup.

The config file is JSON so hooks can parse it quickly without extra
dependencies. Credentials never live in it: they are read from environment
variables by name, so the file stays safe to copy around.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when a config file is valid JSON but holds an unusable value."""


DEFAULTS: dict = {
    "enabled": True,
    "thresholds": {"foreground_seconds": 150, "background_seconds": 90},
    "afk": {"set_claude_afk_timeout_ms": 300000},
    "judge": {"enabled": True, "model": None, "timeout_ms": 8000, "fallback": "first_option"},
    "channels": {
        "feishu": {"enabled": True, "webhook_env": "CC_AUTOPILOT_FEISHU_WEBHOOK"},
        "terminal": {"enabled": True},
        "pebrel": {"enabled": True},
    },
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
    for name in ("feishu", "terminal", "pebrel"):
        if name not in channels:
            continue
        section = _require_object(channels, name)
        _require_bool(section, "enabled", f"channels.{name}.enabled")
        if name == "feishu":
            env = section.get("webhook_env")
            if not isinstance(env, str) or not env:
                raise ConfigError(
                    f"channels.feishu.webhook_env must be an env var name, got {env!r}")

    quiet = _require_object(cfg, "quiet_hours")
    _require_bool(quiet, "enabled", "quiet_hours.enabled")
    for name in ("start", "end"):
        value = quiet.get(name)
        if not isinstance(value, str) or not _HHMM.match(value):
            raise ConfigError(f"quiet_hours.{name} must be HH:MM, got {value!r}")

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
        path = Path.home() / ".config" / "cc-autopilot-notify" / "config.json"
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
    """Read the Feishu webhook from the environment variable the config names."""
    env_name = _section(_section(cfg, "channels"), "feishu").get("webhook_env")
    if not env_name:
        return None
    return os.environ.get(env_name) or None


def judge_model(cfg: dict) -> str | None:
    return _section(cfg, "judge").get("model") or None
