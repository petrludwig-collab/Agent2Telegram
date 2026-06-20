"""Claude Code **Stop hook** for Agent2Telegram.

Claude Code runs this at the end of every assistant turn. When the turn was started by a
Telegram message (its user text begins with the configured origin prefix, e.g. ``Telegram:``),
it writes the final assistant answer — read authoritatively from the session transcript, not
the screen — to the bridge's signal file. The bridge is blocking on that file and sends it.

Register it once (the installer does this) in the agent's settings, e.g. in
``~/.claude/settings.json``::

    {"hooks": {"Stop": [{"hooks": [{"type": "command",
       "command": "python3 -m agent2telegram.stop_hook"}]}]}}

It reads the signal path / origin prefix / optional session guard from the Agent2Telegram
config, so the command line needs no arguments. Always exits 0 (never blocks the agent).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _load_cfg() -> dict:
    try:
        from .config import config_path
        return json.loads(Path(config_path()).read_text("utf-8"))
    except Exception:
        return {}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return ""


def last_turn(path: str, origin: str) -> tuple[bool, list[str]]:
    """Return (started_by_origin, [assistant_texts]) for the final turn only."""
    from_origin, turn = False, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = rec.get("type")
            content = rec.get("message", {}).get("content", [])
            if typ == "user":
                txt = _text_of(content)
                if txt:                                  # a new turn begins
                    from_origin = txt.startswith(origin) if origin else True
                    turn = []
            elif typ == "assistant":
                txt = _text_of(content)
                if txt:
                    turn.append(txt)
    return from_origin, turn


def main() -> None:
    cfg = _load_cfg()
    signal = cfg.get("signal_file")
    if not signal:
        return
    origin = cfg.get("origin_prefix", "Telegram:")
    guard = cfg.get("claude_session_id", "")
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return
    if guard and not os.path.basename(path).startswith(guard):
        return                                           # different session → not ours
    try:
        from_origin, turn = last_turn(path, origin)
    except OSError:
        return
    if not from_origin or not turn:
        return                                           # terminal-originated / nothing to send
    # If the turn used the progress marker, the live watcher already sent everything.
    marker = cfg.get("progress_marker", "[tg]")
    if marker and any(
        any(ln.lstrip().startswith(marker) for ln in t.splitlines()) for t in turn
    ):
        return
    try:
        Path(signal).parent.mkdir(parents=True, exist_ok=True)
        Path(signal).write_text(turn[-1], encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
