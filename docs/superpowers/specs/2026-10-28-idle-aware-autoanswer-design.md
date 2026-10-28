# 系统空闲感知的自动应答阈值设计

- 日期：2026-10-28
- 状态：已部署（0.2.4），锁屏、人在场、前台 150 秒分支均已实测
- 目标仓库：`/var/www/cc-autopilot-notify`
- 取代：`2026-10-27-cc-autopilot-notify-design.md` 的 §1.1 第 2 条、§1.2 的 S4/S5、§5 的 `thresholds` 语义

---

## 1. 问题

当前自动应答的判定由两个量组成（`cc_autopilot_notify/daemon.py:170-175`、`:230`）：

| 量 | 现值 | 来源 |
|---|---|---|
| 是否前台 | `presence.is_foreground()`：Pebrel 窗口是否为 Windows 前景窗口 | PowerShell `GetForegroundWindow` |
| 计时起点 | `pending.last_activity`：面板 revision 最后一次变化的时刻 | `state.touch` |

由此得到的实际行为是：**只要陛下切到浏览器、编辑器或任何别的窗口，终端就不再是"前台"**，于是走 `background_seconds`（本机配置 10 秒），10 秒后替陛下选了一个答案。

这与"人是否在电脑前"无关。陛下在写代码、查资料、读输出时，都会被当成"不在"。2026-10-28 的实际配置（`~/.config/cc-autopilot-notify/config.json`）为：

```json
"thresholds": {"foreground_seconds": 10, "background_seconds": 10}
```

代码出厂默认则是 `foreground_seconds: 150`、`background_seconds: 90`（`config.py:30`）。

## 2. 目标与成功标准

**目标**：自动应答只在**陛下离开电脑**（系统空闲达到阈值）时生效；只要还在使用电脑，就不替陛下做选择。面板内一有交互，立即取消。

| # | 标准 | 判定方式 |
|---|---|---|
| S1 | 系统持续有键盘/鼠标输入时，永不自动应答 | 注入 `idle < 阈值` 的 tick，断言无按键 |
| S2 | 前台空闲 ≥ 150 秒、后台空闲 ≥ 90 秒，才允许应答 | 分别注入 `idle=149/150`、`idle=89/90`，断言按键边界 |
| S3 | daemon 自己模拟的按键会重置系统空闲 | 答完一题后 `idle≈0`，下一题需重新等满窗口 |
| S4 | 空闲探测失败时不自动应答，并告警 | `presence.sample()` 的 idle 为 `None`，断言无按键 |
| S5 | 问题出现后至少经过 `min_duration_seconds` 才应答 | 断言 `appeared_at` 不足 30 秒时不答 |
| S6 | 探测失败可被 `doctor` 诊断 | `doctor` 新增检查项，输出前台状态、空闲秒数或失败原因 |

## 3. 设计与数据流

### 3.1 组合探测：`presence.sample()`

一次 PowerShell 调用同时返回**前台窗口归属**和**系统空闲秒数**。分开探测会让每一轮多花约 0.7 秒，没有必要。

```python
@dataclasses.dataclass
class Presence:
    foreground: bool | None    # True = Pebrel 是前景窗口；None = 未知
    idle_seconds: float | None # None = 空闲读取失败

def sample(powershell: str | None = None,
           timeout: float = 1.5) -> Presence | None
```

PowerShell 脚本：

```powershell
Add-Type -Namespace W -Name I -MemberDefinition '
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out uint p);
[DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
[DllImport("kernel32.dll")] public static extern uint GetTickCount();
public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
';
$fg=[W.I]::GetForegroundWindow();
$p=0; [void][W.I]::GetWindowThreadProcessId($fg,[ref]$p);
$name=(Get-Process -Id $p -ErrorAction SilentlyContinue).ProcessName;
$li=New-Object W.I+LASTINPUTINFO;
$li.cbSize=[System.Runtime.InteropServices.Marshal]::SizeOf($li);
$idle="ERR";
if ([W.I]::GetLastInputInfo([ref]$li) -and $li.dwTime -ne 0) {
  $delta=[W.I]::GetTickCount()-$li.dwTime;
  $idle=[string]::Format([Globalization.CultureInfo]::InvariantCulture,"{0:F3}",$delta/1000.0)
}
"$name|$idle"
```

解析规则：

- `foreground`：进程名小写等于 `pebrel` → `True`；为空 → `None`；其他 → `False`。
- `idle_seconds`：`ERR`、负数、超过 **604800 秒（7 天）** → `None`。
- 输出不是两段、进程无法启动、超时 → `sample()` 返回 `None`。
- 只有 idle 失败时返回 `Presence(foreground=<值>, idle_seconds=None)`；只有前台名读不到时 `foreground=None`。

