# 锁屏自动续跑（autopilot continue）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude Code 回合结束后，锁屏状态下 daemon 自动 Tab+Enter 执行输入框的「下一步建议」，循环直到无建议（阶段完成），带每会话上限与逐条飞书留痕。

**Architecture:** 新模块 `continue.py` 集中建议匹配与续跑状态机（daemon 每秒单点调用）；`Stop`/`StopFailure` hook 写 `finished_at` 触发，`UserPromptSubmit` 以 `last_auto_submit` 标记区分人/daemon 提交；锁屏经 PowerShell WTS `WTSSessionInfoEx` 判定；默认关 + 影子模式先行。

**Tech Stack:** Python 3 标准库（零新依赖）、pytest、PowerShell P/Invoke（WTS）、Pebrel runtime IPC（`pane.read`/`pane.send_key`）。

**Spec:** `docs/superpowers/specs/2026-10-31-lockscreen-autopilot-continue-design.md`（决策 D1–D5 + 三项设计决策 + codex 复核勘误均已批，spec 为现行口径）

## Global Constraints

- 零运行时依赖：只用 Python 标准库（spec「Design notes」）。
- fail-closed：WTS 查询失败/UNKNOWN/输出缺段 → `locked=None` → 视为未锁屏，**零按键**。
- `last_auto_submit` 标记**先落盘、后按 Enter**（codex Critical 勘误，顺序不可反）。
- cap 是每会话的（10 正常 + 2 失败），不加机器级全局闸（D5）。
- 卡文案 = spec §3.5 定稿，utility 语言，无 emoji；建议文本首行 60 字符截断加「…」。
- 匹配 fail-closed：无法识别形态一律 None；正向结构断言为主，黑名单为辅。
- 证明命令：`python -m pytest tests/ -q`（全绿）。
- 影子模式（`shadow_mode` 默认 true）：只记录不按键。

## Review Focus

1. PowerShell 输出缺第三段或 `ERR` → 期望：`locked=None`、全程零按键 —— Task 3 测试钉住。
2. 旧格式 `pending_*.json`（无新字段）加载 → 期望：不崩、字段取默认 —— Task 4 测试钉住。
3. 干扰 tab 提示（`! tab to toggle mcp`、权限对话框）→ 期望：matcher 返回 None —— Task 7 测试钉住（fixture 以 spike 报告为准）。
4. daemon 自己按 Enter 后 10s 内的 UserPromptSubmit → 期望：不清 count、不清 finished_at —— Task 5 测试钉住。
5. `continue_count` 达 10 或 `failure_count` 达 2 → 期望：零按键、上限卡一张 —— Task 8 测试钉住。

---

### Task 1: Spike R1+R2（锁屏注入 + 建议条形态）

**Files:**
- Create: `tools/spike_autopilot_continue.py`
- Create: `docs/spikes/2026-11-XX-autopilot-continue-spike.md`（日期实填）

**Interfaces:**
- Produces: spike 报告两条结论——R2：建议条在 `pane.read` 60 行尾部的**逐字样例**（含出现时 `task_state`）、正向结构断言所需的锚文本；R1：锁屏下 `pane.send_key` 的实际返回。**任一失败 = 全计划重开设计，不得写后续产品代码。**

- [ ] **Step 1: 写探针脚本** `tools/spike_autopilot_continue.py`：复用 `cc_autopilot_notify.pebrel_runtime` 的 `agents()`/`dialog()`/`press()`，子命令 `observe`（每 5s 采样 pane 尾部打印 diff，直到 Ctrl-C）与 `press-once`（对指定 pane_id 发一次 `tab`，打印返回 JSON）。
- [ ] **Step 2: 采建议条形态（R2）**：让一个 Claude Code 会话完成任务停在建议条上，跑 `observe` ≥ 90s，把含建议条的样例逐字抄进 spike 报告（附 `task_state`）。
- [ ] **Step 3: 锁屏试按（R1）**：锁屏（本机常为远程 SSH 场景，可直接锁）→ `press-once` → 记录返回；解锁后核对输入框是否被填入。若运行时拒绝注入，记录错误码原文。
- [ ] **Step 4: 结论落报告**：两条结论 + 原始采样 + 可复算口径（命令、时间、pane_id）。R1/R2 结论贴回本计划的 Task 7。

### Task 2: config `autopilot` 节

