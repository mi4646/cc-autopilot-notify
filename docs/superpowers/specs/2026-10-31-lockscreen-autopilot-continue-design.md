# 锁屏自动续跑（autopilot continue）设计规格

- 日期：2026-10-31
- 状态：三重评审完成（2026-10-31：工程 + 设计 + codex 独立复核）——正文已按全部获批决策与勘误**就地修订**，读到本段即为现行口径；文末「工程评审」段是决策台账与评审记录，不推翻正文——正文 §3.1/§3.3/§4 已按 D2/D3 决策与外部复核勘误**就地修订**，读到本段即为现行口径；文末「工程评审」段是决策台账与评审记录，不推翻正文
- 作者：Claude（调研 + 设计）
- 目标仓库：`/var/www/cc-autopilot-notify`
- 关联：取代 `2026-10-28-idle-aware-autoanswer-design.md`「明确不做」段中「不做屏幕锁定检测」一条（仅限本特性；自动应答的空闲判定不变）。该条当时的否决理由是「锁屏只是无输入的一种情况」，重启条件是「用户要求以锁屏为无人值守开关」——2026-10-31 陛下明确提出，条件满足。

---

## 1. 背景与需求

陛下提出的新逻辑：Claude Code 当前任务完成后，90 秒内输入框上方会出现「下一步建议」快速提示；按 Tab 快速填入、Enter 执行；**直到输入框不再出现下一步建议，才算完成整轮任务**。

现项目只覆盖「自动应答」（AskUserQuestion 面板）。本规格新增「自动续跑」：检测建议条 → 模拟 Tab+Enter → 循环，把自动应答扩展为完整的无人值守。

### 1.1 已定决策（陛下拍板）

| 决策点 | 结论 |
|---|---|
| 触发门槛 | **检测到锁屏才执行**，建议条在场即执行，不等空闲计时 |
| 未锁屏时 | **什么都不做**（不执行、不通知），锁屏 = 无人值守的唯一开关 |
| 循环护栏 | 每轮设执行上限（默认 10），达上限停止并发通知 |

### 1.2 非目标

- **不改自动应答**：150s/90s 空闲门槛、judge、屏幕匹配全部不动。同一 pane 同时有未答问题时，应答优先。
- **未锁屏不续跑**：即使空闲很久也不执行。
- **不解析建议内容**：建议文本原样 Tab 填入执行，不做内容审查；护栏靠上限与通知，不靠判断建议好坏。
- **设计场景 = console 锁屏 + SSH/远程操作 WSL**（codex 复核 #3 边界声明）：锁屏检测基于 console session；RDP 会话 disconnected 状态下的语义未支持（RDP 用户不获得续跑），重启条件：出现 RDP 为主的真实工作流。
- **cap 是每会话的，不是机器级的**（D5，2026-10-31）：锁屏是机器级信号，但 max_continues/failure_continues 按会话记录各自封顶；同机多会话过夜总续跑 = 会话数 × (10+2)。不加全局闸；重启条件：出现 3+ 会话并行的真实工作流。

## 2. 首要风险（决定实施顺序）

| # | 风险 | 现状 | 处置 |
|---|---|---|---|
| R1 | **锁屏下 `pane.send_key` 可能注入失败** | 2026-10-28 spec E15 明确记录「未验证」 | **第一个 spike**：造建议条场景，锁屏后由 daemon 试按一次，记录实际返回。失败则本特性整体重开设计 |
| R2 | **建议条形态未观测**：`pane.read` 文本里长什么样、出现时 `task_state` 是什么 | 完全未知（同 `conversation.read.prompt` 永远为 null 的前科） | spike 先行：建议条在场时采样 pane 文本，据此写匹配规则 |
| R3 | 误判其他 tab 提示（`! tab to toggle mcp`、权限对话框） | 已知存在多种 tab 提示 | 匹配规则必须排除；fail-closed：无法识别的形态不按键 |

spike 产物记入 `docs/spikes/`，附可复算口径。

## 3. 设计

### 3.1 锁屏检测（presence.py）

`SAMPLE_SCRIPT` 在**同一次** PowerShell 调用里追加 WTS 查询：`WTSGetActiveConsoleSessionId` + `WTSQuerySessionInformation`（infoClass 25 = WTSSessionInfoEx），读 `SessionFlags`——**0=LOCKED，1=UNLOCKED，0xFFFFFFFF=UNKNOWN**（取值反直觉；缓冲区偏移是硬编码字节位，须单测钉住——2026-10-31 探针曾因偏移错读出垃圾值）。输出由 `name|idle` 变为 `name|idle|locked`；`Presence` 增加 `locked: bool | None`。查询失败或 UNKNOWN → `locked=None`，**视为未锁屏、不代跑**（fail-closed）。采样缓存沿用现有 5 秒。

> D2（2026-10-31）：弃用 LogonUI 进程启发式——其解锁侧行为无法证伪（解锁后可能常驻 → 永远判「锁屏」，危险方向）；WTS 语义有文档背书且锁定侧已本机验证。锁定/解锁两侧的实测归入 S1 验收。

