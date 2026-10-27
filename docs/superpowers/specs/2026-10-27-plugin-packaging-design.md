# cc-autopilot-notify 插件化设计规格

- 日期：2026-10-27
- 状态：待评审
- 作者：Claude（调研 + 设计）
- 目标仓库：`/var/www/cc-autopilot-notify`
- 前置规格：`docs/superpowers/specs/2026-10-27-cc-autopilot-notify-design.md`（功能设计，本规格不改其 Tier A/B/C 判定逻辑）

---

## 1. 背景与目标

### 1.1 现状

当前安装方式为直装：

- `install.sh` 把 5 个 hook 条目合并进 `~/.claude/settings.json`，命令写死绝对路径 `PYTHONPATH=/var/www/cc-autopilot-notify python3 .../run_hook.py <module>`；
- 写 `~/.config/cc-autopilot-notify/config.json` 与 systemd user unit；
- 飞书 webhook 通过 `settings.json` 的 `env.CC_AUTOPILOT_FEISHU_WEBHOOK` 提供；
- 守护进程（Tier A，每秒轮询 Pebrel）由 systemd 托管。

这套方式能用，但无法用 `/plugin` 管理，也没有可分发形态。

### 1.2 目标

把项目改造成 Claude Code 插件，并让仓库本身兼作 marketplace：

1. `/plugin marketplace add mi4646/cc-autopilot-notify` → `/plugin install cc-autopilot-notify@cc-autopilot-notify` 即可用；
2. 插件安装后**无需手动步骤**：hooks 自动注册，守护进程随会话启动自动拉起；
3. 已直装的旧版本可迁移，且不会出现「旧 hooks + 新 hooks 双重通知」；
4. Tier A/B/C 的行为与判定逻辑保持不变。

### 1.3 成功标准（可验证）

| # | 标准 | 验证方式 |
|---|---|---|
| P1 | 仓库作为 marketplace 被 `claude plugin marketplace add` 识别，插件可安装 | 本机实跑 add/install，`claude plugin list` 显示 enabled |
| P2 | 插件安装后 5 个 hook 事件按预期触发（通知、记录问题、清理记录） | 真实会话实跑，观察通知与 state 目录 |
| P3 | SessionStart 自动拉起守护进程，心跳在 15 秒内出现 | 删除/停止 daemon 后开新会话，`doctor` 心跳 PASS |
| P4 | 同一时刻只有一个 daemon 存活 | 并发触发两次 SessionStart，进程数 = 1 |
| P5 | 插件版本升级后，旧版本的 daemon 被替换 | 模拟两个不同 `CLAUDE_PLUGIN_ROOT`，心跳中的 root 更新 |
| P6 | 迁移脚本跑完后，旧 hooks/unit 消失，config 与 state 保留 | 临时 HOME 下跑 `migrate.sh` 两次（幂等） |
| P7 | `path_b.enabled=false` 时 `PermissionRequest` hook 不阻塞 | 单元测试 + 实跑计时 |

---

## 2. 决策摘要

| 决策点 | 结论 | 否决的备选 | 翻案条件 |
|---|---|---|---|
| 守护进程生命周期 | SessionStart hook 自动拉起（Setsid detached + 单实例锁） | 保留 systemd、MCP server 托管、放弃 daemon | systemd 的自动重启被证明不可或缺，或 daemon 崩溃率显著影响可用性 |
| 密钥入口 | `config.json` 直填 `channels.feishu.webhook`，`webhook_env` 回退 | 仅环境变量、仅直填 | 出现密钥管理器的明确需求 |
| 分发形态 | 仓库根目录兼作 marketplace + plugin（`source: "./"`） | 拆出独立 marketplace 仓库、插件放 `plugins/<name>/` 子目录 | 仓库根目录出现与插件分发冲突的内容 |
| 旧直装脚本 | 保留 `install.sh` / `uninstall.sh` 作为 legacy 路径 | 删除、改造为插件安装器 | legacy 路径长期无人使用 |
| 迁移 | 显式脚本 + 命令，不自动改写 settings.json | SessionStart 自动迁移 | 插件系统提供官方迁移钩子 |

