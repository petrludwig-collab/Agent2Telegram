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


def _cmd_run(args) -> int:
    import os
    from .config import load, ConfigError
    if getattr(args, "config", None):
        os.environ["AGENT2TELEGRAM_CONFIG"] = args.config   # run a specific bridge's config
    try:
        cfg = load()
    except ConfigError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    if cfg.mode == "stream":
        from .stream import StreamBridge
        StreamBridge(cfg).run()
    elif cfg.mode == "attach":
        from .attach import AttachBridge
        AttachBridge(cfg).run()
    else:
        from .bridge import Bridge
        Bridge(cfg).run()
    return 0


def _cmd_notify(args) -> int:
    """Push a message to the configured owner via the bot — for PROACTIVE notifications from
    cron jobs, background tasks or scripts (e.g. "build finished ✅"). This is the supported way
    for the agent to reach you unprompted: the bridge only forwards replies during a Telegram-
    originated turn, so a background job can't deliver through the normal chat flow — it calls
    this instead."""
    import os
    from .config import load, ConfigError
    if getattr(args, "config", None):
        os.environ["AGENT2TELEGRAM_CONFIG"] = args.config
    try:
        cfg = load()
    except ConfigError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    text = args.message if args.message is not None else sys.stdin.read()
    text = (text or "").strip()
    if not text:
        print("✗ nothing to send (pass a message or pipe it on stdin)", file=sys.stderr)
        return 2
    if not cfg.allowed_user_ids:
        print("✗ no owner to notify (allowed_user_ids is empty)", file=sys.stderr)
        return 2
    from .telegram import TelegramClient, TelegramError
    try:
        TelegramClient(cfg.token).send_message(cfg.allowed_user_ids[0], text)
    except TelegramError as e:
        print(f"✗ send failed: {e}", file=sys.stderr)
        return 1
    print("✓ sent")
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


def _cmd_uninstall(args) -> int:
    import json
    import os
    import shutil
    import signal
    import subprocess
    from pathlib import Path
    from .config import DEFAULT_PATH

    print("This stops any running Agent2Telegram bridge and removes its config + state.")
    if not args.yes:
        try:
            if input("Continue? (y/N): ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 0
        except EOFError:
            print("Aborted — no terminal; rerun with --yes.")
            return 0

    # 1) Stop running bridges (never this process — it's `uninstall`, not `run`).
    killed = 0
    if shutil.which("pgrep"):
        try:
            out = subprocess.run(["pgrep", "-f", "agent2telegram run"],
                                 capture_output=True, text=True, timeout=10)
            for pid in out.stdout.split():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    killed += 1
                except (OSError, ValueError):
                    pass
        except (OSError, subprocess.SubprocessError):
            pass
    print(f"  stopped {killed} running bridge(s)" if killed else "  no running bridge found")

    # 2) Unregister the Claude Code Stop hook, if the wizard added one.
    settings = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(settings.read_text("utf-8"))
        stops = data.get("hooks", {}).get("Stop", [])
        kept = [h for h in stops if "agent2telegram.stop_hook" not in json.dumps(h)]
        if len(kept) != len(stops):
            data["hooks"]["Stop"] = kept
            settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"  removed the Stop hook from {settings}")
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass

    # 3) Kill the tmux session that hosts the bridge (the installer/launcher uses 'a2t-bridge').
    if shutil.which("tmux"):
        subprocess.run(["tmux", "kill-session", "-t", "a2t-bridge"], capture_output=True)

    # 4) Remove everything the installer created: config (token+ledger), state, the source clone
    #    and the launcher. All imports are already done, so deleting the clone we run from is safe.
    home = Path.home()
    targets = [DEFAULT_PATH.parent, home / ".local" / "state" / "agent2telegram",
               home / ".agent2telegram-src", home / "start-a2t-bridge.sh"]
    for d in targets:
        try:
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True); print(f"  removed {d}")
            elif d.exists():
                d.unlink(); print(f"  removed {d}")
        except OSError:
            pass

    # 5) Remove the pip package too, if it was installed that way (no-op otherwise).
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "agent2telegram"],
                   capture_output=True)
    print("\n✓ Agent2Telegram fully removed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent2telegram", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    from . import __version__
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-V", "--version", action="version", version=f"agent2telegram {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="interactive setup wizard")
    run_p = sub.add_parser("run", help="start the bridge")
    run_p.add_argument("--config", help="path to a specific config (run multiple bridges from one install)")
    nt = sub.add_parser("notify", help="push a message to the owner (for cron/background jobs)")
    nt.add_argument("message", nargs="?", help="text to send (omit to read from stdin)")
    nt.add_argument("--config", help="path to a specific bridge config")
    sub.add_parser("service", help="print a systemd/launchd service unit")
    sub.add_parser("doctor", help="diagnose config and agent availability")
    st = sub.add_parser("selftest", help="end-to-end attach test against a real agent (no bot)")
    st.add_argument("--agent", default="codex", choices=["codex", "claude-code"],
                    help="which agent to test (default: codex)")
    st.add_argument("--keep", action="store_true", help="keep the throwaway tmux session afterwards")
    un = sub.add_parser("uninstall", help="stop the bridge and remove config + state")
    un.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "setup":
        from . import wizard
        return wizard.run()
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "notify":
        return _cmd_notify(args)
    if args.command == "service":
        from . import service
        return service.print_instructions()
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "selftest":
        from . import selftest
        return selftest.run(args.agent, keep=args.keep)
    if args.command == "uninstall":
        return _cmd_uninstall(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
