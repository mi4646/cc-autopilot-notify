"""Write the probe hook as its own file so it can be registered by hand.

Step 0 of the spec: does Claude Code 2.1.308 still accept a PermissionRequest
hook's `updatedInput.answers`? That cannot be proven offline — it needs a real
session and a real question. This script is the thing you point the hook at.

Register it temporarily, trigger a question, read the terminal, then put your
settings back. The result goes in
docs/spikes/2026-10-27-permission-request-answers.md.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0

    if event.get("tool_name") != "AskUserQuestion":
        return 0

    questions = ((event.get("tool_input") or {}).get("questions")) or []
    answers: dict[str, str] = {}
    for question in questions:
        if not isinstance(question, dict):
            continue
        text = question.get("question")
        options = [o for o in (question.get("options") or []) if isinstance(o, dict)]
        if text and options:
            answers[text] = str(options[0].get("label", ""))

    if not answers:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow", "updatedInput": {"answers": answers}},
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