**Files:**
- Modify: `cc_autopilot_notify/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `continue_enabled(cfg) -> bool`（默认 **False**）、`shadow_mode(cfg) -> bool`（默认 True）、`max_continues(cfg) -> int`（默认 10）、`suggest_wait_seconds(cfg) -> int`（默认 90）、`failure_continues(cfg) -> int`（默认 2）——均从 `_section(cfg, "autopilot")` 取值，校验沿用 `_require_bool`/`_require_positive_int`。

- [ ] **Step 1: 写失败测试**（沿用 test_config.py 既有风格）：

```python
def test_autopilot_defaults():
    cfg = _load_raw({})
    assert config.continue_enabled(cfg) is False
    assert config.shadow_mode(cfg) is True
    assert config.max_continues(cfg) == 10
    assert config.suggest_wait_seconds(cfg) == 90
    assert config.failure_continues(cfg) == 2

def test_autopilot_overrides_and_validation():
    cfg = _load_raw({"autopilot": {"continue_enabled": True, "max_continues": 3}})
    assert config.continue_enabled(cfg) is True
    assert config.max_continues(cfg) == 3
    with pytest.raises(ConfigError):
        _load_raw({"autopilot": {"max_continues": 0}})
```

- [ ] **Step 2: 跑测试确认失败**：`python -m pytest tests/test_config.py -q -k autopilot` → FAIL（无此函数）。
- [ ] **Step 3: 实现**：`config.py` 加 `autopilot` 节校验（`_validate` 内 `_section` + 两个 require 助手）与 5 个访问器。
- [ ] **Step 4: 全量测试通过**：`python -m pytest tests/test_config.py -q` → PASS。
- [ ] **Step 5: Commit**：`git commit -m "feat: autopilot continue config section"`

### Task 3: presence 锁屏字段（WTS）

**Files:**
- Modify: `cc_autopilot_notify/presence.py`
- Test: `tests/test_presence.py`

**Interfaces:**
- Produces: `Presence` 新字段 `locked: bool | None = None`；`SAMPLE_SCRIPT` 输出三段 `name|idle|locked`；解析规则：第三段 `"1"`→True、`"0"`→False、缺段/`ERR`/其它→None。P/Invoke：`WTSGetActiveConsoleSessionId` + `WTSQuerySessionInformation(infoClass=25)`，读 `SessionFlags`（缓冲区偏移 8/12/16：SessionId/SessionState/SessionFlags；**0=LOCKED，1=UNLOCKED，0xFFFFFFFF=UNKNOWN**，UNKNOWN 与查询失败→输出 `ERR`）。

- [ ] **Step 1: 写失败测试**（解析层——P/Invoke 偏移量在 Linux 不可单测，由 Task 10 真机 S1 验收）：

```python
def test_parse_locked_field():
    assert presence._parse_sample("pebrel|12.5|1") == ("pebrel", 12.5, True)
    assert presence._parse_sample("LockApp|71.0|0") == ("LockApp", 71.0, False)
    assert presence._parse_sample("pebrel|12.5|ERR") == ("pebrel", 12.5, None)
    assert presence._parse_sample("pebrel|12.5") == ("pebrel", 12.5, None)  # 缺段 fail-closed

def test_presence_carries_locked():
    p = presence._from_raw("LockApp|71.0|0")
    assert p.foreground is False and p.idle_seconds == 71.0 and p.locked is False
```

- [ ] **Step 2: 跑测试确认失败**：`python -m pytest tests/test_presence.py -q` → FAIL。
- [ ] **Step 3: 实现**：`SAMPLE_SCRIPT` 追加 WTS 段（同一调用），抽出模块级 `_parse_sample(raw) -> tuple` 供测试；`_from_raw` 组装 `Presence`（现 `sample()` 改走它）。
- [ ] **Step 4: 跑测试通过**：`python -m pytest tests/test_presence.py -q` → PASS。
- [ ] **Step 5: 真机快验（锁定侧已验，解锁侧补）**：解锁状态跑 `powershell.exe <SAMPLE_SCRIPT>` 手动一次，输出第三段应为 `1`；结果记入 spike 报告附录。
- [ ] **Step 6: Commit**：`git commit -m "feat: detect windows lock state via WTS in presence sample"`

### Task 4: state 记录字段与助手

**Files:**
- Modify: `cc_autopilot_notify/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `Pending` 新字段（全带默认值，旧记录兼容）：`finished_at: float | None = None`、`finished_failed: bool = False`、`continue_count: int = 0`、`failure_count: int = 0`、`last_auto_submit: float | None = None`、`pressed_at: float | None = None`、`stage_card_sent: bool = False`。函数（全部走既有 `_mutate` 锁内读改写）：`set_finished(sid, root, when, pane_id, *, failed: bool)`（保留计数、清 `stage_card_sent`/`pressed_at`）、`clear_finished(sid, root)`（清 `finished_at`、两计数归零——**仅人发起时调用**）、`mark_auto_submit(sid, root, when)`、`bump_continue(sid, root, *, failed: bool) -> int`、`mark_pressed(sid, root, when)`、`mark_stage_card(sid, root, when)`、`purge_stale(root, now, max_age_seconds=86400)`（删 24h 无 `last_activity` 的记录）。