### 3.2 建议条检测（continuation.py；勘误 2026-10-31：D1 已定匹配知识集中 continuation.py，原 screen.py 标题作废）

依 R2 spike 结论落成 `screen.match_suggestion(text: str) -> str | None`：有建议返回建议文本，无则 None。识别不了的形态一律 None。规则以**正向结构断言**为主（codex 复核 #13：末行匹配建议条形态 + 输入区空闲），§2 R3 的干扰提示黑名单只作辅助排除。

### 3.3 触发与状态机（hook_notify.py + daemon.py + state.py）

`Pending` 增加带默认值的新字段（旧记录兼容）：`finished_at: float | None = None`、`continue_count: int = 0`、`failure_count: int = 0`。

| 事件 | 动作 |
|---|---|
| `Stop` / `StopFailure` hook（回合结束） | 为该会话写/更新记录：`finished_at=now`、pane_id 取 `PEBREL_PANE_ID`，`continue_count` 保留（StopFailure 额外带失败标记，后续卡文案区分） |
| `UserPromptSubmit` | 清 `finished_at` 并将 `continue_count` 归零——**这是「人发起的一轮」的唯一起点**（D3）。**注意**：daemon 自己按下的 Enter 同样触发本事件清 `finished_at`，随后该回合结束的 Stop 重新置位——这是设计内的循环，不是 bug（评审发现 3，2026-10-31） |
| daemon tick，记录带 `finished_at` | 未锁屏（`locked` 非 True）→ 什么都不做。锁屏 → 读 pane：无建议 → 若 `now - finished_at ≥ suggest_wait_seconds + 30`（**勘误 2026-10-31**：陛下观测的「90 秒内出现」是建议的出现时限，完成判定窗须留余量防贴线竞态——codex 复核 #2）→ **阶段完成**：发「阶段完成，累计 N/上限」卡，记录与 count **保留**（D3）；有建议 → 失败回合用 `failure_count < failure_continues`（默认 2，D4），正常回合用 `continue_count < max_continues`（默认 10）→ 通过则 `press tab` → 短隔 → `press enter` → 对应计数 +1 → 飞书卡（建议文本 + 第几次）；达上限 → 停止 + 通知「已达上限」，不再按键。收轮后 Stop 再来 → 复用记录重新进入等待 |
| 记录清理 | UserPromptSubmit 归零（不删文件）、达上限、24h 无活动过期、pane 消失超现有 grace 期 |

续跑逻辑只处理带 `finished_at` 的记录，与现有 attention 应答路径互斥；`Stop` 每回合都触发，人在场时锁屏门槛天然挡住。

- **按键失败复用现有模式**（评审 A1，2026-10-31）：续跑的 tab/enter 被拒绝时走 daemon 既有 `_PressRefused` 退避路径（daemon.py:124-131），累计 2 次放弃并通知，不另起炉灶。
- **匹配知识边界**：pane 文本 → 建议的匹配规则只住 continuation.py，daemon 不预消化文本（架构评审八红旗②的边界规则）。

**Tick 级安全机制**（外部复核 Native fallback 发现 1/2/3/4/6，2026-10-31，均为已批契约的必要实现）：

1. **`last_auto_submit` 标记**（发现 1，Critical；**codex 复核勘误 2026-10-31：必须在按 Enter 之前落盘**——hook 在 Enter 之后可能先于 daemon 的下一条语句执行，事后写标记来不及）：daemon 按下 Enter **前**先把 `last_auto_submit=now` 写入记录；`UserPromptSubmit` hook 读标记——`now - last_auto_submit < 10s` → 视为 daemon 自身提交，**不清 count、不清 finished_at**；否则人发起 → 清 `finished_at`、count 归零。误判方向保守（人恰好在 daemon 按键后 10 秒内自己提交 → 被当作 daemon 提交 → 护栏不减）。
2. **`stage_card_sent` 标记**（发现 2）：阶段完成卡发出后置位；同一段（finished_at 未变）不重发。收到新 Stop（finished_at 更新）时清除。
3. **按键前锁态强采**（发现 3）：有按键意图时绕过 5 秒缓存重采一次 presence，仍 locked 才按。多一次 PowerShell 调用仅发生在将要按键的 tick。
4. **`pressed_at` 静默窗**（发现 4）：Enter 按下后记录 `pressed_at=now`，其后 15 秒内该记录不再触发按键（防 UI 渲染滞后导致双按）；新 Stop 到达时清除。
5. **应答优先的机制化**（发现 6）：续跑分支前置条件 = `pending.questions` 为空 **且** `reading.state != "attention"`；两者任一不满足则跳过续跑，交回应答路径。
6. **匹配失效可观测**（发现 8；**codex 复核勘误：原「阶段完成中零匹配」分母循环**）：doctor 增加一行 canary 统计——「近 24h finished 段中，pane 尾部含 tab 字样但 matcher 返回 None 的次数 M」；M 持续增长即匹配规则失真信号，不再静默。

