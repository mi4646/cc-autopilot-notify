# 问答面板静默跳过（Tier A）端到端测试 Runbook

> 给新会话的执行说明。测试后必须恢复全局阈值。
> 背景：hooks 在 Claude Code **会话启动时**装载；插件安装前启动的会话没有插件 hooks，
> 无法触发 pending 记录。所以测试必须在新会话里做。

## 0. 前置检查（先做，任一失败就停）

```bash
cd /var/www/cc-autopilot-notify
claude plugin list | grep -A3 cc-autopilot-notify     # 必须 enabled
PYTHONPATH=. python3 -m cc_autopilot_notify.cli doctor
```

要求：`daemon heartbeat` PASS、`legacy install` PASS、`pebrel runtime` 与
`pebrel conversation capabilities` PASS。

同时确认本会话确实加载了插件 hooks：本会话的 Stop 摘要里应能看到
`run_hook.py notify`（`~/.claude/projects/<项目>/<session>.jsonl` 里
`stop_hook_summary.hookInfos`）。没有就说明测试仍会在旧会话里做，停。

**其他会话**：测试会把全局阈值降到 10 秒，daemon 会看到所有会话的问题。
先确认没有别的会话正开着提问面板。

## 1. 备份并降阈值

```bash
cp ~/.config/cc-autopilot-notify/config.json \
   ~/.config/cc-autopilot-notify/config.json.bak-$(date +%Y%m%d-%H%M%S)
python3 - <<'PY'
import json, os, pathlib, time
p = pathlib.Path.home()/'.config/cc-autopilot-notify/config.json'
d = json.loads(p.read_text())
d.setdefault('thresholds', {})['foreground_seconds'] = 10
d['thresholds']['background_seconds'] = 10
tmp = p.with_name(p.name + '.tmp')
tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
os.chmod(tmp, 0o600); os.replace(tmp, p)
print('thresholds:', d['thresholds'])
PY
```

## 2. 重启 daemon（阈值只在 daemon 启动时读取）

```bash
cd /var/www/cc-autopilot-notify
PYTHONPATH=. python3 -m cc_autopilot_notify.cli stop-daemon
sleep 2
# 心跳 15 秒内会让 ensure-daemon 判定「仍活着」而 skip；停稳后删心跳再拉起
rm -f ~/.local/state/cc-autopilot-notify/heartbeat.json
CLAUDE_PLUGIN_ROOT=/var/www/cc-autopilot-notify bash scripts/ensure-daemon.sh
python3 -c "import json,pathlib,time; h=json.loads((pathlib.Path.home()/'.local/state/cc-autopilot-notify/heartbeat.json').read_text()); print(h, 'age', round(time.time()-h['at'],1))"
```

预期：输出 `daemon: started pid <N>`，心跳 age < 2s，`pid` 是新的。

## 3. 提问并放手

用 `AskUserQuestion` 发：

- question: `端到端测试：请勿操作，等待 daemon 自动作答。`
- options: `Alpha (Recommended)` / `Beta`

**不要回答**。约 10 秒后 daemon 应注入按键并提交。

## 4. 判定证据

```bash
tail -20 ~/.local/state/cc-autopilot-notify/daemon.log
ls ~/.local/state/cc-autopilot-notify/pending_*.json 2>/dev/null || echo none
```

- PASS：日志出现 `auto-answered option 0`，且 AskUserQuestion 的返回值是
  `Alpha (Recommended)`（daemon 选的是推荐项）。
- FAIL 排查关键词：
  - `user is interacting; timer reset`：面板被当作有人在操作（可能是鼠标/键盘事件），换一次不碰输入设备重试。
  - `screen mismatch`：面板文字没匹配上（重绘/布局变化），看日志里的期望与实况。
  - `cannot locate a pane`：`PEBREL_PANE_ID` 缺失或不是 Pebrel pane。
  - 日志完全没有本会话记录：本会话仍未加载插件 hooks，停。

## 5. 必做：恢复

```bash
python3 - <<'PY'
import json, os, pathlib
p = pathlib.Path.home()/'.config/cc-autopilot-notify/config.json'
d = json.loads(p.read_text())
d['thresholds']['foreground_seconds'] = 150
d['thresholds']['background_seconds'] = 90
tmp = p.with_name(p.name + '.tmp')
tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
os.chmod(tmp, 0o600); os.replace(tmp, p)
print('thresholds restored:', d['thresholds'])
PY
cd /var/www/cc-autopilot-notify
PYTHONPATH=. python3 -m cc_autopilot_notify.cli stop-daemon
sleep 2
rm -f ~/.local/state/cc-autopilot-notify/heartbeat.json
CLAUDE_PLUGIN_ROOT=/var/www/cc-autopilot-notify bash scripts/ensure-daemon.sh
```

## 6. 先跑不等待的单元验证（可选，但建议先跑）

```bash
python -m pytest tests/test_daemon.py tests/test_screen.py -q
```