**不使用 `dwTime` 做任何状态判断。** 微软文档明确说 tick count 不保证单调，且 `SendInput` 可自带 tick；依赖它判断「最后一次输入是不是我们自己按的」不可靠。本设计只把差值当「大约空闲了多少秒」用。

**实测代价**：spike 中单次调用 0.53–0.91 秒。`is_foreground()` 保留给 `doctor` 与现有调用方，不删除。

### 3.2 判定链与探测时机

`Deps.foreground` 替换为 `Deps.sample`：

```python
sample: Callable[[], Presence | None]
```

退避与缓存使用 tick 的 `now`（测试可注入），不再单独引入 monotonic 时钟。

阈值选择复用 `daemon.py:170` 的 `_threshold()`：

```python
def _threshold(cfg, presence):
    if presence.foreground is True:
        return config.foreground_seconds(cfg)   # 150
    return config.background_seconds(cfg)       # 90；False 与 None 都算后台
```

`foreground is None` 走后台 90 秒：这仍然安全，因为还必须同时满足系统空闲 ≥ 90 秒。只要陛下在动键鼠，空闲就是 0。

**探测时机：惰性、成功结果缓存 5 秒。**

一次 PowerShell 调用实测约 0.7 秒，而 `POLL_SECONDS = 1.0`（`daemon.py:26`）。因此：

1. `tick()` 先 `state.load_all(root)`；列表为空 → **不探测**，直接返回。
2. 对每个 pending 先做面板读取与 `attention` 检查（`daemon.py:212-222`）。只有第一个通过 `attention` 的 pending 才触发探测。
3. 成功结果缓存 **5 秒**（`IDLE_SAMPLE_INTERVAL_SECONDS = 5`），同一窗口内的后续 tick 与后续 pending 复用。阈值是 90–150 秒，5 秒粒度不影响判定。
4. 失败不缓存，但进入下面的退避。
5. **本轮一旦执行过 `_answer_sequence`，tick 结束时清空缓存**（`gate.invalidate()`）。否则按键前的旧高空闲值会在下一个 tick 再次触发，造成重复应答。

这保证：没有待答题时零 PowerShell 开销；问题挂起时平均每 5 秒一次，而不是每秒一次。

**失败退避与告警（陛下 2026-10-28 拍板：停摆 + 告警）。**

- 探测失败 → 本 tick 不应答。
- 连续失败后**退避 5 秒**再试，避免故障期间每秒烧 0.7 秒。
- 连续失败累计达到 **60 秒** → 通过 `deps.notify` 发一条告警，本段失败期间只发一次。
- 第一次成功 → 清空失败状态，并再发一条恢复通知。

**文案（2026-10-28 设计评审定稿）：**

| 事件 | 标题 | 正文 |
|---|---|---|
| 故障 | `cc-autopilot-notify` | `空闲探测失败，自动应答已停摆。运行 cc-autopilot-notify doctor 检查 PowerShell。` |
| 恢复 | `cc-autopilot-notify` | `空闲探测已恢复，自动应答重新生效。` |

**送达**：`deps.notify` 直接调用 `channels.send_all`（`daemon.py:296`），不经过 `hook_notify` 的 `quiet_hours` 过滤，所以两条都会在静默时段送达。这是有意的：故障意味着功能已经不可用，用户需要知道。

单个 pending 的判定顺序：

```
1. 面板 state == "attention"                     （保留，daemon.py:222）
2. sample 为 None 或 idle_seconds 为 None        → 不动（进入退避/告警状态机）
3. idle < _threshold(cfg, sample)                → 不动（前台 150 / 后台 90）
4. now - pending.appeared_at ≥ min_duration_seconds
5. 执行 _answer_sequence，应答后 touch
```

**没有 revision 门控。** 面板底部的状态行/计时可以每秒自行重绘，让 60 行文本的哈希每秒变化；2026-10-28 的实机测试正是因此每次都判成「user is interacting」，永远走不到空闲判定。真实交互由系统空闲反映：按任何键或动鼠标，`idle` 立刻归零。因此 revision 只作为应答后的记录，不再作为取消条件。

`background_seconds` 重新启用：`foreground is True` 用前台 150 秒，`False` 与 `None` 用后台 90 秒。`_threshold()` 保留并复用。

### 3.3 为什么不需要 `away` 状态机

原计划担心：自动应答会模拟按键（`pebrel_runtime.press`），键盘输入让系统空闲归零，于是答完一题后下一题要重新等。

**这个"问题"本身就是期望的行为。**

- 我们自己的按键会重置系统空闲，这是 Windows 的客观事实，不需要维护任何内存状态。
- 唯一判据就是 `idle >= _threshold(cfg, sample)`。答完一题后 `idle ≈ 0`，下一题自然要再等满一档（前台 150 秒 / 后台 90 秒）。
- 同一 tick 内可见的多个问题仍由 `_answer_sequence` 一起答完，不会被打断。
- 代价：多 session 同时挂起时，第二个 session 的问题要再等一档。