7. **failure_count 生命周期**（codex 复核 #7）：`failure_count` 与 `continue_count` 都只在 UserPromptSubmit（人发起）归零；按键注入失败（`_PressRefused`）是注入层故障，**不占** failure_count。
8. **pane↔session 复核**（codex 复核 #16）：续跑按键前经 `agents.list` 复核该 pane 当前归属的 session_id 与记录一致，防止 pane 复用后把旧记录的按键打进新会话。
9. **休眠/唤醒**（codex 复核 #11）：机器睡眠期间 daemon 不 tick；唤醒后第一 tick 仍**先读 pane 取新鲜证据再决策**，陈旧 finished_at 只影响「是否已达窗口」，不影响按键依据。窗口计时用墙钟，NTP 跳变风险已知且可接受。
10. **已知局限**（codex 复核 #6）：`pane.send_key` IPC 返回成功 ≠ UI 真正生效（Enter 未提交时不可检测）；依赖 stage 完成卡与 doctor canary 兜底观测。
11. **影子模式**（codex 复核 #9/10/18，陛下拍板 2026-10-31）：`continue_enabled` 默认 **false**（opt-in）。首次启用前先跑一晚影子模式（`autopilot.shadow_mode: true`）：daemon 照常检测并记录「此刻会按 Tab+Enter」到日志，**不真按键**；一晚后核对匹配率再开真跑。

### 3.4 配置（config.py）

```json
{"autopilot": {"continue_enabled": false, "shadow_mode": true, "max_continues": 10, "suggest_wait_seconds": 90, "failure_continues": 2}}
```

`continue_enabled` 默认 **false**（codex 复核 #18 + 陛下拍板：键盘注入类功能不默认启用，opt-in）；`shadow_mode` 默认 true——即首次启用 = 先影子一晚（见 §3.3 机制 11）。现有 `thresholds` 不动。

### 3.5 通知（channels）——设计评审定稿（2026-10-31，三项决策全部获批）

四张卡共用骨架：标题走 `title_with_project`、头色随 event（进 `feishu.py` `_COLORS` 同一张表）、标题进 `hook_notify._TITLES` 同表、时间戳脚注沿用、正文纯文本（终端 OSC9/Windows toast 通道天然降级，无需另写）。

| 卡 | event | 头色 | 标题 | 正文 |
|---|---|---|---|---|
| 续跑 | `auto_continue` | turquoise | `项目 · 自动续跑 3/10` | 首行 `→ 建议摘要…`；次行 `第 3 次 / 上限 10` |
| 失败重试 | `auto_continue` | turquoise | `项目 · 自动续跑（失败重试）` | 首行同上；次行 `失败重试 1 / 2`（D4） |
| 阶段完成 | `continue_stage` | blue | `项目 · 续跑告一段落` | `本次共自动续跑 5 条，无更多建议` |
| 上限 | `continue_capped` | orange | `项目 · 续跑已达上限` | `已自动执行 10 条，剩余建议留待主人处理` |

- **频控（决策 1）**：每次续跑发一张短卡——每次续跑都是一次不可逆的自动执行动作，逐条留痕是失控场景的底线；过夜封顶 10+2 张，量可控。现有 `notify.dedupe_seconds` 对此**不适用**（内容各异），阶段完成卡不重发由 `stage_card_sent` 标记保证（见 §3.3 机制 2）。
- **截断（决策 2）**：建议文本取**第一行**，超 60 字符截断加「…」；为空显示 `(无文本建议)`。完整内容以终端/会话为源，卡只负责扫一眼。
- **文案语言（Pass 4）**：utility（状态+动作），无 emoji、无营销词，与现有「Claude 等你」的克制风格同源。
- 失败回合的 StopFailure 通知照旧（既有行为）；续跑动作本身由 turquoise 卡留痕，不发第二张。

## 4. 成功标准（可验证）

| # | 标准 | 验证方式 |
|---|---|---|
| S1 | 锁屏采样返回第三段，**解锁为 1（UNLOCKED）、锁屏为 0（LOCKED）**（WTS 取值反直觉，勘误 2026-10-31） | PowerShell 实跑，锁屏前后各采一次 |
| S2 | spike 观测到建议条的真实文本与 task_state，匹配函数据此落成 | spike 报告附原始采样 |
| S3 | 未锁屏（含 `locked=None`）时**零按键** | pytest：注入未锁屏/None 的 tick，断言无 press |
| S4 | 锁屏 + 建议在场 → 恰好一次 tab、一次 enter，计数 +1，飞书有卡 | pytest 假 Deps + 真机锁屏实测各一次 |
| S5 | `continue_count` 达 `max_continues` → 不再按键，发护栏卡 | pytest：注入 count=上限 的 tick |
| S6 | finished 后 90 秒无建议 → 「阶段完成，累计 N/上限」卡，**记录与 count 保留**（勘误 2026-10-31：与 D3 对齐，原「记录清理」作废） | pytest：注入 `finished_at = now - 91` |
| S7 | R3 干扰提示不触发按键 | pytest：以 spike 确认的干扰文本喂 `match_suggestion`，断言 None |
| S8 | R1 实测通过：锁屏下按键真实生效 | 锁屏实跑一次续跑，解锁后查**会话记录/新回合输出**确认建议已被执行（勘误 2026-10-31：Enter 已提交，输入框不会有内容） |

pytest 全绿 + `python -m pytest tests/ -q` 为证明命令。

