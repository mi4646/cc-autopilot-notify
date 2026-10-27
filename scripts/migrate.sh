#!/usr/bin/env bash
# Legacy direct install -> plugin. Explicit; never run automatically.
set -euo pipefail

export PYTHONPATH="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m cc_autopilot_notify.migrate
