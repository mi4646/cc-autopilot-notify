# cc-autopilot-notify

Notifications and unattended auto-answer for Claude Code on WSL with the Pebrel
terminal.

When Claude asks a question and you are not there, this answers it — preferring
the option Claude itself marked as recommended — and tells you through Feishu,
the terminal, and a Windows toast.

## How it works

Three tiers, each covering the one above it failing:

| Tier | Mechanism | When it applies |
|---|---|---|
| A (preferred) | A daemon polls Pebrel's runtime and submits the answer with `conversation.choose` | Pebrel runtime reachable |
| B (fallback) | A blocking `PermissionRequest` hook returns `updatedInput.answers` | Only when opted in **and** A is unavailable |
| C (last resort) | Claude Code's own AFK continue (`CLAUDE_AFK_TIMEOUT_MS=300000`) | Anything above fails |

Path A leaves the dialog on screen the whole time, so you can answer yourself
and win. Path B blocks the dialog, which is why it is off unless you ask for it.

Idle thresholds: a question is answered only after the **system** has had no
keyboard or mouse input for **150 s** while Pebrel is the foreground window,
or **90 s** otherwise. Touching the dialog resets the timer immediately;
typing in another window keeps system idle at zero, so the timer does not run.

### Notification policy

- **Questions** notify immediately (`PreToolUse` on `AskUserQuestion`).
- **"Claude finished"** uses Claude Code's own `idle_prompt` notification: it
  fires about 60 s after Claude stops, only if you have not typed and no
  background agent is still running. `Stop` itself fires every turn, so it is
  deliberately silent; `StopFailure` still notifies.
- Only notifications that need a human are forwarded: `idle_prompt`,
  `permission_prompt`, `agent_needs_input`, and elicitation dialogs. Progress
  and background-completion notifications are dropped.
- An identical notification is suppressed for `notify.dedupe_seconds`
  (default 300).
- `quiet_hours` silences `idle_prompt` inside the window; approval and error
  notifications still get through.

## Install (plugin)

```bash
claude plugin marketplace add mi4646/cc-autopilot-notify
claude plugin install cc-autopilot-notify@cc-autopilot-notify
```

The daemon starts automatically on the next session. Set the Feishu webhook in
`~/.config/cc-autopilot-notify/config.json` (created on first SessionStart with
mode `0600`):

```json
{"channels": {"feishu": {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/..."}}}
```

`CC_AUTOPILOT_FEISHU_WEBHOOK` still works as a fallback. Leaving `webhook` as
`null` or an empty string also falls back to it.

### Upgrading from the legacy direct install

```bash
/cc-autopilot-notify:migrate
```

It removes only our `run_hook.py` entries from `settings.json` (entries that
also mention `cc_autopilot_notify`), moves the webhook into the config, and
stops the old systemd unit. Unrelated entries are kept; a backup is written
first.

### Tier C

Plugins cannot write `settings.json`. To keep the AFK fallback, set it yourself:

```json
{"env": {"CLAUDE_AFK_TIMEOUT_MS": 300000}}
```

The legacy section below still works for a non-plugin install.

## Install (legacy direct)

```bash
./install.sh                 # previews nothing, writes settings/hook/config/unit
CC_AUTOPILOT_DRY_RUN=1 ./install.sh   # print the projected settings only
```

Set the Feishu webhook in your environment (never in a file that gets committed):

```bash
export CC_AUTOPILOT_FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/..."
```

Opt in to path B only if the Pebrel runtime is unavailable on your machine:

```bash
CC_AUTOPILOT_ENABLE_PATH_B=1 ./install.sh
```

## Verify

```bash
bash scripts/cc-autopilot-notify doctor
bash scripts/cc-autopilot-notify test-channel feishu
```

`doctor` must show `pebrel runtime`, `pebrel protocol version`,
`pebrel conversation capabilities`, `powershell foreground` and
`daemon heartbeat` as PASS. A FAIL there means auto-answer is not working, and
the reason is on the same line.

## Uninstall (legacy direct)

```bash
./uninstall.sh
```

Removes our hooks, our `env` key, the config and the unit; every other entry in
`settings.json` is left untouched. Backups are written next to `settings.json`
as `settings.json.bak-YYYYMMDD-HHMMSS`.

## Tests

```bash
python -m pytest tests/ -q
bash tests/test_install_merge.sh
```

## Design notes

- Runtime dependencies: none beyond the Python standard library.
- The Pebrel runtime token is read from `%APPDATA%\Pebrel\runtime.port` and is
  never logged, notified, or written to a state file.
- A notification that fails is logged and skipped. It never blocks a turn.
- The live shape of an open question (`conversation.read` → `prompt`) has not
  been observed yet; only `prompt: null` has. An unrecognised shape raises
  instead of guessing, because a silently wrong answer is worse than none.

Design spec: `docs/superpowers/specs/2026-10-27-cc-autopilot-notify-design.md`
Implementation plan: `docs/superpowers/plans/2026-10-27-cc-autopilot-notify.md`