## 5. 实施顺序（依风险排序）

1. R1/R2 spike（可并行）→ 任何一项失败即重开设计，不写产品代码
2. config（`autopilot` 节）——状态机依赖它，先于第 5 步（勘误 2026-10-31）
3. presence 锁屏字段 + doctor 检查行
4. `screen.match_suggestion` + 单测
5. state 字段 + Stop/UserPromptSubmit hook 扩展
6. daemon 续跑状态机 + 单测
7. 通知文案
8. S1–S8 逐条验收
9. 影子模式跑一晚（`shadow_mode: true`）→ 核对匹配率日志 → 陛下手动置 `continue_enabled: true` 真跑（codex 复核 #10，陛下拍板）

---

## 工程评审（/plan-eng-review，2026-10-31）

评审目标：本文档（锁屏自动续跑设计规格）。报告文件 = 本文档。

### 范围记录（Scope Challenge）

- feature answers: 无裁剪提案，范围维持原始四要素（90 秒窗口 / Tab 填入 / Enter 执行 / 无建议收轮）
- structure: **B — 新建 `cc_autopilot_notify/continuation.py`** 集中建议匹配与续跑状态机（D1，陛下选定）；presence 只加 locked 字段，daemon 每秒单点调用
- accepted scope: 原范围 + continuation.py 模块边界；§5 实施顺序不变
- pending remedies: F1（锁屏检测方式）、F2（后台任务下的收轮语义）

### F1 → D2: 锁屏检测信号选型

Finding: F1，CRITICAL，confidence 9/10，spec §3.1，reviewer Claude（本机探针 2026-10-31）
Plan baseline: 原提案 = LogonUI 进程存在性判锁（spec §3.1）
Runtime evidence: 锁屏态探针：LogonUI 在场 ✓、WTS SESSION_FLAGS=0(LOCKED) ✓、quser console Active、FOREGROUND=LockApp、IDLE=191s；LogonUI 解锁侧行为 unverified；WTS 语义与文档一致（0=LOCKED/1=UNLOCKED/0xFFFFFFFF=UNKNOWN）未反转

| 选择 | 当前 | A: WTS 单信号 | B: WTS+LogonUI 双信号 | C: 维持 LogonUI |
|---|---|---|---|---|
| 解锁侧误判风险 | unverified | 消除（文档语义，锁定侧已验证） | AND 组合，保守但需两信号都有据 | unverified，押 spike |
| PowerShell 采样脚本 | name\|idle | +WTS P/Invoke 一段（同一调用） | +两段 | +LogonUI 一段 |
| spike 验证负担 | — | 锁/解锁各采一次 | 各采一次 ×2 信号 | 解锁侧补采 |
| 失败方向 | — | 查询失败→None→不代跑（安全） | 同左且更少触发 | 解锁后若常驻→永远「锁屏」→失控续跑（危险方向） |

Question D2:
锁屏检测用什么信号？（探针实证：LogonUI 在本机锁屏时存在，但解锁侧未知；WTS API 锁屏侧已验证且语义有文档背书）
ELI10: 要判断「Windows 是否锁屏」。原方案看 LogonUI 进程在不在，但解锁后它可能常驻——那样 daemon 会永远以为锁屏、无人值守续跑不停止，方向危险。另一个 Windows 官方 API（WTS）直接回答「会话锁了没」，本机已验证可用。
Stakes: 选错方向是「以为锁了实际没锁」→ 人在时续跑，或「以为没锁实际锁了」→ 特性失效。
Recommendation: A because WTS 语义有文档背书、锁定侧已本机验证、失败即降级为不代跑（安全方向）；LogonUI 的解锁侧行为永远存疑。
Completeness: Note: options differ in kind, not coverage — no completeness score.
Net: 用一个有文档背书的 API 换掉一个永远无法完全证伪的进程启发式。

A) WTS 单信号（推荐）
  ✅ SESSION_FLAGS 直接给出 LOCKED/UNLOCKED，语义有 Microsoft 文档背书，锁定侧本机已验证
  ✅ 查询失败/UNKNOWN → locked=None → 视为未锁屏，fail-closed 方向安全
  ❌ P/Invoke 偏移量是硬编码字节位（本次探针就先读错了偏移），需单测钉住
B) WTS + LogonUI 双信号 AND
  ✅ 双重确认，LogonUI 意外消失时 WTS 仍兜底
  ❌ 两个信号都可能各自失真，组合语义更难测试，收益无实证支撑
C) 维持 LogonUI 启发式
  ✅ 实现最简单，进程存在性一行判断
  ❌ 解锁侧若常驻则永远判「锁屏」→ 人在场也续跑，危险方向无法证伪

State: approved
Actual answer: A) WTS 单信号（D2，2026-10-31）
Accepted scope: spec §3.1 改为 WTSQuerySessionInfoEx 单信号；LogonUI 启发式弃用
History: 原提案 LogonUI 写于 spec 初稿；2026-10-31 探针后升级为决策。

### F2 → D3: 「整轮完成」的定义与计数生命周期

