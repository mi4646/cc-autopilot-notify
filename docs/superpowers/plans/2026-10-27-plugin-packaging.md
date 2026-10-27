# cc-autopilot-notify 插件化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 cc-autopilot-notify 改造成可用 `/plugin` 安装的 Claude Code 插件，仓库兼作 marketplace，并保留 Tier A/B/C 行为。

**Architecture:** 包本体不动；新增 `.claude-plugin/` 清单、`hooks/hooks.json`（命令全部走 `${CLAUDE_PLUGIN_ROOT}`）、`scripts/` 薄包装与 `commands/`。守护进程改由 SessionStart 触发、以 detached 进程 + 生命周期级 flock 运行；配置增加直填 webhook、`path_b.enabled`、`daemon.enabled`；迁移用显式脚本（Python 实现，shell 仅包装）退役旧 settings.json hooks。

**Tech Stack:** Python 3.11+（仅标准库）、pytest、bash（薄包装）、Claude Code plugin/marketplace JSON。

**Spec:** `docs/superpowers/specs/2026-10-27-plugin-packaging-design.md`

## Global Constraints

- 仅使用 Python 标准库；不新增任何依赖。
- 版本 `0.2.0`，三处同步：`pyproject.toml`、`.claude-plugin/plugin.json`、`.claude-plugin/marketplace.json`。
- 插件内所有 hook 命令必须引用 `${CLAUDE_PLUGIN_ROOT}`，不得写绝对路径。
- hook 正常路径必须返回 0；任何异常不得逃逸出 hook（沿用 `hook_common.run_hook`）。
- `install.sh` / `uninstall.sh` 保持不变；本计划不修改其行为。
- 自动创建的 `~/.config/cc-autopilot-notify/config.json` 权限必须为 `0600`。
- Tier A/B/C 的判定逻辑不变；`config.DEFAULTS` 中已有的键与默认值不变。
- 正式测试命令：`python -m pytest tests/ -q`；shell 测试：`bash tests/test_install_merge.sh`、`bash tests/test_migrate.sh`。

## Review Focus

1. **`path_b.enabled=false` 时 `PermissionRequest` 仍被注册** → 用户期望对话框不被阻塞；hook 必须在读 stdin 之前退出（Task 2 覆盖）。
2. **两个会话同时触发 SessionStart** → 用户期望只有一个 daemon；生命周期 flock 必须拒绝第二个（Task 4 覆盖）。
3. **插件升级后旧 root 的心跳仍新鲜** → 用户期望新版本接管；`plugin_root` 不一致时必须替换旧进程（Task 4 覆盖）。
4. **settings.json 为 UTF-16 或坏 JSON 时运行迁移** → 用户期望什么都不被破坏；必须整体中止且不写文件（Task 6 覆盖）。
5. **插件被卸载而 daemon 仍在跑** → 用户期望不留孤儿进程；daemon 必须检测包目录消失并退出（Task 4 覆盖）。

---

### Task 1: 配置 schema（直填 webhook、path_b/daemon 开关、路径覆盖、默认配置写入）

**Files:**
- Modify: `cc_autopilot_notify/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 现有 `DEFAULTS`、`_merge`、`_section`、`_require_object`、`_require_bool`。
- Produces:
  - `DEFAULTS["channels"]["feishu"]["webhook"]: str | None = None`
  - `DEFAULTS["path_b"] = {"enabled": False}`、`DEFAULTS["daemon"] = {"enabled": True}`
  - `config.path_b_enabled(cfg: dict) -> bool`（缺省 False）
  - `config.daemon_enabled(cfg: dict) -> bool`（缺省 True）
  - `config.default_path() -> Path`（优先 `CC_AUTOPILOT_CONFIG`）
  - `config.write_default(path: Path) -> None`（原子写 + `0600`）
  - `config.feishu_webhook(cfg)`：直填非空 → 直填；否则按 `webhook_env` 查环境变量。

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 追加：

```python
def test_direct_webhook_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://env.example/x")
    cfg = _load(tmp_path, {"channels": {"feishu": {"webhook": "https://direct.example/y"}}})
    assert config.feishu_webhook(cfg) == "https://direct.example/y"


def test_env_fallback_when_webhook_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://env.example/x")
    cfg = _load(tmp_path, {"thresholds": {"foreground_seconds": 150,
                                         "background_seconds": 90}})
    assert config.feishu_webhook(cfg) == "https://env.example/x"


def test_empty_direct_webhook_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://env.example/x")
    cfg = _load(tmp_path, {"channels": {"feishu": {"webhook": ""}}})
    assert config.feishu_webhook(cfg) == "https://env.example/x"


def test_bad_webhook_type_raises(tmp_path):
    _assert_config_error(tmp_path, {"channels": {"feishu": {"webhook": 5}}})


def test_path_b_and_daemon_flags_validate(tmp_path):
    assert config.path_b_enabled(_load(tmp_path, {})) is False
    assert config.daemon_enabled(_load(tmp_path, {})) is True
    cfg = _load(tmp_path, {"path_b": {"enabled": True}, "daemon": {"enabled": False}})
    assert config.path_b_enabled(cfg) is True
    assert config.daemon_enabled(cfg) is False
    _assert_config_error(tmp_path, {"path_b": {"enabled": "yes"}})
    _assert_config_error(tmp_path, {"daemon": {"enabled": 1}})


def test_config_path_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom.json"
    target.write_text(json.dumps({"thresholds": {"foreground_seconds": 111,
                                                 "background_seconds": 22}}))
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(target))
    assert config.foreground_seconds(config.load()) == 111


