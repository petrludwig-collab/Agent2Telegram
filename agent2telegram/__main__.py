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

from .config import ConfigError
from .session import SessionError

_DEFAULT_GROUP = object()  # --to-group without an explicit id


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        # Date, not just time. Without it a log line gives no way to tell where a day ends, so
        # the "daily" report actually summed the whole log window — about a month in one install.
        # Anything parsing this log must allow for the optional date prefix.
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from .redaction import install_log_redaction
    install_log_redaction()


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
    # Fill in the bot @username (non-secret) on startup if missing, so other tools (e.g. the
    # Agents Monitoring dashboard's Telegram link) can use it. Self-heals older configs on restart.
    if not cfg.bot_username:
        try:
            from .telegram import TelegramClient
            from .config import save
            me = TelegramClient(cfg.token).get_me()
            if me.get("username"):
                cfg.bot_username = me["username"]
                save(cfg)
        except Exception:
            pass
    if cfg.mode == "attach":
        if cfg.agent == "claude-code":
            from .claude_hooks import register
            register(cfg)
        from .attach import AttachBridge
        AttachBridge(cfg).run()
    else:
        from .bridge import Bridge
        Bridge(cfg).run()
    return 0


def _cmd_notify(args) -> int:
    """Push a message to the configured owner or an allowed group via the bot — for notifications from
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
    group = getattr(args, "to_group", None)
    if group is not None:
        groups = cfg.allowed_group_ids
        if group is _DEFAULT_GROUP:
            if len(groups) != 1:
                print("✗ --to-group requires GROUP_ID unless exactly one group is allowed", file=sys.stderr)
                return 2
            group = groups[0]
        if group not in groups:
            print(f"✗ group {group} is not in allowed_group_ids", file=sys.stderr)
            return 2
        target = group
    elif cfg.allowed_user_ids:
        target = cfg.allowed_user_ids[0]
    else:
        print("✗ no owner to notify (allowed_user_ids is empty)", file=sys.stderr)
        return 2
    files = list(getattr(args, "file", None) or [])
    text = args.message if args.message is not None else ("" if files else sys.stdin.read())
    text = (text or "").strip()
    if not text and not files:
        print("✗ nothing to send (pass a message, pipe it on stdin, or use --file)", file=sys.stderr)
        return 2
    from .telegram import TelegramClient, TelegramError
    from pathlib import Path
    client = TelegramClient(cfg.token)
    logging.getLogger("agent2telegram.notify").info("notify target chat=%s", target)
    try:
        if text:
            client.send_message(target, text)
            print("✓ sent")
    except TelegramError as e:
        print(f"✗ send failed: {e}", file=sys.stderr)
        return 1
    # Same allowlist as the in-chat `[tg-file]` marker: a background job is not more
    # trusted than the agent, so it may only send from the outbox folders.
    allowed = cfg.allowed_outbox_dirs()
    rc = 0
    for raw in files:
        try:
            path = Path(raw).expanduser().resolve()
        except OSError as e:
            print(f"✗ {raw}: cannot resolve ({e.__class__.__name__})", file=sys.stderr)
            rc = 1
            continue
        if not any(path == d or d in path.parents for d in allowed):
            print(f"✗ {path.name}: outside the allowed folders (see 'outbox_dirs')", file=sys.stderr)
            rc = 1
            continue
        if not path.is_file() or path.stat().st_size == 0:
            print(f"✗ {path.name}: not a regular non-empty file", file=sys.stderr)
            rc = 1
            continue
        try:
            client.send_file(target, path)
            print(f"✓ sent {path.name}")
        except TelegramError as e:
            print(f"✗ {path.name}: {e}", file=sys.stderr)
            rc = 1
    return rc


def _cmd_doctor(args) -> int:
    import os
    from .config import load, ConfigError
    from . import adapters
    if getattr(args, 'config', None):
        os.environ['AGENT2TELEGRAM_CONFIG'] = args.config
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
        ok = False
    if cfg.mode == 'attach' and cfg.agent == 'claude-code':
        from .claude_hooks import check, repair_command
        hooked, detail = check(cfg)
        print(f"Claude Stop hook: {'✓' if hooked else '✗'} {detail}")
        if not hooked:
            print(f'Repair: {repair_command()}')
            print('Then resume the same Claude session to load the hook.')
            ok = False
    try:
        from .telegram import TelegramClient
        me = TelegramClient(cfg.token).get_me()
        print(f"telegram: ✓ @{me.get('username')}")
    except Exception as e:
        print(f"telegram: ✗ {e}")
        return 1
    return 0 if ok else 1


def _cmd_uninstall(args) -> int:
    import importlib.util
    import json
    import os
    import shutil
    import signal
    import subprocess
    from pathlib import Path
    from .config import DEFAULT_PATH

    print("This stops this user's Agent2Telegram bridges and removes their default config + state.")
    if not args.yes:
        try:
            if input("Continue? (y/N): ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 0
        except EOFError:
            print("Aborted — no terminal; rerun with --yes.")
            return 0

    # Inspect processes before any destructive step, then disable the default supervisor.
    # Never kill tmux sessions: they may contain the user's agent, not a bridge.
    # Custom supervisors must be disabled by their owner before uninstalling.
    from .updater import _running_bridges, _wait_gone
    try:
        bridges = _running_bridges()
    except RuntimeError as exc:
        print(str(exc))
        return 1
    home = Path.home()
    service = home / '.config/systemd/user/agent2telegram.service'
    plist = home / 'Library/LaunchAgents/com.agent2telegram.bridge.plist'
    if service.is_file():
        if not shutil.which('systemctl'):
            print('Cannot disable the user service without systemctl; files preserved.')
            return 1
        result = subprocess.run(['systemctl', '--user', 'disable', '--now', 'agent2telegram'], capture_output=True)
        if result.returncode:
            print('Could not disable the user service; files preserved.')
            return 1
        service.unlink()
    if plist.is_file() and sys.platform == 'darwin':
        result = subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(plist)], capture_output=True)
        if result.returncode:
            print('Could not unload the user service; files preserved.')
            return 1
        plist.unlink()

    # Stop running bridges (never this process — it's `uninstall`, not `run`).
    killed = 0
    for pid, _ in bridges:
        try:
            os.kill(int(pid), signal.SIGTERM)
            if not _wait_gone(pid):
                print('Bridge did not stop; files preserved. Stop its supervisor and retry.')
                return 1
            killed += 1
        except (OSError, ValueError):
            pass
    print(f"  stopped {killed} running bridge(s)" if killed else "  no running bridge found")

    # 2) Unregister the Claude Code Stop hook, if the wizard added one.
    from .claude_hooks import settings_path
    settings = settings_path()
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

    for directory in {home / '.local/bin', *(Path(p) for p in os.environ.get('PATH', '').split(os.pathsep) if p)}:
        launcher = directory / 'agent2telegram'
        if home not in launcher.parents or not launcher.is_file() or launcher.is_symlink():
            continue
        try:
            text = launcher.read_text()
            if text.startswith('#!/bin/sh\n') and '-m agent2telegram "$@"' in text:
                launcher.unlink()
        except (OSError, UnicodeError):
            pass

    # 4) Remove everything the installer created: config (token+ledger), state, the source clone
    #    and the launcher. All imports are already done, so deleting the clone we run from is safe.
    targets = [DEFAULT_PATH.parent, home / ".local" / "state" / "agent2telegram",
               home / ".agent2telegram-src", home / "start-a2t-bridge.sh"]
    for d in targets:
        try:
            if d.is_symlink():
                d.unlink(); print(f"  removed link {d}")
            elif d.is_dir():
                shutil.rmtree(d); print(f"  removed {d}")
            elif d.exists():
                d.unlink(); print(f"  removed {d}")
        except OSError as exc:
            print(f'Could not remove {d.name}: {type(exc).__name__}')
            return 1

    # 5) Remove the pip package too, if it was installed that way (no-op otherwise).
    if importlib.util.find_spec('pip') is not None:
        result = subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "agent2telegram"],
                                capture_output=True, cwd=home)
        if result.returncode:
            result = subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y",
                                     "--break-system-packages", "agent2telegram"], capture_output=True, cwd=home)
            if result.returncode:
                print('Default files removed, but pip cleanup failed. Check the package in this Python environment.')
                return 1
    print("\n✓ Default installation removed. Custom source/state paths and supervisors are left for their owner.")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent2telegram", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    from . import __version__
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-V", "--version", action="version", version=f"agent2telegram {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="interactive setup wizard")
    cn = sub.add_parser("connect", help="connect one agent (tmux session) to a Telegram bot")
    cn.add_argument("--name", help="name for this bridge (its config file); default: the session name")
    update = sub.add_parser("update", help="pull the latest code and restart the running bridge(s)")
    update.add_argument('--no-restart', action='store_true', help='update code only; keep supervisor-managed bridges running')
    se = sub.add_parser("set-elevenlabs", help="add the ElevenLabs API key (enables voice transcription)")
    se.add_argument("--config", help="path to a specific bridge config")
    run_p = sub.add_parser("run", help="start the bridge")
    run_p.add_argument("--config", help="path to a specific config (run multiple bridges from one install)")
    nt = sub.add_parser("notify", help="push a message to the owner or an allowed group (for cron/background jobs)")
    nt.add_argument("message", nargs="?", help="text to send (omit to read from stdin)")
    nt.add_argument("--config", help="path to a specific bridge config")
    nt.add_argument("--to-group", nargs="?", type=int, const=_DEFAULT_GROUP, metavar="GROUP_ID",
                    help="send to an allowed group; omit GROUP_ID only when exactly one is configured")
    nt.add_argument("--file", action="append",
                    help="attach a file from an outbox folder (repeatable)")
    sub.add_parser("service", help="print a systemd/launchd service unit")
    doctor = sub.add_parser("doctor", help="diagnose config and agent availability")
    doctor.add_argument('--config', help='path to a specific bridge config')
    hook = sub.add_parser('claude-hook', help='register the Claude Stop hook without starting a bridge')
    hook.add_argument('--config', help='path to a specific bridge config')
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
    if args.command == "connect":
        from . import wizard
        return wizard.connect(name=getattr(args, "name", None))
    if args.command == "update":
        from . import updater
        return updater.run(no_restart=args.no_restart)
    if args.command == "set-elevenlabs":
        from . import wizard
        return wizard.set_elevenlabs(config=getattr(args, "config", None))
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "notify":
        return _cmd_notify(args)
    if args.command == "service":
        from . import service
        return service.print_instructions()
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == 'claude-hook':
        from pathlib import Path
        from .claude_hooks import register
        return 1 if register(path=Path(args.config) if args.config else None) == 'error' else 0
    if args.command == "selftest":
        from . import selftest
        return selftest.run(args.agent, keep=args.keep)
    if args.command == "uninstall":
        return _cmd_uninstall(args)
    parser.error("unknown command")
    return 2


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 130
    except EOFError:
        print("\nSetup input ended; run setup from a terminal or provide all answers.", file=sys.stderr)
        return 2
    except (SessionError, ConfigError) as e:
        # Missing tmux or a bad config is NOT a program crash — it is something the user has to
        # do. A traceback here only scares: a first-time installer reads "it broke" instead of
        # "install tmux". The exception's message already carries that advice, so show it on its
        # own. (From walking the install on a clean Ubuntu.)
        print(f"\n{e}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