Finding: F2，CRITICAL，confidence 7/10，spec §3.3，reviewer Claude
Plan baseline: 原提案 = 收轮（90 秒无建议）即发「整轮完成」卡并清记录；continue_count 随记录归零
Runtime evidence: prior learning `notification-spam-stop-per-turn`（confidence 9/10，2026-10-27，本仓库）：Stop 每回合触发而非每请求一次；主回合启动后台任务后 Stop 照常到来，此时建议条未必出现 → 90 秒窗口虚假耗尽

| 选择 | 当前（spec 原案） | A: 轮 = 人发起的回合链 | B: 轮 = 每段等待窗口（原案） |
|---|---|---|---|
| cap 语义 | 每段 10 次 | 人发起的一轮共 10 次 | 每段 10 次（后台任务场景护栏被重置弱化） |
| 收轮后记录 | 清除 | 保留（count 保留），Stop 再来继续守 | 清除；Stop 再来新建 count=0 |
| 收轮卡文案 | 「整轮完成」 | 「阶段完成，累计 N/上限」 | 「整轮完成」 |
| 虚假收轮的伤害 | — | 一张措辞需含糊些的卡，护栏完好 | 护栏失效 + 卡片误导 |
| 记录清理时机 | 收轮 / 达上限 / pane 消失 | UserPromptSubmit（人发起）、达上限、24h 无活动过期 | 收轮 / 达上限 / pane 消失 |

Question D3:
「整轮完成」怎么定义，continue_count 什么时候归零？
ELI10: Claude 的回合结束后台任务还在跑时，Stop 信号照样来，可能让 daemon 误以为「没建议了、整轮完了」。如果那一刻把计数清零，陛下定的 10 次上限就形同虚设——过夜可能不止烧 10 次。
Stakes: 护栏被后台任务绕过 = 锁屏过夜 token 消耗失控；反之计数不归零则更保守。
Recommendation: A because 上限的价值在「人发起的一轮」总量；收轮只代表「这一段没建议了」，不该重置护栏。
Completeness: Note: options differ in kind, not coverage — no completeness score.
Net: 把 cap 从「每段」升级为「每人发起的一轮」，代价是收轮卡措辞要区分「阶段完成」与「整轮完成」。

A) 轮 = 人发起的回合链（推荐）
  ✅ cap 是「人发起的一轮」的硬上限，后台任务虚假收轮无法重置护栏
  ✅ 收轮卡文案「阶段完成，累计 N/上限」，语义与真实状态一致
  ❌ 记录生命周期变长，需要 24h 过期清理兜底，状态机多一个「已完成但保留」态
B) 原案：轮 = 每段等待窗口
  ✅ 状态机最简，收轮即清理，无长生命周期状态
  ❌ 后台任务或偶发无建议都会重置计数，过夜护栏可被反复绕开

State: approved
Actual answer: A) 轮 = 人发起的回合链（D3，2026-10-31）
Accepted scope: 收轮只发「阶段完成，累计 N/上限」卡，记录与 count 保留；count 重置/记录清理仅发生在 UserPromptSubmit（人发起）、达上限、24h 无活动过期；Stop 与 StopFailure 均置 finished_at（StopFailure 带失败标记，卡文案区分）
History: 原案写于 spec 初稿；2026-10-31 评审结合 prior learning 升级为决策。

### F5 → D4: 失败回合（StopFailure）是否自动续跑

Finding: F5，Medium（策略缺口），confidence 8/10，spec §3.3，reviewer：外部复核 Native fallback（2026-10-31）
Plan baseline: D3 accepted scope 只定了「StopFailure 带失败标记，卡文案区分」；「失败后要不要自动执行建议」从未拍板
Runtime evidence: 失败回合（API 报错/限流）后的建议条很可能是 retry 类；锁屏下无人值守会自动重试至多 10 次。失败回合是否出现建议条本身也未观测（归 R2 spike）。

| 选择 | 当前 | A: 失败不续跑 | B: 失败同上限续跑 | C: 失败单独小上限 |
|---|---|---|---|---|
| 瞬时故障（限流/网络）过夜自愈 | — | 无，等主人回来 | 自动重试至多 10 次 | 自动重试至多 2 次 |
| 每跑必败的循环放大 | — | 0 次浪费 | 最多 10 次失败回合 | 最多 2 次浪费 |
| 通知 | StopFailure 本就通知 | 照旧 | 照旧 + 继续跑 | 照旧 + 继续跑 |
| 失败标记的用途 | 卡文案区分 | 决定不按键 | 仅文案区分 | 决定用哪个上限 |

Question D4:
回合以 StopFailure 结束（API 报错、限流等）时，锁屏下的自动续跑还执行吗？
ELI10: 任务失败收场也算「回合结束」。失败后 Claude 常给出「重试」类建议。无人值守时自动重试可能自愈瞬时故障，也可能对着必然失败的任务空转烧钱。
Stakes: 过夜场景：不重试 = 半夜一次抖动就停整晚；重试无上限 = 失败循环最多 10 轮。
Recommendation: C because 兼顾自愈与止损：瞬时故障 2 次内自愈，必然失败最多浪费 2 次；独立计数不占正常轮的上限。
Completeness: Note: options differ in kind, not coverage — no completeness score.
Net: 用一个独立的失败重试小上限，把「要不要自愈」和「正常轮上限」解耦。

