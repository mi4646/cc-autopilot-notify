# GetLastInputInfo 在 WSL / Claude Code 环境下可用性 spike

- 日期：2026-10-28
- 状态：已完成（通过）
- 关联设计：`docs/superpowers/specs/2026-10-28-idle-aware-autoanswer-design.md` §9
- 探针：`tools/probe_idle.py`

## 结论

**GO。**

1. `GetLastInputInfo` 在交互式 WSL shell、`setsid` 后台进程、`env -i` 最小环境下都能读到当前 Windows 会话（session 1 / Console / `UserInteractive=True`）的空闲时间。
2. 无输入时数值单调增长：单进程锁屏采样从 `0.000` 连续增长到 `63.109`，光标坐标全程停在 `762,810`。
3. 每次启动新的 `powershell.exe` 不会重置空闲：跨进程采样中仍读到 `6.422s` 与 `5.906s`，与新进程启动交替出现。
4. 读数完全跟随真实输入：光标持续移动时，所有采样都是 0～0.5 秒，这是正确行为，不是 API 故障。

## 环境

```
WSL2 / Linux 6.6.114.1-microsoft-standard-WSL2
PowerShell: /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
Windows session: 1 (Console)
UserInteractive: True
```

## 方法与结果

探针命令：

```bash
python3 tools/probe_idle.py --samples <N> --interval <秒> --label <标签>
```

### 1. 交互式 WSL shell

```
{"ok": true, "raw": "4.344|7478937|7483296", "elapsed_s": 0.663, "seconds": 4.344, "sample": 1}
{"ok": true, "raw": "0.015|7485875|7485906", "elapsed_s": 0.601, "seconds": 0.015, "sample": 2}
{"ok": true, "raw": "0.078|7488453|7488531", "elapsed_s": 0.633, "seconds": 0.078, "sample": 3}
```

### 2. 脱离终端（`setsid -w`）

```
{"ok": true, "raw": "0.469|7666906|7667375", "elapsed_s": 0.621, "seconds": 0.469, "sample": 1}
{"ok": true, "raw": "0.266|7669671|7669937", "elapsed_s": 0.567, "seconds": 0.266, "sample": 2}
```

会话检查：`SessionId=1`，`UserInteractive=True`。

### 3. 最小环境变量（`env -i`）

```
{"ok": true, "raw": "0.047|7650953|7651015", "elapsed_s": 0.734, "seconds": 0.047, "sample": 1}
{"ok": true, "raw": "0.172|7653718|7653906", "elapsed_s": 0.908, "seconds": 0.172, "sample": 2}
```

### 4. 为什么大部分采样是 0：真实鼠标输入

同时读取光标坐标：

```
0|idle=0.047|pos=141,159
1|idle=0.000|pos=134,142
2|idle=0.578|pos=93,146
3|idle=0.000|pos=100,142
4|idle=0.000|pos=1519,285
5|idle=0.000|pos=1338,194
6|idle=0.047|pos=1366,464
7|idle=0.312|pos=1170,208
8|idle=0.000|pos=501,337
9|idle=0.094|pos=140,146
```

光标坐标每次都在变，说明这些 0 来自真实操作，不是探测器故障。这一段也是「读数跟随输入」的正向证据。

### 5. 锁屏窗口（决定性证据）

15 次采样 × 6 秒，每次新起一个 PowerShell 进程：

```
sample 12: seconds=0.000
sample 13: seconds=6.422
sample 14: seconds=0.016
sample 15: seconds=5.906
```

第 13、15 次的无输入时长约为一个完整采样间隔，且它们与新进程启动交替出现。结论：

- 无输入时数值增长；
- 进程启动本身不重置；
- 第 14 次的 0.016 是探测前又有输入（锁屏/解锁或其他设备）。

### 5b. 单进程锁屏连续采样（决定性证据）

同一个 PowerShell 进程内每 5 秒采样一次，锁屏期间光标坐标全程不变：

```
0|idle=0.000|pos=831,805
1|idle=3.000|pos=762,810
2|idle=8.000|pos=762,810
3|idle=13.016|pos=762,810
4|idle=18.031|pos=762,810
5|idle=23.047|pos=762,810
6|idle=28.047|pos=762,810
7|idle=33.063|pos=762,810
8|idle=38.063|pos=762,810
9|idle=43.078|pos=762,810
10|idle=48.078|pos=762,810
11|idle=53.094|pos=762,810
12|idle=58.109|pos=762,810
13|idle=63.109|pos=762,810
```

14 次采样连续增长到 **63.109 秒**，增量稳定为 5 秒，与采样间隔完全一致。这是「无输入时会持续增长」的直接证据。

### 6. 故障形态

```
# 错误的 powershell 路径
{"ok": false, "error": "FileNotFoundError: [Errno 2] No such file or directory: '/mnt/c/Windows/System32/does-not-exist.exe'", "elapsed_s": 0.001}

# 超时 1.5 秒
timeout raised after 1.50s
```

失败会明确抛出异常/超时，不会返回一个巨大的秒数被误判为「离开」。

## 探测代价

单次调用耗时 **0.53–0.91 秒**（本次 20+ 次采样的观测范围），略高于设计文档记录的 0.7 秒。设计中的「成功缓存 5 秒」仍然必要。

## 未验证 / 限制

1. **未真正触发 SessionStart hook**。测试环境是 Claude Code 启动的 WSL 子进程，与 hook 同源；`setsid` 与 `env -i` 覆盖了环境差异，但不是一次真实的 hook 调用。实现后应在真实 SessionStart 中复测一次。
2. **输入设备可能持续产生输入**。本次观测到鼠标持续移动；若用户的鼠标/触摸板存在漂移，空闲时间可能长期无法达到 180 秒，自动应答就不会触发。这是现实风险，不是 API 缺陷。
3. 采样是在锁屏/解锁边界附近进行的，精确的锁屏时刻未记录。
4. **锁屏时 `pane.send_key` 是否能真正按下面板，未验证。** 本次只验证了「锁屏时能读到空闲时间」，没有在锁屏状态下让 daemon 实际发送按键。按键走 Pebrel IPC，不是本进程的 `SendInput`；必须按设计文档 §5 验收 5 实测。锁屏不参与判定，但可能影响执行。

## 对设计的影响

- §9 的阻塞项通过，可以进入实现。
- `idle_seconds()` 的 7 天上限、`ERR` 标记、超时 1.5 秒、失败退避都仍然必要。
- 实现后必须在真实 SessionStart 环境再跑一次 `tools/probe_idle.py`，并把结果补到本文档。
- 用户端验收要包含「鼠标漂移导致永不触发」的观察项。

## 复跑

```bash
cd /var/www/cc-autopilot-notify
python3 tools/probe_idle.py --samples 10 --interval 5 --label recheck
```

锁屏期间复跑：先启动 `--samples 15 --interval 6`，再按 Win+L 锁屏至少 60 秒。

本次原始 JSONL 位于 `.gstack/tmp/lockprobe.jsonl`（未入库）；上面的数值可直接用上述命令复现。
