# Spike: how Qoder CN implements "问答面板静默跳过"

- Date opened: 2026-10-27
- Qoder CN inspected: **0.4.11** (`build-manifest.json`, build 2026-10-23) —
  the version whose Linux runtime host was running during the inspection.
  The install-root bundle is 0.3.3; both were read, and the logic below is
  identical in both (0.4.11 only obfuscates identifiers further).
- Status: **research complete — static analysis only, no runtime test**

## Why this matters

cc-autopilot-notify answers an idle `AskUserQuestion` from outside Claude Code:
it reads the Pebrel pane, checks whether the Pebrel window is foreground, and
injects a keypress. Qoder CN ships the same user-facing feature ("silently
skip unanswered questions", 150 s foreground / 90 s background) but implements
it **inside its own agent host**, with no screen scraping and no key injection.

The differences are design-relevant:

| Dimension | Qoder CN | cc-autopilot-notify |
|---|---|---|
| Where it runs | In-process; resolves the pending tool call directly | External daemon; reads pane text and presses keys |
| What "skip" means | Empty answer (`{}`, same as the "无偏好" action) | Pick option 0 / judge result |
| Foreground test | Per-session view visibility (in-process state) | PowerShell foreground-window process name |
| Activity signal | `pointermove` / `pointerdown` / `keydown` on its own window | Pane screen revision change |
| Panel interaction | Permanently snoozes that question | Resets the timer only |
| Countdown | Last 60 s shows "N 秒后自动跳过" | None |

## Evidence locations

No public documentation was found; every claim below comes from the local
install.

- Bundle: `C:\Users\<user>\AppData\Local\Programs\Qoder CN\.qoder-versions\0.4.11\resources\app.asar`
  (compared against `...\Qoder CN\resources\app.asar`, version 0.3.3).
- Setting row: `AppData\Roaming\com.qodercn.app.stable\main.sqlite`, table
  `app_settings`.
- Strings are searchable directly in the asar (`b"silentSkip"`,
  `b"activityResetUntil"`); no need to unpack the whole archive.

## Findings

### 1. Setting and default

```js
rAe = Co({key:"chatSession.userQuestionSilentSkipEnabled", defaultValue:!1,
          parse:e=>typeof e=="boolean"?e:void 0})
```

Default **off**. The setting is written to `app_settings` and broadcast to
every live runtime (`setUserQuestionSilentSkipEnabledForAll`).

Snapshot on this machine (2026-10-27):

```
sqlite> select * from app_settings where setting_key='chatSession.userQuestionSilentSkipEnabled';
('chatSession.userQuestionSilentSkipEnabled', 'false', 1790146756617)
```

The i18n string states the contract:

> 开启后，当前任务前台无人操作 150 秒、后台 90 秒后，以空答案自动跳过未回答的问答；
> 面板内交互会取消自动跳过。

### 2. Timer: 90 s base + 60 s when foreground

0.3.3 (readable identifiers) and 0.4.11 (obfuscated) carry the same constants:

```js
// 0.3.3
const Kse=6e4, spt=9e4, apt=6e4, cpt=2147483647;
// 0.4.11
const wN0=6e4, g8e=9e4, m8e=6e4, j8e=2147483647;
```

```js
schedule(t, A=this.now()){
  this.cancelTimer(t);
  const i = t.policy==="explicit" ? t.explicitDurationMs??0 : spt+(this.foreground?Kse:0),
        n = A+i,
        r = n-Math.min(apt,i);
  t.activityResetUntil = t.policy==="default" && this.foreground ? A+Kse : void 0;
  const o = {resolvesAt:new Date(n).toISOString(),
             countdownVisibleAt:new Date(r).toISOString()};
  this.options.onChanged(t.toolUseId,o);
  this.arm(t,n)
}
```

- Background: 90 s. Foreground: 90 + 60 = 150 s.
- `countdownVisibleAt = resolvesAt - 60 s`, so the countdown appears for the
  last 60 seconds of either window.
- Per-question override: tool input or event details may carry
  `autoResolutionMs`; a safe integer in `[0, 8e15]` switches that entry to the
  `explicit` policy with exactly that duration.

### 3. "Foreground" is view visibility, not OS window focus

```js
isForeground(t){ return !!this.activeViewScopesBySession.get(t)?.size }
```

`setVisibleSession(sessionId)` marks the previous session's view inactive and
the new one active (`chatSession:user-question-view-active`). "Background" is
therefore the session whose chat view is not currently visible — e.g. another
session tab — not "the Qoder window is behind another app".

### 4. Activity signal: its own window's input events, throttled to 1/s

```js
window.addEventListener("pointermove", a, {passive:true});
window.addEventListener("pointerdown", a, {passive:true});
window.addEventListener("keydown", a);
```

```js
reportVisibleUserQuestionActivity(){
  const e1=this.visibleSessionId;
  if(!e1 || !this.client.reportChatSessionUserQuestionActivity) return;
  const t1=Date.now();
  if (t1 - this.lastUserQuestionActivityAt < 1e3) return;   // 1 s throttle
  this.lastUserQuestionActivityAt = t1;
  this.client.reportChatSessionUserQuestionActivity(e1, this.client.uiScopeId)
}
```

Only the visible session is reported. `reportActivity()` itself is a no-op
while background, and a default-policy entry is re-scheduled only while
`now < activityResetUntil` (schedule time + 60 s). Consequence: continuous
activity keeps pushing the deadline out; activity that arrives more than 60 s
after the last re-schedule does **not** postpone it.

### 5. Interaction with the panel permanently snoozes that question

```js
m1 = () => { u1.current || (u1.current = true,
                            i1.snoozeUserQuestion(t1, n1.toolUseId)) };
<div onClickCapture={m1} onInputCapture={m1}
     onKeyDownCapture={m1} onPointerDownCapture={m1}>
```

This is the path behind "面板内交互会取消自动跳过": one click or keystroke
inside the panel cancels auto-skip for **that question** (the `u1.current`
guard makes it once-only). It is not a timer reset.

### 6. Expiry submits an empty answer

```js
autoResolveUserQuestion(t,A){
  const i=this.pending.get(t);
  if(!i || i.interaction.type!=="userQuestion") return;
  const n=Object.create(null);                 // answers = {}
  this.pending.delete(t);
  this.assistantProjection?.applyUserQuestionAnswer(t,n);
  i.resolve({decision:"allow",
             updatedInput:{questions:i.interaction.questions, answers:n}});
  ...
}
```

No option is selected; the call resolves as if the user pressed "无偏好"
(`skipAll`). The agent continues with no answer. In 0.4.11 the same body is
still present (obfuscated), and the UI label logic is:

```js
labels: { skipAll: c1===null
  ? t("chatActivity.question.skipAll")                       // "无偏好"
  : t("chatActivity.question.autoSkipCountdown",{count:c1})  // "{{count}} 秒后自动跳过"
}
```

## Reproduction

The bundle is a plain asar; the keywords are directly greppable:

```bash
B="/mnt/c/Users/<user>/AppData/Local/Programs/Qoder CN/.qoder-versions/0.4.11/resources/app.asar"
python3 - "$B" <<'PY'
import re,sys
d=open(sys.argv[1],'rb').read()
for kw in (b'silentSkip', b'activityResetUntil', b'explicitDurationMs',
           b'userQuestionSilentSkipEnabled', b'autoSkipCountdown'):
    hits=[m.start() for m in re.finditer(re.escape(kw), d)]
    print(kw, len(hits), hits[:5])
PY
```

```bash
cp "/mnt/c/Users/<user>/AppData/Roaming/com.qodercn.app.stable/main.sqlite" /tmp/q.sqlite
python3 - <<'PY'
import sqlite3
print(sqlite3.connect('/tmp/q.sqlite').execute(
  "select * from app_settings where setting_key like '%SilentSkip%'").fetchall())
PY
```

Offsets differ between builds; search by keyword, never by offset.

## What is not verified

- No runtime test: the setting was not enabled in a live Qoder session, so the
  150 s / 90 s wall-clock behaviour and the countdown were not observed.
- "A visible session stays foreground while another app has focus" is inferred
  from `setVisibleSession()`; whether the renderer flips view visibility on
  window blur was not tested.
- The `explicit` policy's `autoResolutionMs` producer (which caller sets it)
  was not traced beyond its two consumers.

## Implications for cc-autopilot-notify

- Qoder's "skip" is an empty answer, not option 0. Choosing an option is a
  stronger claim than skipping; keep the distinction in mind when comparing
  behaviour.
- Qoder's foreground signal is per-session view state, which an external
  process cannot see. The closest external equivalents are
  `GetLastInputInfo` (global input idle) or the existing foreground-window
  probe; neither distinguishes "working in another app" from "away".
- The once-only `snooze` on panel interaction is a cheaper and clearer rule
  than "reset the timer on screen change": it cannot loop.