**删除状态机带来的好处：**

- 不再依赖微软文档明确说「不保证单调」的 tick count 判断输入身份。
- 不再有「故障期间用户活动、恢复后标志残留导致误答」的路径。
- 没有跨 tick 的全局内存状态，测试不需要 reset fixture，也不会有状态泄漏。
- `doctor` 不需要读取 daemon 内存状态（本来也读不到）。

**安全方向正确**：误判为「还在」只会延迟应答；误判为「离开」会替用户做选择。删除状态机只在延迟方向付出代价。

### 3.4 配置语义

| 键 | 现值 | 新语义 |
|---|---|---|
| `thresholds.foreground_seconds` | 用户配置 180 → **150** | Pebrel 是前景窗口时的系统空闲阈值 |
| `thresholds.background_seconds` | 10 → **90** | Pebrel 不是前台或前台未知时的空闲阈值 |
| `min_duration_seconds` | 30 | 首次被真正使用：问题至少存在这么久才允许应答 |

键名与语义现在一致，不需要重命名。

**配置只在 daemon 启动时加载一次**（`cli.py:51` `daemon.run(hook_common.state_root(), config.load())`）。改完配置文件必须重启 daemon，否则运行中的进程仍用旧值。`ensure-daemon` 看到心跳新鲜会直接 skip，不会重载。

## 4. 边缘情况与公开假设

| # | 情况 | 行为 | 假设 / 取舍 |
|---|---|---|---|
| E1 | 用户在电脑前纯阅读，不碰键鼠 | 前台 150 秒 / 后台 90 秒后判定为离开，会应答 | "无操作"的字面语义；无法区分"在读"与"离开" |
| E2 | 锁屏 | 锁屏**不是判定条件**；它只是「无输入」的一种情况。锁屏时 Pebrel 通常不再前台，走后台 90 秒 | 判定只看系统空闲 + 前台窗口；锁屏不参与判断。能否按下见 E15 |
| E3 | `idle` 探测失败（PowerShell 缺失/被杀/会话不对） | 不应答；连续失败 5 秒退避重试；累计 60 秒发一条告警 | 未知前台 → 后台 90 秒，但仍必须同时满足系统空闲 ≥ 90 秒，所以不会抢答 |
| E4 | 多个 pending | 同一 tick 共用一次采样（5 秒缓存） | 系统空闲与前台状态都是全局的，逐 pending 探测既慢又不一致 |
| E5 | 多 session 同时停滞 | 同一 tick 内全部应答；跨 tick 需要新的空闲窗口（150/90） | 我们自己的按键会重置空闲，删除状态机后这是自然结果 |
| E6 | daemon 重启 | 无内存状态，首次采样重新判定 | 见 3.3 |
| E7 | 系统时间被 NTP 调整 | 不影响判定 | 空闲秒数来自 Windows；退避计时用 `Deps.monotonic` |
| E8 | `GetTickCount` 不保证单调或 49.7 天回绕 | 负值/异常大值 → `None`，按失败处理 | 见 3.1；不再用 tick 做身份判断 |
| E9 | PowerShell 每次调用约 0.7 秒（实测） | 惰性探测 + 成功结果缓存 5 秒 + 失败退避；超时 1.5 秒 | 无待答题时零开销；问题挂起时平均每 5 秒一次 |
| E10 | 问题在离开状态下弹出 | `appeared_at` 起算，30 秒后应答 | 给"刚好回来"留 30 秒缓冲 |
| E11 | 用户在我们应答序列中途回来 | 当前 `_answer_sequence` 不会中止（一个 tick 内完成）；下一 tick `idle` 归零，不再应答后续 | 合成按键与用户输入无法区分，这是无法消除的竞态；只影响一个 tick |
| E12 | daemon 不在交互式 Windows 会话中（systemd 拉起、会话切换） | `GetLastInputInfo` 可能失败 → 按 E3 停摆并告警 | 2026-10-28 spike 已验证交互式/setsid/最小环境均为 session 1；真实 SessionStart 待补测，见 §9 |
| E13 | 改了仓库代码但 daemon 仍从插件缓存运行 | 行为不变 | 心跳 `plugin_root` 证明运行副本是 `~/.plugins/cache/.../0.2.1`；交付必须重新安装并重启，见 §8 |
| E14 | `doctor` 看不到 daemon 内存状态 | 只做一次本地采样，输出标注 `inferred`，不声称是 daemon 的实际判定 | `doctor` 是独立进程；要显示 daemon 状态必须持久化，成本大于收益 |
| E15 | 锁屏时自动应答是否真能按下 | **未验证** | 按键走 Pebrel 的 `pane.send_key` IPC，不是本进程的 `SendInput`。锁屏时 Pebrel 是否仍能注入，必须实测（§5 验收 5） |

