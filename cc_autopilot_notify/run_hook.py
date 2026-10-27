"""Single entry point for the hooks wired into Claude Code settings.

Claude Code runs one command per hook; rather than shipping four scripts, the
settings point here with a module name. Keeps the settings diff small and the
dispatch in one place.
"""

from __future__ import annotations

import sys

_MODULES = {
    "pre_tool": "hook_pre_tool",
    "notify": "hook_notify",
    "permission": "hook_permission",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in _MODULES:
        print(f"usage: run_hook.py {{{'|'.join(_MODULES)}}}", file=sys.stderr)
        return 2
    module = __import__(f"cc_autopilot_notify.{_MODULES[argv[0]]}", fromlist=["main"])
    return module.main()


if __name__ == "__main__":
    sys.exit(main())