A) 失败不续跑（最保守）
  ✅ 与「StopFailure 仍通知」的现有语义完全一致，零新风险
  ❌ 半夜一次限流抖动就把整轮停到主人回来，无人值守价值打折
B) 失败也续跑（共用上限 10）
  ✅ 自愈能力最强，实现最少（与正常轮同路径）
  ❌ 必然失败的任务会被空转重试最多 10 轮，烧钱方向
C) 失败单独小上限（默认 2，可配）(recommended)
  ✅ 瞬时故障大概率 2 次内自愈；必然失败最多浪费 2 次，止损明确
  ❌ 多一个配置项（failure_continues）与一条计数路径，状态机稍复杂

State: approved
Actual answer: C) 失败单独小上限（D4，2026-10-31，默认 2，可配 failure_continues）
Accepted scope: StopFailure 回合走独立失败计数，上限 failure_continues（默认 2）；不占正常轮 max_continues；两计数都在 UserPromptSubmit（人发起）归零
History: 外部复核发现 5 升级为决策。

### F7 → D5: 多会话下的 cap 语义

Finding: F7，Medium（缺口），confidence 8/10，spec §3.3，reviewer：外部复核 Native fallback（2026-10-31）
Plan baseline: 隐含单 pane 假设，从未声明
Runtime evidence: 锁屏是**机器级**信号；max_continues 是**单记录（单会话）**级。同机 N 个 Claude 会话同时带 finished_at → 锁屏后全部进入续跑，过夜总按键上限 = N × (10 + 2)，通知卡也翻倍。

| 选择 | 当前 | A: 每会话各自 cap（声明现状） | B: 加机器级全局 cap |
|---|---|---|---|
| 单会话护栏 | 每会话 10+2 | 不变 | 不变 |
| 多会话总按键 | N × 12 无界（N 不定） | N × 12，N 由用户自知 | 全局 ≤ 全局上限 |
| 实现复杂度 | — | 零（写进 spec 即可） | 需跨记录汇总计数与全局判断 |
| 通知量 | 随会话数翻倍 | 随会话数翻倍 | 可选汇总 |

Question D5:
锁屏是多会话共享的机器级信号，上限却是单会话的——要不要加机器级全局上限？
ELI10: 您同时开着 3 个 Claude 会话过夜，每个最多续跑 12 次，总共可能 36 次。要么接受「每会话各自封顶」，要么加一道全机器的总闸。
Stakes: 多会话是真实用法时，总消耗无上界；单会话用户则完全无感。
Recommendation: A because 实际使用通常 1–2 个活跃会话，每会话 12 次已是明确预算；全局闸多一套跨记录状态，YAGNI。声明边界即可，重启条件：出现 3+ 会话并行的真实工作流。
Completeness: Note: options differ in kind, not coverage — no completeness score.
Net: 先声明「cap 是每会话的」这一边界，把全局闸留给真实需求出现的那天。

A) 每会话各自 cap（推荐）
  ✅ 零实现成本，与既有「人发起的一轮」语义天然对齐（每会话各有其轮）
  ✅ 边界写进 spec 声明段，读者不再误读为机器级
  ❌ 多会话过夜总消耗 = 会话数 × 12，只有用户自己有数
B) 加机器级全局 cap
  ✅ 总消耗有硬上界，多会话场景可控
  ❌ 跨记录全局状态引入新的并发面，单会话用户为用不到的闸付费

State: approved
Actual answer: A) 每会话各自 cap（D5，2026-10-31）
Accepted scope: 声明 cap 为每会话边界，不加机器级全局闸；重启条件 = 出现 3+ 会话并行的真实工作流
History: 外部复核发现 7 升级为决策。

### 外部复核记录（Outside Voice，2026-10-31）

- **Codex 直跑**：3 次尝试均不可用——①模型配置 `glm-5.3-flash` 不被账号支持（修复：改设端点实服务的 `auto/deepseek-v4.1-flash`）；②5 分钟超时（codex 自主探库烧尽预算，印证本仓 learning `codex-review-times-out-on-large-repos`）；③禁探库重试后输出仍被 LunaRoute 代理在 240 秒处截断、缺收尾标记。部分输出按规则**不计入**评审。`outside_status: unavailable`。
- **Native fallback（Claude Plan 子代理）**：完成，11 条发现。核实处置：#1 Critical（UserPromptSubmit 归零 count 被 daemon 自己的 Enter 打穿）——属实，修法落文为 D3 必要实现（`last_auto_submit` 标记）；#2 阶段卡重发、#3 锁态缓存竞态、#4 双按、#6 互斥无机制、#8 匹配失效静默、#9 状态行矛盾、#10 S8 验证方式、#11 实施顺序——属实，均已落文；#5 失败回合重试策略 → D4；#7 多会话 cap → D5。无未采纳项。

### NOT in scope

- 机器级全局 cap（D5 否决，重启条件已记）
- 建议内容审查（§1.2 非目标）
- 解析/结构化读取建议条（R2 只做文本匹配，`conversation.read.prompt` 前科在案）
- 未锁屏时的任何续跑动作（陛下拍板）