## 5. 测试计划

| 测试 | 断言 |
|---|---|
| `test_presence.test_parses_presence` | `"pebrel\|123.5"` → `Presence(True, 123.5)` |
| `test_presence.test_foreground_other_process` | `"chrome\|123.5"` → `Presence(False, 123.5)` |
| `test_presence.test_foreground_empty_is_none` | `"\|123.5"` → `foreground is None` |
| `test_presence.test_idle_err_is_none` | `"pebrel\|ERR"` → `idle_seconds is None`，foreground 仍为 True |
| `test_presence.test_idle_non_numeric_is_none` | `"oops"` / `""` / `"-3"` / `"nan"` / `"inf"` → `idle_seconds is None` |
| `test_presence.test_idle_out_of_range_is_none` | 秒数 > 604800 → `idle_seconds is None` |
| `test_presence.test_sample_timeout_is_none` | 超时 → `sample()` 返回 `None` |
| `test_presence.test_uses_invariant_culture` | 断言脚本包含 `InvariantCulture`，防止区域设置回归 |
| `test_daemon.test_panel_redraw_alone_does_not_block` | 面板文本每秒自绘但系统空闲足够 → 仍然应答 |
| `test_daemon.test_foreground_threshold_boundary` | `foreground=True`：`idle=149` 不答，`150` 答 |
| `test_daemon.test_background_threshold_boundary` | `foreground=False`：`idle=89` 不答，`90` 答 |
| `test_daemon.test_unknown_foreground_uses_background` | `foreground=None`、`idle=90` → 答 |
| `test_daemon.test_unknown_idle_never_fires` | `idle_seconds=None` → 无按键 |
| `test_daemon.test_min_duration_blocks_fresh_question` | `idle=200` 但 `appeared_at` 仅 10 秒 → 无按键 |
| `test_daemon.test_no_pending_no_sample` | 无 pending → `sample` 调用 0 次 |
| `test_daemon.test_sample_probed_once_per_window` | 三个 attention pending → 1 次；5 秒内第二个 tick 不再调用 |
| `test_daemon.test_sample_failure_backs_off_and_warns_once` | 连续失败：5 秒内不重试；60 秒发一条 notify；恢复不重复告警 |
| `test_daemon.test_outage_and_recovery_messages` | 故障与恢复各发一条，文案与 §3.2 一致；静默时段仍发 |
| `test_daemon.test_answer_resets_idle_window` | 答完一题后下一次采样 `idle=1`，第二个 pending 不答 |
| `test_daemon.test_shared_gate_resamples_after_an_answer` | 共享 gate 时，应答后缓存必须失效并重新采样，不能重复应答 |
| `test_daemon.test_batch_answers_all_pending_in_one_tick` | 同一 tick 内三个 pending 均满足条件 → 全部应答 |
| `test_doctor.test_presence_check` | PASS 详情含 `foreground=... idle=...s threshold=...`；FAIL 显示原因；探测函数可注入 |

测试注入：`Deps.sample` 返回构造好的 `Presence | None`；`Deps.monotonic` 注入可控时间；`Deps.notify` 记录告警；`doctor` 的探测函数可注入。测试不得调用真实 PowerShell。

实机验收（`docs/acceptance.md` 追加）：

1. 坐在电脑前持续打字 5 分钟，确认无自动应答。
2. 离开座位超过 2.5 分钟（前台 150 秒；锁屏通常走后台 90 秒）后回来，确认问题已被应答，飞书/终端有记录。
3. 停掉 PowerShell 路径（临时改 `DEFAULT_POWERSHELL`），确认 `doctor` 报 FAIL、daemon 不抢答、约 60 秒后收到一条告警。
4. 用 `systemd` 之外的方式（从交互式 WSL shell）分别启动 daemon，确认 `GetLastInputInfo` 两种场景的实际结果；失败场景按 E12 记录。
5. **锁屏下的自动应答**：制造一个问答面板，锁屏后离开 2 分钟以上（后台 90 秒），确认面板被自动答完；解锁后检查飞书/终端记录。若失败，记录 `pane.send_key` 在锁屏下的实际返回，并按 E15 重开设计（可能需要解锁后补答或其他注入方式）。

## 6. 明确不做

