"""Interactive 3-step setup wizard for attach mode.

  1. **Provider** — auto-detect which agents are installed (Claude Code / Codex / Antigravity)
     and pick one.
  2. **Session** — attach to an existing tmux session or create a fresh one (launching the agent
     in it for you).
  3. **Telegram** — paste the bot token; we validate it live and capture your user id from the
     first message you send the bot, then write the config and offer to start.

Everything is validated as we go so you never end up with a config that "looks fine" but doesn't
work. Codex needs no extra setup (its rollout log records turn boundaries); for Claude Code we
also register the Stop hook that marks end-of-turn.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import adapters
from .config import Config, save, config_path
from .telegram import TelegramClient, TelegramError

#: Agents we can drive in attach mode (have a transcript reader). Others are oneshot-only.
ATTACH_SUPPORTED = {"claude-code", "codex"}


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def _yes(prompt: str, default_yes: bool = False) -> bool:
    d = "Y/n" if default_yes else "y/N"
    ans = _ask(f"{prompt} ({d})").lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes")


# ---------------------------------------------------------------- step 1: provider
def _choose_provider() -> type:
    agents = [a for a in adapters.available() if a.name != "generic"]
    print("\n── Step 1/3 · Which agent do you want to connect? ──\n")
    for i, a in enumerate(agents, 1):
        found = a.detect()
        mark = "✓ installed" if found else "· not found on PATH"
        attach = "" if a.name in ATTACH_SUPPORTED else "  (oneshot only — no live updates yet)"
        print(f"  {i}) {a.label:<14} {mark}{attach}")
    print()
    # Default to the first *installed* attach-capable agent.
    default = next((str(i) for i, a in enumerate(agents, 1)
                    if a.detect() and a.name in ATTACH_SUPPORTED), "1")
    while True:
        choice = _ask("Pick a number", default)
        if choice.isdigit() and 1 <= int(choice) <= len(agents):
            a = agents[int(choice) - 1]
            if not a.detect() and not _yes(f"'{a.binary}' isn't on PATH. Use it anyway?"):
                continue
            return a
        print("Please enter a valid number.")


# ---------------------------------------------------------------- step 2: session
def _tmux() -> str | None:
    return shutil.which("tmux")


def _list_sessions() -> list[str]:
    if not _tmux():
        return []
    try:
        out = subprocess.run(["tmux", "list-sessions", "-F", "#S"],
                             capture_output=True, text=True, timeout=5)
        return [s for s in out.stdout.splitlines() if s.strip()]
    except (subprocess.SubprocessError, OSError):
        return []


def _create_session(name: str, agent_cls) -> bool:
    """Create a detached tmux session and launch the agent's CLI inside it."""
    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", name], check=True, timeout=10)
        if agent_cls.binary:
            subprocess.run(["tmux", "send-keys", "-t", name, agent_cls.binary, "Enter"],
                           check=True, timeout=10)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        print(f"  ✗ couldn't create the session: {e}")
        return False


def _choose_session(agent_cls) -> str:
    print("\n── Step 2/3 · Which tmux session should I drive? ──\n")
    if not _tmux():
        print("  ⚠️  tmux isn't installed. Attach mode drives a tmux session — install tmux first.")
        return _ask("tmux session name to use anyway", "main")
    sessions = _list_sessions()
    for i, s in enumerate(sessions, 1):
        print(f"  {i}) {s}")
    print(f"  n) create a NEW session and launch {agent_cls.label} in it")
    print()
    while True:
        choice = _ask("Pick a number, or 'n' for new", "n" if not sessions else "1")
        if choice.lower() == "n":
            name = _ask("Name for the new session", "lana")
            if _create_session(name, agent_cls):
                print(f"  ✓ created '{name}' and started {agent_cls.label} in it.")
                return name
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(sessions):
            return sessions[int(choice) - 1]
        print("Please enter a valid number or 'n'.")


# ---------------------------------------------------------------- step 3: telegram
def _enter_token() -> tuple[str, dict]:
    print("\n── Step 3/3 · Connect Telegram ──\n")
    print("Create a bot with @BotFather and paste its token below.")
    while True:
        token = _ask("Telegram bot token")
        if not token:
            continue
        try:
            me = TelegramClient(token).get_me()
            print(f"  ✓ token valid — bot is @{me.get('username')}")
            return token, me
        except TelegramError as e:
            print(f"  ✗ rejected by Telegram: {e}")


