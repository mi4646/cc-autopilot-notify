# cc-autopilot-notify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Claude Code unattended operation — notify the user through Feishu/terminal/Windows when it needs input, and auto-answer its `AskUserQuestion` dialog after a foreground/background idle threshold, preferring the option Claude itself marked as recommended.

**Architecture:** A Python package with two entry paths. Short-lived hook processes (spawned by Claude Code) register a pending question and push notifications, then exit immediately so the dialog stays visible. A long-lived daemon polls Pebrel's runtime TCP protocol; when a pending question sits idle past its threshold, the daemon judges the answer and submits it via `conversation.choose`. Three tiers degrade in order: Pebrel runtime, then a blocking `PermissionRequest` hook, then Claude Code's native AFK.

**Tech Stack:** Python 3.14 (stdlib only, zero third-party runtime deps), pytest 9 for tests, systemd user units for daemon hosting.

**Spec:** `docs/superpowers/specs/2026-10-27-cc-autopilot-notify-design.md`

## Global Constraints

- **Zero third-party runtime dependencies.** Everything outside `tests/` uses the Python standard library only. Rationale from spec §4.4: a hook that fails to import a package fails silently.
- **The runtime token must never be logged, notified, or written to any state file.** It is read from `%APPDATA%\Pebrel\runtime.port` line 2 and used only as a socket request field. (Spec §3.4)
- **Never write non-zero exit from a notification path.** A failed channel logs and continues. (Spec §6)
- **Never modify existing `~/.claude/settings.json` entries.** There are 10 hook events already configured (gstack `question-preference-hook` on `PreToolUse`, `pebrel-hook` on `PermissionRequest`) plus 6 existing `env` keys. All writes merge; none replace. (Spec §9)
- **Foreground threshold 150 seconds; background threshold 90 seconds.** Values are config-overridable; these are the defaults. (Spec §1.2 S4/S5)
- **Native AFK fallback is set to 300000 ms.** It must always exceed our own thresholds. (Spec §4.2)
- **On any judge failure, fall back to option index 0 and say so in the notification.** Never fail silently. (Spec §4.3)
- **CC version under test: 2.1.308.** Record it wherever behavior is asserted.

## Review Focus

These are the input classes and failure modes the spec implies but no task's own tests fully exercise. Each line names the input and the behavior a reasonable person expects; the owning task carries the test that pins it.

1. **Pebrel returns a `prompt` shape we have never observed.** Every `conversation.read` we ran returned `prompt: null` (two live panes, both mid-turn). If a live question returns a shape our parser does not expect, we must fail loudly to a notification rather than submit a wrong option. Owned by Task 3.
2. **The user answers in the dialog between our `read` and our `choose`.** Pebrel answers `prompt_changed`. We must re-read once and never retry blindly, or we double-answer. Owned by Task 7.
3. **`conversation.read` reports a stale `revision` for a pane whose dialog is open but untouched.** If we treat an unchanged revision as "user is active", we never fire; if we ignore revision entirely, we fire while the user is mid-keystroke. Owned by Task 7.
4. **Claude Code discards our `updatedInput.answers` on 2.1.308.** The spec's step 0. If this fails, path B must report unavailable and the system must still work through path A. Owned by Task 11, with the `doctor` check added there.
5. **The Feishu webhook returns 200 but rejects the payload** (Feishu returns HTTP 200 with a non-zero `code` in the body for some failures). Treating HTTP status alone as success means we think we notified and the user never sees it. Owned by Task 5.

---

## File Structure

```
cc-autopilot-notify/
├── cc_autopilot_notify/
│   ├── __init__.py          # version, package marker
│   ├── config.py            # load/validate config.json, defaults, env credential lookup
│   ├── state.py             # pending-question records, JSON + flock, per-session keys
│   ├── pebrel_runtime.py    # TCP client: describe / conversation.read / conversation.choose
│   ├── presence.py          # foreground detection via powershell GetForegroundWindow
│   ├── judge.py             # "(Recommended)" marker extraction + model fallback
│   ├── channels/
│   │   ├── __init__.py      # dispatch to enabled channels, isolate failures
│   │   ├── feishu.py        # interactive card, body-code aware
│   │   ├── terminal.py      # OSC 9
│   │   └── pebrel.py        # delegate to pebrel native notification
│   ├── hook_pre_tool.py     # PreToolUse(AskUserQuestion): register + notify + exit 0
│   ├── hook_permission.py   # PermissionRequest(AskUserQuestion): path B blocking answer
│   ├── hook_notify.py       # Notification/Stop/StopFailure: result notifications
│   ├── daemon.py            # poll loop, threshold logic, heartbeat
│   ├── doctor.py            # self-check command
│   └── cli.py               # argparse entry: doctor / test-channel / daemon
├── tests/
│   ├── test_config.py
│   ├── test_state.py
│   ├── test_judge.py
│   ├── test_pebrel_runtime.py
│   └── test_daemon.py
├── install.sh
├── uninstall.sh
├── pyproject.toml
└── README.md
```

Each file owns one responsibility. `state.py` is the only module that writes to disk; `pebrel_runtime.py` is the only module that opens a socket; `presence.py` is the only module that shells out to Windows.

---

### Task 1: Package skeleton, config, and defaults

**Files:**
- Create: `pyproject.toml`
- Create: `cc_autopilot_notify/__init__.py`
- Create: `cc_autopilot_notify/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `config.DEFAULTS: dict` — the full default config tree
  - `config.load(path: Path | None = None) -> dict` — merged config; missing file yields `DEFAULTS`
  - `config.foreground_seconds(cfg: dict) -> int` — default 150
  - `config.background_seconds(cfg: dict) -> int` — default 90
  - `config.feishu_webhook(cfg: dict) -> str | None` — reads the env var named by `cfg["channels"]["feishu"]["webhook_env"]`
  - `config.judge_model(cfg: dict) -> str | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import json
