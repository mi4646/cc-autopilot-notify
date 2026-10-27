#!/usr/bin/env bash
# Verify install.sh merges into settings.json without losing existing entries.
# Runs install in dry-run mode against a temp file; nothing real is touched.
set -euo pipefail
cd "$(dirname "$0")/.."

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/settings.json" <<'JSON'
{
  "env": {"KEEP_ME": "1"},
  "hooks": {
    "PreToolUse": [
      {"matcher": "x", "hooks": [{"type": "command", "command": "existing"}]}
    ]
  }
}
JSON

CC_AUTOPILOT_SETTINGS="$TMP/settings.json" CC_AUTOPILOT_DRY_RUN=1 ./install.sh > "$TMP/out.json"
CC_AUTOPILOT_SETTINGS="$TMP/settings.json" CC_AUTOPILOT_DRY_RUN=1 \
  CC_AUTOPILOT_ENABLE_PATH_B=1 ./install.sh > "$TMP/out-pathb.json"

# Dry run must not modify the input file.
grep -q '"KEEP_ME"' "$TMP/settings.json"
grep -q 'existing' "$TMP/settings.json"

# The projected result keeps everything and adds ours.
python3 - "$TMP/out.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert data["env"]["KEEP_ME"] == "1", "existing env key lost"
assert data["env"]["CLAUDE_AFK_TIMEOUT_MS"] == 300000, "afk timeout missing"
pre = data["hooks"]["PreToolUse"]
assert any(h["hooks"][0]["command"] == "existing" for h in pre), "existing hook lost"
flat = json.dumps(data)
assert "cc_autopilot_notify" in flat, "our hooks missing"
assert "PermissionRequest" not in data["hooks"], \
    "path B must stay off by default: it blocks the dialog"
PY

# Opting in wires path B.
python3 - "$TMP/out-pathb.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
assert "PermissionRequest" in data["hooks"], "path B not wired when opted in"
PY

echo "OK: merge preserves existing entries and adds ours"

# --- Real write path, from a foreign cwd -------------------------------
# The dry-run assertions above cannot see #5/#6: a broken writer, or a writer
# that only works when cwd is the repo. Exercise the actual install.
W=$(mktemp -d)
cat > "$W/settings.json" <<'JSON'
{"env": {"CLAUDE_AFK_TIMEOUT_MS": "600000"}, "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "other-tool"}]}]}}
JSON
(
  cd "$W" || exit 1
  CC_AUTOPILOT_FORCE=1 \
  CC_AUTOPILOT_SETTINGS="$W/settings.json" \
  CC_AUTOPILOT_CONFIG="$W/cfg/config.json" \
  CC_AUTOPILOT_UNIT="$W/unit.service" \
  /var/www/cc-autopilot-notify/install.sh >/dev/null
) || { echo "install failed from a foreign cwd"; exit 1; }

[ -f "$W/cfg/config.json" ] || { echo "config.json not written"; exit 1; }
[ -f "$W/unit.service" ] || { echo "unit not written"; exit 1; }

python3 - "$W/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert "PYTHONPATH=" in json.dumps(d["hooks"]), "hook command lacks PYTHONPATH"
assert any(h["hooks"][0]["command"] == "other-tool" for h in d["hooks"]["Stop"]), "foreign hook lost"
assert "StopFailure" in d["hooks"], "StopFailure not wired"
PY

# A same-value string must not abort (no FORCE needed the second time).
CC_AUTOPILOT_SETTINGS="$W/settings.json" CC_AUTOPILOT_CONFIG="$W/cfg/config.json" \
  CC_AUTOPILOT_UNIT="$W/unit.service" /var/www/cc-autopilot-notify/install.sh >/dev/null \
  || { echo "re-install aborted on a same-value string"; exit 1; }

CC_AUTOPILOT_SETTINGS="$W/settings.json" CC_AUTOPILOT_CONFIG="$W/cfg/config.json" \
  CC_AUTOPILOT_UNIT="$W/unit.service" /var/www/cc-autopilot-notify/uninstall.sh >/dev/null
python3 - "$W/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert str(d["env"]["CLAUDE_AFK_TIMEOUT_MS"]) == "600000", "original value not restored"
PY
rm -rf "$W"
echo "OK: real write path, foreign cwd, value restore"
