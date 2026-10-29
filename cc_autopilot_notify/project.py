"""A session's cwd reduced to a name that fits in a notification title.

Ported from the cc-notify-hooks fork this project learned from: a worktree path
resolves to its repository root, the home directory renders as `~`, and deep
paths drop their middle so the project name survives title truncation.
"""

from __future__ import annotations

import os

_WORKTREE_MARKERS = ("/.claude/worktrees", "/.worktrees")


def _lexical(value: str) -> str:
    """Fold `.`, `..` and duplicate slashes without touching the filesystem.

    Symlinks must NOT be resolved: a cwd can point at something that no longer
    exists (a removed worktree, a test path), and realpath would then return a
    directory that is not the project at all.
    """
    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts) if parts else "/"


def _worktree_root(path: str) -> str:
    for marker in _WORKTREE_MARKERS:
        index = path.find(marker)
        if index <= 0:
            continue
        end = index + len(marker)
        if len(path) == end or path[end] == "/":
            return path[:index]
    return path


def project_name(cwd: str, home: str | None = None) -> str:
    """Name the project a session belongs to, for use inside a title."""
    raw = (cwd or "").strip().replace("\\", "/")
    if not raw:
        return "unknown"
    path = _worktree_root(_lexical(raw))
    if path == "/":
        return "unknown"

    home = (os.environ.get("HOME", "") if home is None else home)
    home_path = _lexical(home.replace("\\", "/")) if home.strip() else ""
    if home_path not in ("", "/"):
        if path == home_path:
            return "~"
        if path.startswith(home_path + "/"):
            parts = path[len(home_path) + 1:].split("/")
            rel = f"{parts[0]}/{parts[1]}/…/{parts[-1]}" if len(parts) > 2 else "/".join(parts)
            return f"~/{rel}"
    return path.rsplit("/", 1)[-1] or "unknown"
