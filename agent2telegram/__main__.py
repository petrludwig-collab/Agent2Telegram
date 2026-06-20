"""Command-line entry point: ``python -m agent2telegram <command>``.

Commands:
  setup     interactive wizard (choose agent, enter token, authorize yourself)
  run       start the bridge
  service   print an OS service unit (systemd/launchd) for boot persistence
  doctor    check the current config and agent availability
"""
from __future__ import annotations

import argparse
import logging
import sys


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _cmd_run(_args) -> int:
    from .config import load, ConfigError
    try:
        cfg = load()
    except ConfigError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    if cfg.mode == "attach":
        from .attach import AttachBridge
        AttachBridge(cfg).run()
    else:
        from .bridge import Bridge
        Bridge(cfg).run()
    return 0


def _cmd_doctor(_args) -> int:
    from .config import load, ConfigError
    from . import adapters
    try:
        cfg = load()
    except ConfigError as e:
        print(f"✗ config: {e}", file=sys.stderr)
        return 2
    print("config:", cfg.redacted())
    cls = adapters.REGISTRY.get(cfg.agent)
    if cls is None:
        print(f"✗ unknown agent '{cfg.agent}'")
        return 2
    ok = cls.detect() if cfg.agent != "generic" else True
    print(f"agent '{cfg.agent}': {'✓ binary found' if ok else '✗ binary NOT found on PATH'}")
    if not cfg.allowed_user_ids:
        print("⚠️  allowed_user_ids is empty — the bot will refuse everyone.")
    try:
        from .telegram import TelegramClient
        me = TelegramClient(cfg.token).get_me()
        print(f"telegram: ✓ @{me.get('username')}")
    except Exception as e:
        print(f"telegram: ✗ {e}")
        return 1
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent2telegram", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    from . import __version__
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-V", "--version", action="version", version=f"agent2telegram {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="interactive setup wizard")
    sub.add_parser("run", help="start the bridge")
    sub.add_parser("service", help="print a systemd/launchd service unit")
    sub.add_parser("doctor", help="diagnose config and agent availability")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "setup":
        from . import wizard
        return wizard.run()
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "service":
        from . import service
        return service.print_instructions()
    if args.command == "doctor":
        return _cmd_doctor(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
