# cc-autopilot-notify 设计规格

- 日期：2026-10-27
- 状态：待陛下评审
- 作者：Claude（调研 + 设计）
- 目标仓库：`/var/www/cc-autopilot-notify`（新建，独立仓库）

---

## 1. 背景与问题

陛下在 WSL 中运行 Claude Code，并启用了 `bypass permissions`。bypass 只能自动通过**权限审批**，无法处理**问答面板**：当 Claude 调用 `AskUserQuestion` 时，会话会停下等人，无人值守的自动化就此中断。

同时存在通知缺口：人离开座位后，无法在 Windows 桌面或手机上及时得知「Claude 正在等你」。

### 1.1 目标

1. **通知**：Claude 需要人时，通过飞书、终端、Windows 系统通知三条通道送达；点 Windows 通知能回到发起它的那个终端 pane。
2. **自动跳过**：开启后，问答面板长时间无人操作时自动提交答案继续跑。前台 150 秒、后台 90 秒。
3. **智能作答**：自动提交时优先选 Claude 自己认为正确的选项，而不是机械选第一项。
4. **人在场优先**：面板内一旦有人操作，自动跳过立即取消。

### 1.2 成功标准（可验证）

| # | 标准 | 验证方式 |
|---|---|---|
| S1 | 飞书收到卡片，含项目名/问题/选项/耗时 | 真实发送并截图 |
| S2 | 终端收到 OSC 9 通知 | 在 Pebrel 中实跑，肉眼确认 |
| S3 | Windows 右下角弹框，点击回到正确 pane | 多 pane 场景实测 |
| S4 | 前台（Pebrel 是前台窗口）无人操作 150s → 自动提交并继续 | daemon 独立计时实测 |
| S5 | 后台（Pebrel 非前台）无人操作 90s → 自动提交并继续 | daemon 独立计时实测 |
| S6 | 面板内按键 → 不自动提交 | 实测 |
| S7 | 自动提交选的是 Claude 标注的推荐项 | 构造带 `(Recommended)` 的提问实测 |
| S8 | 任一环节故障都不导致会话永久卡死 | 逐项注入故障 |

### 1.3 非目标

- 不做跨设备远程操控（回复飞书消息去驱动 Claude）。
- 不支持 Codex / Gemini 等其他 agent（仅 Claude Code）。
- 不接管权限审批（由 bypass 负责）。
- 不做 GUI 应用。

---

## 2. 调研结论

### 2.1 候选项目横向比较

以下元数据均由 GitHub API 逐条核实（star / 最近推送 / 许可证），非凭记忆。

