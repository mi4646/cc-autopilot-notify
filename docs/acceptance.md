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

Re-run 2026-10-27 after the plugin-packaging fix wave
(`python -m pytest tests/ -q`, `bash tests/test_install_merge.sh`):

```
$ python -m pytest tests/ -q
156 passed

$ bash tests/test_install_merge.sh
OK: merge preserves existing entries and adds ours
OK: real write path, foreign cwd, value restore
OK: non-UTF-8 settings refused without touching anything
```

## Post-review fixes

A fresh-context review of the whole branch found four Critical wire-format
defects that the unit tests could not see (they injected a fake `read`). All
were fixed with tests that assert the real shape:

| Defect | Fix | Verified by |
|---|---|---|
| Hooks could not import the package | `PYTHONPATH` in `run_hook.py` and every installed hook command | ran from `/tmp`, exit 0, state file written |
| `pane_id`/`window_id` fabricated; runtime refuses pane 0 | `PEBREL_PANE_ID`, then `agents.list` resolves session → (window, pane) | live: pane 0 refused, real pane reads |
| `conversation.choose` missing `identity.epoch` | epoch passed from `read`'s `identity` | `test_choose_sends_the_fields_the_live_server_requires` |
| `prompt_id` read from a key `read` never returns | `prompt_id_of()` searches the prompt and the top level; missing → give up loudly | `test_missing_prompt_id_gives_up_instead_of_guessing` |

The live `choose` call still has **not** been observed succeeding, because no
live pane currently returns a non-null `prompt`. What is proven is that the
client no longer sends a request the server is documented to reject.

## Install / uninstall round trip

Verified on a temp copy of a realistic settings file, run from a foreign cwd:

- after install: existing `env.KEEP_ME`, existing `PreToolUse` hook, our
  entries, `config.json`, and the unit all present
- hook commands carry `PYTHONPATH`
- a pre-existing `CLAUDE_AFK_TIMEOUT_MS` of `"600000"` survives install
  (with `CC_AUTOPILOT_FORCE=1`) and is **restored** by uninstall
- `StopFailure` is wired

## doctor on this machine

```
PASS  claude version: 2.1.308 (Claude Code)
PASS  pebrel runtime: app 2.3.1 (7311e07)
PASS  pebrel protocol version: protocol_version=1 (expected 1)
PASS  pebrel conversation capabilities: conversation.read+choose
PASS  powershell foreground: foreground=False
FAIL  daemon heartbeat: no heartbeat file; daemon not running
SKIP  feishu webhook: not configured
PASS  legacy install: no legacy entries in settings.json
SKIP  updatedInput.answers accepted: unverified on this CC version
```

The FAIL and the two SKIPs are the correct state before install.

## Summary

| Area | State |
|---|---|
| Unit tests | 156 passing (2026-10-27, `python -m pytest tests/ -q`) |
| Installer round trip (real write, foreign cwd, value restore) | verified |
| Hook end-to-end (import, state, OSC, exit 0) | verified from an external cwd |
| Pebrel protocol (describe / read / agents.list) | verified live |
| Terminal + Pebrel channels | verified live |
| Feishu | blocked on a webhook; body-code handling unit-tested |
| Live `conversation.choose` success | **not observed** — no live non-null `prompt` yet |
| Dual thresholds (wall clock) | **not run** |
| Path B (`updatedInput.answers`) | **not verified on 2.1.308** |

The three not-run items need the same thing: install into the global Claude
Code settings, start the daemon, and ask one real question.


## 2026-10-27 追加：路径 A 重定向（A′）

实测结论（见上文与 journalctl 记录）：

1. `conversation.read.prompt` 对 Claude Code 的问答面板**永远为 null**，
   即使 `task_state=attention`（130 秒探针、11 次采样、0 次命中）。
   路径 A 依赖的 `conversation.choose` 因此不可用。
2. 新王牌：`agents.list` 的 `task_state == "attention"` 能标记「面板正在等」；
   `pane read` 能拿到完整面板文本；`send-key` 能注入按键（净零探测通过）。
3. A′ 机制 = attention 发现 + hook 结构化数据选答案 + send-key 提交。
   spec §4.2 路径 A 自此按 A′ 理解；`conversation.choose` 实现保留但不再调用。

## 2026-10-27 22:xx 端到端验证：通过

隔离测试会话（Pebrel `agent-start` 起 pane 16），让 Claude 用 AskUserQuestion
问「Alpha / Beta」，并把 Alpha 标为 `(Recommended)`。全程无人操作。

```
18:36:16 INFO daemon: 1e90fda8-…: auto-answered option 0
```

```
● User answered Claude's questions:
  ⎿  · 陛下要选哪一个？（我推荐 Alpha） → Alpha (Recommended)
```

判定：**路径 A′ 打通**。选中的正是 Claude 自己标注的推荐项，说明判卷第一级
（读推荐标记）生效，且按键注入（`pane.send_key`）把答案提交到了正确的行。

失败过的两次及其原因，留作记录：

1. 首轮未答题：`prompt` 为 `null` 被当成「问题已结束」→ 记录一秒即被清除。
2. 二轮未答题：daemon 因 `Deps` 缺 `press` 字段崩溃，systemd 重启 6 次后放弃。

