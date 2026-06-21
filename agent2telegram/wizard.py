"""Interactive 3-step setup wizard for attach mode.

  1. **Provider** — auto-detect which agents are installed (Claude Code / Codex) and pick one.
  2. **Session** — attach to an existing tmux session or create a fresh one (launching the agent
     in it for you).
  3. **Telegram** — paste the bot token; we validate it live and capture your user id from the
     first message you send the bot, then write the config and offer to start.

Everything is validated as we go so you never end up with a config that "looks fine" but doesn't
work. Codex needs no extra setup (its rollout log records turn boundaries); for Claude Code we
also register the Stop hook that marks end-of-turn.
"""
from __future__ import annotations

import getpass
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import adapters
from .config import Config, save, config_path
from .telegram import TelegramClient, TelegramError

#: Agents fully supported in attach mode (live progress, status bubble, typing). The Telegram
#: bridge officially supports these two; other CLIs would need their own transcript reader.
ATTACH_SUPPORTED = {"claude-code", "codex"}


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def _ask_secret(prompt: str) -> str:
    """Read a secret (token / API key) with **live masking**: the first 4 characters show, the
    rest appear as ``*`` as you type/paste. People expect to see *something* while typing (a fully
    blank prompt confuses them), but the full secret still never lands in scrollback/screenshots.
    Falls back to plain input when there's no real terminal (e.g. piped)."""
    if not sys.stdin.isatty():
        return _ask(prompt)
    try:
        import termios
        import tty
    except ImportError:
        return getpass.getpass(f"{prompt}: ").strip()   # non-Unix → hidden, no live mask

    sys.stdout.write(f"{prompt}: ")
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf: list[str] = []
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n", "\x04"):          # Enter / Ctrl-D
                break
            if ch == "\x03":                         # Ctrl-C
                raise KeyboardInterrupt
            if ch in ("\x7f", "\b"):                 # backspace
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if ord(ch) < 32:                         # ignore other control chars
                continue
            buf.append(ch)
            sys.stdout.write(ch if len(buf) <= 4 else "*")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return "".join(buf).strip()


def _yes(prompt: str, default_yes: bool = False) -> bool:
    d = "Y/n" if default_yes else "y/N"
    ans = _ask(f"{prompt} ({d})").lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes")


