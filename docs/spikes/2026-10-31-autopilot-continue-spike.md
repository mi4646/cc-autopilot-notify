# Spike: autopilot continue（R1 锁屏注入 / R2 建议条形态）— 进行中

- 日期：2026-10-31
- 状态：**R2 未定案**（两轮采样未捕获建议条）；R1 待授权
- 探针：`tools/spike_autopilot_continue.py`（throwaway）
- 原始数据：`.superpowers/sdd/2026-10-31-lockscreen-autopilot-continue/spike-observe{,2}.log`

## 已确证事实（可复算：observe 采样，pane.read lines=60）

1. **`task_state` 词汇**：`running`（回合进行中）、`finished`（回合结束静息态）。共 19 个 finished 样本。spec 的触发门应认 `finished`。
2. **buffer 保真**：pane.read 的文本含完整 TUI chrome——输入框 `❯ ` 行、分隔线、状态栏（模型/分支/计时/上下文）、页脚（`⏵⏵ bypass permissions on (shift+tab to cycle)`）。
3. **输入框文本可读**：用户未提交的输入（如 pane 17 的 `❯ 继续`）与历史提示行都逐字可读。
4. **未捕获建议条**：两轮共 44 个样本。第二轮 pane 8 在 10:27:31–10:30:27 静息 ≥3 分钟（finished、输入框空），无建议条出现。第一轮 pane 17 完成后 30–60 秒同样无。
5. **混淆源教训**：块内第一个 `❯` 行可能是历史回合的用户输入（如 `清掉浏览器吧`），定位当前输入框必须以「最后一条分隔线之间」为准。

## 未定案：建议条形态

剩余假设：
- H1：建议条仅在特定任务类型/条件下由模型生成（建议文本是模型输出；flash 档模型或 LunaRoute 代理可能不产出建议）。
- H2：建议条以 overlay 渲染，不进 pane.read buffer。
- H3：出现时窗 > 3 分钟（与陛下「90 秒内」的观测冲突，暂不信）。

**定案路径（事件驱动）**：陛下下次肉眼看到建议条时知会，立即对该 pane 做单次快照 diff——一次采样即可定案 H1/H2。

## R1（锁屏注入）：待授权

本机 console 当前锁屏（陛下远程）。计划向 pane 8 发一次 `pane.send_key("tab")` 并记录返回。未执行。

## 试按回包的新事实（2026-10-31 10:33，UNLOCKED 状态——R1 未定案）

- `pane.send_key` 空输入框发 tab：`{"action": {..., "bytes_sent": 4}, "snapshot": {...}}`，exit 0，无害。
- 快照新字段：每 pane `task_state` + `state_change_seq`（单调递增）+ `agent{session_id, state_source: hook|osc7501, hook_seen}` + `running_program: "claude"`；顶层 `pane_lifecycles`（pane closed 事件）。
- 设计启示：daemon 可用一次 `agents.list` 快照做全 pane 状态门与 pane↔session 复核（替代逐 pane pane.read 探状态）；`state_change_seq` 可替代 revision 哈希做变更检测。

## R1 状态：未定案

第一次试按发生在解锁后（用户回来应答导致锁屏窗口关闭）。待用户再次锁屏后补测。

## R1：通过（2026-10-31 10:37，SESSION_FLAGS=0 锁屏态实测）

- 前置：WTS 采样 locked=True（PowerShell 实测）。
- `pane.send_key(pane 8, "tab")` → 正常回包（bytes_sent: 4 + snapshot），**无拒绝、无错误码**。
- 结论：Pebrel 的按键注入不经过前台窗口/安全桌面，锁屏下可用。E15 遗留疑虑（2026-10-28 spec）就此关闭。
- 代价口径：被按 pane 当时输入框为空（finished、无建议），Tab 无可见效果。

## R2：定案（2026-10-31 10:45，陛下确认）

- 样本：pane 8 finished 静息态，当前输入框 `❯ 生成一下周报`——陛下确认非手打，是系统建议。
- **结论**：建议条以普通文本渲染在当前输入框内，进 pane.read buffer（H1 部分成立：建议由模型生成、特定条件下出现；H2 否决）。
- **matcher 设计定稿**：提取最后一对 `─{20,}` 分隔线之间的 `❯ ` 行，非空即建议文本；无分隔线/空输入框 → None。锁屏判别在 handle 层（锁屏时用户不可能打字，❯ 非空即建议）；历史提示行与页脚 tab 提示被结构性排除，无需黑名单。
- 残留风险（已记录）：用户锁屏前遗留未提交输入会被当作建议；Tab 对非空输入框是替换还是追加未观测——cap 与逐条卡兜底。
