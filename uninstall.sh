#!/usr/bin/env bash
# Remove cc-autopilot-notify: our hooks, our env key, the config, and the unit.
# Everything else in settings.json is left exactly as found.
set -euo pipefail

SETTINGS="${CC_AUTOPILOT_SETTINGS:-$HOME/.claude/settings.json}"
CONFIG="${CC_AUTOPILOT_CONFIG:-$HOME/.config/cc-autopilot-notify/config.json}"
UNIT="${CC_AUTOPILOT_UNIT:-$HOME/.config/systemd/user/cc-autopilot-notify.service}"

# Rewrite settings BEFORE removing anything. A settings file we cannot read must
# abort here: crashing after the unit is gone would leave a half-uninstall, with
# the service removed but our hooks still registered.
if [ -f "$SETTINGS" ]; then
  python3 - "$SETTINGS" <<'PY'
import json, os, shutil, sys, tempfile, time
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except json.JSONDecodeError as exc:
    sys.exit(f"refusing to touch {path}: invalid JSON ({exc}); nothing removed")
except UnicodeDecodeError as exc:
    # Not a JSONDecodeError: Windows editors write UTF-16, which utf-8 cannot
    # read. Refuse rather than overwrite a file we do not understand.
    sys.exit(f"refusing to touch {path}: not UTF-8 ({exc}); nothing removed")

env = data.get("env") or {}
# Put back whatever the user had before install, rather than just deleting.
previous = (data.get("_ccAutopilotPrevious") or {}).get("CLAUDE_AFK_TIMEOUT_MS")
if previous is not None:
    env["CLAUDE_AFK_TIMEOUT_MS"] = previous
else:
    env.pop("CLAUDE_AFK_TIMEOUT_MS", None)
if env:
    data["env"] = env
else:
    data.pop("env", None)

prev_map = data.get("_ccAutopilotPrevious") or {}
prev_map.pop("CLAUDE_AFK_TIMEOUT_MS", None)
if prev_map:
    data["_ccAutopilotPrevious"] = prev_map
else:
    data.pop("_ccAutopilotPrevious", None)

hooks = data.get("hooks") or {}
for event in list(hooks):
    kept = []
    for item in hooks[event]:
        # Only remove our own runner. Another project may also ship a
        # run_hook.py, and uninstalling this plugin must not touch it.
        def is_ours(command):
            return ("run_hook.py" in command
                    and "cc_autopilot_notify" in command)

        inner = [h for h in item.get("hooks", [])
                 if not is_ours(str(h.get("command", "")))]
        if inner:
            kept.append({**item, "hooks": inner})
        elif not item.get("hooks"):
            kept.append(item)
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event)
if hooks:
    data["hooks"] = hooks
else:
    data.pop("hooks", None)

# The README promises a backup before this file is rewritten.
backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
try:
    shutil.copy2(path, backup)
except OSError as exc:
    sys.exit(f"refusing to rewrite {path}: cannot write backup ({exc})")

# Write through a temp file: opening settings.json for writing truncates it
# first, so a failure mid-dump would leave the file empty. mkstemp defaults to
# 0600, so carry the original mode over before replacing.
original_mode = os.stat(path).st_mode
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                           prefix=".settings-", suffix=".json")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.chmod(tmp, original_mode)
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
  echo "cleaned hooks from $SETTINGS"
fi

# Only now remove the unit: everything that can fail has already run.
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if command -v systemctl >/dev/null && [ "$(dirname "$UNIT")" = "$SYSTEMD_USER_DIR" ]; then
  systemctl --user disable --now "$(basename "$UNIT")" 2>/dev/null || true
fi
rm -f "$UNIT"
command -v systemctl >/dev/null && [ "$(dirname "$UNIT")" = "$SYSTEMD_USER_DIR" ] \
  && systemctl --user daemon-reload 2>/dev/null || true

rm -f "$CONFIG"
rmdir "$(dirname "$CONFIG")" 2>/dev/null || true
echo "removed $CONFIG"
echo "uninstalled. verify with: python3 -m cc_autopilot_notify.cli doctor"