- **保留 `background_seconds` 并重新启用**：作为 Pebrel 不是前台或前台未知时的 90 秒阈值。
- **保留并复用 `_threshold()`**：前台 150 / 后台 90 的选择仍由它完成。
- **不做屏幕锁定检测**：锁屏只是无输入的一种情况，不参与判定。
- **不区分多个终端/显示器**：系统空闲是全局量，做细分需要窗口级输入钩子，成本不成比例。
- **不改通知通道**（飞书、终端、Pebrel），只复用 `deps.notify` 发失败告警。
- **不改 `is_foreground()`**：`doctor` 仍需要它。

## 7. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| PowerShell 不可用 | 自动应答永久停摆 | 5 秒退避 + 60 秒告警 + `doctor` FAIL；不静默降级为抢答 |
| 前台 150 秒 / 后台 90 秒纯阅读被误判 | 替陛下选了答案 | 可调大对应阈值；或启用更长的 `min_duration_seconds` |
| `GetLastInputInfo` 在非交互会话返回 0 或错误会话 | 整个方案不成立 | 已通过 §9 spike（session 1、63 秒连续增长）；真实 SessionStart 需实现后补测 |
| 每个采样窗口 0.7 秒 PowerShell 开销 | 问题挂起期间循环变慢 | 惰性探测 + 成功缓存 5 秒 + 失败退避 + 超时 1.5 秒 |
| 微软文档：`GetTickCount` 不保证单调 | 秒数偶发负值/异常大值 | 不做输入身份判断；越界一律 `None` |
| 锁屏时 Pebrel 拒绝 `pane.send_key` | 用户在锁屏状态下离开时，判定通过但按不下去 | 验收 5 实测；失败则记录返回并重开注入方案 |

## 8. 交付步骤

仓库里的代码不等于正在运行的 daemon。心跳文件写明的 `plugin_root` 是
`/home/anonymous/.claude/plugins/cache/cc-autopilot-notify/cc-autopilot-notify/0.2.1`。

1. 在仓库跑 `python -m pytest tests/ -q` 全绿。
2. 同步提升三处版本，`0.2.1` → `0.2.2`：`pyproject.toml`、`.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json`。少改一处会让 `tests/test_plugin_package.py` 失败。
3. 版本改完**重新跑全量测试**，不要复用第 1 步的结果。
4. **先确认安装源**：marketplace 指向 GitHub 仓库时，本地未提交的代码不会被安装。必须先 commit + push（或明确改用本地路径安装）。这一步不做，第 5 步装到的仍是旧代码。
5. 安装新版本，确认插件缓存出现 `0.2.2` 目录。
6. 改用户配置 `~/.config/cc-autopilot-notify/config.json`：`thresholds.foreground_seconds` → `150`、`thresholds.background_seconds` → `90`，原子写入并保持 0600（本 spec 的修改不自动改配置，必须有这一步）。
7. 停止 daemon，**等待进程真正退出**（`stop-daemon` 只发 SIGTERM，不等待）；不要紧接着 `ensure-daemon`，否则可能读到旧心跳。
8. 用新 `CLAUDE_PLUGIN_ROOT` 启动 `ensure-daemon`，确认新心跳的 `pid`、`plugin_root`、`version` 都是新的。
9. 新开一个 Claude Code 会话，让 hooks 解析到新插件版本（旧会话可能仍持有旧路径）。
10. 验证：`doctor` 显示 `powershell presence` PASS；运行时读值为前台 150、后台 90。
11. **回滚**：停 daemon → 重装 `0.2.1` → 恢复配置备份 → 重新启动。回滚路径必须在动手前就确认可用。

## 9. 实现前 spike（已完成）

**执行结果（2026-10-28）：通过。** 交互式、`setsid` 后台、`env -i` 最小环境均读到 Windows session 1；锁屏单进程采样从 `0.000` 连续增长到 `63.109s`；跨进程采样也读到 `6.422s`，证明进程启动不重置。第 2 项（真实 SessionStart hook）只做了同源近似，需在实现后补测。完整命令、原始输出与限制见 `docs/spikes/2026-10-28-getlastinputinfo-wsl.md`。

以下为原始检查项：

1. **交互式 WSL shell**：调用 `presence.sample()` 脚本三次，间隔 2 秒，确认返回值随用户输入变化。
2. **SessionStart hook 环境**：在 Claude Code 的 hook 里调用同一脚本，确认它仍能读到当前 Windows 会话的输入。
3. **systemd / 后台环境**：从 `systemd` 或 `setsid` 启动，确认返回值是否仍是当前用户的空闲秒数。
4. **锁屏**：锁屏 1 分钟后解锁，确认 `idle` 增长到约 60 秒。
5. **故障形态**：临时改错 `DEFAULT_POWERSHELL`，确认脚本返回 `None` 而不是一个巨大的秒数。

**判定**：第 2、3 项任一失败，本设计的核心前提不成立。此时不要带着已知失败去实现，改用「显式 AFK 开关」或保留现有前台窗口方案，并重开设计。

## Engineering Review (2026-10-28)

