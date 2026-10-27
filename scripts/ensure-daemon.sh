#!/usr/bin/env bash
# SessionStart entry: start the polling daemon when it is not alive.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m cc_autopilot_notify.cli ensure-daemon