| 项目 | ★ | 最近推送 | 许可证 | 栈 | 与本需求的关系 |
|---|---|---|---|---|---|
| [tuchg/Lucarne](https://github.com/tuchg/Lucarne) | 340 | 2026-08-02 | MIT | Rust 守护进程 | 能力最全（含 AskUserQuestion 答题），但无飞书、架构重、不解决 Windows 弹框 |
| [mylee04/code-notify](https://github.com/mylee04/code-notify) | 289 | 2026-09-04 | MIT | Shell | 跨平台桌面通知，无飞书、无答题、无点击回跳 |
| [JessyTsui/Claude-Code-Remote](https://github.com/JessyTsui/Claude-Code-Remote) | 1287 | 2025-12-06 | MIT | Node.js | **超 6 个月未更新**，邮件架构，不适用 |
| [kdush/Claude-Code-Notifier](https://github.com/kdush/Claude-Code-Notifier) | 97 | 2026-02-01 | Apache-2.0 | Python | 近 9 个月无推送 |
| [wyattjoh/claude-code-notification](https://github.com/wyattjoh/claude-code-notification) | 95 | 2026-08-19 | MIT | Rust | 仅 macOS |
| [chuilishi/claude-code-notify](https://github.com/chuilishi/claude-code-notify) | 37 | 2026-05-29 | MIT | Rust | Windows Toast + `claude-focus://` + HWND/UIA 激活，最接近弹框需求；但无飞书、无答题 |
| [flaviomartil/claude-code-toast](https://github.com/flaviomartil/claude-code-toast) | 37 | 2026-05-05 | MIT | PowerShell+Bun | WSL2 Toast，无点击回跳 |
| [MarioZZJ/cc-notify-hooks](https://github.com/MarioZZJ/cc-notify-hooks) | 20 | 2026-08-18 | MIT | Shell+jq | 11 通道含飞书、分级推送；本机已在用。无终端通知、无 Windows Toast、无答题 |
| [QZYH123/OrbCue](https://github.com/QZYH123/OrbCue) | 14 | 2026-10-05 | MIT | Rust+Tauri | Windows 悬浮球，需常驻 GUI |
| [claudes-world/cctoast-wsl](https://github.com/claudes-world/cctoast-wsl) | 6 | 2026-10-19 | **NOASSERTION** | TS | **许可证不可确认，排除** |
| [whereisdw/claude-code-hooks-recipes](https://github.com/whereisdw/claude-code-hooks-recipes) | 1 | 2026-10-25 | MIT | Shell+py | 关键技术验证：`PermissionRequest` + `updatedInput.answers` 答题（仅 2.1.283 验证） |

技术参考（非产品）：[PeteChu/cc-answer](https://github.com/PeteChu/cc-answer)、[i-noma-ru/claude-ask-choice](https://github.com/i-noma-ru/claude-ask-choice)、[togishima/better-ask-picker](https://github.com/togishima/better-ask-picker)。

### 2.2 结论

**自研新仓，选择性借鉴而非整体复用。**

理由：

- 没有单一项目同时覆盖「飞书 + 终端 + Windows 点击回跳 + 答题」。最接近的 `cc-notify-hooks`（飞书）与 `chuilishi`（Windows Toast）在架构上互斥（Shell vs Rust 二进制）。
- 本需求的答题逻辑依赖 CC 内部尚未稳定的 `updatedInput` 语义，必须由我们自己掌握，才能随时跟随 CC 版本调整。
- 工作量可控：三个外部依赖（飞书 webhook、powershell.exe、Pebrel CLI）均已在本机可用。

**借鉴清单**：`cc-notify-hooks` 的分级推送与 `clear_pending`（人回来即撤销）思路；`chuilishi` 的点击回跳思路（但本方案改由 Pebrel 原生承担，见 3.3）。

---

## 3. 关键机制事实（取证结论）

本节所有结论均来自对本机 `~/.local/share/claude/versions/2.1.308` 二进制的取证，以及本机环境的实测。**这些事实决定了架构形状。**

### 3.1 Claude Code 原生 AFK 自动继续

二进制中存在：

```js
x = a.CLAUDE_AFK_TIMEOUT_MS ?? x ?? VTo    // VTo = gqe = 60000（默认 60 秒）
L = Math.min(L ?? a.CLAUDE_AFK_COUNTDOWN_MS ?? KTo, x)   // KTo = 20000
```

计时逻辑：

```js
if (TNe() === true) Fe.current = Ke;              // 终端有焦点 → 每秒重置起点
let Ge = Ke - Math.max(Tp(), Fe.current);
if (Ge >= x) { /* 触发超时 */ }
```

超时后构造：

```js
{ behavior: "allow", updatedInput: { ...input, answers, annotations,
    ...(afkTimeoutMs && { afkTimeoutMs }) } }
```

界面文案：`auto-continue in {n} s · any key to stay`。

**结论与影响**：

1. 「静默跳过」官方已有，**但语义是「终端失焦后计时」，不是「前台/后台双阈值」**。终端保持焦点时永不超时。
2. 官方 AFK 是进程内读取的**单一超时值**，且无任何外部传输入口。
3. 因此「前台 150s / 后台 90s」**无法通过环境变量实现**，必须由本项目自身值守。

> 更正记录：初版方案曾提出「用 hook 动态改写环境变量实现双阈值」。该方案不成立——hook 是 CC 的子进程，改自身 env 不影响父进程。此结论已在设计阶段推翻，未进入实现。

### 3.2 `updatedInput.answers` 的采纳条件与风险

`PermissionRequest` hook 的 `updatedInput` 只接受四个字段：

```js
re = new Set(["answers", "annotations", "response", "followUp"]);
ae = "afkTimeoutMs";   // 判为 host_field，hook 不得设置
```

`answers` 必须是已存在的选项 label；`multiSelect` 时可为数组。

**风险点**：`AskUserQuestion` 工具定义中 `requiresUserInteraction(){ return !0 }` 为**硬编码**，而决策合并逻辑存在分支会丢弃 hook 的 `updatedInput`：

```js
if (n.requiresUserInteraction?.()) {
  let Se = Cut(await cA(n, r, {...y, toolUseId: h}, { hookUpdatedInput: void 0 }), n.name);
  ...
}
```

参考实现（whereisdw）仅在 CC 2.1.283 验证过，本机为 2.1.308。

**因此：这是整个方案的版本风险单点。必须先证，证不过则退回纯官方 AFK 方案。**

### 3.3 `hasExternalRacer` 与阻塞语义

二进制中 `hasExternalRacer` 为真时，AFK 计时器被禁用：

```js
Hl = !wr && !x.hasExternalRacer && !$t() && (be !== null || CLAUDE_AFK_TIMEOUT_MS !== void 0)
```

`PermissionRequest` hook 是阻塞的：hook 不返回，问答面板不显示。

**因此**：若在 hook 内「等 150 秒再答题」，用户会看到面板消失 150 秒，违背「面板内交互会取消」的需求。

**采用方案**：面板照常显示（hook 快速放行），由外部抢答者在超时后调用 Pebrel **运行时协议** `conversation.choose` 作答。详见 4.2 首选路径。

### 3.4 本机环境事实

| 项 | 实测结果 |
|---|---|
| 终端 | Pebrel 2.3.1（`TERM_PROGRAM=pebrel`），GPU 终端模拟器，非 Windows Terminal |
| WSL Interop | 可用（`/proc/sys/fs/binfmt_misc/WSLInterop`） |
| PowerShell | `/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe` 可用 |
| BurntToast | **未安装**（`BURNTOAST_MISSING`） |
| Pebrel 通知 | `pebrel notify-test` → AUMID 注册 OK、Toast 显示 OK、exit=0 |
| **Pebrel 驱动方式** | **CLI 无 `conversation` 子命令**（`pebrel ctl` 共 32 个子命令，`ctl conversation` 报 `unrecognized subcommand`）。`conversation.*` 只存在于**运行时 JSON 协议** |
| **Pebrel 运行时入口** | `%APPDATA%\Pebrel\runtime.port` 首行端口、次行 32-hex token；`TCP 127.0.0.1:<port>` 直连，JSON Lines 收发 |
| **运行时实测** | `runtime.describe` ok；`conversation.read` ok（返回 `identity.epoch`、`prompt`、`revision`）；`conversation.choose` 用非法 `prompt_id` 被正确拒绝（`{code:"prompt_changed"}`） |
| `conversation.choose` 参数 | `window_id` / `pane_id` / `identity{kind,session_id,epoch}` / `prompt_id`(64 hex) / `option`(0–8) |
| `orchestrate` 能力边界 | step 类型仅 7 种（`new_tab`/`focus`/`split`/`prompt`/`run`/`agent_launch`/`wait`），**不含 conversation 类 op** |
| 前后台判定 | `GetForegroundWindow()` 可用，已取到 `FG_HWND` 与 `pebrel` 进程 HWND |
| Pebrel 窗口激活 | CHANGELOG 明确记载：`clicking one focuses the terminal that raised it`、`Windows notification activation restores the corresponding application window` |
| `session_id → pane_id` | `pebrel pane list` 已回传每个 pane 的 `agent.session_id`，可直接映射 |
| systemd user | 运行中（`systemctl --user is-system-running` → `running`） |
| CC 凭据形态 | `ANTHROPIC_API_KEY` **未设置**；实际为 `ANTHROPIC_AUTH_TOKEN` + 自定义 `ANTHROPIC_BASE_URL`（网关） |

**运行时 token 安全**：token 在 `runtime.port` 中明文。本项目读取后**不得写入任何日志、通知或状态文件**。

### 3.5 现有配置（不得破坏）

`~/.claude/settings.json` 中已存在：

- `PreToolUse` matcher `(AskUserQuestion|mcp__.*__AskUserQuestion)` → gstack 的 `question-preference-hook`
- `PermissionRequest`（全匹配）→ pebrel 的 `pebrel-hook`

本项目的 hook **与之并列新增，不修改、不替换、不排序依赖**。

---

## 4. 架构

### 4.1 总览

```
                    ┌──────────────────────────────────────┐
   CC 事件 ────────►│  cc-autopilot-notify (Python 单包)    │
                    └──────────────────────────────────────┘
        │                     │                    │
        │                     │                    │
   PreToolUse            PermissionRequest     Notification / Stop
   (AskUserQuestion)     (AskUserQuestion)     (完成/失败)
        │                     │                    │
        ▼                     ▼                    ▼
   即时通知推送         可选抢答值守          结果通知
   (飞书/终端)         (见 4.2)                  │
        │                                          │
        └──────────────► presence（前后台判定）◄───┘
                                  │
                                  ▼
                         powershell GetForegroundWindow
```

### 4.2 自动跳过：两条路径（首选 / 兜底）

**路径 A（首选）：Pebrel 运行时抢答**

```
PreToolUse(AskUserQuestion)
  → 记录 {session_id, pane_id, window_id, questions, 出现时刻}
  → 推送通知
  → 快速放行（不 exit 2、不返回 decision）

面板正常显示，人能随时操作

守护进程（常驻，单线程事件循环）
  → 维护每个未决项的 last_activity（初值 = 面板出现的时刻）
  → 用 conversation.read 轮询：revision 变化即更新 last_activity（说明人在动）
  → idle = now - last_activity
  → 前台判定为真时用 150s，否则 90s
  → idle >= 阈值 且 prompt 仍存在：
       1. conversation.read 取当前 prompt 的 prompt_id 与 identity.epoch
       2. judge 决定 option 序号
       3. 直连 TCP 发 conversation.choose
       4. 若回 prompt_changed → 重新 read 一次并重试，最多 1 次
       5. 记录结果并推送「已自动选择 X」
```

> **2026-10-27 实测修正**：`conversation.read.prompt` 对 Claude Code 的问答
> 面板**恒为 null**（130 秒探针、11 次采样、0 命中），即使 `task_state` 为
> `attention`。故路径 A 改为 A′：以 `task_state == "attention"` 作为面板门，
> 以 hook 的结构化选项决定答案，以 `pane.read` 的文本做一致性校验，以
> `pane.send_key`（方向键 + Enter）提交。`conversation.choose` 保留在
> `pebrel_runtime` 中但不再被调用。端到端已验证通过。

**驱动方式（原设计，已被 A′ 取代，保留作记录）**：

- CLI 没有 `conversation` 子命令。`pebrel ctl conversation` 返回 `error: unrecognized subcommand 'conversation'`。
- 必须直连运行时：读 `%APPDATA%\Pebrel\runtime.port`（首行端口，次行 32-hex token），`TCP 127.0.0.1:<port>`，JSON Lines 收发，请求体为 `{"protocol":"nebula.runtime","version":1,"id":...,"token":...,"method":"conversation.choose","params":{...}}`。
- 连接建立前先发 `runtime.describe` 校验 `protocol_version`；不匹配则路径 A 关闭。
- token 只用于连接，**不得落日志**。

**「有人在面板操作」的判据**（不依赖单一来源）：`conversation.read` 的 `revision` 字段变化即视为有交互。若连续两次读取 `revision` 不变，且 `prompt` 仍在，才计入 idle。

**路径 B（兜底）：`PermissionRequest` hook 同步答题**

仅当路径 A 的前置条件不满足（运行时不可达、协议版本不符、连续 2 次 `choose` 失败）时启用：

```
PermissionRequest(AskUserQuestion)
  → 若配置开启且路径 A 不可用
  → 同步等待 min(阈值, hook_timeout - 10s)
  → 到点返回 {"behavior":"allow","updatedInput":{"answers":{...}}}
```

> 路径 B 是阻塞的：hook 不返回则面板不显示。因此它只在路径 A 不可用时启用，作为「宁可看不到面板也要跑完」的降级。

**路径 C（最后兜底）：官方 AFK**

`CLAUDE_AFK_TIMEOUT_MS = 300000`（300 秒）。恒大于本项目阈值，只在 A/B 都失效时接住，避免会话永久等待。

### 4.3 「Claude 自己选」的实现

分两级，默认零成本：

**第一级 · 读推荐标记（默认，零 API 成本）**

从题干与选项中提取推荐信号，按优先级：

1. 选项 label 以 `(Recommended)` / `（推荐）` 结尾
2. 选项 description 含 `recommended` / `推荐`
3. 题干含 `Recommendation: X` 且 X 能匹配某个 label

命中即选该项。**多义或无命中 → 进入第二级。**

**第二级 · 模型判卷（可配置，默认开启但带预算）**

- 输入：题干 + 全部选项（label + description）
- 输出：选项序号
- **凭据链**：本机 `ANTHROPIC_API_KEY` **未设置**，实际为 `ANTHROPIC_AUTH_TOKEN` + 自定义 `ANTHROPIC_BASE_URL`（网关）。实现须读这两个变量并发送 `Authorization: Bearer <ANTHROPIC_AUTH_TOKEN>` 到 `<ANTHROPIC_BASE_URL>/v1/messages`。**不得假设 `ANTHROPIC_API_KEY` 存在**；两者都缺时跳过第二级，视同无标记。
- 超时 8 秒、单次最多 1 次重试
- **失败回落：选第一个选项**（陛下已确认），并在飞书/终端通知中明示「判卷失败，已回落第一项」

### 4.4 组件与职责

| 模块 | 职责 | 依赖 |
|---|---|---|
| `hook_pre_tool.py` | 解析 `AskUserQuestion` 事件，登记未决项，触发通知，快速返回 | stdlib |
| `hook_permission.py` | 路径 B 同步答题 | stdlib |
| `hook_notify.py` | `Notification` / `Stop` / `StopFailure` 事件通知 | stdlib |
| `daemon.py` | 常驻守护：遍历未决项、计时、判定人是否在场、执行抢答、心跳 | stdlib |
| `pebrel_runtime.py` | 运行时 TCP 客户端（`runtime.describe` / `conversation.read` / `conversation.choose`） | stdlib `socket` |
| `presence.py` | 前后台判定 | `powershell.exe` |
| `judge.py` | 推荐标记解析 + 模型判卷 | stdlib（HTTP） |
| `channels/feishu.py` | 飞书交互卡片 | stdlib `urllib` |
> 2026-10-27 变更（-owner 决定）：`channels/terminal.py` 与 `channels/pebrel.py`
> 已移除。Pebrel 自带 Claude 通知弹框，双份弹窗太吵，Windows 侧交给 Pebrel
> 原生；本项目只保留飞书推送 + 问答静默跳过。
| `state.py` | 未决项持久化（JSON 文件 + `flock`） | stdlib |
| `config.py` | 配置加载与校验 | stdlib |
| `doctor.py` | 自检命令（见第 7 节） | stdlib |

**技术栈决定**：Python 3，**零第三方依赖**。理由：hook 冷启动快于 Node；本机已有 Python；避免 hook 因依赖缺失而静默失败。

**为何重写而非复用 `cc-notify-hooks` 的飞书实现**：现有实现是 bash + jq，与本项目 Python 单包的契约不兼容（需要「判卷失败必须明示」「降级不阻断」等语义，bash 版无条件吞错）。按 reuse ladder，契约不兼容时不迁就复用。

### 4.5 数据流：一次问答的完整生命周期

```
1. CC 调 AskUserQuestion
2. PreToolUse hook 触发
   → 写 state: {id, session_id, pane_id, window_id, questions, appeared_at, last_activity}
   → 推送通知（飞书 + 终端）
   → exit 0（放行）
3. 面板显示，人可能操作
4. daemon 循环（每 1s）：
   a. conversation.read → revision 变化 → 更新 last_activity（人在动）
      若 idle > 阈值 → 删除未决项，本轮结束
   b. idle >= 阈值（前台 150s / 后台 90s）→ 判卷 → conversation.choose
5. PostToolUse(AskUserQuestion) 触发 → 清理未决项（无论是否抢答）
6. Stop / Notification 推送完成通知
```

> `deadline_front` / `deadline_back` 不再预先算成绝对时间：阈值依赖「当前是否前台」，而前台状态会变。改存 `last_activity` + 每次循环实时判定。

---

## 5. 配置

配置文件：`~/.config/cc-autopilot-notify/config.json`（JSON，便于 hook 快速解析）。

```json
{
  "enabled": true,
  "thresholds": { "foreground_seconds": 150, "background_seconds": 90 },
  "afk": { "set_claude_afk_timeout_ms": 300000 },
  "judge": { "enabled": true, "model": null, "timeout_ms": 8000, "fallback": "first_option" },
  "channels": {
    "feishu": { "enabled": true, "webhook_env": "CC_AUTOPILOT_FEISHU_WEBHOOK" },
    "terminal": { "enabled": true },
    "pebrel": { "enabled": true }
  },
  "quiet_hours": { "enabled": false, "start": "00:00", "end": "09:00" },
  "min_duration_seconds": 30
}
```

**凭据处理**：飞书 webhook **不得写入配置文件**，从环境变量读取。环境变量在 `~/.claude/settings.json` 的 `env` 段设置（与既有做法一致）。若环境变量缺失，飞书通道降级为关闭并写日志，**不阻断**。

**`judge.model` 为 `null` 时**：跳过第二级模型判卷，只用第一级推荐标记；无标记则回落第一项。这样陛下可随时把判卷完全关掉而不影响其余功能。

**`afk.set_claude_afk_timeout_ms`** 由安装脚本写入 `~/.claude/settings.json` 的 `env` 段；这是唯一需要修改 CC 全局配置的地方，安装时须展示 diff 并二次确认。

---

## 6. 错误处理与降级

| 故障 | 检测方式 | 行为 |
|---|---|---|
| **daemon 自身挂掉** | daemon 每 10s 写心跳文件；hook 发现心跳超 60s | 通知中标注「自动跳过不可用」，并自动重启 daemon（systemd user service `Restart=on-failure`） |
| PowerShell 调用失败 | 非 0 退出 / 超时 2s | 判为「后台」，用 90s 保守值，写日志，不阻断 |
| Pebrel 运行时不可达 | TCP 连接失败 / `runtime.describe` 超时 | 路径 A 关闭，启用路径 B |
| Pebrel 协议版本不符 | `runtime.describe` 的 `protocol_version != 1` | 路径 A 关闭并告警（需人工确认升级后语义） |
| `conversation.choose` 被拒 | 返回 `ok:false` | 记一次失败；连续 2 次则该 session 降级路径 B |
| 模型判卷超时/报错 | 8s 超时 / 非 2xx | 回落第一项，通知中明示兜底 |
| 判卷凭据缺失 | `ANTHROPIC_AUTH_TOKEN` 与 `ANTHROPIC_API_KEY` 均未设置 | 跳过第二级，等同「无推荐标记」 |
| 飞书 webhook 失败 | `urllib` 异常 / 非 2xx | 写日志，其余通道照常 |
| 状态文件损坏 | JSON 解析失败 | 丢弃并重建，写日志 |
| hook 超时被杀 | CC 侧 | 官方 AFK 在 300s 接住 |
| 并发多 session | 状态文件按 `session_id` 分键 + `flock` | 互不干扰 |

**统一原则**：任何通知通道失败都不得让 hook 返回非 0 或阻塞；任何判定失败都向「更安全/更保守」方向回落，并**必须让用户看见兜底发生**（写日志 + 通知中标注）。

---

## 7. 验证计划

按 `verification.md`：每条声称都须有跑过的命令与亲眼所见的输出。

### 第 0 步 · 版本风险先证（阻塞后续全部工作）

**问题**：在 CC 2.1.308 上，`PermissionRequest` hook 返回 `updatedInput.answers` 是否仍被采纳？

**方法**：
1. 写最小 hook：固定返回 `{"behavior":"allow","updatedInput":{"answers":{"<问题文本>":"<第一个选项>"}}}`
2. 在真实会话中触发一个 `AskUserQuestion`
3. 观察：会话是否直接显示 `User answered Claude's questions: · <问题> → <label>` 并继续，且**面板未出现**

**判定**：
- 通过 → 按本 spec 实施（路径 B 可用作兜底）
- 不通过 → 路径 B 作废，路径 A（Pebrel 抢答）+ 路径 C（官方 AFK）仍成立，方案继续但能力降级

**产出**：`docs/spikes/2026-10-27-permission-request-answers.md`，记录 CC 版本、hook 内容、实际输出。

**该探测必须固化为 `cc-autopilot-notify doctor` 的一项**，不得只做一次性 spike——CC 升级后要能一条命令复跑。

### 第 1 步 · `doctor` 自检命令（可重复验证）

`cc-autopilot-notify doctor` 输出每项 `PASS/FAIL/SKIP` 与依据，退出码非 0 表示有 FAIL：

| 检查项 | 方法 | 失败含义 |
|---|---|---|
| CC 版本 | 读 `claude --version` | 记录用于对照 |
| `updatedInput.answers` 仍被采纳 | 第 0 步的最小 hook 回放（可离线断言 hook 输出格式；在线验证需真实会话，标 SKIP 并提示） | 路径 B 失效 |
| Pebrel 运行时可达 | TCP 连接 + `runtime.describe` | 路径 A 失效 |
| Pebrel 协议版本 | `protocol_version == 1` | 需人工确认 |
| `conversation.read/choose` 存在 | `runtime.describe` 的 `capabilities` 含两者 | 路径 A 失效 |
| PowerShell 可达 | 调 `GetForegroundWindow` | 前后台判定降级 |
| 飞书 webhook 可达 | 可选，需配置 | 该通道降级 |
| daemon 心跳新鲜 | 心跳文件 mtime | daemon 未运行 |
| 卸载完整性 | 扫描 `settings.json` 是否有本项目残留条目 | 有残留 |

### 第 2 步 · 通道单测

| 项 | 命令 | 期望 |
|---|---|---|
| 飞书 | `cc-autopilot-notify test-channel feishu` | 手机/桌面飞书收到卡片 |
| 终端 | `cc-autopilot-notify test-channel terminal` | Pebrel 中看到 OSC 9 |
| Pebrel | `cc-autopilot-notify test-channel pebrel` | 系统通知出现，点击回到当前 pane |

### 第 3 步 · 行为验收

- S4：Pebrel 保持前台，不操作，150s 后自动提交
- S5：切到别的窗口，90s 后自动提交
- S6：面板中按任意键，确认不再自动提交
- S7：构造带 `(Recommended)` 的提问，确认选中该项
- S8：逐项注入故障（断网 / 杀 daemon / 改坏配置），确认会话不卡死

### 第 4 步 · 自动化测试（pytest）

单元测试覆盖三个纯逻辑模块，不依赖外部环境：

| 模块 | 测什么 | 样例断言 |
|---|---|---|
| `judge.py` | 推荐标记提取 | `(Recommended)` 后缀命中；`（推荐）` 命中；多义 → 返回 None；无标记 → 返回 None |
| `judge.py` | 回落策略 | 模型调用抛异常 → 返回 0（第一项）并置 `fallback=True` |
| `config.py` | 配置校验 | 缺 `thresholds` → 用默认 150/90；非法值 → 报错不崩溃 |
| `state.py` | 并发 | 两进程同时写不同 `session_id` → 均成功；同 key → 后者覆盖且不损坏 |

测试运行：`python -m pytest tests/ -q`。

### 第 5 步 · 负向验证

新加的每个闸门**必须对着已知坏输入跑一次、看它拒绝**，才允许放行：

- `conversation.choose` 传非法 `prompt_id` → 必须收到 `prompt_changed` 而非崩溃（**已实测通过**）
- `conversation.choose` 传 `option: 9`（越界，schema 上限 8）→ 必须被拒
- `config.json` 写坏 JSON → 必须回落默认并告警
- 心跳文件删除 → `doctor` 必须报 FAIL

### 第 6 步 · 卸载与回滚验证

```bash
cc-autopilot-notify uninstall
cc-autopilot-notify doctor   # 必须全部 SKIP/无残留
```

校验：`settings.json` 的 hooks 与 `env.CLAUDE_AFK_TIMEOUT_MS` 均已移除，且**既有条目（gstack hook、pebrel-hook、6 个 env 键）原样保留**。差异对比用 `diff <(jq -S . backup.json) <(jq -S . ~/.claude/settings.json)`。

---

## 8. 风险与未决

| 风险 | 影响 | 缓解 |
|---|---|---|
| **`updatedInput.answers` 在 2.1.308 可能失效** | 路径 B 作废 | 第 0 步先证；失效则降级到 A+C |
| CC 升级改变 hook 语义 | 静默失效 | `doctor` 每次升级后跑；任一 FAIL 则告警 |
| Pebrel 升级改变运行时协议 | 路径 A 失效 | `runtime.describe` 校验 `protocol_version` 与 `capabilities`；不符则关闭路径 A |
| 官方 AFK 抢先触发 | 重复作答 | AFK 设为 300s，恒大于本项目阈值 |
| 与既有 gstack / pebrel hook 冲突 | 行为异常 | 并列新增、不改动；实测组合行为 |
| 模型判卷误选 | 任务跑偏 | 优先读推荐标记（零成本且贴合 Claude 本意）；判卷仅作补充 |
| 运行时 token 泄露 | 本机凭据外泄 | 只读不写；任何日志/通知/状态文件均不落 token |

**未决**：

1. 判卷用的模型 id 需要在实现阶段按陛下网关实际可用模型填写（`judge.model` 默认 `null` 即关闭第二级）。

> 原「未决 1：路径 A 判定人在面板操作的确切信号」已解决：`conversation.read` 的 `revision` 变化即代表有交互（见 4.2）。

---

## 9. 交付物

```
cc-autopilot-notify/
├── docs/
│   ├── superpowers/specs/2026-10-27-cc-autopilot-notify-design.md
│   └── spikes/2026-10-27-permission-request-answers.md
├── cc_autopilot_notify/
│   ├── __init__.py
│   ├── cli.py                 # doctor / test-channel / install 入口
│   ├── hook_pre_tool.py
│   ├── hook_permission.py
│   ├── hook_notify.py
│   ├── daemon.py
│   ├── pebrel_runtime.py
│   ├── presence.py
│   ├── judge.py
│   ├── state.py
│   ├── config.py
│   ├── doctor.py
│   └── channels/{feishu,terminal,pebrel}.py
├── tests/
│   ├── test_judge.py
│   ├── test_config.py
│   └── test_state.py
├── install.sh
├── uninstall.sh
├── README.md
└── LICENSE            # MIT
```

**安装影响面**（`install.sh` 须展示 diff 并确认）：

1. **`~/.claude/settings.json` 的 hooks 条目**：以**合并**方式新增（读现有 JSON、追加到对应事件的数组末尾、写回）。**不得覆盖**既有 `PreToolUse`（gstack `question-preference-hook`）、`PermissionRequest`（`pebrel-hook`）与其他 10 个事件条目。
2. **`env.CLAUDE_AFK_TIMEOUT_MS = 300000`**：该 `env` 段当前已有 6 个键（`CLAUDE_CODE_MAX_CONTEXT_TOKENS`、`CONTEXT7_API_KEY`、`ENABLE_CODE_SECURITY_REVIEW`、`ENABLE_COMMIT_REVIEW`、`SECURITY_REVIEW_MODEL`、`SG_AGENTIC_MODEL`），**只新增这一个键**；若已存在且值不同，须展示新旧值并确认后才改。
3. 写入 `~/.config/cc-autopilot-notify/config.json`（已存在则备份为 `.bak-YYYYMMDD-HHMMSS`）。
4. **daemon 托管方式：systemd user service**（实测 `systemctl --user is-system-running` → `running`，可用）。不选「Pebrel 启动时拉起」——Pebrel 是 Windows 侧进程，无法可靠托管 WSL 内的 daemon。
   - unit 文件：`~/.config/systemd/user/cc-autopilot-notify.service`
   - `Restart=on-failure`、`RestartSec=5`
5. 备份原 `settings.json` 到 `settings.json.bak-YYYYMMDD-HHMMSS`。

`uninstall.sh` 须能完整还原以上五项，并按第 6 步用 `diff <(jq -S . backup.json) <(jq -S . ~/.claude/settings.json)` 验证零差异。
