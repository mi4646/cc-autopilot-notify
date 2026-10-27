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


def _validate(cfg: dict) -> None:
    thresholds = cfg.get("thresholds", {})
    for name in ("foreground_seconds", "background_seconds"):
        value = thresholds.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"thresholds.{name} must be a positive integer, got {value!r}")


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
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("config: cannot read %s (%s); using defaults", path, exc)
        return copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        log.warning("config: %s is not a JSON object; using defaults", path)
        return copy.deepcopy(DEFAULTS)
    cfg = _merge(DEFAULTS, raw)
    _validate(cfg)
    return cfg


def foreground_seconds(cfg: dict) -> int:
    return int(cfg["thresholds"]["foreground_seconds"])


def background_seconds(cfg: dict) -> int:
    return int(cfg["thresholds"]["background_seconds"])


def feishu_webhook(cfg: dict) -> str | None:
    """Read the Feishu webhook from the environment variable the config names."""
    env_name = cfg.get("channels", {}).get("feishu", {}).get("webhook_env")
    if not env_name:
        return None
    return os.environ.get(env_name) or None


def judge_model(cfg: dict) -> str | None:
    return cfg.get("judge", {}).get("model") or None