### Scope Challenge

- **已有解法**：`min_duration_seconds` 已在 `config.py:43` 定义但从未被读取——本次直接启用，不新建配置项。`state.Pending.appeared_at`（`state.py:34`）已提供问题出现时刻。`deps.notify`（`daemon.py:296`）已存在，告警复用。`notify.dedupe_seconds` 已有去重机制。
- **最小改动**：`presence.py` 新增 `sample()`（一次调用同时取前台与空闲）；`daemon.py` 替换 `Deps.foreground` 为 `Deps.sample` 并复用 `_threshold()`；`doctor.py` 增加一项检查；测试同步。2 个源文件、0 个新类，低于 8 文件 / 2 类阈值，跳过 complexity selectors。
- **检索**：`GetLastInputInfo` / `GetTickCount` 是 Windows 标准 API（Layer 1），本机实测 `rc=0`。
- **结论**：`scope accepted as-is`（未削减范围）。

`REALITY: premise-table pass 12 条断言中 3 条实测（0.7s、脚本 rc=0、缓存 plugin_root），9 条引代码行；unverified=3（均为 proposed 行为，implementation 后验证）`
`REALITY: already-done pass git log 无空闲检测实现 cc_autopilot_notify/presence.py:1`
`REALITY: surface-check pass powershell.exe 路径存在、GetLastInputInfo 实测可用、pytest 命令见 README.md:124`
`REALITY: binding-decisions pass gstack-decision-search 无 branch 决策`
`REALITY: numbers finding 原计划「2^32 秒上限」量纲错误，已改为 7 天；0.7s 来自 3 次实测 0.77/0.71/0.70`
`REALITY: deferred-asks pass 无 linked issue`
`REALITY: pr-count pass 默认单 PR（无 .gstack/ship-policy.json）`

### Architecture

1. **[CRITICAL] (confidence: 9/10) `daemon.py:26` `POLL_SECONDS = 1.0` + 实测 0.7s/PowerShell** — 每 tick 无条件探测会吃掉 70% 循环预算，且无待答题时也在烧。已改为惰性探测：无 pending 零调用，成功结果缓存 5 秒；前台与空闲合并为一次调用。
2. **[HIGH → 已否决] (confidence: 9/10) tick 身份状态机** — 初版试图用 `dwTime` 判断「最后一次输入是不是我们自己按的」。微软文档说明 tick 不保证单调，且 `SendInput` 可自带 tick；Codex 还指出故障期间标志残留会误答。**已整体删除状态机**，改用「合成按键自然重置空闲」的隐式等待。
3. **[HIGH → 已消除] (confidence: 9/10) 异常路径漏记自按键** — 状态机删除后该路径不存在。
4. **[HIGH] (confidence: 9/10) 部署边界** — 心跳 `plugin_root` 证明运行副本是插件缓存 `0.2.1`；改仓库不影响 daemon。已在 §8 增加三处版本号、重新安装、停等、重启、回滚步骤。
5. **[HIGH → 已通过 spike] (confidence: 9/10) WSL 会话假设** — `GetLastInputInfo` 只反映调用进程所在 Windows 会话。2026-10-28 实测：交互式、`setsid`、`env -i` 均为 session 1；锁屏期间连续增长到 63.1 秒。真实 SessionStart hook 仍待实现后补测。

### Code Quality

6. **[MEDIUM] (confidence: 9/10) `dwTime=0` 失败检测** — 原计划靠「超过 2^32 秒」拦截，量纲错误；实测失败时差值约 4916 秒。已改为脚本显式输出 `ERR`，并设 7 天上限。
7. **[MEDIUM] (confidence: 7/10) PowerShell 数字本地化** — 未指定 culture 时小数分隔符随区域变化。已改为 `InvariantCulture`。
8. **[MEDIUM] (confidence: 8/10) `doctor.py:76` 只有 foreground 检查** — 新增 `powershell presence` 检查，同时显示前台与空闲，失败路径可被诊断。
9. **[MEDIUM] (confidence: 8/10) 配置重启** — `cli.py:51` 只在启动时加载配置，`ensure-daemon` 心跳新鲜会 skip。已在 §3.4 与 §8 写明必须重启。
10. **[LOW] (confidence: 9/10) `_threshold()`（`daemon.py:170`）** — 双阈值模型下继续复用：前台 150 / 后台（含未知）90。

### Test Review

11. **[MEDIUM → 已消除] (confidence: 9/10) 状态机不可注入时钟** — 状态机删除后不再需要；`Deps.monotonic` 只用于退避/告警计时，可注入。
12. **[MEDIUM] (confidence: 8/10) 缺用例** — 无 pending 零探测、5 秒缓存窗口、失败退避/告警、应答后重置窗口、批量应答。已加入 §5。

