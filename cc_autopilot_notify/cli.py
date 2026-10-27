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
    from . import config, daemon, hook_common

    daemon.run(hook_common.state_root(), config.load())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cc-autopilot-notify")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="run self-checks")
    channel = sub.add_parser("test-channel", help="send a test notification")
    channel.add_argument("name", choices=["feishu", "terminal", "pebrel"])
    sub.add_parser("daemon", help="run the polling daemon")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    if args.command == "test-channel":
        return _test_channel(args.name)
    return _daemon()


if __name__ == "__main__":
    sys.exit(main())
