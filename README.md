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

Idle thresholds: **150 s** while the terminal is focused, **90 s** otherwise.
Touching the dialog resets the timer.

## Install

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
python3 -m cc_autopilot_notify.cli doctor
python3 -m cc_autopilot_notify.cli test-channel terminal
python3 -m cc_autopilot_notify.cli test-channel feishu
```

`doctor` must show `pebrel runtime`, `pebrel protocol version`,
`pebrel conversation capabilities`, `powershell foreground` and
`daemon heartbeat` as PASS. A FAIL there means auto-answer is not working, and
the reason is on the same line.

## Uninstall

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