def test_write_default_is_private(tmp_path):
    target = tmp_path / "cfg" / "config.json"
    config.write_default(target)
    assert json.loads(target.read_text())["daemon"]["enabled"] is True
    assert (target.stat().st_mode & 0o777) == 0o600
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL（`AttributeError` / `KeyError`，例如 `config.path_b_enabled` 不存在）。

- [ ] **Step 3: 实现**

`DEFAULTS` 改为：

```python
    "channels": {
        "feishu": {"enabled": True, "webhook": None,
                   "webhook_env": "CC_AUTOPILOT_FEISHU_WEBHOOK"},
        "terminal": {"enabled": True},
        "pebrel": {"enabled": True},
    },
    "path_b": {"enabled": False},
    "daemon": {"enabled": True},
```

`_validate` 在 `channels` 块内新增：

```python
        if name == "feishu":
            env = section.get("webhook_env")
            if not isinstance(env, str) or not env:
                raise ConfigError(
                    f"channels.feishu.webhook_env must be an env var name, got {env!r}")
            webhook = section.get("webhook")
            if webhook is not None and (not isinstance(webhook, str) or not webhook):
                raise ConfigError(
                    f"channels.feishu.webhook must be a URL string or null, got {webhook!r}")
```

并在 `quiet_hours` 校验之后加：

```python
    path_b = _require_object(cfg, "path_b")
    _require_bool(path_b, "enabled", "path_b.enabled")
    daemon = _require_object(cfg, "daemon")
    _require_bool(daemon, "enabled", "daemon.enabled")
```

`load()` 的默认路径改为：

```python
    if path is None:
        override = os.environ.get("CC_AUTOPILOT_CONFIG")
        path = Path(override) if override else (
            Path.home() / ".config" / "cc-autopilot-notify" / "config.json")
```

`feishu_webhook()` 改为：

```python
def feishu_webhook(cfg: dict) -> str | None:
    """Direct URL first; otherwise the env var the config names."""
    feishu = _section(_section(cfg, "channels"), "feishu")
    direct = feishu.get("webhook")
    if isinstance(direct, str) and direct:
        return direct
    env_name = feishu.get("webhook_env")
    if not env_name:
        return None
    return os.environ.get(env_name) or None
```

新增：

```python
def path_b_enabled(cfg: dict) -> bool:
    return _section(cfg, "path_b").get("enabled") is True


def daemon_enabled(cfg: dict) -> bool:
    return _section(cfg, "daemon").get("enabled", True) is True


def default_path() -> Path:
    override = os.environ.get("CC_AUTOPILOT_CONFIG")
    return Path(override) if override else (
        Path.home() / ".config" / "cc-autopilot-notify" / "config.json")


def write_default(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(copy.deepcopy(DEFAULTS), handle, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
```

同步改写模块 docstring 中「Credentials never live in it」一句为「直填优先、环境变量回退」，并注明文件权限要求。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS（原有用例全部保留）。

- [ ] **Step 5: 提交**

```bash
git add cc_autopilot_notify/config.py tests/test_config.py
git commit -m "feat: config accepts a direct webhook and plugin runtime switches"
```

---

### Task 2: PermissionRequest 门控

**Files:**
- Modify: `cc_autopilot_notify/hook_permission.py`
- Test: `tests/test_permission.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `config.path_b_enabled`、`config.load`。
- Produces: `hook_permission.main() -> int` 在配置关闭时不读 stdin、立即返回 0。

- [ ] **Step 1: 写失败测试**

```python
import json

from cc_autopilot_notify import hook_permission


class _ExplodingStdin:
    def read(self, *args, **kwargs):
        raise AssertionError("stdin must not be read when path B is disabled")


def test_main_returns_without_reading_stdin_when_disabled(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"thresholds": {"foreground_seconds": 150,
                                              "background_seconds": 90}}))
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(cfg))
    monkeypatch.setattr("sys.stdin", _ExplodingStdin())
    assert hook_permission.main() == 0


def test_main_reads_stdin_when_enabled(tmp_path, monkeypatch, capsys):
    import io
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"path_b": {"enabled": True}}))
    monkeypatch.setenv("CC_AUTOPILOT_CONFIG", str(cfg))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert hook_permission.main() == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_permission.py -q`
Expected: 第一个用例 FAIL（`AssertionError: stdin must not be read`）。

- [ ] **Step 3: 实现**

把 `main()` 改为：

```python
def main() -> int:
    from . import config

    try:
        cfg = config.load()
    except Exception:  # noqa: BLE001 - a hook must never wedge Claude Code
        log.exception("permission hook: cannot load config; leaving the dialog alone")
        return 0
    if not config.path_b_enabled(cfg):
        return 0
    return hook_common.run_hook(handle)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_permission.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add cc_autopilot_notify/hook_permission.py tests/test_permission.py
git commit -m "feat: gate the blocking permission hook on path_b.enabled"
```

---

### Task 3: 心跳载荷扩展

**Files:**
- Modify: `cc_autopilot_notify/state.py:138-150`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: 现有 `_atomic_write`、`_locked`。
- Produces:
  - `state.write_heartbeat(root, when, *, pid=None, plugin_root=None, version=None) -> None`
  - `state.read_heartbeat(root) -> dict | None`（键：`at`、`pid`、`plugin_root`、`version`）
  - `state.heartbeat_age(root, now)` 行为不变，仍只读 `at`。

- [ ] **Step 1: 写失败测试**

```python
def test_heartbeat_carries_process_identity(tmp_path):
    state.write_heartbeat(tmp_path, when=1000.0, pid=4242,
                          plugin_root="/plugin/root", version="0.2.0")
    hb = state.read_heartbeat(tmp_path)
    assert hb == {"at": 1000.0, "pid": 4242,
                  "plugin_root": "/plugin/root", "version": "0.2.0"}
    assert state.heartbeat_age(tmp_path, now=1010.0) == 10.0