# ---------------------------------------------------------------- step 1: provider
def _choose_provider() -> type:
    agents = [a for a in adapters.available() if a.name in ATTACH_SUPPORTED]
    print("\n── Step 1/3 · Which agent do you want to connect? ──\n")
    for i, a in enumerate(agents, 1):
        found = a.detect()
        mark = "✓ installed" if found else "· not found on PATH"
        print(f"  {i}) {a.label:<14} {mark}")
    print()
    # Default to the first *installed* agent.
    default = next((str(i) for i, a in enumerate(agents, 1) if a.detect()), "1")
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
    """Create a detached tmux session and launch the agent's interactive CLI inside it, with the
    flag that lets the bridge drive it unattended (no per-command approval prompt)."""
    launch = " ".join(agent_cls.tui_launch())
    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", name], check=True, timeout=10)
        if launch:
            subprocess.run(["tmux", "send-keys", "-t", name, launch, "Enter"],
                           check=True, timeout=10)
            print(f"  launched: {launch}")
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
    print("Create a bot with @BotFather and paste its token below (hidden as you type).")
    while True:
        token = _ask_secret("Telegram bot token")
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
        cfg.elevenlabs_api_key = _ask_secret("ElevenLabs API key (hidden)")

    path = save(cfg)
    print(f"\n✓ Saved config to {path} (permissions 0600).")

    if agent_cls.name == "codex":
        print("  ✓ Codex needs no hook — turn boundaries come from its rollout log.")
    elif agent_cls.name == "claude-code":
        _register_claude_hook()

    if not cfg.allowed_user_ids:
        print("⚠️  No authorized user — add your id to 'allowed_user_ids' before using the bot.")
    # Just start it — no prompt. Asking "start now? (y/N)" only confuses people (esp. anyone
    # used to a Windows installer that simply finishes). The bridge belongs running.
    print("\nAll set! Starting the bridge in the background…")
    log = state / "run.log"
    state.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([sys.executable, "-m", "agent2telegram", "run"],
                     stdout=open(log, "a"), stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    print(f"  ✓ running — logs at {log}")
    print(f"  Message @{me.get('username')} on Telegram to test it.")
    print("  (Stop it anytime with:  python3 -m agent2telegram uninstall)")
    return 0


# ---------------------------------------------------------------- connect (one agent → one bot)
def _slug(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "bridge"


def _session_cwd(session: str) -> str | None:
    try:
        out = subprocess.run(["tmux", "display-message", "-p", "-t", session, "#{pane_current_path}"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def _claude_session_id_for(session: str) -> str:
    """The Claude session UUID running in *session*, resolved from ~/.claude/projects/ by the
    session's working directory — used as the Stop-hook guard so multiple Claude bridges each get
    turn-end signals for their own session only."""
    import glob
    import os
    import re
    cwd = _session_cwd(session)
    base = Path.home() / ".claude" / "projects"
    if not cwd or not base.is_dir():
        return ""
    target = os.path.realpath(cwd)
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    files = glob.glob(str(base / "**" / "*.jsonl"), recursive=True)
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    for f in files[:300]:
        try:
            with open(f, encoding="utf-8") as fh:
                head = [fh.readline() for _ in range(5)]
        except OSError:
            continue
        for line in head:
            try:
                c = json.loads(line).get("cwd")
            except ValueError:
                continue
            if c and os.path.realpath(c) == target:
                m = uuid_re.search(os.path.basename(f))
                return m.group(0) if m else ""
    return ""


def _detect_agent_in_session(session: str):
    """Which supported agent is actually running inside *session* — so we don't ask again after
    the user already picked the session. Walks the session's pane process subtree and matches the
    adapter binaries. Returns the adapter class, or None if it can't tell."""
    import re
    try:
        panes = subprocess.run(["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
                               capture_output=True, text=True, timeout=5).stdout.split()
        pane_pids = [int(p) for p in panes if p.isdigit()]
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if not pane_pids:
        return None
    ps = subprocess.run(["ps", "-eo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    children: dict[int, list[int]] = {}
    cmd: dict[int, str] = {}
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        children.setdefault(ppid, []).append(pid)
        cmd[pid] = parts[2]
    seen: set[int] = set()
    stack = list(pane_pids)
    cmds: list[str] = []
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        if p in cmd:
            cmds.append(cmd[p])
        stack.extend(children.get(p, []))
    # Match each command on its own (so the regex's ^ anchor is per-command, not lost in a join),
    # and anchor the binary only at start-of-string or a path '/', with no trailing anchor — so it
    # also catches node-wrapper forms like '/.../claude-code/cli.js'.
    for cls in adapters.available():
        if cls.name not in ATTACH_SUPPORTED or not cls.binary:
            continue
        pat = re.compile(rf"(?:^|/){re.escape(cls.binary)}")
        if any(pat.search(c) for c in cmds):
            return cls
    return None


def connect(name: str | None = None) -> int:
    """`agent2telegram connect` — wire ONE existing agent (a tmux session) to its OWN Telegram bot,
    as a separate bridge with its own config file. Lets you run several bots from one install
    (e.g. one per agent created with `agentsmon new`) without touching the others."""
    print("=== Connect an agent to Telegram ===")
    if not _tmux():
        print("⚠️  tmux not found — attach mode drives a tmux session. Install tmux first.")
        return 1
    sessions = _list_sessions()
    if not sessions:
        print("No tmux sessions found — create an agent first (e.g. `agentsmon new`), then re-run.")
        return 1
    print("\nWhich agent (tmux session) do you want to connect?\n")
    for i, s in enumerate(sessions, 1):
        print(f"  {i}) {s}")
    print()
    while True:
        ch = _ask("Pick a number", "1")
        if ch.isdigit() and 1 <= int(ch) <= len(sessions):
            session = sessions[int(ch) - 1]
            break
        print("Please enter a valid number.")

    # We know the session — detect which agent runs in it instead of asking again.
    agent_cls = _detect_agent_in_session(session)
    if agent_cls:
        print(f"\n  ✓ detected {agent_cls.label} running in '{session}'.")
    else:
        print(f"\n  (couldn't tell which agent is in '{session}')")
        agent_cls = _choose_provider()
    token, me = _enter_token()
    owner = _capture_owner_id(token, me.get("username", "your_bot"))

    slug = _slug(name or session)
    cfg_path = config_path().parent / f"{slug}.json"
    if cfg_path.exists() and not _yes(f"A bridge config {cfg_path} already exists. Overwrite?"):
        print("Aborted — keeping the existing bridge.")
        return 0
    state = Path.home() / ".local" / "state" / "agent2telegram" / slug
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
        claude_session_id=(_claude_session_id_for(session) if agent_cls.name == "claude-code" else ""),
    )
    path = save(cfg, cfg_path)
    print(f"\n✓ Saved bridge config to {path} (permissions 0600).")

    if agent_cls.name == "codex":
        print("  ✓ Codex needs no hook — turn boundaries come from its rollout log.")
    elif agent_cls.name == "claude-code":
        _register_claude_hook()
        if not cfg.claude_session_id:
            print("  ⚠️  Couldn't pin this session's id yet (talk to the agent once, then it'll "
                  "route turn-end signals precisely). It still works meanwhile.")

    if not cfg.allowed_user_ids:
        print("⚠️  No authorized user — add your id to 'allowed_user_ids' before using the bot.")
    print(f"\nAll set! Starting the bridge for '{session}' in the background…")
    state.mkdir(parents=True, exist_ok=True)
    log = state / "run.log"
    subprocess.Popen([sys.executable, "-m", "agent2telegram", "run", "--config", str(path)],
                     stdout=open(log, "a"), stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    print(f"  ✓ running — logs at {log}")
    print(f"  Message @{me.get('username')} on Telegram to test it.")
    print("  Keep it alive across reboots by adding it to your monitoring (e.g.  agentsmon add).")
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(run())
