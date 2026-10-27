"""Feishu (Lark) group-bot webhook channel.

Feishu answers HTTP 200 while rejecting the payload in the body, with a
non-zero `code`. Trusting the status alone would make us believe we notified
someone who never saw a thing, so the body is what decides success.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

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
    code = parsed.get("code", parsed.get("StatusCode", 0))
    if code not in (0, None):
        raise FeishuError(f"code={code} msg={parsed.get('msg') or parsed.get('StatusMessage')}")


def _card(n) -> dict:
    fields = [f"**项目**\n{n.project or 'unknown'}", f"**事件**\n{n.event or 'unknown'}"]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue",
                       "title": {"tag": "plain_text", "content": n.title}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": n.body}},
                {"tag": "div", "fields": [
                    {"is_short": True,
                     "text": {"tag": "lark_md", "content": f}}
                    for f in fields
                ]},
            ],
        },
    }


def send(n, webhook: str, timeout: float = 10) -> None:
    payload = json.dumps(_card(n), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            check_response(response.status, body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FeishuError(f"HTTP {exc.code}: {detail[:200]}") from None