`source: "./"` 的可行性已用本机 marketplace `claude-hud` 的 `marketplace.json` 验证。

---

## 3. 仓库布局与分发

```text
/var/www/cc-autopilot-notify/
├── .claude-plugin/
│   ├── plugin.json          # name/description/version/author/repository/license
│   └── marketplace.json     # plugins: [{ name, source: "./", ... }]
├── hooks/
│   └── hooks.json           # 全部 hook 注册，命令使用 ${CLAUDE_PLUGIN_ROOT}
├── commands/
│   ├── doctor.md            # /cc-autopilot:doctor
│   └── migrate.md           # /cc-autopilot:migrate
├── scripts/
│   ├── ensure-daemon.sh     # SessionStart 入口（薄包装）
│   ├── migrate.sh           # 旧直装 → 插件
│   └── cc-autopilot-notify  # CLI 包装：导出 PYTHONPATH 后调用 python3 -m
├── cc_autopilot_notify/     # 包本体，路径不变
├── install.sh               # legacy 直装，保持不变
├── uninstall.sh             # legacy 直装，保持不变
├── pyproject.toml           # version 升至 0.2.0
└── tests/
```

- 版本：`pyproject.toml`、`.claude-plugin/plugin.json`、`marketplace.json` 三处同步为 `0.2.0`。
- 插件命令命名空间由插件名决定：`/cc-autopilot:doctor`、`/cc-autopilot:migrate`。
- Python 依赖保持为零（仅标准库），`requires-python >= 3.11` 不变。

---

## 4. hooks 注册

`hooks/hooks.json` 内容：

| 事件 | matcher | 命令 | 说明 |
|---|---|---|---|
| `PreToolUse` | `AskUserQuestion` | `python3 ${CLAUDE_PLUGIN_ROOT}/cc_autopilot_notify/run_hook.py pre_tool` | 记录待答问题并通知 |
| `PostToolUse` | `AskUserQuestion` | `... run_hook.py notify` | 问题结束，清 pending |
| `Notification` | `*` | `... run_hook.py notify` | 通知 |
| `Stop` | `*` | `... run_hook.py notify` | 本轮完成 |
| `StopFailure` | `*` | `... run_hook.py notify` | 本轮失败 |
| `PermissionRequest` | `AskUserQuestion` | `... run_hook.py permission` | 始终注册，配置门控（见 6 节） |
| `SessionStart` | `*` | `bash ${CLAUDE_PLUGIN_ROOT}/scripts/ensure-daemon.sh` | 确保 daemon 存活 |

要点：

- `run_hook.py` 已有「把自身所在包根插入 `sys.path`」的逻辑（`run_hook.py:16-18`），因此插件形态下**不需要** `PYTHONPATH`。
- hook 失败必须吞掉并返回 0（`hook_common.run_hook` 已保证），插件形态不改变这一点。
- `PermissionRequest` 的 timeout 保持 180 秒（必须大于路径 B 自身等待时间），但配置关闭时 hook 立即 `exit 0`，不阻塞。

---

## 5. 守护进程生命周期

### 5.1 新增模块

`cc_autopilot_notify/daemon_control.py`，纯函数 + 少量进程操作，便于单测：

- `should_spawn(state_root, now, plugin_root) -> SpawnDecision`
- `spawn(state_root, plugin_root) -> int`（返回 PID）
- `stop(state_root) -> bool`
- 心跳结构扩展为 `{"at": float, "pid": int, "plugin_root": str, "version": str}`。

### 5.2 算法（SessionStart 触发）

1. `config.daemon.enabled == False` → 退出。
2. 读 `heartbeat.json`：
   - 心跳年龄 `< 15s` 且 `plugin_root == ${CLAUDE_PLUGIN_ROOT}` → 已在运行，退出；
   - 心跳年龄 `< 15s` 但 `plugin_root` 不同（插件升级）→ 对旧 `pid` 发 `SIGTERM`，最多等 5s，然后继续第 3 步；
   - 心跳过期或缺失 → 继续第 3 步。
3. 以 `setsid` + `nohup` 拉起：
   `python3 -m cc_autopilot_notify.cli daemon >> $state/daemon.log 2>&1 &`
   环境变量 `PYTHONPATH=${CLAUDE_PLUGIN_ROOT}`。
