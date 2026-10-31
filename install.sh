#!/usr/bin/env bash
# Install cc-autopilot-notify: merge hooks into Claude Code settings, set the
# AFK fallback, write the config, and install a systemd user unit.
#
# Safety rules (spec §9):
#   - never replace existing settings entries, only merge alongside them
#   - back up settings.json before touching it
#   - CC_AUTOPILOT_DRY_RUN=1 prints the projected settings and writes nothing
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SETTINGS="${CC_AUTOPILOT_SETTINGS:-$HOME/.claude/settings.json}"
CONFIG="${CC_AUTOPILOT_CONFIG:-$HOME/.config/cc-autopilot-notify/config.json}"
UNIT="${CC_AUTOPILOT_UNIT:-$HOME/.config/systemd/user/cc-autopilot-notify.service}"
# Honour a configured AFK timeout; fall back to the default when there is no
# readable config yet (the normal first-install case).
AFK_MS=$(PYTHONPATH="$REPO" python3 -c '
import sys
from cc_autopilot_notify import config
try:
    print(config.afk_timeout_ms(config.load(sys.argv[1])))
except Exception:
    print(300000)
' "$CONFIG" 2>/dev/null || echo 300000)

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

if [ ! -f "$SETTINGS" ]; then
  # stdout carries the projected settings in dry-run mode; keep it parseable.
  echo "note: $SETTINGS does not exist; a fresh one will be created" >&2
fi

# Projected settings go to stdout; nothing is written in dry-run mode.
PROJECTED=$(mktemp)
trap 'rm -f "$PROJECTED"' EXIT

python3 - "$SETTINGS" "$REPO" "$AFK_MS" > "$PROJECTED" <<'PY'
import json, os, shlex, sys

settings_path, repo, afk_ms = sys.argv[1], sys.argv[2], int(sys.argv[3])
setting = os.environ.get("CC_AUTOPILOT_DRY_RUN") != "1"

try:
    with open(settings_path, encoding="utf-8") as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
except json.JSONDecodeError as exc:
    sys.exit(f"refusing to touch {settings_path}: invalid JSON ({exc})")
except UnicodeDecodeError as exc:
    # Not a JSONDecodeError: Windows editors write UTF-16, which utf-8 cannot
    # read. Refuse rather than overwrite a file we do not understand.
    sys.exit(f"refusing to touch {settings_path}: not UTF-8 ({exc})")

# A null section is valid JSON but not usable; treat it as absent rather than
# crashing with AttributeError after the user already lost nothing.
if not isinstance(data.get("env"), dict):
    data["env"] = {}
# Existing entries are stored as strings, so compare the rendered value: a
# same-value string must not abort a fresh install.
existing = data["env"].get("CLAUDE_AFK_TIMEOUT_MS")
if existing is not None and str(existing) != str(afk_ms):
    if os.environ.get("CC_AUTOPILOT_FORCE") != "1":
        sys.exit(
            f"env.CLAUDE_AFK_TIMEOUT_MS is {existing!r}, this install wants "
            f"{afk_ms}.\nRe-run with CC_AUTOPILOT_FORCE=1 to change it."
        )
    # Remember the user's value so uninstall can put it back.
    data.setdefault("_ccAutopilotPrevious", {})["CLAUDE_AFK_TIMEOUT_MS"] = existing
data["env"]["CLAUDE_AFK_TIMEOUT_MS"] = afk_ms

runner = os.path.join(repo, "cc_autopilot_notify", "run_hook.py")
if not isinstance(data.get("hooks"), dict):
    data["hooks"] = {}
hooks = data["hooks"]

def entry(module, matcher, timeout=10):
    # PYTHONPATH is set explicitly: a hook is launched by path from an arbitrary
    # cwd, so the package is not importable without it. Quote both paths: a
    # checkout under a directory with spaces must still yield one argv token.
    return {"matcher": matcher,
            "hooks": [{"type": "command",
                       "command": (f"PYTHONPATH={shlex.quote(repo)} "
                                   f"python3 {shlex.quote(runner)} {module}"),
                       "timeout": timeout}]}

def add(event, module, matcher, timeout=10):
    bucket = hooks.setdefault(event, [])
    ours = entry(module, matcher, timeout)
    for item in bucket:
        for h in item.get("hooks", []):
            command = str(h.get("command", ""))
            # Only our own runner counts. Another project's run_hook.py is not
            # evidence that this hook is installed, and a stale checkout path
            # must be refreshed rather than skipped.
            if ("cc_autopilot_notify" in command and "run_hook.py" in command
                    and command.endswith(" " + module)):
                h.update(ours["hooks"][0])
                return
    bucket.append(ours)

add("PreToolUse", "pre_tool", "AskUserQuestion")
add("PostToolUse", "notify", "AskUserQuestion")
add("UserPromptSubmit", "notify", "")
add("Notification", "notify", "")
add("Stop", "notify", "")
add("StopFailure", "notify", "")

# Path B blocks the dialog until it returns (spec §4.2), so it stays off
# unless the user explicitly opts in. Its timeout must exceed its own wait,
# or Claude Code kills it before it can answer.
if os.environ.get("CC_AUTOPILOT_ENABLE_PATH_B") == "1":
    # wait is background_seconds (90s) + margin; the hook must outlive it
    add("PermissionRequest", "permission", "AskUserQuestion", timeout=180)

print(json.dumps(data, ensure_ascii=False, indent=2))
PY

if [ "${CC_AUTOPILOT_DRY_RUN:-}" = "1" ]; then
  cat "$PROJECTED"
  exit 0
fi

# Back up, then write the merged settings.
STAMP=$(date +%Y%m%d-%H%M%S)
if [ -f "$SETTINGS" ]; then
  cp "$SETTINGS" "$SETTINGS.bak-$STAMP"
  echo "backed up settings to $SETTINGS.bak-$STAMP"
fi
mkdir -p "$(dirname "$SETTINGS")"
mv "$PROJECTED" "$SETTINGS"

# Config file, backed up when present.
mkdir -p "$(dirname "$CONFIG")"
if [ -f "$CONFIG" ]; then
  cp "$CONFIG" "$CONFIG.bak-$STAMP"
  echo "backed up config to $CONFIG.bak-$STAMP"
fi
CONFIG_EXISTED=0
[ -f "$CONFIG" ] && CONFIG_EXISTED=1
PYTHONPATH="$REPO" python3 -c "
import sys
from cc_autopilot_notify import config
config.write_default(sys.argv[1])
" "$CONFIG" || { echo "error: could not write $CONFIG" >&2; exit 1; }
if [ "$CONFIG_EXISTED" = "1" ]; then
  echo "kept existing $CONFIG"
else
  echo "wrote $CONFIG"
fi

# systemd user unit: Pebrel is a Windows process, so it cannot host this.
mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNIT
[Unit]
Description=cc-autopilot-notify daemon
After=default.target

[Service]
Type=simple
ExecStart=$(command -v python3) -m cc_autopilot_notify.cli daemon
Restart=on-failure
RestartSec=5
WorkingDirectory="$REPO"
Environment="PYTHONPATH=$REPO"

[Install]
WantedBy=default.target
UNIT
echo "wrote $UNIT"

# Only touch systemd when the unit really lives in the systemd user dir. A
# throwaway CC_AUTOPILOT_UNIT (tests, dry runs) must never enable or start the
# real service: the service name is global, so a hardcoded one would.
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if command -v systemctl >/dev/null && [ "$(dirname "$UNIT")" = "$SYSTEMD_USER_DIR" ]; then
  systemctl --user daemon-reload || true
  systemctl --user enable --now "$(basename "$UNIT")" || \
    echo "warn: could not start the unit; run: systemctl --user enable --now $(basename "$UNIT")" >&2
fi

echo "installed. verify with: python3 -m cc_autopilot_notify.cli doctor"