### What already exists（复用清单）

按键 `pebrel_runtime.press`、读屏 `dialog()`、状态记录全套（state.py 原子写+flock）、`_PressRefused` 退避（daemon.py:124-131）、通知三通道扇出与 `title_with_project`、config 校验助手（config.py:92-99）、pytest 假 Deps 模式（tests/test_daemon.py）。新写只有：WTS P/Invoke、match_suggestion、continue 状态机、hook 字段扩展。

### Failure modes（新路径的现实故障）

| 路径 | 故障 | 覆盖 |
|---|---|---|
| 锁屏按键 | Pebrel 拒绝注入（R1 未验证） | spike 打头，失败即重开设计；按键失败走 _PressRefused 退避 |
| 匹配 | Claude Code 更新改措辞 → 静默失配 | doctor 零匹配统计行（发现 8） |
| 锁态 | WTS 查询失败/UNKNOWN | locked=None → 不代跑；doctor 行可观测 |
| 按键时序 | UI 滞后双按 / 陈旧锁态 | `pressed_at` 静默窗 + 按键前锁态强采 |

无「无测试 + 无处理 + 静默」三者兼备的路径 → **critical gaps: 0**。

### Worktree 并行策略

spike 阶段可并行（R1 ∥ R2，互不依赖）；产品代码依赖 spike 结论，**串行**：

| 步骤 | 模块 | 依赖 |
|---|---|---|
| spike R1/R2 | tools/ | — |
| config → presence → continuation.py → state/hooks → daemon → doctor | cc_autopilot_notify/ | spike |
| S1–S8 验收 | 真机 | 全部 |

## Implementation Tasks

Synthesized from this review's findings. Each task derives from a specific finding above. Run with Claude Code or Codex; checkbox as you ship.

- [ ] **T1 (P1, human: ~2h / CC: ~10min)** — spike：R1 锁屏注入 + R2 建议条形态观测
  - Surfaced by: §2 R1/R2；外部复核 #4（R1 依赖 R2，先造建议条再锁屏试按）
  - Files: tools/（一次性探针脚本）、docs/spikes/
  - Verify: spike 报告附原始采样与可复算口径；R1 失败即整体重开
- [ ] **T2 (P1, human: ~3h / CC: ~30min)** — config `autopilot` 节 + presence WTS locked 字段
  - Surfaced by: D2（WTS 单信号）；外部复核 #11（config 先行）
  - Files: cc_autopilot_notify/config.py, presence.py, tests/test_config.py, tests/test_presence.py
  - Verify: pytest 解析层用例（三段/缺段/ERR→None）；S1 真机锁/解锁各采一次
- [ ] **T3 (P1, human: ~1d / CC: ~1h)** — continuation.py：match_suggestion + tick 安全机制（last_auto_submit / stage_card_sent / pressed_at / 锁态强采 / 应答优先）
  - Surfaced by: D1/D3；外部复核 #1–#4、#6
  - Files: cc_autopilot_notify/continuation.py（新）, tests/test_continuation.py（新）
  - Verify: pytest——未锁屏零按键（S3）、恰好一次按键（S4）、上限停（S5）、90s 阶段卡不重发（S6）、干扰文本不匹配（S7）
- [ ] **T4 (P1, human: ~4h / CC: ~30min)** — state 字段扩展 + Stop/StopFailure/UserPromptSubmit hook
  - Surfaced by: §3.3；外部复核 #1（hook 端 last_auto_submit 判定）
  - Files: cc_autopilot_notify/state.py, hook_notify.py, tests/test_state.py, tests/test_hooks.py
  - Verify: pytest——旧记录兼容、daemon 提交不清 count、人提交归零
- [ ] **T5 (P2, human: ~4h / CC: ~30min)** — daemon 接线 + doctor 两行（locked / 零匹配统计）
  - Surfaced by: §3.3；外部复核 #8
  - Files: cc_autopilot_notify/daemon.py, doctor.py, tests/test_daemon.py, tests/test_doctor.py
  - Verify: pytest 全绿 + `bash scripts/cc-autopilot-notify doctor` 新行 PASS
- [ ] **T6 (P2, human: ~2h / CC: ~20min)** — 通知文案三卡（续跑/阶段完成/上限）+ 失败标记文案
  - Surfaced by: §3.5；D4
  - Files: cc_autopilot_notify/channels/, tests/test_channels.py
  - Verify: pytest 文案断言 + test-channel 实发
- [ ] **T7 (P1, human: ~1h / CC: ~20min)** — S1–S8 真机验收（含锁屏实跑）
  - Surfaced by: §4
  - Files: 无代码；结论回填 spec 状态行
  - Verify: `python -m pytest tests/ -q` 全绿 + 真机锁屏实测记录

### Unresolved decisions

无（D1–D5 全部获批；外部复核 11 条全部处置）。

### Completion summary

