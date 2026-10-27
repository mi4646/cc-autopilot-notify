# Acceptance results — cc-autopilot-notify

Date: 2026-10-27
CC version: 2.1.308
Pebrel: 2.3.1 (7311e07)
Branch: `feat/implementation`

This file records what was actually run and observed, and what was not. A row
marked **not run** is a gap, not a pass.

## S1–S3: channels

| # | Check | Command | Result |
|---|---|---|---|
| S2 | Terminal OSC 9 | `cli test-channel terminal` | **sent** — emitted `ESC ]9;cc-autopilot-notify 测试: 测试通道 terminal BEL` (confirmed via `cat -v`) |
| S3 | Pebrel native toast | `cli test-channel pebrel` | **sent** — `pebrel notify-test` exited 0; toast delivered by Pebrel itself |
| S1 | Feishu card | `cli test-channel feishu` | **not run** — `CC_AUTOPILOT_FEISHU_WEBHOOK` is unset on this machine. The command correctly exits non-zero with `feishu: no webhook configured` rather than silently succeeding. |

To close S1: export the webhook, re-run the command, confirm the card arrives.

## S4–S5: dual thresholds

**Not run.** These need a live `AskUserQuestion` with the daemon running and
the user deliberately not answering for 150 s / 90 s. Driving that requires
installing the hooks into the global `~/.claude/settings.json` and starting a
fresh session, which is outside what can be done from inside this session.

What *is* proven instead: the threshold logic is covered by
`tests/test_daemon.py::test_does_not_fire_before_foreground_threshold`,
`::test_fires_at_foreground_threshold`,
`::test_background_threshold_is_used_when_not_foreground`,
`::test_undeterminable_foreground_uses_background_threshold` — all passing.

Unit coverage does not replace the wall-clock acceptance; it only proves the
arithmetic. Record real measured times here once run.

## S6: human wins

**Not run** (same blocker as S4). Logic covered by
`::test_revision_change_resets_activity`, which additionally was checked
mutation-style: removing the reset branch makes it fail.

## S7: recommended option

Covered by unit tests: `tests/test_judge.py::test_english_recommended_suffix`,
`::test_chinese_recommended_suffix`, `::test_description_marker`,
`::test_marker_wins_without_calling_model`, `::test_ambiguous_markers_return_none`.

**Not run** against a live dialog.

## S8: no permanent stall

Partially proven:

- `::test_prompt_null_means_answered_and_record_is_cleared` — a question that
  disappears is cleaned up.
- `::test_two_choose_failures_drop_record` — two refusals drop the record
  rather than retrying forever.
- `::test_prompt_changed_is_retried_once` — the race with a human answer is
  bounded to one retry.
- `doctor` reports `daemon heartbeat` FAIL when the daemon is not running, so a
  dead daemon is visible rather than silent.

**Not run**: killing the daemon mid-question and confirming native AFK resumes
at 300 s.

## Full suite

```
$ python -m pytest tests/ -q
68 passed

$ bash tests/test_install_merge.sh
OK: merge preserves existing entries and adds ours
```

## Install / uninstall round trip

Verified on a temp copy of a realistic settings file:

- after install: existing `env.KEEP_ME`, existing `PreToolUse` hook, and our
  new entries all present
- after uninstall: exact same file as before install, `0` occurrences of
  `cc_autopilot_notify` or `CLAUDE_AFK_TIMEOUT_MS`

## doctor on this machine

```
PASS  claude version: 2.1.308 (Claude Code)
PASS  pebrel runtime: app 2.3.1 (7311e07)
PASS  pebrel protocol version: protocol_version=1 (expected 1)
PASS  pebrel conversation capabilities: conversation.read+choose
PASS  powershell foreground: foreground=True
FAIL  daemon heartbeat: no heartbeat file; daemon not running
SKIP  feishu webhook: not configured
PASS  settings residue: no cc_autopilot_notify entries
SKIP  updatedInput.answers accepted: unverified on this CC version
```

Two of these are expected before install (daemon not started, webhook unset).
The third is the honest state of path B.

## Summary

| Area | State |
|---|---|
| Unit tests | 68 passing |
| Terminal + Pebrel channels | verified live |
| Feishu | blocked on a webhook |
| Install/uninstall | verified by round trip |
| Dual thresholds (live) | **not run** |
| Path B (`updatedInput.answers`) | **not verified on 2.1.308** |
| Path A (Pebrel `conversation.choose`) | protocol verified; live auto-answer **not run** |

The three not-run items all need the same thing: installing into the global
Claude Code settings and starting a fresh session with the daemon up.
