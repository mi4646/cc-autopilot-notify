#!/usr/bin/env bash
# Verify install.sh merges into settings.json without losing existing entries.
# Runs install in dry-run mode against a temp file; nothing real is touched.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)

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
  "$REPO/install.sh" >/dev/null
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
  CC_AUTOPILOT_UNIT="$W/unit.service" "$REPO/install.sh" >/dev/null \
  || { echo "re-install aborted on a same-value string"; exit 1; }

chmod 644 "$W/settings.json"
MODE_BEFORE=$(stat -c '%a' "$W/settings.json")
CC_AUTOPILOT_SETTINGS="$W/settings.json" CC_AUTOPILOT_CONFIG="$W/cfg/config.json" \
  CC_AUTOPILOT_UNIT="$W/unit.service" "$REPO/uninstall.sh" >/dev/null
python3 - "$W/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert str(d["env"]["CLAUDE_AFK_TIMEOUT_MS"]) == "600000", "original value not restored"
PY
# The atomic rewrite must not silently change the file mode (mkstemp is 0600).
[ "$(stat -c '%a' "$W/settings.json")" = "$MODE_BEFORE" ] \
  || { echo "uninstall changed settings.json mode"; exit 1; }
rm -rf "$W"
echo "OK: real write path, foreign cwd, value restore"

# --- Non-UTF-8 settings (Windows editors write UTF-16) -------------------
# uninstall must abort BEFORE removing the unit: crashing after it is gone
# leaves the service deleted but our hooks still registered.
U=$(mktemp -d)
python3 - "$U" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
(d / "settings.json").write_bytes(json.dumps({
    "env": {"CLAUDE_AFK_TIMEOUT_MS": "300000"},
    "hooks": {"PreToolUse": [{"hooks": [{"type": "command",
                                         "command": "python run_hook.py pre_tool"}]}]},
}).encode("utf-16"))
(d / "unit.service").write_text("unit\n")
(d / "settings.before").write_bytes((d / "settings.json").read_bytes())
PY

if CC_AUTOPILOT_SETTINGS="$U/settings.json" CC_AUTOPILOT_CONFIG="$U/cfg/config.json" \
     CC_AUTOPILOT_UNIT="$U/unit.service" ./uninstall.sh >/dev/null 2>&1; then
  echo "uninstall accepted a non-UTF-8 settings.json"; exit 1
fi
[ -f "$U/unit.service" ] || { echo "unit removed despite uninstall aborting"; exit 1; }
cmp -s "$U/settings.json" "$U/settings.before" || { echo "settings.json was rewritten"; exit 1; }

if CC_AUTOPILOT_DRY_RUN=1 CC_AUTOPILOT_SETTINGS="$U/settings.json" \
     CC_AUTOPILOT_CONFIG="$U/cfg/config.json" CC_AUTOPILOT_UNIT="$U/unit.service" \
     ./install.sh >/dev/null 2>&1; then
  echo "install accepted a non-UTF-8 settings.json"; exit 1
fi
cmp -s "$U/settings.json" "$U/settings.before" || { echo "install rewrote settings.json"; exit 1; }
rm -rf "$U"
echo "OK: non-UTF-8 settings refused without touching anything"

# --- uninstall removes only our runner ---------------------------------
# Another project may also ship run_hook.py; only entries that name our
# package may be removed.
V=$(mktemp -d)
cat > "$V/settings.json" <<'JSON'
{"hooks": {"Stop": [
  {"matcher": "", "hooks": [{"type": "command", "command": "python3 /other/project/run_hook.py notify"}]},
  {"matcher": "", "hooks": [{"type": "command", "command": "PYTHONPATH=/repo python3 /repo/cc_autopilot_notify/run_hook.py notify"}]}
]}}
JSON
CC_AUTOPILOT_SETTINGS="$V/settings.json" CC_AUTOPILOT_CONFIG="$V/config.json" \
  CC_AUTOPILOT_UNIT="$V/unit.service" "$REPO/uninstall.sh" >/dev/null
python3 - "$V/settings.json" <<'PY'
import json, sys
flat = json.dumps(json.load(open(sys.argv[1])))
assert "/other/project/run_hook.py" in flat, "uninstall removed another project's hook"
assert "cc_autopilot_notify" not in flat, "our hook survived uninstall"
PY
rm -rf "$V"
echo "OK: uninstall leaves foreign run_hook.py entries alone"