```
CODE PATHS                                          USER FLOWS
[+] presence.sample()
  ├── [GAP] "pebrel|秒" → Presence(True, s)          [+] 人在电脑前工作 → 不应答 [GAP]
  ├── [GAP] 其他进程 / 空名 → False / None           [+] 离开 ≥150s(前台) / ≥90s(后台) → 应答 [GAP]
  └── [GAP] ERR / 越界 / 超时 → None                 [+] 应答后下一题 → 再等一档 [GAP]
[+] daemon._tick_one()                               [+] PowerShell 故障 → 停摆 + 60s 告警 [GAP]
  ├── [★★ TESTED] attention 门控 — test_daemon.py
  ├── [GAP] 惰性探测 / 缓存 5 秒 / 无 pending 零次
  ├── [GAP] 面板自绘不阻止应答（无 revision 门控）
  ├── [GAP] 前台 150 / 后台 90 的阈值选择
  ├── [GAP] 失败退避 + 告警 + 恢复
  └── [GAP] min_duration 门控
[+] doctor._powershell_presence() [GAP]

COVERAGE: 1/11 paths tested (9%)  |  Code paths: 1/7  |  User flows: 0/4
QUALITY: ★★:1  |  GAPS: 10
```

新增测试全部列入 §5；测试不得调用真实 PowerShell，注入 `Presence | None` 即可。

### Performance

13. **[CRITICAL → 已修] (confidence: 9/10) 0.53–0.91s/次** — 见 Architecture #1。修复后：无 pending 0 次；问题挂起时每 5 秒最多 1 次；前台与空闲合并为一次调用；失败 5 秒退避。
14. **[MEDIUM] (confidence: 8/10) 故障期每秒烧 0.7s** — 5 秒退避解决。

### Outside Voice（Codex，部分完成）

Codex 在 9 分钟超时前没有产出最终答复，但推理流里给出了可用的批评。以下按原意记录并逐条处置：

| # | Codex 指出 | 处置 |
|---|---|---|
| C1 | 微软文档说明 tick 不保证单调，`SendInput` 可自带 tick，tick 身份判断不可靠 | 采纳：删除整个状态机（§3.3） |
| C2 | 故障期间用户活动、恢复后 `_own_press_pending` 残留 → 误答 | 采纳：状态机删除后路径不存在 |
| C3 | `GetLastInputInfo` 是会话级 API，systemd/非交互会话不可靠，应在实现前 spike | 采纳：升级为 §9 阻塞性 spike |
| C4 | `doctor` 看不到 daemon 内存状态，`state=away` 无法实现 | 采纳：改为本地采样并标注 `inferred`（E14） |
| C5 | 成功探测也应降频，不必每秒一次 | 采纳：成功缓存 5 秒 |
| C6 | 版本 bump 漏 `.claude-plugin/marketplace.json`；bump 后未重跑测试 | 采纳：§8 步骤 2–3 |
| C7 | 未提交的本地代码无法通过 GitHub marketplace 安装 | 采纳：§8 步骤 4 明确安装源 |
| C8 | `stop-daemon` 不等待进程退出，`ensure-daemon` 可能读旧心跳 | 采纳：§8 步骤 7–8 |
| C9 | 交付步骤没有真正修改配置 | 采纳：§8 步骤 6 明确改为前台 150 / 后台 90 |
| C10 | `doctor` 单测若调用真实 PowerShell 会很糟 | 采纳：测试要求探测函数可注入（§5） |
| C11 | 应答序列运行期间用户回来无法中止 | 记录为已知竞态 E11；不为此增加每步探测 |

Codex 未完成，故不计为完整 outside coverage。

### Failure modes

| 路径 | 失败 | 处理 | 用户可见 |
|---|---|---|---|
| `sample()` | PowerShell 超时/缺失/非交互会话 | idle 为 `None` → 停摆 + 退避 + 60s 告警 | 是（告警 + doctor FAIL） |
| `_answer_sequence()` | 用户在中途回来 | 当前序列无法中止；下一 tick 空闲归零后不再应答 | 否（已知竞态 E11） |
| 配置 | 改完未重启 | 行为不变 | 否（§8 写明） |

**Critical gaps: 0。**

### NOT in scope

- 不做屏幕锁定检测（`GetLastInputInfo` 已覆盖）。
- 不区分多终端/显示器。
- 不改通知通道实现，只复用 `deps.notify`。
- 不删除 `background_seconds`；它是后台 90 秒阈值。
- 不合并前台/后台两档为单档。
- 不引入新的配置项（复用 `foreground_seconds` 与 `min_duration_seconds`）。

### What already exists