4. daemon 启动时对 `$state/daemon.lock` 持有**生命周期级 flock**；拿不到锁即退出。这是并发 SessionStart 的单实例保证。
5. daemon 每 60 个 tick（约 60 秒）检查自身包目录是否存在；不存在则退出（插件被卸载后不留孤儿进程）。
6. 日志：`$state/daemon.log` 超过 5 MB 时在启动前截断为 0。

### 5.3 已知代价

- 失去 systemd 的 `Restart=on-failure`：daemon 崩溃后，要等**下一个会话启动**才恢复。
- 若会话长期不重启，daemon 不会自我重启；daemon 自身 tick 已捕获异常不退出（`daemon.run` 的 while 循环），因此只有进程级崩溃受影响。

---

## 6. 配置 schema 变更

`DEFAULTS` 新增/调整：

```jsonc
{
  "channels": {
    "feishu": {
      "enabled": true,
      "webhook": null,                                   // 新增：直填 URL，优先
      "webhook_env": "CC_AUTOPILOT_FEISHU_WEBHOOK"       // 保留：回退
    }
  },
  "path_b": { "enabled": false },                        // 新增：取代安装期环境变量开关
  "daemon": { "enabled": true }                          // 新增
}
```

规则：

- `config.feishu_webhook()`：`channels.feishu.webhook` 非空字符串 → 返回它；否则读取 `webhook_env` 指定的环境变量。
- `_validate` 同步新增：`webhook` 为 `null`、空字符串或非空字符串（空字符串表示「未配置」，回退到 `webhook_env`）；`path_b.enabled`、`daemon.enabled` 为 bool。
- `hook_permission` 开头读取 `path_b.enabled`，false 即 `return 0`；该判断发生在读 stdin 之前，保证不阻塞。
- 首次 SessionStart 若 `~/.config/cc-autopilot-notify/config.json` 不存在：写默认配置并 `chmod 0600`。已存在则不动。
- `config.py` 模块 docstring 中「Credentials never live in it」一句需要改写为「直填优先、env 回退」，并在 README 说明文件权限要求。

### 6.1 环境变量

| 变量 | 用途 |
|---|---|
| `CC_AUTOPILOT_FEISHU_WEBHOOK` | 回退密钥入口（名称来自配置） |
| `CC_AUTOPILOT_STATE` | 覆盖 state 目录（测试用，已有） |
| `CC_AUTOPILOT_CONFIG` | 覆盖配置路径（迁移/测试用，新增支持由脚本读取） |

插件**不**写入用户 `settings.json`。`CLAUDE_AFK_TIMEOUT_MS=300000`（Tier C）仍由用户自行设置；迁移脚本会从旧 `env` 中保留该值并提示。

---

## 7. 迁移（旧直装 → 插件）

`scripts/migrate.sh`，由 `/cc-autopilot:migrate` 命令触发，**不自动执行**。

步骤：

1. 读取 `~/.claude/settings.json`；JSON 无效或非 UTF-8 → 整体中止，不做任何修改（沿用 `uninstall.sh` 的安全前置）。
2. 删除**同时**含 `run_hook.py` 与 `cc_autopilot_notify` 的 hook 条目（只有内层 hooks 匹配，同组其他 hook 保留；某组内层清空时才删除整组）；删除 `_ccAutopilotPrevious` 中由本工具写入的键；其他条目一字不动。
3. 写回前备份为 `settings.json.bak-YYYYMMDD-HHMMSS`。
4. 若 `~/.config/systemd/user/cc-autopilot-notify.service` 存在且内容含 `cc_autopilot_notify`：`systemctl --user disable --now` 后删除 unit；否则只提示不碰。
5. 若旧 `env.CC_AUTOPILOT_FEISHU_WEBHOOK` 存在且配置中尚无非空 `webhook`：把值写入 `config.json` 的 `channels.feishu.webhook`，成功后从 `settings.json` 的 `env` 中移除该键；写入失败则保留 env 并报错。
6. 若旧 `env.CLAUDE_AFK_TIMEOUT_MS` 存在：保留（Tier C 需要），并在输出中提示用户确认。
7. 幂等：第二次运行检测到无旧条目时输出「nothing to migrate」并返回 0。

