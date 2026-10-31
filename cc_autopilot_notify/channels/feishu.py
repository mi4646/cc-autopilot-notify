"""Feishu (Lark) group-bot webhook channel.

Feishu answers HTTP 200 while rejecting the payload in the body, with a
non-zero `code`. Trusting the status alone would make us believe we notified
someone who never saw a thing, so the body is what decides success.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request

from ..project import project_name

log = logging.getLogger(__name__)


class FeishuError(Exception):
    """The webhook did not accept the message."""


def check_response(status: int, body: str) -> None:
    if status < 200 or status >= 300:
        raise FeishuError(f"HTTP {status}: {body[:200]}")
    try:
        parsed = json.loads(body)
    except ValueError:
        raise FeishuError(f"non-JSON reply: {body[:200]}") from None
    if not isinstance(parsed, dict):
        raise FeishuError(f"non-object reply: {body[:200]}")
    # A proxy or gateway error body has no `code` at all. Defaulting a missing
    # key to 0 would report a message delivered that nobody received.
    if "code" not in parsed and "StatusCode" not in parsed:
        raise FeishuError(f"reply has no status code: {body[:200]}")
    raw = parsed.get("code", parsed.get("StatusCode"))
    try:
        code = int(raw)
    except (TypeError, ValueError):
        raise FeishuError(f"unparseable code={raw!r}") from None
    if code != 0:
        raise FeishuError(f"code={code} msg={parsed.get('msg') or parsed.get('StatusMessage')}")


_COLORS = {
    "AskUserQuestion": "orange",
    "permission_prompt": "orange",
    "agent_needs_input": "orange",
    "elicitation_dialog": "orange",
    "elicitation_url_dialog": "orange",
    "idle_prompt": "blue",
    "auto_answer": "green",
    "auto_continue": "turquoise",
    "continue_stage": "blue",
    "continue_capped": "orange",
    "StopFailure": "red",
}


# Question bodies and option labels come from Claude, which can be shaped by
# repository content. `<at id=all>` would ping everyone; `](` starts a link.
_AT_TAG = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)


def _escape_lark(text: str) -> str:
    text = _AT_TAG.sub("", str(text))
    text = text.replace("](", "] (")
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _field(label: str, value: str) -> dict:
    return {"is_short": True,
            "text": {"tag": "lark_md",
                     "content": f"**{label}**\n{_escape_lark(value)}"}}


def _timestamp(created_at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(created_at or time.time()))


def _card(n) -> dict:
    fields = [_field("项目", project_name(n.project)),
              _field("事件", n.event or "unknown")]
    if n.question_count > 0:
        fields.append(_field("问题数", str(n.question_count)))
    if n.options:
        fields.append(_field("选项", " / ".join(n.options)))
    if n.session_id:
        fields.append(_field("Session", n.session_id[:8]))
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": _COLORS.get(n.event, "blue"),
                       "title": {"tag": "plain_text", "content": n.title}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                                        "content": _escape_lark(n.body)}},
                {"tag": "div", "fields": fields},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": _timestamp(n.created_at)}]},
            ],
        },
    }


def send(n, webhook: str, timeout: float = 10) -> None:
    payload = json.dumps(_card(n), ensure_ascii=False).encode("utf-8")
    try:
        request = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            check_response(response.status, body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FeishuError(f"HTTP {exc.code}: {detail[:200]}") from None
    except (ValueError, OSError, http.client.HTTPException) as exc:
        # The webhook token lives in the URL path; never put the URL (or the
        # exception's rendering of it) into a message that reaches a log.
        raise FeishuError(f"request failed: {type(exc).__name__}") from None