def _capture_owner_id(token: str, bot_username: str) -> int | None:
    client = TelegramClient(token)
    print(f"\nOpen Telegram, message @{bot_username} anything (e.g. 'hi') — I'll detect your id.")
    input("Press Enter once you've sent it… ")
    offset = 0
    for _ in range(8):
        try:
            updates = client.get_updates(offset, timeout=5)
        except TelegramError as e:
            print(f"  (couldn't read updates: {e})")
            break
        for upd in updates:
            offset = upd["update_id"] + 1
            frm = (upd.get("message") or {}).get("from")
            if frm and frm.get("id"):
                who = frm.get("username") or frm.get("first_name") or "you"
                print(f"  ✓ authorized {who} (id {frm['id']})")
                return int(frm["id"])
        time.sleep(1)
    manual = _ask("Couldn't auto-detect. Enter your Telegram user id manually")
    return int(manual) if manual.isdigit() else None


# ---------------------------------------------------------------- Claude Stop hook
def _register_claude_hook() -> None:
    """Add the end-of-turn Stop hook to ~/.claude/settings.json (idempotent, non-destructive)."""
    settings = Path.home() / ".claude" / "settings.json"
    cmd = f"{sys.executable} -m agent2telegram.stop_hook"
    try:
        data = json.loads(settings.read_text("utf-8")) if settings.exists() else {}
        hooks = data.setdefault("hooks", {}).setdefault("Stop", [])
        already = json.dumps(hooks).find("agent2telegram.stop_hook") != -1
        if not already:
            hooks.append({"matcher": "", "hooks": [{"type": "command", "command": cmd, "timeout": 15}]})
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"  ✓ Stop hook registered in {settings}")
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ⚠️  Couldn't auto-register the Stop hook ({e}). Add this to {settings} under "
              f'hooks.Stop manually:\n      {{"type":"command","command":"{cmd}"}}')


# ---------------------------------------------------------------- run
def run() -> int:
    print("=== Agent2Telegram setup ===")
    existing = config_path()
    if existing.exists() and not _yes(f"A config already exists at {existing}. Overwrite?"):
        print("Aborted — keeping the existing config.")
        return 0

    agent_cls = _choose_provider()
    session = _choose_session(agent_cls)
    token, me = _enter_token()
    owner = _capture_owner_id(token, me.get("username", "your_bot"))

    state = Path.home() / ".local" / "state" / "agent2telegram"
    cfg = Config(
        agent=agent_cls.name,
        token=token,
        allowed_user_ids=[owner] if owner else [],
        mode="attach",
        tmux_session=session,
        transcript_path="auto",
        signal_file=str(state / "answer.txt"),
        origin_prefix="[TG] ",
        progress_marker="[TG]",
    )
    if _yes("\nEnable voice messages via ElevenLabs Scribe?"):
        cfg.elevenlabs_api_key = _ask("ElevenLabs API key")

    path = save(cfg)
    print(f"\n✓ Saved config to {path} (permissions 0600).")

    if agent_cls.name == "codex":
        print("  ✓ Codex needs no hook — turn boundaries come from its rollout log.")
    elif agent_cls.name == "claude-code":
        _register_claude_hook()
    elif agent_cls.name not in ATTACH_SUPPORTED:
        print(f"  ⚠️  {agent_cls.label} has no live-transcript reader yet — progress/typing won't "
              "work in attach mode. Codex and Claude Code are fully supported.")

    if not cfg.allowed_user_ids:
        print("⚠️  No authorized user — add your id to 'allowed_user_ids' before using the bot.")
    print("\nAll set! Start the bridge with:\n  python3 -m agent2telegram run")
    if _yes("\nStart it now in the background?"):
        log = state / "run.log"
        state.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([sys.executable, "-m", "agent2telegram", "run"],
                         stdout=open(log, "a"), stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        print(f"  ✓ started — logs at {log}")
        print(f"  Message @{me.get('username')} on Telegram to test it.")
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(run())
