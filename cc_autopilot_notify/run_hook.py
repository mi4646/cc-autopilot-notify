"""Single entry point for the hooks wired into Claude Code settings.

Claude Code runs one command per hook; rather than shipping four scripts, the
settings point here with a module name. Keeps the settings diff small and the
dispatch in one place.
"""

from __future__ import annotations

import os
import sys

# A hook is launched by path, so sys.path[0] is this package directory and the
# package itself is not importable. Put the repo root on the path before any
# package import happens.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_MODULES = {
    "pre_tool": "hook_pre_tool",
    "notify": "hook_notify",
    "permission": "hook_permission",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in _MODULES:
        print(f"usage: run_hook.py {{{'|'.join(_MODULES)}}}", file=sys.stderr)
        # Exit 2 means "block the tool" for PreToolUse; a usage mistake must
        # never wedge Claude Code.
        return 0
    module = __import__(f"cc_autopilot_notify.{_MODULES[argv[0]]}", fromlist=["main"])
    return module.main()


if __name__ == "__main__":
    sys.exit(main())