from pathlib import Path

from cc_autopilot_notify import config


def test_missing_file_yields_defaults(tmp_path):
    cfg = config.load(tmp_path / "absent.json")
    assert config.foreground_seconds(cfg) == 150
    assert config.background_seconds(cfg) == 90


def test_partial_file_merges_over_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"thresholds": {"foreground_seconds": 30}}))
    cfg = config.load(p)
    assert config.foreground_seconds(cfg) == 30
    assert config.background_seconds(cfg) == 90


def test_corrupt_json_falls_back_to_defaults(tmp_path, caplog):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    cfg = config.load(p)
    assert config.foreground_seconds(cfg) == 150
    assert any("config" in r.message.lower() for r in caplog.records)


def test_invalid_threshold_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"thresholds": {"foreground_seconds": "soon"}}))
    try:
        config.load(p)
    except config.ConfigError:
        return
    raise AssertionError("expected ConfigError")


def test_webhook_reads_named_env(monkeypatch):
    cfg = config.DEFAULTS
    monkeypatch.setenv("CC_AUTOPILOT_FEISHU_WEBHOOK", "https://example.test/x")
    assert config.feishu_webhook(cfg) == "https://example.test/x"


def test_webhook_absent_env_is_none(monkeypatch):
    monkeypatch.delenv("CC_AUTOPILOT_FEISHU_WEBHOOK", raising=False)
    assert config.feishu_webhook(config.DEFAULTS) is None


def test_judge_model_defaults_to_none():
    assert config.judge_model(config.DEFAULTS) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cc_autopilot_notify'`

- [ ] **Step 3: Implement `config.py`**

Write `pyproject.toml` declaring the package with `requires-python = ">=3.11"` and no `dependencies` key. In `config.py`, define `ConfigError(Exception)`, `DEFAULTS` matching spec §5 exactly (including `"judge": {"model": None, ...}` and `"channels": {"pebrel": {"enabled": True}}`), a `_merge(base, over)` that recurses into dicts, and `load()` which validates thresholds are positive ints and raises `ConfigError` otherwise. Corruption is caught, logged via `logging.getLogger(__name__).warning`, and returns `DEFAULTS`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml cc_autopilot_notify/__init__.py cc_autopilot_notify/config.py tests/test_config.py
git commit -m "feat: package skeleton and config loading"
```

---

### Task 2: Pending-question state store

**Files:**
- Create: `cc_autopilot_notify/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `config.foreground_seconds`, `config.background_seconds`
- Produces:
  - `state.Pending` dataclass: `session_id: str`, `window_id: int`, `pane_id: int`, `questions: list[dict]`, `appeared_at: float`, `last_activity: float`, `revision: str | None = None`, `choose_failures: int = 0`
  - `state.record(pending: Pending, root: Path) -> None` — atomic write under `root`
  - `state.load_all(root: Path) -> list[Pending]`
  - `state.forget(session_id: str, root: Path) -> None`
  - `state.touch(session_id: str, root: Path, when: float, revision: str | None = None) -> None`
  - `state.bump_choose_failures(session_id: str, root: Path) -> int`
  - `state.write_heartbeat(root: Path, when: float) -> None`
  - `state.heartbeat_age(root: Path, now: float) -> float | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state.py
import json
import time

from cc_autopilot_notify import state


def _pending(sid="s1", pane=3):
    return state.Pending(
        session_id=sid, window_id=1, pane_id=pane,
        questions=[{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}],
        appeared_at=1000.0, last_activity=1000.0,
    )


def test_roundtrip(tmp_path):
    state.record(_pending(), tmp_path)
    got = state.load_all(tmp_path)
    assert len(got) == 1
    assert got[0].session_id == "s1"
    assert got[0].pane_id == 3
    assert got[0].last_activity == 1000.0


def test_two_sessions_are_independent(tmp_path):
    state.record(_pending("s1", 3), tmp_path)
    state.record(_pending("s2", 4), tmp_path)
    got = {p.session_id: p for p in state.load_all(tmp_path)}
    assert got["s1"].pane_id == 3
    assert got["s2"].pane_id == 4


def test_forget_removes_only_one(tmp_path):
    state.record(_pending("s1"), tmp_path)
    state.record(_pending("s2"), tmp_path)
    state.forget("s1", tmp_path)
    assert [p.session_id for p in state.load_all(tmp_path)] == ["s2"]


def test_touch_updates_last_activity(tmp_path):
    state.record(_pending(), tmp_path)
    state.touch("s1", tmp_path, when=2000.0, revision="r9")
    got = state.load_all(tmp_path)[0]
    assert got.last_activity == 2000.0
    assert got.revision == "r9"


def test_bump_counts(tmp_path):
    state.record(_pending(), tmp_path)
    assert state.bump_choose_failures("s1", tmp_path) == 1
    assert state.bump_choose_failures("s1", tmp_path) == 2


def test_corrupt_record_is_skipped_not_fatal(tmp_path):
    (tmp_path / "pending_s1.json").write_text("{broken")
    state.record(_pending("s2"), tmp_path)
    assert [p.session_id for p in state.load_all(tmp_path)] == ["s2"]


def test_heartbeat_roundtrip(tmp_path):
    state.write_heartbeat(tmp_path, when=500.0)
    assert state.heartbeat_age(tmp_path, now=560.0) == 60.0


def test_heartbeat_missing_is_none(tmp_path):
    assert state.heartbeat_age(tmp_path, now=1.0) is None


def test_session_id_is_sanitised_for_filenames(tmp_path):
    state.record(_pending("../../etc/passwd"), tmp_path)
    names = [p.name for p in tmp_path.glob("pending_*")]
    assert all("/" not in n for n in names)
    assert len(state.load_all(tmp_path)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_state.py -v`
Expected: FAIL — `ImportError: cannot import name 'state'`

