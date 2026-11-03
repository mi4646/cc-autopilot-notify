# CLAUDE.md

> 行为判据在 `~/.claude/CLAUDE.md` 与 `~/.claude/rules/*.md`，本文件只写本项目事实。
> 机制、通知策略、安装与迁移细节见 `README.md`；验收标准见 `docs/acceptance.md`；设计与计划在 `docs/superpowers/`。

## 项目是什么

Claude Code 插件（纯 Python 标准库，零运行时依赖）：人不在时自动应答 `AskUserQuestion`（优先选 Claude 标记的推荐项），并通过飞书卡片通知。三层降级：A（daemon 轮询 Pebrel runtime 提交答案）→ B（`PermissionRequest` hook 阻塞返回）→ C（Claude 自带 AFK 续跑）。

## 命令

```bash
python -m pytest tests/ -q            # 全量测试
bash tests/test_install_merge.sh      # settings.json 合并脚本测试
./install.sh                          # 旧式直装（写 settings/hook/config/systemd unit）
CC_AUTOPILOT_DRY_RUN=1 ./install.sh   # 只打印将写入的 settings
cc-autopilot-notify doctor            # 体检：pebrel runtime/协议版本/capabilities/前台/心跳，全 PASS 才算可用
```

插件形态安装：`claude plugin install cc-autopilot-notify@cc-autopilot-notify`，daemon 在下次 SessionStart 启动。

## 结构

- `cc_autopilot_notify/` —— 包本体（hooks / channels / daemon / judge / config / cli）。
- `hooks/` + `commands/` —— 插件钩子声明与 `/doctor`、`/migrate` 命令；`.claude-plugin/` 是插件清单。
- `install.sh` / `uninstall.sh` —— 只增删自己的条目，其余 settings 原样保留，改动前写 `settings.json.bak-YYYYMMDD-HHMMSS`。
- 配置：`~/.config/cc-autopilot-notify/config.json`（0600，首次 SessionStart 创建）；飞书 webhook 也可用 `CC_AUTOPILOT_FEISHU_WEBHOOK` 兜底。

## 硬约束

- **改代码要同步 bump 版本**（`pyproject.toml` 与 `.claude-plugin/plugin.json`），否则 `/plugin update` 在其他机器上不会拉到新代码。
- 通知失败只记日志、**绝不阻塞回合**——这是与上游 bash+jq 实现的核心差异。
- Pebrel runtime token 只从 `%APPDATA%\Pebrel\runtime.port` 读，**不得写入日志、通知或状态文件**。
- 未识别的 `conversation.read` 形状要抛错，不许猜——静默错答比不答更糟。
- `quiet_hours` 屏蔽窗口内**所有**通知；窗口相等或格式错时宁可全放行，也不能让人一天收不到告警。
- 路径 B 默认关闭，只有 `CC_AUTOPILOT_ENABLE_PATH_B=1` 才开——它会让对话框阻塞。

## 验证

改完跑 `python -m pytest tests/ -q` 与 `bash tests/test_install_merge.sh`。涉及 daemon / 自动应答时按 `docs/acceptance.md` 用 `doctor` 逐项 PASS 验收，不要只看单测。
