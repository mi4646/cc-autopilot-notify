#!/usr/bin/env bash
# Remove cc-autopilot-notify: our hooks, our env key, the config, and the unit.
# Everything else in settings.json is left exactly as found.
set -euo pipefail

SETTINGS="${CC_AUTOPILOT_SETTINGS:-$HOME/.claude/settings.json}"
CONFIG="${CC_AUTOPILOT_CONFIG:-$HOME/.config/cc-autopilot-notify/config.json}"
UNIT="${CC_AUTOPILOT_UNIT:-$HOME/.config/systemd/user/cc-autopilot-notify.service}"

if command -v systemctl >/dev/null; then
  systemctl --user disable --now cc-autopilot-notify.service 2>/dev/null || true
fi
rm -f "$UNIT"
command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true

if [ -f "$SETTINGS" ]; then
  python3 - "$SETTINGS" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)

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
        inner = [h for h in item.get("hooks", [])
                 if "run_hook.py" not in str(h.get("command", ""))]
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

with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
  echo "cleaned hooks from $SETTINGS"
fi

rm -f "$CONFIG"
rmdir "$(dirname "$CONFIG")" 2>/dev/null || true
echo "removed $CONFIG"
echo "uninstalled. verify with: python3 -m cc_autopilot_notify.cli doctor"