- [ ] **Step 3: Implement `state.py`**

One JSON file per session at `root/pending_<sanitised-session>.json`. Sanitise by replacing any character outside `[A-Za-z0-9_.-]` with `_` and truncating to 96 chars — session ids come from Claude Code and must not be trusted as path components. Writes go through `tempfile.NamedTemporaryFile` in the same directory then `os.replace`, under `fcntl.flock` on a `root/.lock` file. `load_all` globs `pending_*.json` and skips any file that fails to parse, logging a warning.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_state.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add cc_autopilot_notify/state.py tests/test_state.py
git commit -m "feat: pending-question state store with per-session isolation"
```

---

### Task 3: Pebrel runtime client

**Files:**
- Create: `cc_autopilot_notify/pebrel_runtime.py`
- Test: `tests/test_pebrel_runtime.py`

**Interfaces:**
- Produces:
  - `pebrel_runtime.RuntimeError(Exception)`
  - `pebrel_runtime.endpoint(appdata: Path) -> tuple[int, str]` — reads `Pebrel/runtime.port`
  - `pebrel_runtime.describe(appdata: Path) -> dict` — the `result` object, raises `RuntimeError` on transport failure
  - `pebrel_runtime.read(appdata: Path, window_id: int, pane_id: int, session_id: str) -> dict`
  - `pebrel_runtime.choose(appdata: Path, window_id: int, pane_id: int, session_id: str, prompt_id: str, option: int) -> dict`
  - `pebrel_runtime.supports_conversation(desc: dict) -> bool`
  - `pebrel_runtime.question_options(prompt: dict) -> list[dict]` — normalises the unobserved prompt shape into `[{"label": str, "description": str}]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pebrel_runtime.py
import json

import pytest

from cc_autopilot_notify import pebrel_runtime


def _fake_appdata(tmp_path, port, token="a" * 32):
    d = tmp_path / "Pebrel"
    d.mkdir()
    (d / "runtime.port").write_text(f"{port} {token} 1\n")
    return d


def test_endpoint_parses_port_and_token(tmp_path):
    appdata = _fake_appdata(tmp_path, 54345)
    port, token = pebrel_runtime.endpoint(appdata)
    assert port == 54345
    assert token == "a" * 32


def test_endpoint_missing_raises(tmp_path):
    with pytest.raises(pebrel_runtime.RuntimeError):
        pebrel_runtime.endpoint(tmp_path)


def test_supports_conversation_true():
    desc = {"capabilities": ["conversation.read", "conversation.choose"]}
    assert pebrel_runtime.supports_conversation(desc) is True


def test_supports_conversation_false_when_choose_missing():
    desc = {"capabilities": ["conversation.read"]}
    assert pebrel_runtime.supports_conversation(desc) is False


def test_question_options_handles_null_prompt():
    assert pebrel_runtime.question_options(None) == []


def test_question_options_reads_labels_and_descriptions():
    prompt = {"options": [{"label": "A", "description": "first"},
                          {"label": "B", "description": "second"}]}
    assert pebrel_runtime.question_options(prompt) == [
        {"label": "A", "description": "first"},
        {"label": "B", "description": "second"},
    ]


