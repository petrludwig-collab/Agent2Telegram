"""Claude Code **Stop hook** for Agent2Telegram (attach mode).

Claude Code has no end-of-turn record in its transcript, so to switch the "typing…" indicator
off and clear the live status bubble at the *exact* moment a turn ends, the bridge relies on a
tiny marker file. Claude Code runs this hook at the end of every assistant turn; it writes that
marker for the matching session. (Codex needs no hook — its rollout log records ``task_complete``.)

The bridge forwards every assistant message by tailing the transcript directly, so this hook does
**not** send anything itself — it only signals "turn ended".

Register it once (the setup wizard does this) in the agent's settings, e.g. ``~/.claude/settings.json``::

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
       "command": "python3 -m agent2telegram.stop_hook"}]}]}}

It reads the signal path / optional session guard from the Agent2Telegram config, so the command
line needs no arguments. Always exits 0 (never blocks the agent).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _load_cfg() -> dict:
    try:
        from .config import config_path
        return json.loads(Path(config_path()).read_text("utf-8"))
    except Exception:
        return {}


def _all_cfgs() -> list[dict]:
    """Every bridge config in the config directory — so a single global Stop hook serves multiple
    Claude bridges (each `agent2telegram connect` writes its own <name>.json)."""
    try:
        from .config import config_path
        d = Path(config_path()).parent
    except Exception:
        return []
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                item = json.loads(p.read_text("utf-8"))
                if isinstance(item, dict):
                    out.append(item)
            except Exception:
                pass
    return out


def _mark(signal: str, turn: dict) -> None:
    marker = Path(signal).parent / "turn_end"
    pending = marker.with_name(f".turn_end-{os.getpid()}")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps({"timestamp": time.time(), **turn}), encoding="utf-8")
        pending.replace(marker)  # the bridge must never see a partially written identity
    except OSError:
        pass
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass


def _parent(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out) if out else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _own_tmux_session() -> str:
    """The tmux session whose pane runs the Claude process that called this hook.

    Claude Code may continue in a new conversation (new transcript id) after a restart or
    /clear, so a stored session guard can go stale. The pane ancestry still says which bridge
    owns this turn."""
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    panes = {}
    for line in out.splitlines():
        pid, _, name = line.partition("\t")
        if pid.isdigit():
            panes[int(pid)] = name
    pid = os.getppid()
    for _ in range(32):
        if pid in panes:
            return panes[pid]
        if pid <= 1:
            break
        pid = _parent(pid)
    return ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    path = payload.get("transcript_path")
    if not path:
        return
    base = os.path.basename(path)
    # Capture the prompt BEFORE config/tmux discovery can delay the marker write.
    # A later prompt must not make this old Stop look like the end of the new turn.
    from .readers import ClaudeCodeReader, latest_user
    prompt = latest_user(Path(path), ClaudeCodeReader())
    turn = {"transcript_path": os.path.realpath(path),
            "user_offset": prompt[0] if prompt else None,
            "user_uuid": prompt[1].get("uuid") if prompt else None}

    def eligible(c):
        # Legacy configs omitted agent/mode. Never finish a Codex turn from a Claude hook.
        return (isinstance(c, dict) and c.get('agent', 'claude-code') == 'claude-code'
                and c.get('mode', 'attach') == 'attach' and c.get('signal_file'))

    cfgs = [c for c in _all_cfgs() if eligible(c)]
    if not cfgs:                                   # legacy: just the env/default config
        c = _load_cfg()
        if eligible(c):
            cfgs = [c]

    # A config with a session guard fires only for ITS session. If none matched (e.g. a single
    # legacy bridge with no guard), fall back to the guard-less configs — old behaviour preserved.
    matched = False
    guardless = []
    for c in cfgs:
        guard = c.get("claude_session_id", "")
        if guard:
            if base.startswith(guard):
                _mark(c["signal_file"], turn)
                matched = True
        else:
            guardless.append(c)
    if not matched:
        # Stale guard (Claude continued in a new conversation): match by the tmux session.
        session = _own_tmux_session()
        for c in cfgs:
            if session and c.get("tmux_session") == session and c.get("claude_session_id"):
                _mark(c["signal_file"], turn)
                matched = True
    if not matched:
        for c in guardless:
            _mark(c["signal_file"], turn)


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