- [ ] **Step 1: 写失败测试**：

```python
def test_old_record_loads_with_defaults(tmp_path):
    (tmp_path / "pending_s1.json").write_text('{"session_id":"s1","window_id":1,"pane_id":2,'
        '"questions":[],"appeared_at":1.0,"last_activity":1.0}')
    ps = state.load_all(tmp_path)
    assert ps[0].finished_at is None and ps[0].continue_count == 0

def test_set_finished_keeps_counts_clears_markers(tmp_path):
    state.record(_pending("s1"), tmp_path)
    state.bump_continue("s1", tmp_path, failed=False)
    state.set_finished("s1", tmp_path, when=100.0, pane_id=2, failed=False)
    p = state.load_all(tmp_path)[0]
    assert p.finished_at == 100.0 and p.continue_count == 1
    assert p.stage_card_sent is False and p.pressed_at is None

def test_human_clear_zeroes_both_counts(tmp_path):
    ...  # clear_finished 后 finished_at None、continue_count==0、failure_count==0
```

- [ ] **Step 2: 跑测试确认失败**：`python -m pytest tests/test_state.py -q` → FAIL。
- [ ] **Step 3: 实现**：字段 + 上述函数（每个 3–6 行，走 `_mutate`）。
- [ ] **Step 4: 跑测试通过**：`python -m pytest tests/test_state.py -q` → PASS。
- [ ] **Step 5: Commit**：`git commit -m "feat: pending record gains autopilot-continue fields"`

### Task 5: hook 接线（Stop / StopFailure / UserPromptSubmit）

**Files:**
- Modify: `cc_autopilot_notify/hook_notify.py`（`handle()` 内三个分支）
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: Task 4 的 `set_finished`/`clear_finished`/`mark_auto_submit`；pane id 取 `int(os.environ.get("PEBREL_PANE_ID") or 0)`（沿用 `hook_pre_tool.py:46` 的写法）。
- Produces: 行为契约——`Stop` → `set_finished(failed=False)`；`StopFailure` → `set_finished(failed=True)`；`UserPromptSubmit` → 若 `now - last_auto_submit < 10s` 则**原样返回**（daemon 自提交），否则 `clear_finished`。

- [ ] **Step 1: 写失败测试**：

```python
def test_stop_marks_finished(hook_env, tmp_path):
    hook_notify.handle({"hook_event_name": "Stop", "session_id": "s1",
                        "cwd": "/tmp"}, tmp_path, _cfg())
    p = state.load_all(tmp_path)[0]
    assert p.finished_at is not None and p.finished_failed is False

def test_user_prompt_after_auto_submit_keeps_counts(hook_env, tmp_path):
    state.record(_pending("s1"), tmp_path)
    state.bump_continue("s1", tmp_path, failed=False)
    state.mark_auto_submit("s1", tmp_path, when=time.time())
    hook_notify.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1"},
                       tmp_path, _cfg())
    p = state.load_all(tmp_path)[0]
    assert p.continue_count == 1 and p.finished_at is not None  # 未清

def test_user_prompt_from_human_zeroes(tmp_path):
    ...  # 无 last_auto_submit → finished_at None、两计数 0
```

- [ ] **Step 2: 跑测试确认失败**：`python -m pytest tests/test_hooks.py -q -k autopilot` → FAIL。
- [ ] **Step 3: 实现**：`handle()` 里 `Stop`/`StopFailure`/`UserPromptSubmit` 三个事件名各加一段（注意现有 Stop 分支「不通知」逻辑保持不动）。
- [ ] **Step 4: 跑全量 hook 测试**：`python -m pytest tests/test_hooks.py -q` → PASS（既有通知用例不回归）。
- [ ] **Step 5: Commit**：`git commit -m "feat: hooks mark finished turns and gate on auto-submit marker"`

### Task 6: channels 文案包（事件色板 + 截断）

