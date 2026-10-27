# Spike: does CC 2.1.308 still accept `updatedInput.answers`?

- Date opened: 2026-10-27
- CC version under test: **2.1.308** (`claude --version`)
- Status: **NOT YET RUN — blocking path B**

## Why this matters

`PermissionRequest` hook can return
`{"decision":{"behavior":"allow","updatedInput":{"answers":{...}}}}` to answer
`AskUserQuestion` without showing the dialog. The reference implementation
(whereisdw/claude-code-hooks-recipes) verified this on **2.1.283**. The binary
we inspected (2.1.308) contains:

```js
re = new Set(["answers", "annotations", "response", "followUp"]);
...
if (n.requiresUserInteraction?.()) {
  let Se = Cut(await cA(n, r, {...y, toolUseId: h}, { hookUpdatedInput: void 0 }), n.name);
  ...
}
```

`AskUserQuestion` defines `requiresUserInteraction(){ return !0 }` as a
constant. Whether that branch discards our `updatedInput` on 2.1.308 is
**not determinable by reading the bundle** — the path taken depends on
`e.kind` and other runtime state we cannot resolve statically.

So this is an empirical question, not a code-reading question.

## Probe

`tools/probe_path_b.py` — picks the first option of each question and returns
the hook payload above.

Verified offline that it emits the right shape:

```
$ echo '{"tool_name":"AskUserQuestion","tool_input":{"questions":[{"question":"Pick one","options":[{"label":"A"},{"label":"B"}]}]}}' \
    | python3 tools/probe_path_b.py
{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow", "updatedInput": {"answers": {"Pick one": "A"}}}}}
```

## Procedure

```bash
# 1. back up
cp ~/.claude/settings.json ~/.claude/settings.json.bak-probe

# 2. add a PermissionRequest entry pointing at the probe, e.g.
#    {"matcher": "AskUserQuestion",
#     "hooks": [{"type": "command",
#                "command": "python3 /var/www/cc-autopilot-notify/tools/probe_path_b.py",
#                "timeout": 10}]}

# 3. in a NEW claude session, ask something that makes Claude call AskUserQuestion

# 4. read the terminal:
#    A) accepted  -> "User answered Claude's questions: · <q> → <label>",
#                    and NO dialog appeared
#    B) discarded -> the dialog appears and waits

# 5. restore
cp ~/.claude/settings.json.bak-probe ~/.claude/settings.json
```

## Result

**Not run.** Requires editing the global `~/.claude/settings.json` and starting
a fresh Claude Code session, which cannot be done from inside the session being
edited.

## Consequence if B (discarded)

- Path B is unavailable on 2.1.308. Do not wire `PermissionRequest`.
- The system still works: path A (Pebrel `conversation.choose`) answers at the
  idle threshold, and native AFK covers the case where Pebrel is down.
- Record this in the ledger and in `doctor` so a future version bump re-checks
  rather than assumes.
