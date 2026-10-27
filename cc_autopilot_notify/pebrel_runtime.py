"""Client for Pebrel's runtime protocol.

Pebrel exposes no `conversation` CLI subcommand; the only way to read or
answer an open question dialog is this JSON-Lines protocol over a loopback
TCP socket. The token in `runtime.port` is a bearer secret and must never
appear in a log line, notification, or state file.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

PROTOCOL = "nebula.runtime"
PROTOCOL_VERSION = 1
_PORT_FILE = Path("Pebrel") / "runtime.port"
_TIMEOUT = 5.0


class RuntimeError(Exception):
    """Any failure talking to the Pebrel runtime.

    This shadows the builtin only inside this module, which is deliberate: every
    runtime failure in here is one class, and callers catch it by this name.
    """


def endpoint(appdata: Path) -> tuple[int, str]:
    """Read (port, token) from Pebrel's runtime.port.

    Accepts either the appdata root (…/Roaming) or the Pebrel directory itself:
    both spellings occur in real callers, and guessing wrong would look like
    "runtime unreachable" rather than a path mistake.
    """
    base = Path(appdata)
    path = base / _PORT_FILE
    if not path.exists():
        path = base / "runtime.port"
    try:
        first = path.read_text(encoding="utf-8").splitlines()[0].split()
        port, token = int(first[0]), first[1]
    except (OSError, IndexError, ValueError) as exc:
        raise RuntimeError(f"cannot read Pebrel runtime.port at {path}: {exc}") from None
    if not port or not token:
        raise RuntimeError(f"Pebrel runtime.port at {path} is incomplete")
    return port, token


def _call(appdata: Path, method: str, params: dict) -> dict:
    port, token = endpoint(appdata)
    request = {
        "protocol": PROTOCOL,
        "version": PROTOCOL_VERSION,
        "id": method,
        "token": token,
        "method": method,
        "params": params,
    }
    line = (json.dumps(request) + "\n").encode("utf-8")
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=_TIMEOUT)
        sock.settimeout(_TIMEOUT)
        sock.sendall(line)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
    except OSError as exc:
        # str(exc) never contains the token: it is only in the request body.
        raise RuntimeError(f"Pebrel runtime unreachable on port {port}: {exc}") from None
    finally:
        if sock is not None:
            sock.close()

    if not buf.strip():
        raise RuntimeError(f"Pebrel runtime closed the connection during {method}")
    try:
        reply = json.loads(buf.decode("utf-8", errors="replace").split("\n", 1)[0])
    except ValueError:
        raise RuntimeError(f"Pebrel runtime sent non-JSON for {method}") from None
    if not isinstance(reply, dict):
        raise RuntimeError(f"Pebrel runtime sent a non-object for {method}")
    if reply.get("ok") is False:
        err = reply.get("error") or {}
        raise RuntimeError(
            f"{method} refused: {err.get('code', 'unknown')} {err.get('message', '')}".strip()
        )
    result = reply.get("result")
    return result if isinstance(result, dict) else {}


def describe(appdata: Path) -> dict:
    return _call(appdata, "runtime.describe", {})


def agents(appdata: Path) -> list[dict]:
    """Every pane running an agent, with its session_id, window_id and pane_id."""
    result = _call(appdata, "agents.list", {})
    found = result.get("agents")
    return found if isinstance(found, list) else []


def locate(appdata: Path, session_id: str) -> tuple[int, int] | None:
    """Resolve a Claude session to (window_id, pane_id).

    Claude Code's hook payload carries no pane identity, so the mapping must come
    from Pebrel itself. Guessing pane 0 is refused by the runtime with
    `invalid_params`.
    """
    for agent in agents(appdata):
        info = agent.get("agent") or {}
        if info.get("session_id") == session_id:
            return int(agent.get("window_id", 0)), int(agent.get("pane_id", 0))
    return None


def dialog(appdata: Path, pane_id: int) -> dict:
    """The pane's current dialog state and visible text.

    Returns {"state": <task_state>, "text": <buffer tail>, "revision": <str>}.

    `conversation.read.prompt` is NOT used: a 130 s live probe showed it stays
    null for Claude Code's AskUserQuestion even while `task_state` is
    "attention" (0 hits across 11 samples). The gate is the task state; the
    text is what the answer plan is checked against.
    """
    result = _call(appdata, "pane.read", {"pane_id": pane_id, "lines": 60})
    text = result.get("text") or ""
    state = result.get("task_state")
    # `pane.read` carries no sequence number, so the revision is the tail's
    # digest: a user navigating the dialog changes the visible text, and that
    # is exactly the "someone is here" signal the daemon needs.
    revision = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return {"state": state, "text": text, "revision": revision}


def press(appdata: Path, pane_id: int, key: str, repeat: int = 1) -> dict:
    """Send a named key to a pane. `key` is one of the runtime's key names."""
    params = {"pane_id": pane_id, "key": key}
    if repeat != 1:
        params["repeat"] = repeat
    return _call(appdata, "pane.send_key", params)