**Files:**
- Modify: `cc_autopilot_notify/channels/feishu.py:53-55`（`_COLORS`）、`cc_autopilot_notify/channels/__init__.py`、`cc_autopilot_notify/hook_notify.py`（`_TITLES` 不加——续跑卡标题由 continue.py 构造，见 Task 8）
- Test: `tests/test_channels.py`

**Interfaces:**
- Produces: `_COLORS` 增 `"auto_continue": "turquoise"`、`"continue_stage": "blue"`、`"continue_capped": "orange"`；`first_line(text: str, limit: int = 60) -> str`（取首行、超限截断加「…」、空/空白返回 `"(无文本建议)"`）。

- [ ] **Step 1: 写失败测试**：

```python
def test_new_event_colors():
    n = channels.Notification(title="p · 自动续跑 1/10", body="→ x", event="auto_continue")
    card = feishu._card(n)   # 断言 header.template == "turquoise"（按既有测试的取法）
    assert _template(card) == "turquoise"

def test_first_line_truncates():
    assert channels.first_line("abc\ndef") == "abc"
    assert channels.first_line("x" * 80) == "x" * 60 + "…"
    assert channels.first_line("") == "(无文本建议)"
```

- [ ] **Step 2: 跑测试确认失败** → **Step 3: 实现**（色表三行 + `first_line` 一个函数） → **Step 4: `python -m pytest tests/test_channels.py -q` PASS**。
- [ ] **Step 5: Commit**：`git commit -m "feat: continuation card palette and first-line truncation"`

### Task 7: continue.match_suggestion（依 Task 1 spike 结论）

**Files:**
- Create: `cc_autopilot_notify/continue.py`
- Test: `tests/test_continue.py`

**Interfaces:**
- Consumes: Task 1 spike 报告的建议条逐字样例（锚文本与 task_state 记在那里）。
- Produces: `match_suggestion(text: str) -> str | None`。契约：**正向结构断言**——pane 尾部末尾若干行匹配建议条锚模式（spike 确认的正则常量 `_SUGGESTION_RE`）且其后无其它 UI 行 → 返回建议文本；`! tab to toggle mcp`、权限对话框等干扰行 → None；无法识别 → None。

- [ ] **Step 1: 写失败测试**（fixture 行来自 spike 报告，逐字引用）：

```python
SPIKE_SUGGESTION = "...spike 报告的建议条原始行..."      # Task 1 产出
SPIKE_INTERFERENCE = ["! tab to toggle mcp", "...权限对话框行..."]

def test_matches_spike_sample():
    assert continue_.match_suggestion(SPIKE_SUGGESTION) == "期望的建议文本"

def test_interference_returns_none():
    for line in SPIKE_INTERFERENCE:
        assert continue_.match_suggestion(line) is None

def test_unrecognized_returns_none():
    assert continue_.match_suggestion("任意普通输出尾部") is None
```

- [ ] **Step 2: 跑测试确认失败** → **Step 3: 实现** `_SUGGESTION_RE` + `match_suggestion`（约 15 行） → **Step 4: PASS**。
- [ ] **Step 5: Commit**：`git commit -m "feat: suggestion-bar matcher from spike sample"`

### Task 8: continue.handle 状态机

**Files:**
- Modify: `cc_autopilot_notify/continue.py`
- Test: `tests/test_continue.py`

**Interfaces:**
- Consumes: Task 2 访问器、Task 3 `Presence.locked`、Task 4 state 函数、Task 6 `first_line` + `Notification`、Task 7 `match_suggestion`、`pebrel_runtime.agents`（经 Deps）。
- Produces:

```python
@dataclasses.dataclass
class ContinueDeps:
    read: Callable[[state.Pending], dict]            # pane.read，同 daemon Deps.read
    press: Callable[[state.Pending, str, int], dict]
    fresh_presence: Callable[[], "presence.Presence | None"]   # 绕 5s 缓存强采
    pane_owner_ok: Callable[[state.Pending], bool]   # agents.list 复核 pane↔session
    notify: Callable[[object], None]
    sleep: Callable[[float], None]

def handle(pending: state.Pending, now: float, root: Path, cfg: dict,
           deps: ContinueDeps) -> list[str]   # 返回 action 行，同 daemon tick 惯例
```