def test_read_heartbeat_returns_none_when_missing(tmp_path):
    assert state.read_heartbeat(tmp_path) is None


def test_legacy_heartbeat_without_pid_still_reads(tmp_path):
    (tmp_path / "heartbeat.json").write_text('{"at": 5.0}')
    hb = state.read_heartbeat(tmp_path)
    assert hb["at"] == 5.0 and hb["pid"] is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_state.py -q`
Expected: FAIL（`read_heartbeat` 不存在 / `write_heartbeat` 不接受新参数）。

- [ ] **Step 3: 实现**

```python
def write_heartbeat(root: Path, when: float, *, pid: int | None = None,
                    plugin_root: str | None = None,
                    version: str | None = None) -> None:
    root = Path(root)
    with _locked(root):
        _atomic_write(root / f"{_HEARTBEAT}.json",
                      {"at": float(when), "pid": pid,
                       "plugin_root": plugin_root, "version": version})


def read_heartbeat(root: Path) -> dict | None:
    path = Path(root) / f"{_HEARTBEAT}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("at"), (int, float)):
        return None
    return {"at": float(data["at"]), "pid": data.get("pid"),
            "plugin_root": data.get("plugin_root"),
            "version": data.get("version")}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_state.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add cc_autopilot_notify/state.py tests/test_state.py
git commit -m "feat: heartbeat records pid and plugin root"
```

---

### Task 4: daemon_control（判活、拉起、停止、单实例锁）与 daemon/CLI 接线

**Files:**
- Create: `cc_autopilot_notify/daemon_control.py`
- Modify: `cc_autopilot_notify/daemon.py`（`tick`、`run`）
- Modify: `cc_autopilot_notify/cli.py`
- Test: `tests/test_daemon_control.py`（新建）、`tests/test_daemon.py`

**Interfaces:**
- Consumes: Task 1 的 `config.daemon_enabled`/`config.default_path`/`config.write_default`；Task 3 的 `state.read_heartbeat`。
- Produces:
  - `daemon_control.POLL_STALE_SECONDS = 15.0`
  - `daemon_control.Decision(action: str, reason: str, stale_pid: int | None = None)`，`action ∈ {"skip","spawn","replace"}`
  - `daemon_control.should_spawn(root: Path, plugin_root: str, now: float | None = None) -> Decision`
  - `daemon_control.acquire_lock(root: Path)` → 持有锁的文件句柄，或 `None`
  - `daemon_control.start(root: Path, plugin_root: str) -> int`（返回子进程 PID）
  - `daemon_control.stop(root: Path) -> bool`
  - `daemon_control.wait_gone(pid: int, timeout: float = 5.0) -> bool`
  - `daemon_control.plugin_version(plugin_root: str) -> str | None`
  - `daemon_control.package_present(plugin_root: str) -> bool`
  - `daemon.tick(..., *, pid=None, plugin_root=None, version=None)`
  - CLI 子命令 `ensure-daemon`、`stop-daemon`。

- [ ] **Step 1: 写失败测试**

`tests/test_daemon_control.py`：

```python
import json
import time

from cc_autopilot_notify import daemon_control, state


def test_spawns_when_no_heartbeat(tmp_path):
    d = daemon_control.should_spawn(tmp_path, "/p", now=100.0)
    assert d.action == "spawn"


def test_skips_fresh_heartbeat_same_root(tmp_path):
    state.write_heartbeat(tmp_path, when=100.0, pid=7, plugin_root="/p")
    assert daemon_control.should_spawn(tmp_path, "/p", now=105.0).action == "skip"


def test_replaces_fresh_heartbeat_from_another_root(tmp_path):
    state.write_heartbeat(tmp_path, when=100.0, pid=7, plugin_root="/old")
    d = daemon_control.should_spawn(tmp_path, "/new", now=105.0)
    assert (d.action, d.stale_pid) == ("replace", 7)


def test_replaces_legacy_heartbeat_without_root(tmp_path):
    (tmp_path / "heartbeat.json").write_text('{"at": 100.0}')
    assert daemon_control.should_spawn(tmp_path, "/p", now=105.0).action == "replace"


def test_spawns_when_heartbeat_stale(tmp_path):
    state.write_heartbeat(tmp_path, when=100.0, pid=7, plugin_root="/p")
    assert daemon_control.should_spawn(tmp_path, "/p", now=130.0).action == "spawn"


def test_lock_is_exclusive(tmp_path):
    first = daemon_control.acquire_lock(tmp_path)
    assert first is not None
    try:
        assert daemon_control.acquire_lock(tmp_path) is None
    finally:
        first.close()


