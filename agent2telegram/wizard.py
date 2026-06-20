"""Interactive setup wizard: pick an agent, enter the Telegram token, authorize yourself.

Everything is validated as we go so the user never ends up with a config that "looks fine"
but doesn't work:
  * the token is checked live against Telegram (``getMe``),
  * the chosen agent's binary is probed on PATH (warn, don't block — it may be installed
    later or live behind a custom command), and
  * the owner's Telegram user id is captured automatically from a real message.
"""
from __future__ import annotations

import sys
import time

from . import adapters
from .config import Config, save, config_path
from .telegram import TelegramClient, TelegramError


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def _choose_agent() -> type:
    agents = adapters.available()
    print("\nWhich agent do you want to connect?\n")
    for i, a in enumerate(agents, 1):
        status = ""
        if a.name != "generic":
            status = "  ✓ found" if a.detect() else "  (not found on PATH)"
        print(f"  {i}) {a.label}{status}")
    print()
    while True:
        choice = _ask("Enter a number", "1")
        if choice.isdigit() and 1 <= int(choice) <= len(agents):
            return agents[int(choice) - 1]
        print("Please enter a valid number.")


def _verify_token(token: str) -> dict:
    client = TelegramClient(token)
    return client.get_me()


def _capture_owner_id(token: str) -> int | None:
    """Ask the user to message the bot, then read their user id from the update."""
    client = TelegramClient(token)
    print("\nTo authorize yourself, open Telegram, find your bot and send it any message")
    print("(e.g. 'hi'). I'll detect your user id automatically.\n")
    input("Press Enter once you've sent the message… ")
    # Drain a few polls; the message may take a moment to arrive.
    offset = 0
    for _ in range(6):
        try:
            updates = client.get_updates(offset, timeout=5)
        except TelegramError as e:
            print(f"  (couldn't read updates: {e})")
            break
        for upd in updates:
            offset = upd["update_id"] + 1
            frm = (upd.get("message") or {}).get("from")
            if frm and frm.get("id"):
                name = frm.get("username") or frm.get("first_name") or "you"
                print(f"  ✓ detected {name} (id {frm['id']})")
                return int(frm["id"])
        time.sleep(1)
    print("  Couldn't detect a message automatically.")
    manual = _ask("Enter your Telegram user id manually (or leave blank to skip)")
    return int(manual) if manual.isdigit() else None


def run() -> int:
    print("=== Agent2Telegram setup ===")
    existing = config_path()
    if existing.exists():
        if _ask(f"A config already exists at {existing}. Overwrite? (y/N)").lower() not in ("y", "yes"):
            print("Aborted — keeping the existing config.")
            return 0
    agent_cls = _choose_agent()

    command = None
    if agent_cls.name == "generic":
        raw = _ask("Command to run (use {prompt} for the message)", "my-agent {prompt}")
        command = raw.split()
    elif not agent_cls.detect():
        print(f"\n⚠️  '{agent_cls.binary}' isn't on PATH yet. Install {agent_cls.label} and make"
              f" sure you can run it before starting the bridge.")

    print("\nCreate a bot with @BotFather in Telegram and paste its token below.")
    token = ""
    while not token:
        token = _ask("Telegram bot token")
        if not token:
            continue
        try:
            me = _verify_token(token)
            print(f"  ✓ token valid — bot is @{me.get('username')}")
        except TelegramError as e:
            print(f"  ✗ token rejected by Telegram: {e}")
            token = ""

    owner = _capture_owner_id(token)
    allowed = [owner] if owner else []

    cfg = Config(agent=agent_cls.name, token=token, allowed_user_ids=allowed, command=command)
    path = save(cfg)
    print(f"\n✓ Saved config to {path} (permissions 0600).")
    if not allowed:
        print("⚠️  No authorized user set — add your id to 'allowed_user_ids' before the bot is usable.")
    print("\nStart the bridge with:\n  python -m agent2telegram run\n")
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(run())