# --- re-install is idempotent ------------------------------------------
R=$(mktemp -d)
for _ in 1 2; do
  CC_AUTOPILOT_SETTINGS="$R/settings.json" CC_AUTOPILOT_CONFIG="$R/cfg/config.json" \
    CC_AUTOPILOT_UNIT="$R/unit.service" "$REPO/install.sh" >/dev/null
done
python3 - "$R/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
stops = [h for item in d["hooks"]["Stop"] for h in item.get("hooks", [])
         if "cc_autopilot_notify" in str(h.get("command", ""))
         and "run_hook.py" in str(h.get("command", ""))]
assert len(stops) == 1, f"duplicate Stop hooks after re-install: {stops}"
PY
rm -rf "$R"
echo "OK: re-install does not duplicate hooks"

# --- an existing config is never overwritten ---------------------------
C=$(mktemp -d)
mkdir -p "$C/cfg"
printf '%s' '{"channels":{"feishu":{"webhook":"https://open.feishu.cn/open-apis/bot/v2/hook/KEEP"}}}' \
  > "$C/cfg/config.json"
chmod 600 "$C/cfg/config.json"
CC_AUTOPILOT_SETTINGS="$C/settings.json" CC_AUTOPILOT_CONFIG="$C/cfg/config.json" \
  CC_AUTOPILOT_UNIT="$C/unit.service" "$REPO/install.sh" >/dev/null
python3 - "$C/cfg/config.json" <<'PY'
import json, os, sys
path = sys.argv[1]
d = json.load(open(path))
assert d["channels"]["feishu"]["webhook"].endswith("KEEP"), "install overwrote the webhook"
assert (os.stat(path).st_mode & 0o777) == 0o600, "config mode is not 0600"
PY
rm -rf "$C"
echo "OK: install keeps an existing config and its mode"

# --- a checkout path with spaces still yields one argv token -----------
S="$TMP/with space"
mkdir -p "$S"
tar -C "$REPO" --exclude=.git --exclude=.context --exclude=.gstack \
  --exclude=.pytest_cache -cf - . | tar -C "$S" -xf -
CC_AUTOPILOT_SETTINGS="$TMP/space-settings.json" \
  CC_AUTOPILOT_CONFIG="$TMP/space-cfg/config.json" \
  CC_AUTOPILOT_UNIT="$TMP/space.service" CC_AUTOPILOT_DRY_RUN=1 \
  "$S/install.sh" > "$TMP/space-out.json" 2> "$TMP/space-err.txt" \
  || { echo "install failed under a path with spaces"; cat "$TMP/space-err.txt"; exit 1; }
[ -s "$TMP/space-out.json" ] \
  || { echo "install produced no settings under a path with spaces"; cat "$TMP/space-err.txt"; exit 1; }
python3 - "$TMP/space-out.json" "$S" <<'PY'
import json, shlex, sys
data = json.load(open(sys.argv[1]))
commands = [h["command"] for items in data["hooks"].values()
            for item in items for h in item.get("hooks", [])]
assert commands, "no hooks projected"
for command in commands:
    parts = shlex.split(command)
    assert any(sys.argv[2] in part for part in parts), \
        f"checkout path split into multiple argv tokens: {command}"
PY
echo "OK: paths with spaces stay a single token"

# --- null sections are absent, not a crash -----------------------------
N=$(mktemp -d)
printf '%s' '{"env": null, "hooks": null}' > "$N/settings.json"
CC_AUTOPILOT_SETTINGS="$N/settings.json" CC_AUTOPILOT_CONFIG="$N/cfg/config.json" \
  CC_AUTOPILOT_UNIT="$N/unit.service" "$REPO/install.sh" >/dev/null \
  || { echo "install crashed on null sections"; exit 1; }
python3 - "$N/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["env"]["CLAUDE_AFK_TIMEOUT_MS"] == 300000
assert "PreToolUse" in d["hooks"]
PY
rm -rf "$N"
echo "OK: null env/hooks sections are replaced"

# --- uninstall writes the promised backup ------------------------------
B=$(mktemp -d)
printf '%s' '{"hooks": {}}' > "$B/settings.json"
CC_AUTOPILOT_SETTINGS="$B/settings.json" CC_AUTOPILOT_CONFIG="$B/config.json" \
  CC_AUTOPILOT_UNIT="$B/unit.service" "$REPO/uninstall.sh" >/dev/null
ls "$B"/settings.json.bak-* >/dev/null 2>&1 \
  || { echo "uninstall wrote no backup"; exit 1; }
rm -rf "$B"
echo "OK: uninstall backs up settings.json"