行为契约（全部有对应测试）：`continue_enabled` False → 立即返回；`locked` 非 True → 零按键零通知；`pending.questions` 非空或 `reading["state"] == "attention"` → 返回（应答优先）；影子模式 → log `SHADOW would press: <摘要>` 后返回；无建议且 `now - finished_at ≥ suggest_wait_seconds + 30` → 阶段卡（event `continue_stage`，文案见 spec §3.5）+ `mark_stage_card`，已发过则不重发；有建议 → `pressed_at` 未满 15s → 返回；`pane_owner_ok` False → 返回；达上限（正常 10 / 失败 2）→ 上限卡（`continue_capped`）一次 + 不再按键；否则 `mark_auto_submit`（**先落盘**）→ `mark_pressed` → `press tab` → sleep 0.3 → `press enter` → `bump_continue` → 续跑卡（`auto_continue`，正文首行 `→ <first_line>`、次行 `第 N 次 / 上限 M` 或 `失败重试 N / 2`）。

- [ ] **Step 1: 写失败测试**（假 Deps 沿用 test_daemon.py 的 `_deps` 风格；spec §4 的 S3–S7 各一个用例）：

```python
def test_unlocked_zero_presses(...)
def test_locked_suggestion_presses_tab_then_enter_and_counts(...)
def test_cap_stops_with_single_capped_card(...)
def test_stage_card_sent_once_then_not_resent(...)
def test_shadow_mode_logs_without_pressing(...)
def test_press_refused_twice_gives_up(...)   # 复用 daemon._PressRefused 语义
```

- [ ] **Step 2: 跑测试确认失败** → **Step 3: 实现** `handle`（约 90–120 行，单函数按契约顺序早退） → **Step 4: `python -m pytest tests/test_continue.py -q` PASS**。
- [ ] **Step 5: 全量回归**：`python -m pytest tests/ -q`（test_daemon.py 既有用例必须全绿——「应答不动」契约）。
- [ ] **Step 6: Commit**：`git commit -m "feat: continuation state machine in continue.py"`

### Task 9: daemon 接线 + doctor

**Files:**
- Modify: `cc_autopilot_notify/daemon.py`（`build_deps` 加 continue 分支依赖；`_tick_one` 对 `finished_at` 记录调用 `continue.handle`）、`cc_autopilot_notify/doctor.py`
- Test: `tests/test_daemon.py`、`tests/test_doctor.py`

**Interfaces:**
- Consumes: Task 8 `ContinueDeps`/`handle`、Task 3 `presence.sample`（`fresh_presence` 即再采一次）。
- Produces: daemon tick 内——有 `finished_at` 的记录走 continue 分支（在 attention 应答分支之前判断 `finished_at`）；每 tick 开头调 `state.purge_stale(root, now)` 做 24h 过期清理；doctor 增两行：`lock state`（当前 locked 值或 UNKNOWN）与 canary 统计（近 24h finished 段中 pane 尾部含 tab 字样但 matcher 为 None 的次数；统计源：daemon 写入的日志聚合文件或 state 计数，实现取简者）。

- [ ] **Step 1: 写失败测试**：daemon 假 Deps 注入 `finished_at` 记录 + locked=True + 建议在场 → 断言 press 被调用且 action 行出现；locked=False → 零 press。doctor：注入样例断言两行输出。
- [ ] **Step 2: 确认失败** → **Step 3: 实现** → **Step 4: `python -m pytest tests/ -q` 全绿** → **Step 5: `bash scripts/cc-autopilot-notify doctor` 新行 PASS**。
- [ ] **Step 6: Commit**：`git commit -m "feat: wire continuation into daemon tick and doctor"`

### Task 10: 真机验收 S1–S8 + 影子一晚

**Files:**
- Modify: `docs/superpowers/specs/2026-10-31-lockscreen-autopilot-continue-design.md`（状态行改「已验收」）

- [ ] **Step 1: S1**：解锁/锁屏各采一次 PowerShell 样本，第三段解锁=1、锁屏=0。
- [ ] **Step 2: S8（R1 终验）**：锁屏实跑一次续跑，解锁后查会话记录确认建议被执行。
- [ ] **Step 3: S3–S7**：`python -m pytest tests/ -q` 全绿（引用输出）。
- [ ] **Step 4: 影子夜**：`shadow_mode: true`（默认）+ 陛下手动置 `continue_enabled: true` 过一晚 → 晨起核对 shadow 日志的「会按次数/时机」与预期一致。
- [ ] **Step 5: 开真跑**：`shadow_mode: false`；次日核对飞书卡序列（turquoise 续跑卡 + blue/orange 收尾卡）。
- [ ] **Step 6: 状态行改「已验收」并 Commit**：`git commit -m "docs: autopilot continue accepted (S1-S8)"`