def test_question_options_unknown_shape_raises():
    """Unobserved prompt shapes must fail loudly, never yield a wrong answer."""
    with pytest.raises(pebrel_runtime.RuntimeError):
        pebrel_runtime.question_options({"mystery": "payload"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pebrel_runtime.py -v`
Expected: FAIL — `ImportError: cannot import name 'pebrel_runtime'`

- [ ] **Step 3: Implement `pebrel_runtime.py`**

JSON Lines over `socket.create_connection(("127.0.0.1", port), timeout=5)`; send one line, read until `\n`, parse. A single `_call(method, params)` helper builds `{"protocol": "nebula.runtime", "version": 1, "id": method, "token": token, "method": method, "params": params}`. `question_options` accepts a prompt whose `options` is a list of dicts carrying `label`; anything else raises `RuntimeError` with the observed keys in the message. **Never put the token in the exception message.**

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pebrel_runtime.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Verify against the live runtime (bounded probe)**

Run: `python -c "from pathlib import Path; from cc_autopilot_notify import pebrel_runtime as p; d=p.describe(Path('/mnt/c/Users/Anonymous/AppData/Roaming')); print(p.supports_conversation(d))"`
Expected: `True`. If `False`, record the observed capability list in the commit message — path A is unavailable on this Pebrel build.

- [ ] **Step 6: Commit**

```bash
git add cc_autopilot_notify/pebrel_runtime.py tests/test_pebrel_runtime.py
git commit -m "feat: Pebrel runtime TCP client"
```

---

### Task 4: Presence — foreground detection

**Files:**
- Create: `cc_autopilot_notify/presence.py`
- Test: `tests/test_presence.py`

**Interfaces:**
- Produces:
  - `presence.is_foreground(powershell: str | None = None, timeout: float = 2.0) -> bool | None` — `True` foreground, `False` background, `None` undeterminable
  - `presence.PROCESS_NAME = "pebrel"`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_presence.py
from cc_autopilot_notify import presence


def test_parses_foreground_match(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "pebrel\n")
    assert presence.is_foreground() is True


def test_parses_foreground_mismatch(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "chrome\n")
    assert presence.is_foreground() is False


def test_empty_output_is_undeterminable(monkeypatch):
    monkeypatch.setattr(presence, "_run_ps", lambda *a, **k: "")
    assert presence.is_foreground() is None


def test_powershell_failure_is_undeterminable(monkeypatch):
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(presence, "_run_ps", boom)
    assert presence.is_foreground() is None


def test_timeout_is_undeterminable(monkeypatch):
    import subprocess

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=2)
    monkeypatch.setattr(presence, "_run_ps", slow)
    assert presence.is_foreground() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_presence.py -v`
Expected: FAIL — `ImportError: cannot import name 'presence'`

- [ ] **Step 3: Implement `presence.py`**

`_run_ps(script, timeout)` shells out to `powershell.exe` via `subprocess.run(..., capture_output=True, text=True, timeout=timeout)`; the caller-facing helper runs a script that prints the foreground process name, lowercased. Default powershell path is `/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe`. `is_foreground` returns `None` for empty output, `OSError`, or `TimeoutExpired` — the daemon then applies the background threshold, per spec §6.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_presence.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add cc_autopilot_notify/presence.py tests/test_presence.py
git commit -m "feat: foreground detection via powershell"
```

---

### Task 5: Notification channels

**Files:**
- Create: `cc_autopilot_notify/channels/__init__.py`
- Create: `cc_autopilot_notify/channels/feishu.py`
- Create: `cc_autopilot_notify/channels/terminal.py`
- Create: `cc_autopilot_notify/channels/pebrel.py`
- Test: `tests/test_channels.py`

**Interfaces:**
- Consumes: `config.feishu_webhook`
- Produces:
  - `channels.Notification` dataclass: `title: str`, `body: str`, `project: str`, `session_id: str`, `event: str`
  - `channels.send_all(n: Notification, cfg: dict) -> list[str]` — returns names of channels that failed
  - `channels.feishu.send(n, webhook, timeout=10) -> None` — raises on failure
  - `channels.feishu.check_response(status: int, body: str) -> None` — raises `FeishuError` unless Feishu's own `code` is 0
  - `channels.terminal.send(n) -> None` — writes OSC 9 to stdout
  - `channels.pebrel.send(n) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_channels.py
import json

import pytest

from cc_autopilot_notify import channels


def _n():
    return channels.Notification(title="Needs input", body="Pick one",
                                 project="demo", session_id="s1", event="AskUserQuestion")


def test_feishu_rejects_http_200_with_error_code():
    """Feishu returns 200 with a non-zero code on some failures."""
    body = json.dumps({"code": 19024, "msg": "key not found"})
    with pytest.raises(channels.feishu.FeishuError):
        channels.feishu.check_response(200, body)


def test_feishu_accepts_code_zero():
    channels.feishu.check_response(200, json.dumps({"code": 0}))


def test_feishu_rejects_non_200():
    with pytest.raises(channels.feishu.FeishuError):
        channels.feishu.check_response(500, "boom")


def test_terminal_emits_osc9(monkeypatch, capsys):
    monkeypatch.setenv("TERM", "xterm-256color")
    channels.terminal.send(_n())
    out = capsys.readouterr().out
    assert "\x1b]9;" in out
    assert "Needs input" in out


def test_terminal_silent_without_tty(monkeypatch, capsys):
    monkeypatch.setenv("TERM", "dumb")
    channels.terminal.send(_n())
    assert "\x1b]9;" not in capsys.readouterr().out


def test_send_all_reports_failures_without_raising(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(channels.feishu, "send", boom)
    monkeypatch.setattr(channels.terminal, "send", lambda n: None)
    cfg = {"channels": {"feishu": {"enabled": True, "webhook_env": "W"},
                        "terminal": {"enabled": True}, "pebrel": {"enabled": False}}}
    monkeypatch.setenv("W", "https://example.test/h")
    assert channels.send_all(_n(), cfg) == ["feishu"]


def test_disabled_channel_is_not_called(monkeypatch):
    called = []
    monkeypatch.setattr(channels.terminal, "send", lambda n: called.append("t"))
    cfg = {"channels": {"feishu": {"enabled": False}, "terminal": {"enabled": False},
                        "pebrel": {"enabled": False}}}
    assert channels.send_all(_n(), cfg) == []
    assert called == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_channels.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cc_autopilot_notify.channels'`

- [ ] **Step 3: Implement the channels**

`feishu.py` posts an interactive card with `urllib.request`; `check_response` parses the JSON body and raises `FeishuError` when `code != 0`, **regardless of HTTP status** — this is the Review Focus item 5. `terminal.py` writes `\x1b]9;<title>: <body>\x07` to stdout and no-ops when `TERM` is `dumb` or unset. `pebrel.py` shells `pebrel notify-test`-equivalent via the runtime when available, else no-ops. `send_all` iterates enabled channels in a fixed order, catches every exception, and returns the failed names.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_channels.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add cc_autopilot_notify/channels tests/test_channels.py
git commit -m "feat: notification channels with Feishu body-code checking"
```

---

### Task 6: Answer judge

**Files:**
- Create: `cc_autopilot_notify/judge.py`
- Test: `tests/test_judge.py`

**Interfaces:**
- Consumes: `config.judge_model`
- Produces:
  - `judge.Decision` dataclass: `index: int`, `label: str`, `reason: str`, `fallback: bool`
  - `judge.pick(question: dict, model: str | None, call_model=None) -> Decision`
  - `judge.marker_index(question: dict) -> int | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_judge.py
from cc_autopilot_notify import judge


def _q(options, question="Which?"):
    return {"question": question, "options": options}


def test_english_recommended_suffix():
    q = _q([{"label": "Alpha"}, {"label": "Beta (Recommended)"}])
    assert judge.marker_index(q) == 1


def test_chinese_recommended_suffix():
    q = _q([{"label": "甲"}, {"label": "乙（推荐）"}])
    assert judge.marker_index(q) == 1


def test_description_marker():
    q = _q([{"label": "Alpha", "description": "recommended for speed"},
            {"label": "Beta", "description": "slower"}])
    assert judge.marker_index(q) == 0


def test_no_marker_returns_none():
    assert judge.marker_index(_q([{"label": "A"}, {"label": "B"}])) is None


def test_ambiguous_markers_return_none():
    q = _q([{"label": "A (Recommended)"}, {"label": "B (Recommended)"}])
    assert judge.marker_index(q) is None


def test_marker_wins_without_calling_model():
    calls = []
    q = _q([{"label": "A"}, {"label": "B (Recommended)"}])
    d = judge.pick(q, model="m", call_model=lambda *a: calls.append(a) or 0)
    assert (d.index, d.fallback) == (1, False)
    assert calls == []


def test_model_used_when_no_marker():
    q = _q([{"label": "A"}, {"label": "B"}])
    d = judge.pick(q, model="m", call_model=lambda qq, mm: 1)
    assert (d.index, d.fallback) == (1, False)


def test_model_failure_falls_back_to_first():
    q = _q([{"label": "A"}, {"label": "B"}])
    def boom(*a):
        raise RuntimeError("gateway down")
    d = judge.pick(q, model="m", call_model=boom)
    assert (d.index, d.fallback) == (0, True)


def test_no_model_falls_back_to_first():
    q = _q([{"label": "A"}, {"label": "B"}])
    d = judge.pick(q, model=None)
    assert (d.index, d.fallback) == (0, True)


def test_model_index_out_of_range_falls_back():
    q = _q([{"label": "A"}, {"label": "B"}])
    d = judge.pick(q, model="m", call_model=lambda qq, mm: 7)
    assert (d.index, d.fallback) == (0, True)


def test_empty_options_raises():
    try:
        judge.pick(_q([]), model=None)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_judge.py -v`
Expected: FAIL — `ImportError: cannot import name 'judge'`

- [ ] **Step 3: Implement `judge.py`**

`marker_index` lowercases labels for matching, accepts `(recommended)` and `（推荐）` at the end of a label, and `recommended` inside a description. Returns `None` when zero or more than one option matches. The default `call_model` posts to `<ANTHROPIC_BASE_URL>/v1/messages` with `Authorization: Bearer <ANTHROPIC_AUTH_TOKEN>`, asks for a bare integer, has an 8 s timeout and one retry; it reads credentials exclusively from those two env vars (spec §4.3 corrected text) — `ANTHROPIC_API_KEY` is not consulted. Any exception, out-of-range index, or absent model yields `Decision(index=0, fallback=True)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_judge.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add cc_autopilot_notify/judge.py tests/test_judge.py
git commit -m "feat: answer judge with recommendation marker and model fallback"
```

---

### Task 7: Daemon poll loop

**Files:**
- Create: `cc_autopilot_notify/daemon.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `state`, `pebrel_runtime`, `presence`, `judge`, `config`
- Produces:
  - `daemon.tick(now: float, root: Path, cfg: dict, deps: Deps) -> list[str]` — one iteration; returns action log lines
  - `daemon.Deps` dataclass: `read`, `choose`, `foreground`, `judge_fn`, `notify` — all injectable callables, so tests never touch the network
  - `daemon.should_fire(pending, now, threshold) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon.py
import time

from cc_autopilot_notify import daemon, state


def _deps(revision="r1", foreground=True, choose_result=None, judge_index=1, choose_calls=None):
    calls = choose_calls if choose_calls is not None else []
    return daemon.Deps(
        read=lambda p: {"revision": revision, "prompt": {"options": [
            {"label": "A"}, {"label": "B"}]}, "identity": {"epoch": 1}},
        choose=lambda p, prompt_id, option: calls.append((option, prompt_id)) or (
            choose_result if choose_result is not None else {"ok": True}),
        foreground=lambda: foreground,
        judge_fn=lambda q: judge_index,
        notify=lambda msg: None,
    )


def _seed(tmp_path, appeared, last_activity=None):
    p = state.Pending(session_id="s1", window_id=1, pane_id=2,
                      questions=[{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}],
                      appeared_at=appeared, last_activity=last_activity or appeared)
    state.record(p, tmp_path)
    return p


def test_does_not_fire_before_foreground_threshold(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    assert daemon.tick(1000.0 + 149, tmp_path, {"thresholds": {"foreground_seconds": 150,
                                                              "background_seconds": 90}},
                       _deps(foreground=True)) == []


def test_fires_at_foreground_threshold(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    calls = []
    daemon.tick(1000.0 + 150, tmp_path, {"thresholds": {"foreground_seconds": 150,
                                                        "background_seconds": 90}},
                _deps(foreground=True, choose_calls=calls))
    assert calls and calls[0][0] == 1


def test_background_threshold_is_used_when_not_foreground(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    calls = []
    daemon.tick(1000.0 + 90, tmp_path, {"thresholds": {"foreground_seconds": 150,
                                                       "background_seconds": 90}},
                _deps(foreground=False, choose_calls=calls))
    assert len(calls) == 1


def test_undeterminable_foreground_uses_background_threshold(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    calls = []
    daemon.tick(1000.0 + 90, tmp_path, {"thresholds": {"foreground_seconds": 150,
                                                       "background_seconds": 90}},
                _deps(foreground=None, choose_calls=calls))
    assert len(calls) == 1


def test_revision_change_resets_activity(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    deps = _deps(revision="r2")
    daemon.tick(1000.0 + 200, tmp_path, {"thresholds": {"foreground_seconds": 150,
                                                        "background_seconds": 90}}, deps)
    got = state.load_all(tmp_path)[0]
    assert got.last_activity > 1000.0


def test_prompt_null_means_answered_and_record_is_cleared(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    deps = _deps()
    deps.read = lambda p: {"revision": "r", "prompt": None, "identity": {"epoch": 1}}
    daemon.tick(1000.0 + 500, tmp_path, {"thresholds": {"foreground_seconds": 150,
                                                        "background_seconds": 90}}, deps)
    assert state.load_all(tmp_path) == []


def test_prompt_changed_is_retried_once(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    calls = []
    deps = _deps(choose_result={"ok": False, "error": {"code": "prompt_changed"}},
                 choose_calls=calls)
    daemon.tick(1000.0 + 200, tmp_path, {"thresholds": {"foreground_seconds": 150,
                                                        "background_seconds": 90}}, deps)
    assert len(calls) == 2


def test_two_choose_failures_drop_record(tmp_path):
    _seed(tmp_path, appeared=1000.0)
    deps = _deps(choose_result={"ok": False, "error": {"code": "prompt_changed"}})
    cfg = {"thresholds": {"foreground_seconds": 150, "background_seconds": 90}}
    daemon.tick(1000.0 + 200, tmp_path, cfg, deps)
    daemon.tick(1000.0 + 260, tmp_path, cfg, deps)
    assert state.load_all(tmp_path) == []


def test_tick_writes_heartbeat(tmp_path):
    daemon.tick(1234.0, tmp_path, {"thresholds": {"foreground_seconds": 150,
                                                  "background_seconds": 90}}, _deps())
    assert state.heartbeat_age(tmp_path, now=1234.0) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_daemon.py -v`
Expected: FAIL — `ImportError: cannot import name 'daemon'`

- [ ] **Step 3: Implement `daemon.py`**

`tick` writes a heartbeat first, then for each pending record: call `deps.read`; if it raises, log and skip (no state change); if `prompt` is `None`, `state.forget`; if `revision` differs from the stored one, `state.touch(now)` and continue; otherwise compute `idle = now - last_activity`, choose threshold from `deps.foreground()` (`True` → foreground, `False`/`None` → background), and if `idle >= threshold`, call `deps.judge_fn`, then `deps.choose`. On `prompt_changed`, re-read once and retry with the fresh `prompt_id`; on a second failure call `state.bump_choose_failures` and `state.forget` when the count reaches 2. Store the observed `revision` on the record so the next tick can compare.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_daemon.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add cc_autopilot_notify/daemon.py tests/test_daemon.py
git commit -m "feat: daemon tick with dual thresholds and prompt-changed retry"
```

---

### Task 8: Hooks — PreToolUse, Notification, CLI

**Files:**
- Create: `cc_autopilot_notify/hook_pre_tool.py`
- Create: `cc_autopilot_notify/hook_notify.py`
- Create: `cc_autopilot_notify/cli.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `state`, `channels`, `config`
- Produces:
  - `hook_pre_tool.handle(event: dict, root: Path, cfg: dict) -> int` — always returns 0
  - `hook_notify.handle(event: dict, root: Path, cfg: dict) -> int` — always returns 0
  - `cli.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hooks.py
import json

from cc_autopilot_notify import hook_pre_tool, hook_notify, state


def _auq_event(pane=2):
    return {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
            "session_id": "s1", "cwd": "/var/www/demo",
            "tool_input": {"questions": [{"question": "Q",
                                          "options": [{"label": "A"}, {"label": "B"}]}]}}


def test_registers_pending(tmp_path):
    rc = hook_pre_tool.handle(_auq_event(), tmp_path, {"channels": {}, "thresholds": {}})
    assert rc == 0
    pend = state.load_all(tmp_path)
    assert len(pend) == 1 and pend[0].session_id == "s1"


def test_ignores_non_auq_tools(tmp_path):
    ev = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "s1"}
    assert hook_pre_tool.handle(ev, tmp_path, {"channels": {}, "thresholds": {}}) == 0
    assert state.load_all(tmp_path) == []


def test_malformed_event_returns_zero(tmp_path):
    assert hook_pre_tool.handle({}, tmp_path, {"channels": {}, "thresholds": {}}) == 0


def test_notification_channel_failure_still_returns_zero(tmp_path, monkeypatch):
    import cc_autopilot_notify.channels as channels
    monkeypatch.setattr(channels, "send_all", lambda n, c: ["feishu"])
    cfg = {"channels": {"terminal": {"enabled": True}}, "thresholds": {}}
    assert hook_pre_tool.handle(_auq_event(), tmp_path, cfg) == 0


def test_stop_event_notifies(tmp_path, monkeypatch):
    import cc_autopilot_notify.channels as channels
    seen = []
    monkeypatch.setattr(channels, "send_all", lambda n, c: seen.append(n.title) or [])
    ev = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/var/www/demo"}
    assert hook_notify.handle(ev, tmp_path, {"channels": {}}) == 0
    assert seen


def test_post_tool_use_clears_pending(tmp_path):
    hook_pre_tool.handle(_auq_event(), tmp_path, {"channels": {}, "thresholds": {}})
    ev = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
          "session_id": "s1"}
    assert hook_notify.handle(ev, tmp_path, {"channels": {}}) == 0
    assert state.load_all(tmp_path) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: FAIL — `ImportError: cannot import name 'hook_pre_tool'`

- [ ] **Step 3: Implement the hooks and CLI**

Both hook modules read JSON from stdin, call their `handle`, and `sys.exit(0)` unconditionally — a hook that exits non-zero blocks Claude Code, which spec §6 forbids on notification paths. `hook_pre_tool` only acts when `tool_name == "AskUserQuestion"`, builds a `Pending` with `appeared_at = time.time()`, then notifies. `hook_notify` dispatches `Stop`/`StopFailure`/`Notification` to notifications and `PostToolUse` with `tool_name == "AskUserQuestion"` to `state.forget`. `cli.main` provides `doctor`, `test-channel <name>`, and `daemon` subcommands.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add cc_autopilot_notify/hook_pre_tool.py cc_autopilot_notify/hook_notify.py cc_autopilot_notify/cli.py tests/test_hooks.py
git commit -m "feat: PreToolUse and notification hooks, CLI entry"
```

---

### Task 9: Path B hook and doctor

**Files:**
- Create: `cc_autopilot_notify/hook_permission.py`
- Create: `cc_autopilot_notify/doctor.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `pebrel_runtime`, `presence`, `state`, `config`
- Produces:
  - `hook_permission.answer(event: dict, wait_seconds: float, judge_fn) -> dict | None` — the hook payload, or `None` to decline
  - `doctor.Check` dataclass: `name: str`, `status: str` (`PASS`/`FAIL`/`SKIP`), `detail: str`
  - `doctor.run_all(root: Path, cfg: dict, probe=None) -> list[Check]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor.py
import time

from cc_autopilot_notify import doctor, hook_permission, state


def _auq():
    return {"hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion",
            "session_id": "s1",
            "tool_input": {"questions": [{"question": "Q",
                                          "options": [{"label": "A"}, {"label": "B (Recommended)"}]}]}}


def test_answer_returns_hook_shape():
    out = hook_permission.answer(_auq(), wait_seconds=0, judge_fn=lambda q: 1)
    assert out["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    answers = out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]
    assert answers["Q"] == "B (Recommended)"


def test_answer_ignores_other_tools():
    ev = dict(_auq(), tool_name="Bash")
    assert hook_permission.answer(ev, wait_seconds=0, judge_fn=lambda q: 0) is None


def test_answer_multiselect_joins_labels():
    ev = _auq()
    ev["tool_input"]["questions"][0]["multiSelect"] = True
    out = hook_permission.answer(ev, wait_seconds=0, judge_fn=lambda q: 0)
    assert "," in out["hookSpecificOutput"]["decision"]["updatedInput"]["answers"]["Q"]


def test_doctor_reports_missing_heartbeat_as_fail(tmp_path):
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {"ok": False})
    by = {c.name: c for c in checks}
    assert by["daemon heartbeat"].status == "FAIL"


def test_doctor_reports_fresh_heartbeat_as_pass(tmp_path):
    state.write_heartbeat(tmp_path, when=time.time())
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: {"ok": False})
    assert {c.name: c for c in checks}["daemon heartbeat"].status == "PASS"


def test_doctor_passes_runtime_check_when_capabilities_present(tmp_path):
    desc = {"protocol_version": 1, "capabilities": ["conversation.read", "conversation.choose"]}
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: desc)
    by = {c.name: c for c in checks}
    assert by["pebrel runtime"]["status"] == "PASS"
    assert by["pebrel conversation capabilities"]["status"] == "PASS"


def test_doctor_fails_on_wrong_protocol_version(tmp_path):
    desc = {"protocol_version": 2, "capabilities": ["conversation.read", "conversation.choose"]}
    checks = doctor.run_all(tmp_path, {"channels": {}}, probe=lambda: desc)
    assert {c.name: c for c in checks}["pebrel protocol version"].status == "FAIL"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_doctor.py -v`
Expected: FAIL — `ImportError: cannot import name 'doctor'`

- [ ] **Step 3: Implement `hook_permission.py` and `doctor.py`**

`hook_permission.answer` returns `None` for any tool other than `AskUserQuestion`. Otherwise it builds `answers` keyed by question text, using `judge_fn(question)` for the index; `multiSelect` joins labels with `", "`. It sleeps `wait_seconds` **before** judging (the wait is what lets a human win). `doctor.run_all` returns one `Check` per row in spec §7 step 1: CC version, runtime reachable, protocol version, conversation capabilities, PowerShell, Feishu (SKIP when unconfigured), daemon heartbeat (PASS when age under 60 s), and uninstall residue. `probe` is an injectable replacement for `pebrel_runtime.describe` so tests never open a socket.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_doctor.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass, 0 failures

- [ ] **Step 6: Commit**

```bash
git add cc_autopilot_notify/hook_permission.py cc_autopilot_notify/doctor.py tests/test_doctor.py
git commit -m "feat: path B permission hook and doctor self-check"
```

---

### Task 10: Install and uninstall scripts

**Files:**
- Create: `install.sh`
- Create: `uninstall.sh`
- Create: `README.md`

**Interfaces:**
- Consumes: everything above
- Produces: no Python interface; the scripts merge into `~/.claude/settings.json` and install a systemd user unit

- [ ] **Step 1: Write the failing test**

```bash
# tests/test_install_merge.sh — run directly, no pytest
set -euo pipefail
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/settings.json" <<'JSON'
{"env": {"KEEP_ME": "1"}, "hooks": {"PreToolUse": [{"matcher": "x", "hooks": [{"type": "command", "command": "existing"}]}]}}
JSON
CC_AUTOPILOT_SETTINGS="$TMP/settings.json" CC_AUTOPILOT_DRY_RUN=1 ./install.sh >/dev/null
grep -q '"KEEP_ME"' "$TMP/settings.json"
grep -q 'existing' "$TMP/settings.json"
grep -q 'CLAUDE_AFK_TIMEOUT_MS' "$TMP/settings.json"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash tests/test_install_merge.sh`
Expected: FAIL — `./install.sh: No such file or directory`

- [ ] **Step 3: Implement `install.sh` and `uninstall.sh`**

`install.sh` must: refuse to run without a backup (writes `settings.json.bak-YYYYMMDD-HHMMSS`); merge hooks into the existing arrays without replacing them; add `env.CLAUDE_AFK_TIMEOUT_MS = 300000` only if absent, and when present with a different value, print both values and require `--force`; write `~/.config/cc-autopilot-notify/config.json` with a timestamped backup if it exists; install and enable `~/.config/systemd/user/cc-autopilot-notify.service` with `Restart=on-failure` and `RestartSec=5`; and support `CC_AUTOPILOT_DRY_RUN=1` which prints the resulting JSON to stdout and writes nothing. `uninstall.sh` reverses all five and then runs `diff <(jq -S . "$BACKUP") <(jq -S . ~/.claude/settings.json)` to prove zero difference, exiting non-zero if they differ.

- [ ] **Step 4: Run the test to verify it passes**

Run: `bash tests/test_install_merge.sh`
Expected: exit 0, all three greps match

- [ ] **Step 5: Run doctor against the live machine**

Run: `python -m cc_autopilot_notify.cli doctor`
Expected: prints one line per check; `pebrel runtime` and `pebrel conversation capabilities` are `PASS`; `daemon heartbeat` is `FAIL` before the service starts. Record the full output in the commit message.

- [ ] **Step 6: Commit**

```bash
git add install.sh uninstall.sh README.md tests/test_install_merge.sh
git commit -m "feat: install/uninstall with non-destructive settings merge"
```

---

### Task 11: Step 0 probe — does 2.1.308 still accept updatedInput.answers?

**Files:**
- Create: `docs/spikes/2026-10-27-permission-request-answers.md`
- Modify: `cc_autopilot_notify/doctor.py` (add the `answers accepted` check)

**Interfaces:**
- Produces: `doctor.answers_probe() -> Check` — `PASS` only after a real session confirms it; `SKIP` with instructions otherwise

- [ ] **Step 1: Write the probe hook**

Create a throwaway hook that, on `PermissionRequest` with `tool_name == "AskUserQuestion"`, prints:

```json
{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow","updatedInput":{"answers":{"<exact question text>":"<exact first option label>"}}}}}
```

- [ ] **Step 2: Register it temporarily and trigger a real question**

In a scratch project, point `~/.claude/settings.json` `PermissionRequest` at the probe (back up first), then start Claude Code and ask it a question that uses `AskUserQuestion`.

Expected A (accepted): the session prints `User answered Claude's questions: · <question> → <label>` and continues, and **the dialog never appears**.
Expected B (discarded): the dialog appears and waits.

- [ ] **Step 3: Record the result in the spike document**

Write `docs/spikes/2026-10-27-permission-request-answers.md` with the CC version string from `claude --version`, the exact hook JSON, the observed terminal transcript, and the verdict. If B, state explicitly that path B is unavailable on this version and that the system relies on path A plus native AFK.

- [ ] **Step 4: Restore settings and add the doctor check**

Restore the backed-up `settings.json` and confirm with `diff`. Then add `answers_probe()` to `doctor.py`, returning `SKIP` with the spike document path when it has not been re-run.

- [ ] **Step 5: Commit**

```bash
git add docs/spikes/2026-10-27-permission-request-answers.md cc_autopilot_notify/doctor.py
git commit -m "docs: record updatedInput.answers probe on CC 2.1.308"
```

---

### Task 12: End-to-end acceptance

**Files:**
- Modify: `README.md`
- Create: `docs/acceptance.md`

**Interfaces:** none — this task verifies the running system

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest tests/ -q && bash tests/test_install_merge.sh`
Expected: both exit 0

- [ ] **Step 2: Verify S1–S3 (channels)**

Run: `python -m cc_autopilot_notify.cli test-channel feishu`, then `terminal`, then `pebrel`
Expected: Feishu card received on phone/desktop; OSC 9 line appears in Pebrel; system toast appears and clicking it focuses the pane

- [ ] **Step 3: Verify S4/S5 (dual thresholds)**

With the daemon running, trigger an `AskUserQuestion` and do not touch the dialog. Keep Pebrel focused; expect auto-submit at ~150 s. Repeat with another window focused; expect ~90 s. Record actual wall-clock times in `docs/acceptance.md` — these are measured values and must carry their measurement method.

- [ ] **Step 4: Verify S6 (human wins)**

Trigger a question, type in the dialog before the threshold. Expect: no auto-submit, and the record cleared.

- [ ] **Step 5: Verify S7/S8 (judge and degradation)**

Trigger a question whose options carry `(Recommended)`; confirm that option is chosen. Then stop the daemon mid-question and confirm native AFK still continues the session at 300 s.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/acceptance.md
git commit -m "docs: end-to-end acceptance results"
```

---

## Self-Review

**1. Spec coverage**

| Spec section | Task |
|---|---|
| §1.2 S1–S3 notifications | 5, 12 |
| §1.2 S4–S5 dual thresholds | 7, 12 |
| §1.2 S6 human wins | 7, 12 |
| §1.2 S7 recommended option | 6, 12 |
| §1.2 S8 no permanent stall | 7 (degradation), 12 |
| §3.1 native AFK | 10 (env value), 12 (verify 300 s) |
| §3.2 updatedInput risk | 11 |
| §3.4 runtime TCP + token safety | 3, 10 (constraint) |
| §4.2 path A / B / C | 7 (A), 9 (B), 10 (C) |
| §4.3 judge two tiers | 6 |
| §4.4 components | 1–9 |
| §5 config | 1 |
| §6 error handling | 7 (degradation), 9 (doctor), 5 (channel isolation) |
| §7 steps 0–6 | 11 (0), 9 (1), 12 (2–3), all (4), 9 (5), 10 (6) |
| §9 install surface | 10 |

No gaps.

**2. Step scan**

Every step names a file, a signature, or a command with expected output. No step says "handle edge cases" or "add validation" without naming which. Task 11 is deliberately a probe rather than an implementation — its deliverable is a recorded verdict, which is what spec step 0 asks for.

**3. Type consistency**

`state.Pending` fields are used identically in Tasks 2, 7, 8. `pebrel_runtime.read` returns a dict whose `revision` / `prompt` / `identity` keys are consumed the same way in Tasks 3 and 7. `judge.pick` returns `Decision` in Tasks 6 and 9; Task 9 passes a bare `judge_fn` returning an int, matching `daemon.Deps.judge_fn`.

**4. Review Focus**

Each of the five lines has a test: item 1 → `test_question_options_unknown_shape_raises` (Task 3); item 2 → `test_prompt_changed_is_retried_once` (Task 7); item 3 → `test_revision_change_resets_activity` (Task 7); item 4 → Task 11's probe + `doctor` check; item 5 → `test_feishu_rejects_http_200_with_error_code` (Task 5).

**5. Proportion**

The plan is longer than the spec, but most of that is test code the spec does not contain. The implementation steps are signatures and one-line approaches, not bodies.