- Step 0: Scope Challenge — scope accepted as-is（结构定为独立 continuation.py，D1）
- Reality rows: 7/7 applicable rows emitted，premise unverified=2（LogonUI 解锁侧、建议条 90s 窗口——均归 spike）
- Architecture Review: 1 informational（A1 按键失败路径落文）；design-twice 完成，红八旗无违例
- Code Quality Review: 1 informational（B1 config accessor 模式落文）
- Test Review: 图已产出，16 条拟增路径全部有对应计划测试；回归规则：test_daemon.py 既有用例即「应答不动」契约证明
- Performance Review: 0 issues
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 0（无需新 TODO；doctor 零匹配统计已入 T5 范围）
- Failure modes: 0 critical gaps
- Unresolved decisions: 0
- Outside voice: codex unavailable（3 次尝试）→ Native fallback 完成，11 条发现全部处置
- Parallelization: 1 lane（spike 内部 2 路并行），其余串行
- Lake Score: 5/5 = D1–D5 全部按推荐项通过

## Design Review Completion Summary

```
  +====================================================================+
  |         DESIGN PLAN REVIEW — COMPLETION SUMMARY                    |
  +====================================================================+
  | System Audit         | 无 DESIGN.md；UI 面=三通道通知卡（窄）；     |
  |                      | 设计二进制可用（mockup 不适用：网页风示意图  |
  |                      | 会捏造新视觉方向，决策空间在文案/频控）      |
  | Step 0               | 初始 5/10；焦点=通知体验（陛下选定 A）       |
  | Pass 1  (Info Arch)  | 4/10 → 9/10（四卡信息层级定稿）             |
  | Pass 2  (States)     | 4/10 → 9/10（频控决策 1；dedupe 不适用已记） |
  | Pass 3  (Journey)    | 6/10 → 9/10（阶段/上限卡安抚文案定稿）       |
  | Pass 4  (AI Slop)    | 8/10 → 9/10（utility 语言成文）             |
  | Pass 5  (Design Sys) | 5/10 → 9/10（event id+色板进既有映射表）     |
  | Pass 6  (Responsive) | 6/10 → 9/10（截断 60 字符定稿）             |
  | Pass 7  (Decisions)  | 3 resolved, 0 deferred                     |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (2 items)                           |
  | What already exists  | written                                     |
  | TODOS.md updates     | 0 items proposed                            |
  | Approved Mockups     | 0 generated（不适用，见上）                  |
  | Decisions made       | 3 added to plan                             |
  | Decisions deferred   | 0                                           |
  | Overall design score | 4/10 → 9/10（六轮最低分）                   |
  +====================================================================+
```

### NOT in scope（设计评审）

- 通知卡网页风 mockup：真实渲染在飞书侧，示意图会凭空捏造新视觉方向（现有卡片惯例已是既定方向）。
- 通知中心聚合/汇总卡视图：飞书侧能力，不在本项目控制面内。

### What already exists（设计评审）

`_COLORS` 事件→头色映射（feishu.py:53-55，默认 blue）、`_TITLES` 事件→标题映射（hook_notify.py）、`title_with_project` 项目名前缀、时间戳脚注、`notify.dedupe_seconds` 去重、`message_translations` 文案替换、Windows 通知点击回 pane。四张新卡全部挂进既有词汇表，不新建设计系统。

### Implementation Tasks（设计评审）

- [ ] **TD1 (P2, human: ~2h / CC: ~15min)** — channels：三个新 event id 进 `_COLORS`/`_TITLES`，四张卡按 §3.5 文案包实现（含 60 字符截断与空建议占位）
  - Surfaced by: Pass 1/2/5/7 — 决策 1/2/3
  - Files: cc_autopilot_notify/channels/feishu.py, hook_notify.py, continuation.py（卡构造调用方）, tests/test_channels.py
  - Verify: pytest 文案/色板断言 + `test-channel feishu` 实发四张样卡

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | — | Scope & strategy | 0 | — | — |
| Outside Review | codex-plan-review（codex 不可用 → native fallback） | Independent 2nd opinion | 1 | unavailable→in-host completed | codex 3 次不可用；native 11 条发现全部处置（1 Critical） |
| Eng Review | /plan-eng-review | Architecture & tests (required) | 1 | issues_open | 18 issues（2 informational + 16 计划内测试缺口），0 critical gaps；D1–D5 获批 |
| Design Review | /plan-design-review | UI/UX gaps | 1 | clean | score: 4/10 → 9/10，3 decisions；四卡文案包/频控/截断定稿 |
| DX Review | — | Developer experience gaps | 0 | — | — |

**OUTSIDE COVERAGE:** provider codex, phase plan-review, 4 次尝试——3 次不可用（模型配置/超时/代理截断）+ 1 次 /codex 专项复核输出为泄漏草稿（18 条发现，无收尾标记）；草稿发现经逐条核实：2 Critical 采纳（marker 时序、窗口余量）、多项加固采纳、2 条被代码证据否定（pane 寻址、state 无锁）；Native fallback（in-host）完成，11 条发现全部处置。设计评审 outside voices 未运行（用户已指定流程，记录为 skipped）。
**VERDICT:** ENG + DESIGN + CODEX-CONSULT CLEARED — 设计定稿（含影子上线策略），可进入实施计划。eng review required: 已完成。

NO UNRESOLVED DECISIONS