两次都不是协议问题，是本地实现缺陷。

## 2026-10-27 收尾：飞书接入与旧插件停用

- `env.CC_AUTOPILOT_FEISHU_WEBHOOK` 写入 `~/.claude/settings.json`
  （值取自旧 cc-notify-hooks 配置，全程未打印）。修改前已备份
  `settings.json.bak-*`。
- `enabledPlugins."cc-notify-hooks@cc-notify-hooks"` 置 `false`。
- 实测 `test-channel feishu` → `feishu: sent`（HTTP 200 且业务 code=0）。
- doctor：`feishu webhook: configured`、daemon 心跳 PASS。直装写入 settings.json
  的 hooks 在插件版的 `legacy install` 检查中会显示为 legacy，需运行
  `/cc-autopilot-notify:migrate`。

## 2026-10-28: 系统空闲阈值（0.2.2）

逻辑变更：自动应答不再以「面板 revision 多久没变」单独计时，而是要求
**Windows 系统空闲**达到阈值：

- Pebrel 是前台窗口 → `idle ≥ 150 s`
- Pebrel 不是前台或未知 → `idle ≥ 90 s`
- 面板 revision 变化不再作为取消条件；**任何真实键鼠输入**会让系统空闲归零，从而立即取消本轮
- 问题出现不足 `min_duration_seconds`（30 s）→ 不答
- 探测失败 → 不答；连续失败 60 s 发一条告警，恢复后发一条恢复通知

探测由 `presence.sample()` 一次 PowerShell 调用同时取回前台归属与空闲秒数，
成功结果缓存 5 s。spike 见 `docs/spikes/2026-10-28-getlastinputinfo-wsl.md`。

单元覆盖（`python -m pytest tests/ -q`，211 passed）：

- `test_presence.py::test_sample_*` — 解析、ERR、越界、超时、单次调用
- `test_daemon.py::test_foreground_idle_*` / `::test_background_idle_*` — 150/90 边界
- `::test_unknown_foreground_uses_background_threshold`、`::test_unknown_idle_never_fires`
- `::test_min_duration_blocks_a_fresh_question`、`::test_batch_answers_every_pending_in_one_tick`
- `::test_own_press_resets_the_next_window`
- `::test_shared_gate_resamples_after_an_answer` — 应答后缓存失效，不会重复答
- `::test_gate_*` — 5 s 缓存、失败退避、60 s 告警、恢复通知
- `test_doctor.py::test_doctor_reports_presence_*`

**实机验收：部分运行。** 锁屏路径已通过（见下）；「人在电脑前不答」仍需单独跑一次。

### 2026-10-28 锁屏实测第 2 次（0.2.3）：通过

- **操作**：再次发出测试用 `AskUserQuestion`，按 Win+L 锁屏，等待约 90 秒。
- **结果**：本题被自动选为 **A**。`daemon.log` 记录：
  `2026-10-28 17:42:52 INFO daemon: 4892ef03-…: auto-answered option 0`
- **同时验证**：
  - 没有 `user is interacting` 刷屏——revision 自绘不再阻塞；
  - 约 90 秒触发，与后台阈值一致；
  - `pane.send_key` 在锁屏状态下成功（前提：陛下在返回前没有解锁或手动点击）。
- **残留限制**：没有独立记录按下瞬间的锁屏状态与 `idle` 值。若要严格证明「按键发生在锁屏期间」，需要在按下时同步记录 `GetForegroundWindow` 与 `idle`。本次按操作约定判定为通过。

### 部署记录（已核对）

| 项 | 证据 |
|---|---|
| 提交 | `0cad1f9 feat: gate auto-answer on system idle`，已推送 `master` |
| 测试 | `python -m pytest tests/ -q` → **212 passed** |
| 插件 | `claude plugin list` → `cc-autopilot-notify@cc-autopilot-notify` **0.2.2** |
| 缓存 | `~/.claude/plugins/cache/cc-autopilot-notify/cc-autopilot-notify/0.2.2/` |
| daemon | pid `504363`，心跳 `version=0.2.2`、`plugin_root=.../0.2.2` |
| doctor | `powershell presence: PASS foreground=false idle=0s`；`daemon heartbeat: PASS` |
| 配置 | `foreground_seconds=150`、`background_seconds=90`，运行时读值一致 |

部署完成不等于验收通过：上面三项实机场景仍未运行。

### 2026-10-28 锁屏实测第 1 次：失败（已定位并修复）

- **操作**：发出一个测试用 `AskUserQuestion`，锁屏等待 90 秒以上。
- **结果**：300 秒后工具超时，面板没有被自动应答。
- **日志**：`daemon.log` 每秒一条
  `4892ef03-…: user is interacting; timer reset`，从未进入空闲判定。
- **原因**：面板底部的状态行/计时每秒自行重绘，`pane.read` 的 60 行文本
  sha256 每秒变化；旧的 revision 门控把它误判为「用户正在操作」。
- **修复**：删除 revision 硬门控；真实交互由系统空闲判定（按键/鼠标立即归零）。
  新增回归测试 `tests/test_daemon.py::test_panel_redraw_alone_does_not_block`。
- **状态**：已在 0.2.3 重测通过（见下一节）。第一次实测不能算通过。