def test_start_launches_detached_process(tmp_path, monkeypatch):
    seen = {}

    class FakeProc:
        pid = 4321

    def fake_popen(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return FakeProc()

    monkeypatch.setattr(daemon_control.subprocess, "Popen", fake_popen)
    assert daemon_control.start(tmp_path, "/plugin") == 4321
    assert seen["argv"][1:] == ["-m", "cc_autopilot_notify.cli", "daemon"]
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["env"]["PYTHONPATH"] == "/plugin"


def test_stop_signals_heartbeat_pid(tmp_path, monkeypatch):
    state.write_heartbeat(tmp_path, when=time.time(), pid=99, plugin_root="/p")
    killed = []
    monkeypatch.setattr(daemon_control.os, "kill", lambda pid, sig: killed.append(pid))
    assert daemon_control.stop(tmp_path) is True
    assert killed == [99]


def test_stop_returns_false_without_pid(tmp_path):
    assert daemon_control.stop(tmp_path) is False


def test_package_present(tmp_path):
    assert daemon_control.package_present(str(tmp_path)) is True
    assert daemon_control.package_present(str(tmp_path / "gone")) is False
```

在 `tests/test_daemon.py` 追加：

```python
def test_tick_writes_identity_into_heartbeat(tmp_path):
    daemon.tick(1234.5, tmp_path, _cfg(), _deps(),
                pid=55, plugin_root="/p", version="0.2.0")
    hb = state.read_heartbeat(tmp_path)
    assert (hb["pid"], hb["plugin_root"], hb["version"]) == (55, "/p", "0.2.0")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_daemon_control.py tests/test_daemon.py -q`
Expected: FAIL（模块不存在 / 参数不匹配）。

- [ ] **Step 3: 实现 `daemon_control.py`**

```python
"""Liveness decisions and process control for the polling daemon."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import state

log = logging.getLogger(__name__)

POLL_STALE_SECONDS = 15.0
LOCK_NAME = "daemon.lock"
LOG_NAME = "daemon.log"
LOG_MAX_BYTES = 5 * 1024 * 1024


@dataclasses.dataclass
class Decision:
    action: str            # skip | spawn | replace
    reason: str
    stale_pid: int | None = None


def should_spawn(root: Path, plugin_root: str, now: float | None = None) -> Decision:
    now = time.time() if now is None else now
    hb = state.read_heartbeat(root)
    if hb is None:
        return Decision("spawn", "no heartbeat")
    age = now - hb["at"]
    if age >= POLL_STALE_SECONDS:
        return Decision("spawn", f"heartbeat stale by {age:.0f}s")
    if hb.get("plugin_root") != plugin_root:
        return Decision("replace", "plugin root changed", stale_pid=hb.get("pid"))
    return Decision("skip", f"daemon alive ({age:.0f}s old)")


def acquire_lock(root: Path):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / LOCK_NAME, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def start(root: Path, plugin_root: str) -> int:
    root = Path(root)
    log_path = root / LOG_NAME
    if log_path.exists() and log_path.stat().st_size > LOG_MAX_BYTES:
        os.truncate(log_path, 0)
    env = dict(os.environ, PYTHONPATH=plugin_root)
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "cc_autopilot_notify.cli", "daemon"],
            cwd=plugin_root, env=env, stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=log_file,
            start_new_session=True, close_fds=True)
    return proc.pid


def stop(root: Path) -> bool:
    hb = state.read_heartbeat(root)
    pid = (hb or {}).get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        log.warning("daemon_control: cannot stop pid %s (%s)", pid, exc)
        return False
    return True


def wait_gone(pid: int, timeout: float = 5.0) -> bool:
    """Wait for `pid` to exit, so a replacement never overlaps the old daemon."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def plugin_version(plugin_root: str) -> str | None:
    try:
        data = json.loads((Path(plugin_root) / ".claude-plugin" / "plugin.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def package_present(plugin_root: str) -> bool:
    return (Path(plugin_root) / "cc_autopilot_notify").is_dir()
```

`daemon.tick` 签名与心跳写入改为：

```python
def tick(now: float, root: Path, cfg: dict, deps: Deps, *, pid: int | None = None,
         plugin_root: str | None = None, version: str | None = None) -> list[str]:
    root = Path(root)
    state.write_heartbeat(root, when=now, pid=pid,
                          plugin_root=plugin_root, version=version)
    ...
```

`daemon.run` 开头加锁与身份，并在循环里做孤儿检查：

```python
def run(root: Path, cfg: dict, appdata: Path | None = None) -> None:
    from . import daemon_control

    root = Path(root)
    lock = daemon_control.acquire_lock(root)
    if lock is None:
        log.warning("daemon: another instance holds the lock; exiting")
        return
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(
        Path(__file__).resolve().parent.parent)
    version = daemon_control.plugin_version(plugin_root)
    appdata = appdata or Path("/mnt/c/Users/Anonymous/AppData/Roaming")
    deps = build_deps(appdata, cfg)
    ticks = 0
    while True:
        try:
            for action in tick(time.time(), root, cfg, deps, pid=os.getpid(),
                               plugin_root=plugin_root, version=version):
                log.info("daemon: %s", action)
        except Exception:  # noqa: BLE001 - the daemon must outlive any one tick
            log.exception("daemon: tick raised")
        ticks += 1
        if ticks % 60 == 0 and not daemon_control.package_present(plugin_root):
            log.warning("daemon: package root %s is gone; exiting", plugin_root)
            return
        time.sleep(POLL_SECONDS)
```

`daemon.py` 顶部补 `import os`。

`cli.py` 新增：

```python
def _ensure_daemon() -> int:
    import os
    from pathlib import Path

    from . import config, daemon_control, hook_common

    try:
        cfg = config.load()
    except config.ConfigError as exc:
        # A SessionStart hook must never block the session on a bad config.
        print(f"daemon: config error: {exc}", file=sys.stderr)
        return 0
    if not config.daemon_enabled(cfg):
        print("daemon: disabled by config")
        return 0
    path = config.default_path()
    if not path.exists():
        config.write_default(path)
        print(f"daemon: wrote default config to {path}")
    root = hook_common.state_root()
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(
        Path(__file__).resolve().parent.parent)
    decision = daemon_control.should_spawn(root, plugin_root)
    if decision.action == "skip":
        print(f"daemon: {decision.reason}")
        return 0
    if decision.action == "replace" and decision.stale_pid:
        daemon_control.stop(root)
        if not daemon_control.wait_gone(decision.stale_pid):
            print(f"daemon: pid {decision.stale_pid} did not exit; not starting a duplicate")
            return 0
    pid = daemon_control.start(root, plugin_root)
    print(f"daemon: started pid {pid} ({decision.reason})")
    return 0


def _stop_daemon() -> int:
    from . import daemon_control, hook_common

    if daemon_control.stop(hook_common.state_root()):
        print("daemon: stop signal sent")
        return 0
    print("daemon: not running")
    return 0
```

`main()` 注册并分发：

```python
    sub.add_parser("ensure-daemon", help="start the daemon when it is not alive")
    sub.add_parser("stop-daemon", help="stop a running daemon")
    ...
    if args.command == "ensure-daemon":
        return _ensure_daemon()
    if args.command == "stop-daemon":
        return _stop_daemon()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_daemon_control.py tests/test_daemon.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add cc_autopilot_notify/daemon_control.py cc_autopilot_notify/daemon.py \
        cc_autopilot_notify/cli.py tests/test_daemon_control.py tests/test_daemon.py
git commit -m "feat: auto-spawn the daemon with a single-instance lock"
```

---

### Task 5: 插件清单、hooks、薄包装脚本与命令

**Files:**
- Create: `.claude-plugin/plugin.json`
- Create: `.claude-plugin/marketplace.json`
- Create: `hooks/hooks.json`
- Create: `scripts/ensure-daemon.sh`
- Create: `scripts/cc-autopilot-notify`
- Create: `commands/doctor.md`
- Create: `commands/migrate.md`
- Modify: `pyproject.toml`（版本 0.2.0）
- Test: `tests/test_plugin_package.py`（新建）

**Interfaces:**
- Consumes: Task 4 的 CLI 子命令 `ensure-daemon`、`doctor`。
- Produces: 可被 `/plugin marketplace add <repo>` 识别的清单；hook 命令全部形如
  `python3 ${CLAUDE_PLUGIN_ROOT}/cc_autopilot_notify/run_hook.py <module>`。

- [ ] **Step 1: 写失败测试**

`tests/test_plugin_package.py`：

```python
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _json(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_plugin_manifest_matches_pyproject_version():
    plugin = _json(".claude-plugin/plugin.json")
    assert plugin["name"] == "cc-autopilot-notify"
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{plugin["version"]}"' in text


def test_marketplace_points_at_the_repo_root():
    market = _json(".claude-plugin/marketplace.json")
    assert market["plugins"][0]["source"] == "./"
    assert market["plugins"][0]["name"] == "cc-autopilot-notify"


def test_hooks_use_plugin_root_and_exist():
    hooks = _json("hooks/hooks.json")["hooks"]
    for event in ("PreToolUse", "PostToolUse", "Notification", "Stop",
                  "StopFailure", "PermissionRequest", "SessionStart"):
        assert event in hooks, f"{event} not registered"
    for items in hooks.values():
        for item in items:
            for hook in item["hooks"]:
                command = hook["command"]
                assert "${CLAUDE_PLUGIN_ROOT}" in command, command
                rel = command.split("${CLAUDE_PLUGIN_ROOT}/", 1)[1].split()[0]
                assert (ROOT / rel).exists(), f"missing {rel}"


def test_wrapper_scripts_are_executable():
    for rel in ("scripts/ensure-daemon.sh", "scripts/cc-autopilot-notify"):
        assert os.access(ROOT / rel, os.X_OK), f"{rel} is not executable"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_plugin_package.py -q`
Expected: FAIL（`FileNotFoundError`）。

- [ ] **Step 3: 创建清单与脚本**

`.claude-plugin/plugin.json`：

```json
{
  "name": "cc-autopilot-notify",
  "description": "Notifications and unattended auto-answer for Claude Code on WSL with the Pebrel terminal",
  "version": "0.2.0",
  "author": {"name": "mi4646"},
  "repository": "https://github.com/mi4646/cc-autopilot-notify",
  "license": "MIT",
  "keywords": ["notification", "auto-answer", "feishu", "pebrel", "wsl"]
}
```

`.claude-plugin/marketplace.json`：

```json
{
  "name": "cc-autopilot-notify",
  "owner": {"name": "mi4646"},
  "metadata": {
    "description": "Notifications and unattended auto-answer for Claude Code"
  },
  "plugins": [
    {
      "name": "cc-autopilot-notify",
      "source": "./",
      "description": "Feishu/terminal/toast notifications plus unattended auto-answer",
      "version": "0.2.0",
      "category": "productivity",
      "tags": ["notification", "auto-answer", "hooks"]
    }
  ]
}
```

`hooks/hooks.json`：

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "AskUserQuestion", "hooks": [
        {"type": "command", "timeout": 10,
         "command": "python3 ${CLAUDE_PLUGIN_ROOT}/cc_autopilot_notify/run_hook.py pre_tool"}]}
    ],
    "PostToolUse": [
      {"matcher": "AskUserQuestion", "hooks": [
        {"type": "command", "timeout": 10,
         "command": "python3 ${CLAUDE_PLUGIN_ROOT}/cc_autopilot_notify/run_hook.py notify"}]}
    ],
    "Notification": [
      {"matcher": "*", "hooks": [
        {"type": "command", "timeout": 10,
         "command": "python3 ${CLAUDE_PLUGIN_ROOT}/cc_autopilot_notify/run_hook.py notify"}]}
    ],
    "Stop": [
      {"matcher": "*", "hooks": [
        {"type": "command", "timeout": 10,
         "command": "python3 ${CLAUDE_PLUGIN_ROOT}/cc_autopilot_notify/run_hook.py notify"}]}
    ],
    "StopFailure": [
      {"matcher": "*", "hooks": [
        {"type": "command", "timeout": 10,
         "command": "python3 ${CLAUDE_PLUGIN_ROOT}/cc_autopilot_notify/run_hook.py notify"}]}
    ],
    "PermissionRequest": [
      {"matcher": "AskUserQuestion", "hooks": [
        {"type": "command", "timeout": 180,
         "command": "python3 ${CLAUDE_PLUGIN_ROOT}/cc_autopilot_notify/run_hook.py permission"}]}
    ],
    "SessionStart": [
      {"matcher": "*", "hooks": [
        {"type": "command", "timeout": 20,
         "command": "bash ${CLAUDE_PLUGIN_ROOT}/scripts/ensure-daemon.sh"}]}
    ]
  }
}
```

`scripts/ensure-daemon.sh`：

```bash
#!/usr/bin/env bash
# SessionStart entry: start the polling daemon when it is not alive.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m cc_autopilot_notify.cli ensure-daemon
```

`scripts/cc-autopilot-notify`：

```bash
#!/usr/bin/env bash
# Run the CLI from a plugin checkout without installing the package.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m cc_autopilot_notify.cli "$@"
```

`commands/doctor.md`：

```markdown
---
description: Run cc-autopilot-notify self-checks
---

!`bash ${CLAUDE_PLUGIN_ROOT}/scripts/cc-autopilot-notify doctor`

Summarize the checks above. Any FAIL names the broken layer; SKIP means unverified, not passing.
```

`commands/migrate.md`：

```markdown
---
description: Retire legacy settings.json hooks and move the webhook into config
---

!`bash ${CLAUDE_PLUGIN_ROOT}/scripts/migrate.sh`

Report what the migration removed or kept. Do not edit settings.json yourself.
```

把 `pyproject.toml` 的 `version` 改为 `0.2.0`；给两个脚本加可执行位：

```bash
chmod +x scripts/ensure-daemon.sh scripts/cc-autopilot-notify
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_plugin_package.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add .claude-plugin hooks scripts commands pyproject.toml tests/test_plugin_package.py
git commit -m "feat: package the project as a Claude Code plugin and marketplace"
```

---

### Task 6: 迁移模块、脚本与 doctor 的 legacy 检查

**Files:**
- Create: `cc_autopilot_notify/migrate.py`
- Create: `scripts/migrate.sh`
- Modify: `cc_autopilot_notify/doctor.py`（`_settings_clean` → `_legacy_install`）
- Test: `tests/test_migrate.py`（新建）、`tests/test_doctor.py`

**Interfaces:**
- Consumes: Task 1 的 `config.default_path`、`config.write_default`。
- Produces:
  - `class MigrationError(Exception)`
  - `migrate.migrate(settings: Path, config_path: Path, unit: Path, *, run_systemctl: bool = True) -> str`
  - `migrate.main(argv: list[str] | None = None) -> int`
  - doctor 检查名 `legacy install`：settings.json 中存在含 `run_hook.py` 的命令时 FAIL。
  - 环境变量：`CC_AUTOPILOT_SETTINGS`、`CC_AUTOPILOT_CONFIG`、`CC_AUTOPILOT_UNIT`（与 legacy 脚本一致）。

- [ ] **Step 1: 写失败测试**

`tests/test_migrate.py`：

```python
import json
import stat

import pytest

from cc_autopilot_notify import migrate


def _settings(tmp_path, payload):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload))
    return path


def _ours():
    return {"type": "command",
            "command": "PYTHONPATH=/x python3 /x/run_hook.py notify"}


def _foreign():
    return {"type": "command", "command": "other-tool"}


def test_removes_only_our_hooks_and_keeps_foreign(tmp_path):
    settings = _settings(tmp_path, {"hooks": {
        "Stop": [{"matcher": "", "hooks": [_ours()]},
                 {"matcher": "", "hooks": [_foreign()]}],
        "PreToolUse": [{"matcher": "x", "hooks": [_foreign()]}],
    }})
    migrate.migrate(settings, tmp_path / "cfg" / "config.json",
                    tmp_path / "unit.service", run_systemctl=False)
    data = json.loads(settings.read_text())
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "other-tool"
    assert data["hooks"]["Stop"] == [{"matcher": "", "hooks": [_foreign()]}]


def test_invalid_json_aborts_without_touching_files(tmp_path):
    bad = tmp_path / "settings.json"
    bad.write_text("{not json")
    before = bad.read_bytes()
    with pytest.raises(migrate.MigrationError):
        migrate.migrate(bad, tmp_path / "cfg.json", tmp_path / "unit.service",
                        run_systemctl=False)
    assert bad.read_bytes() == before


def test_moves_webhook_from_env_into_config(tmp_path):
    settings = _settings(tmp_path, {
        "env": {"CC_AUTOPILOT_FEISHU_WEBHOOK": "https://hook.example/x",
                "KEEP": "1"},
        "hooks": {"Stop": [{"matcher": "", "hooks": [_ours()]}]}})
    config_path = tmp_path / "cfg" / "config.json"
    migrate.migrate(settings, config_path, tmp_path / "unit.service",
                    run_systemctl=False)
    assert json.loads(config_path.read_text())["channels"]["feishu"]["webhook"] == \
        "https://hook.example/x"
    data = json.loads(settings.read_text())
    assert "CC_AUTOPILOT_FEISHU_WEBHOOK" not in data["env"]
    assert data["env"]["KEEP"] == "1"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_keeps_existing_direct_webhook(tmp_path):
    settings = _settings(tmp_path, {
        "env": {"CC_AUTOPILOT_FEISHU_WEBHOOK": "https://old/x"},
        "hooks": {}})
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"channels": {"feishu": {
        "webhook": "https://new/x"}}}))
    migrate.migrate(settings, config_path, tmp_path / "unit.service",
                    run_systemctl=False)
    assert json.loads(config_path.read_text())["channels"]["feishu"]["webhook"] == \
        "https://new/x"
    assert json.loads(settings.read_text())["env"]["CC_AUTOPILOT_FEISHU_WEBHOOK"] == \
        "https://old/x"


def test_unit_removed_only_with_our_marker(tmp_path):
    unit = tmp_path / "unit.service"
    unit.write_text("ExecStart=python3 -m cc_autopilot_notify.cli daemon\n")
    settings = _settings(tmp_path, {"hooks": {}})
    migrate.migrate(settings, tmp_path / "cfg.json", unit, run_systemctl=False)
    assert not unit.exists()


def test_foreign_unit_is_kept(tmp_path):
    unit = tmp_path / "unit.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/other\n")
    settings = _settings(tmp_path, {"hooks": {}})
    migrate.migrate(settings, tmp_path / "cfg.json", unit, run_systemctl=False)
    assert unit.exists()


def test_second_run_is_a_no_op(tmp_path):
    settings = _settings(tmp_path, {"hooks": {"Stop": [
        {"matcher": "", "hooks": [_ours()]}]}})
    migrate.migrate(settings, tmp_path / "cfg.json", tmp_path / "u.service",
                    run_systemctl=False)
    first = settings.read_bytes()
    summary = migrate.migrate(settings, tmp_path / "cfg.json",
                              tmp_path / "u.service", run_systemctl=False)
    assert "nothing to migrate" in summary
    assert settings.read_bytes() == first
```

`tests/test_doctor.py` 追加：

```python
def test_doctor_fails_when_legacy_hooks_remain(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"command": "python3 /x/run_hook.py notify"}]}]}}))
    monkeypatch.setenv("CC_AUTOPILOT_SETTINGS", str(settings))
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {})
    assert {c.name: c for c in checks}["legacy install"].status == "FAIL"


def test_doctor_passes_when_settings_have_no_legacy_hooks(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {}}))
    monkeypatch.setenv("CC_AUTOPILOT_SETTINGS", str(settings))
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {})
    assert {c.name: c for c in checks}["legacy install"].status == "PASS"
```

（`tests/test_doctor.py` 顶部需补 `import json`。）

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_migrate.py tests/test_doctor.py -q`
Expected: FAIL（`cc_autopilot_notify.migrate` 不存在；`legacy install` 检查不存在）。

- [ ] **Step 3: 实现**

`cc_autopilot_notify/migrate.py` 的核心（步骤顺序固定，不可调换）：

```python
"""Retire a legacy direct install without touching unrelated entries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class MigrationError(Exception):
    """Abort the whole migration; nothing was written."""


WEBHOOK_ENV = "CC_AUTOPILOT_FEISHU_WEBHOOK"
AFK_KEY = "CLAUDE_AFK_TIMEOUT_MS"


def _read_json(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationError(f"refusing to touch {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MigrationError(f"refusing to touch {path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise MigrationError(f"refusing to touch {path}: not a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _without_our_hooks(data: dict) -> int:
    removed = 0
    hooks = data.get("hooks") or {}
    for event in list(hooks):
        kept = []
        for item in hooks[event] or []:
            inner = item.get("hooks") or []
            if any("run_hook.py" in str(h.get("command", "")) for h in inner):
                removed += 1
                continue
            kept.append(item)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    return removed


def _stop_unit(unit: Path, run_systemctl: bool) -> bool:
    if not unit.exists():
        return False
    try:
        text = unit.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if "cc_autopilot_notify" not in text:
        return False
    if run_systemctl and subprocess.run(
            ["systemctl", "--user", "disable", "--now", unit.name],
            capture_output=True).returncode not in (0,):
        # Not fatal: the unit file still has to go, or it restarts on login.
        print(f"warn: could not stop {unit.name}", file=sys.stderr)
    unit.unlink(missing_ok=True)
    return True


def migrate(settings: Path, config_path: Path, unit: Path,
            *, run_systemctl: bool = True) -> str:
    settings, config_path, unit = Path(settings), Path(config_path), Path(unit)
    data = _read_json(settings)            # aborts before any write
    env = data.get("env") or {}
    legacy_webhook = env.get(WEBHOOK_ENV)
    legacy_afk = env.get(AFK_KEY)

    removed = _without_our_hooks(data)
    prev = data.get("_ccAutopilotPrevious") or {}
    if isinstance(prev, dict):
        for key in (WEBHOOK_ENV, AFK_KEY):
            prev.pop(key, None)
        if prev:
            data["_ccAutopilotPrevious"] = prev
        else:
            data.pop("_ccAutopilotPrevious", None)

    moved = False
    if isinstance(legacy_webhook, str) and legacy_webhook:
        cfg = _read_json(config_path)
        channels = cfg.setdefault("channels", {})
        feishu = channels.setdefault("feishu", {})
        if not feishu.get("webhook"):
            feishu["webhook"] = legacy_webhook
            _write_json(config_path, cfg)
            os.chmod(config_path, 0o600)
            moved = True
        if moved:
            env.pop(WEBHOOK_ENV, None)

    if env:
        data["env"] = env
    else:
        data.pop("env", None)

    if removed or moved:
        backup = settings.with_name(
            f"{settings.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        if settings.exists():
            backup.write_bytes(settings.read_bytes())
        _write_json(settings, data)
    stopped = _stop_unit(unit, run_systemctl)

    parts = []
    if removed:
        parts.append(f"removed {removed} legacy hook entries")
    if moved:
        parts.append(f"moved {WEBHOOK_ENV} into config")
    if stopped:
        parts.append(f"removed {unit}")
    if legacy_afk is not None:
        parts.append(f"kept {AFK_KEY}={legacy_afk}")
    return "; ".join(parts) if parts else "nothing to migrate"


def main(argv: list[str] | None = None) -> int:
    settings = Path(os.environ.get(
        "CC_AUTOPILOT_SETTINGS", Path.home() / ".claude" / "settings.json"))
    config_path = Path(os.environ.get(
        "CC_AUTOPILOT_CONFIG",
        Path.home() / ".config" / "cc-autopilot-notify" / "config.json"))
    unit = Path(os.environ.get(
        "CC_AUTOPILOT_UNIT",
        Path.home() / ".config" / "systemd" / "user" / "cc-autopilot-notify.service"))
    try:
        print(migrate(settings, config_path, unit))
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`scripts/migrate.sh`：

```bash
#!/usr/bin/env bash
# Legacy direct install -> plugin. Explicit; never run automatically.
set -euo pipefail

export PYTHONPATH="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m cc_autopilot_notify.migrate
```

`doctor.py`：把 `_settings_clean` 替换为：

```python
def _legacy_install() -> Check:
    """FAIL while legacy settings.json hooks remain: they double-fire with the plugin."""
    import json
    import os

    path = Path(os.environ.get("CC_AUTOPILOT_SETTINGS")
                or Path.home() / ".claude" / "settings.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return Check("legacy install", "SKIP", f"cannot read {path}")
    except ValueError:
        return Check("legacy install", "FAIL", f"{path} is not valid JSON")
    for items in (data.get("hooks") or {}).values():
        for item in items or []:
            for hook in item.get("hooks") or []:
                if "run_hook.py" in str(hook.get("command", "")):
                    return Check("legacy install", "FAIL",
                                 "legacy hooks in settings.json; run /cc-autopilot-notify:migrate")
    return Check("legacy install", "PASS", "no legacy entries in settings.json")
```

`run_all` 中把 `checks.append(_settings_clean())` 换成 `checks.append(_legacy_install())`。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_migrate.py tests/test_doctor.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add cc_autopilot_notify/migrate.py cc_autopilot_notify/doctor.py \
        scripts/migrate.sh tests/test_migrate.py tests/test_doctor.py
git commit -m "feat: migrate legacy direct installs without touching foreign entries"
```

---

### Task 7: README、脚本可执行位与全量验证

**Files:**
- Modify: `README.md`
- Modify: `scripts/migrate.sh`（可执行位）

**Interfaces:**
- Consumes: Task 5、Task 6 的全部产物。
- Produces: 文档中的安装/迁移/配置步骤与真实文件一致。

- [ ] **Step 1: 更新 README**

把 `## Install` 一节替换为插件安装，并保留 legacy 段：

```markdown
## Install (plugin)

```bash
claude plugin marketplace add mi4646/cc-autopilot-notify
claude plugin install cc-autopilot-notify@cc-autopilot-notify
```

The daemon starts automatically on the next session. Set the Feishu webhook in
`~/.config/cc-autopilot-notify/config.json` (created on first SessionStart with
mode `0600`):

```json
{"channels": {"feishu": {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/..."}}}
```

`CC_AUTOPILOT_FEISHU_WEBHOOK` still works as a fallback.

### Upgrading from the legacy direct install

```bash
/cc-autopilot-notify:migrate
```

It removes only our `run_hook.py` entries from `settings.json`, moves the
webhook into the config, and stops the old systemd unit. Unrelated entries are
kept; a backup is written first.

### Tier C

Plugins cannot write `settings.json`. To keep the AFK fallback, set it yourself:

```json
{"env": {"CLAUDE_AFK_TIMEOUT_MS": 300000}}
```

The legacy section below still works for a non-plugin install.
```

其余章节（Verify、Tests、Design notes）保留；`Verify` 中的命令改为：

```bash
bash scripts/cc-autopilot-notify doctor
bash scripts/cc-autopilot-notify test-channel feishu
```

- [ ] **Step 2: 加可执行位并跑全量测试**

```bash
chmod +x scripts/migrate.sh scripts/ensure-daemon.sh scripts/cc-autopilot-notify
python -m pytest tests/ -q
bash tests/test_install_merge.sh
```

Expected：pytest 全部 PASS（原 95 条 + 本计划新增）；`test_install_merge.sh` 输出 `OK:` 若干行且退出码 0。

- [ ] **Step 3: 验证 plugin 清单可被 CLI 识别（手工，需本机 Claude Code）**

Run: `claude plugin marketplace add /var/www/cc-autopilot-notify && claude plugin list`
Expected: 列表中出现 `cc-autopilot-notify`（若 CLI 要求交互确认，记录输出并跳过，不得伪造 PASS）。

随后在**新会话**中手工确认三项（对应 spec P2–P4）：

1. `bash scripts/cc-autopilot-notify doctor` → `daemon heartbeat` PASS；
2. `pgrep -af "cc_autopilot_notify.cli daemon"` → 只有一行；
3. 触发一次 AskUserQuestion → 收到通知，state 目录出现 pending 记录，移开后再清掉。

WSL 下 `start_new_session=True` 的实际效果以第 2 项为准；若进程数 > 1，记录为失败并停下，不得继续。

- [ ] **Step 4: 提交**

```bash
git add README.md scripts/migrate.sh
git commit -m "docs: document plugin install, migration, and the Tier C caveat"
```