def read(appdata: Path, window_id: int, pane_id: int, session_id: str) -> dict:
    return _call(appdata, "conversation.read", {
        "window_id": window_id,
        "pane_id": pane_id,
        "identity": {"kind": "claude", "session_id": session_id},
    })


def choose(appdata: Path, window_id: int, pane_id: int, session_id: str,
           epoch: int, prompt_id: str, option: int) -> dict:
    """Choose an option. Raises RuntimeError when Pebrel refuses.

    `epoch` and `prompt_id` are both required by the live server:
    `invalid conversation operation fields` and `the current prompt_id is
    required` respectively. A refusal is expected when the user answered first,
    so callers treat the raise as an outcome, not a crash.
    """
    return _call(appdata, "conversation.choose", {
        "window_id": window_id,
        "pane_id": pane_id,
        "identity": {"kind": "claude", "session_id": session_id, "epoch": epoch},
        "prompt_id": prompt_id,
        "option": option,
    })


# The live shape of an open question has never been observed (`prompt` is always
# null on a live pane), so the id is looked for in every plausible place rather
# than pinned to one unverified key.
_PROMPT_ID_KEYS = ("prompt_id", "promptId", "id")


def prompt_id_of(reading: dict) -> str | None:
    """Extract the id `conversation.choose` requires, or None if absent."""
    prompt = reading.get("prompt")
    if isinstance(prompt, dict):
        for key in _PROMPT_ID_KEYS:
            value = prompt.get(key)
            if isinstance(value, str) and value:
                return value
    for key in _PROMPT_ID_KEYS:
        value = reading.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def epoch_of(reading: dict) -> int | None:
    identity = reading.get("identity")
    if isinstance(identity, dict):
        epoch = identity.get("epoch")
        if isinstance(epoch, int) and not isinstance(epoch, bool):
            return epoch
    return None


def supports_conversation(desc: dict) -> bool:
    caps = desc.get("capabilities") or []
    return "conversation.read" in caps and "conversation.choose" in caps


def question_options(prompt: dict | None) -> list[dict]:
    """Normalise a prompt payload into a list of {label, description}.

    Every `conversation.read` observed so far returned `prompt: null`, so the
    live shape of an open question is unverified. An unrecognised shape raises
    rather than guessing: submitting a wrong option is worse than not
    answering, because the user may never notice.
    """
    if prompt is None:
        return []
    if not isinstance(prompt, dict):
        raise RuntimeError(f"unexpected prompt type {type(prompt).__name__}")
    options = prompt.get("options")
    if not isinstance(options, list):
        raise RuntimeError(
            "prompt has no options list; observed keys: "
            + ",".join(sorted(prompt.keys()))
        )
    out: list[dict] = []
    for item in options:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            raise RuntimeError(
                "prompt option without a string label; observed: "
                + json.dumps(item, ensure_ascii=False)[:120]
            )
        out.append({
            "label": item["label"],
            "description": item.get("description") or "",
        })
    return out
