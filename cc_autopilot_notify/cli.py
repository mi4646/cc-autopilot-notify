"""Command line entry point."""

from __future__ import annotations

import argparse
import sys


def _doctor() -> int:
    from . import config, doctor, hook_common

    checks = doctor.run_all(hook_common.state_root(), config.load())
    failed = 0
    for check in checks:
        print(f"{check.status:4}  {check.name}: {check.detail}")
        if check.status == "FAIL":
            failed += 1
    return 1 if failed else 0


def _test_channel(name: str) -> int:
    from . import channels, config

    cfg = config.load()
    notification = channels.Notification(title="cc-autopilot-notify 测试",
                                        body=f"测试通道 {name}", project="selftest",
                                        event="test-channel")
    if name == "feishu":
        webhook = config.feishu_webhook(cfg)
        if not webhook:
            print("feishu: no webhook configured", file=sys.stderr)
            return 1
        channels.feishu.send(notification, webhook)
    elif name in ("terminal", "pebrel"):
        getattr(channels, name).send(notification)
    else:
        print(f"unknown channel: {name}", file=sys.stderr)
        return 2
    print(f"{name}: sent")
    return 0


def _daemon() -> int:
    import logging

    from . import config, daemon, hook_common

    # Actions are only observable if the logger speaks; systemd captures stderr.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    daemon.run(hook_common.state_root(), config.load())
    return 0


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cc-autopilot-notify")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="run self-checks")
    channel = sub.add_parser("test-channel", help="send a test notification")
    channel.add_argument("name", choices=["feishu", "terminal", "pebrel"])
    sub.add_parser("daemon", help="run the polling daemon")
    sub.add_parser("ensure-daemon", help="start the daemon when it is not alive")
    sub.add_parser("stop-daemon", help="stop a running daemon")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    if args.command == "test-channel":
        return _test_channel(args.name)
    if args.command == "ensure-daemon":
        return _ensure_daemon()
    if args.command == "stop-daemon":
        return _stop_daemon()
    return _daemon()


if __name__ == "__main__":
    sys.exit(main())
