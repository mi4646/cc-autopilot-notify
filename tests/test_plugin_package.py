import json
import os
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _json(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _hook_commands():
    for items in _json("hooks/hooks.json")["hooks"].values():
        for item in items:
            for hook in item["hooks"]:
                yield hook["command"]


def test_plugin_manifest_matches_pyproject_version():
    plugin = _json(".claude-plugin/plugin.json")
    assert plugin["name"] == "cc-autopilot-notify"
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{plugin["version"]}"' in text


def test_marketplace_points_at_the_repo_root():
    market = _json(".claude-plugin/marketplace.json")
    assert market["plugins"][0]["source"] == "./"
    assert market["plugins"][0]["name"] == "cc-autopilot-notify"


def test_marketplace_version_matches_the_plugin():
    plugin = _json(".claude-plugin/plugin.json")
    market = _json(".claude-plugin/marketplace.json")
    assert market["plugins"][0]["version"] == plugin["version"]


def test_hooks_use_quoted_plugin_root_and_exist():
    hooks = _json("hooks/hooks.json")["hooks"]
    for event in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Notification",
                  "Stop", "StopFailure", "PermissionRequest", "SessionStart"):
        assert event in hooks, f"{event} not registered"
    for command in _hook_commands():
        assert "${CLAUDE_PLUGIN_ROOT}" in command, command
        assert '"${CLAUDE_PLUGIN_ROOT}/' in command, (
            f"plugin-root path must be quoted: {command}")
        paths = [part for part in shlex.split(command)
                 if part.startswith("${CLAUDE_PLUGIN_ROOT}/")]
        assert len(paths) == 1, command
        rel = paths[0].split("${CLAUDE_PLUGIN_ROOT}/", 1)[1]
        assert (ROOT / rel).exists(), f"missing {rel}"


def test_hook_commands_survive_a_plugin_root_with_spaces():
    for command in _hook_commands():
        rendered = command.replace("${CLAUDE_PLUGIN_ROOT}", "/tmp/plugin root")
        parts = shlex.split(rendered)
        assert any(part.startswith("/tmp/plugin root/") for part in parts), (
            f"an unquoted plugin root splits a path with spaces: {command}")


def test_wrapper_scripts_are_executable():
    for rel in ("scripts/ensure-daemon.sh", "scripts/cc-autopilot-notify"):
        assert os.access(ROOT / rel, os.X_OK), f"{rel} is not executable"
