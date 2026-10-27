import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _json(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_plugin_manifest_matches_pyproject_version():
    plugin = _json(".claude-plugin/plugin.json")
    assert plugin["name"] == "cc-autopilot-notify"
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{plugin["version"]}"' in text


def test_marketplace_points_at_the_repo_root():
    market = _json(".claude-plugin/marketplace.json")
    assert market["plugins"][0]["source"] == "./"
    assert market["plugins"][0]["name"] == "cc-autopilot-notify"


def test_hooks_use_plugin_root_and_exist():
    hooks = _json("hooks/hooks.json")["hooks"]
    for event in ("PreToolUse", "PostToolUse", "Notification", "Stop",
                  "StopFailure", "PermissionRequest", "SessionStart"):
        assert event in hooks, f"{event} not registered"
    for items in hooks.values():
        for item in items:
            for hook in item["hooks"]:
                command = hook["command"]
                assert "${CLAUDE_PLUGIN_ROOT}" in command, command
                rel = command.split("${CLAUDE_PLUGIN_ROOT}/", 1)[1].split()[0]
                assert (ROOT / rel).exists(), f"missing {rel}"


def test_wrapper_scripts_are_executable():
    for rel in ("scripts/ensure-daemon.sh", "scripts/cc-autopilot-notify"):
        assert os.access(ROOT / rel, os.X_OK), f"{rel} is not executable"
