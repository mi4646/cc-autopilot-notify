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