`doctor` 新增检查 `legacy install`：settings.json 中仍存在同时含 `run_hook.py` 与 `cc_autopilot_notify` 的条目时 FAIL，提示运行 `/cc-autopilot:migrate`；没有则为 PASS。

---

## 8. 兼容与 legacy

- `install.sh` / `uninstall.sh` 保持现状，继续作为不使用插件时的直装路径。
- 两者写入的 hook 命令与新插件 hooks 会**同时生效**，因此迁移是安装插件后的必要步骤；`doctor` 的 `legacy install` 检查负责暴露该状态。
- 现有 95 个 Python 测试与 `tests/test_install_merge.sh` 不受影响。
- 状态目录、配置目录、Tier 判定逻辑均不变。

---

## 9. 测试与验收

新增测试：

| 对象 | 用例 |
|---|---|
| `daemon_control.should_spawn` | 无心跳→spawn；心跳新鲜且同 root→不 spawn；心跳新鲜但 root 不同→replace；过期→spawn；`daemon.enabled=false`→不 spawn |
| `daemon_control` 单实例 | 两个进程同时抢 `daemon.lock`，只有一个获得 |
| `hook_permission` | `path_b.enabled=false` 时立即返回 0 且不读 stdin（用永不结束的 stdin 验证不阻塞） |
| `config` | 新增字段校验、直填优先于 env、坏类型报 ConfigError、未知键告警 |
| `migrate.sh` | 临时 HOME：旧 hooks 被删、无关 hooks 保留、config 保留、webhook 搬迁、二次运行幂等 |
| `doctor` | legacy 条目存在时 FAIL |

手工验收（对应 1.3 的 P1–P7）：

1. `claude plugin marketplace add /var/www/cc-autopilot-notify` → `claude plugin list` 显示插件。
2. 开新会话 → `python3 -m cc_autopilot_notify.cli doctor` 心跳 PASS。
3. `ps` 确认 daemon 只有一个；`kill` 后开新会话说恢复。
4. 实跑一次 AskUserQuestion，确认通知与 pending 清理。
5. 跑 `migrate.sh`，确认旧 hooks 消失且无双重通知。

---

## 10. 明确不做

| 不做的事 | 原因 | 重启条件 |
|---|---|---|
| MCP server 托管 daemon | 需实现 MCP 握手；且 daemon 与单会话绑定，多会话语义复杂 | 插件系统提供官方后台进程能力，或需要按会话隔离 daemon |
| 自动改写用户 settings.json | 静默修改用户配置风险高，且与 legacy 安装冲突难以撤回 | 插件提供官方迁移钩子 |
| 删除 legacy `install.sh` / `uninstall.sh` | 现有测试与用户路径依赖，删除属破坏性动作 | legacy 路径确认无人使用 |
| 改变 Tier A/B/C 判定逻辑 | 本次只改包装与生命周期 | 另行设计 |
| 把包移入 `plugins/<name>/` 子目录 | 现有包路径与测试均以根目录为基准，移动收益不足 | 仓库根目录出现分发冲突 |
| 插件写入 `CLAUDE_AFK_TIMEOUT_MS` | 插件无写 settings 的机制 | 官方提供插件级 env 声明 |

---

## 11. 风险与未决项

1. **`${CLAUDE_PLUGIN_ROOT}` 在 hook 命令中的可用性**：已由本机 `cc-notify-hooks` 的 `hooks/hooks.json` 实际使用验证；本设计沿用同一形态。
2. **SessionStart 与 hooks 的加载顺序**：若 SessionStart 未触发（例如 `--no-session-start` 类场景），daemon 不会自动拉起；`doctor` 会显示心跳过期。这是可接受的降级。
3. **WSL 下 `setsid` 的行为**：需在实现后用 P3/P4 实测确认；若 `setsid` 不可用，回退为 `nohup ... &` 并记录差异。
4. **插件缓存路径随版本变化**：由 5.2 第 2 步的 root 比较处理，需 P5 实测。
