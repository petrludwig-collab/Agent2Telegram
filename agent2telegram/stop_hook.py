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
import sys
import time
from pathlib import Path


def _load_cfg() -> dict:
    try:
        from .config import config_path
        return json.loads(Path(config_path()).read_text("utf-8"))
    except Exception:
        return {}


def main() -> None:
    cfg = _load_cfg()
    signal = cfg.get("signal_file")
    if not signal:
        return
    guard = cfg.get("claude_session_id", "")
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    path = payload.get("transcript_path")
    if not path:
        return
    if guard and not os.path.basename(path).startswith(guard):
        return                                   # a different Claude session → not ours
    marker = Path(signal).parent / "turn_end"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