- `min_duration_seconds`（`config.py:43`）：已定义、已校验、从未读取 → 本次启用。
- `Pending.appeared_at`（`state.py:34`）：问题出现时刻，直接用作最短存在时间。
- `deps.notify`（`daemon.py:296`）：告警复用，含通道降级。
- `notify.dedupe_seconds`（`config.py:41`）：告警去重可复用。
- `presence.is_foreground()` 与 `doctor._powershell()`：保留给 `doctor`。

### TODOS.md

无 `TODOS.md`，未新增 TODO。

### Implementation Tasks

- [x] **T0 (P1, human: ~1h / CC: ~15min)** — spike — 已完成：交互式 / setsid / 最小env / 锁屏 / 故障形态均通过；真实 SessionStart 实现后补测
  - Surfaced by: Architecture #5、Outside Voice C3
  - Result: `docs/spikes/2026-10-28-getlastinputinfo-wsl.md`（锁屏连续增长 0→63.109s）
- [x] **T1 (P1)** — presence — 已完成：`Presence` + `sample()`；`tests/test_presence.py` 17 passed
- [x] **T2 (P1)** — daemon — 已完成:双阈值 + 5s 缓存 + 失败退避/告警 + 缓存失效；`tests/test_daemon.py` 31 passed
- [x] **T3 (P1)** — doctor — 已完成:`powershell presence` 检查；`tests/test_doctor.py` 19 passed
- [x] **T4 (P2)** — config/docs — 已完成:README 写明系统空闲条件；`_threshold` 保留并由两档阈值使用
- [x] **T5 (P1)** — delivery — 已完成:0.2.4 已推送（`6e25a85`）、插件已更新、daemon 已重启（pid 599717，心跳 0.2.4）。**锁屏实测通过**（约 90 秒）；**人在场实测通过**（300 秒无应答）；**前台分支通过**（日志 `foreground=True threshold=150s`）
  - Verify: `python -m pytest tests/ -q`；`tests/test_plugin_package.py`；新心跳版本 0.2.2；`doctor` 显示 `powershell presence` PASS

---

## Design Review (2026-10-28)

本计划没有 UI 界面，以下按 UX/可解释性维度评分，不生成视觉 mockup。

| 维度 | 初始 | 修复后 | 主要 gap 与修复 |
|---|---|---|---|
| 「我什么时候会被替选」的可预测性 | 6/10 | 9/10 | 规则本身清楚（前台空闲 150 秒 / 后台 90 秒 + 问题存在 30 秒），但用户无法在运行时确认当前判定。已在 doctor 暴露空闲秒数与阈值。 |
| 状态可见性 | 2/10 | 7/10 | 原计划只有后台日志。现在 `doctor` 输出 `powershell presence` 检查，详情包含 `foreground=… idle=…s threshold=…`；明确标注是本地采样，不是 daemon 内存状态（Codex C4）。 |
| 故障可解释性 | 5/10 | 9/10 | 明确故障文案、恢复文案与「运行 doctor」的下一步。 |
| 文案与语气 | 3/10 | 9/10 | 原计划只有一句草稿。定稿见表 §3.2：直接说明后果与下一步，不写「出了点问题」这类模糊措辞。 |
| 配置可发现性 | 5/10 | 8/10 | `doctor` 详情显示当前生效阈值；README 增加「自动应答何时生效」一节，并写明改配置后必须重启 daemon。 |
| 首次使用 | 5/10 | 8/10 | 安装后 `doctor` 即可回答「现在会不会自动答」。README 用一句话说明默认值与如何调整。 |
| 恢复路径 | 4/10 | 9/10 | 故障恢复后发一条恢复通知；`doctor` 可随时复核。 |

**平均分：初始 4.3/10 → 8.4/10。**

设计决策：

- **故障与恢复各一条通知**，都绕过 `quiet_hours`（陛下 2026-10-28 拍板）。理由：故障意味着自动应答已不可用，静默时段吞掉它会让用户第二天才发现。
- **不加托盘图标、不做桌面状态面板**：这是无人值守的后台工具，状态入口收敛到 `doctor` 一个命令。
- **文案不含技术细节**（不提 `GetLastInputInfo`），只说后果与下一步；技术原因留在 `daemon.log` 与 `doctor` 详情里。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Outside Review | `/codex` | Independent 2nd opinion | 1 | UNAVAILABLE | 11 partial findings, no final answer |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES OPEN | 14 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | ISSUES OPEN | score: 4.3/10 → 8.4/10, 3 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex, plan-review, **unavailable** — CLI ran 9 minutes and was terminated (exit 124) with no final answer; 11 partial findings were retained and incorporated in `### Outside Voice`. This is not a completed review.
- **VERDICT:** ENG + DESIGN ISSUES OPEN — accepted fixes are in the plan; outside coverage unavailable. eng review required until re-run.

NO UNRESOLVED DECISIONS
