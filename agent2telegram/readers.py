"""Transcript readers: turn an agent's session log into one normalized event stream.

``AttachBridge`` is agent-agnostic — it does Telegram I/O (progress messages, the live tool
status bubble, the typing indicator) the same way for every agent. Each reader here knows the
on-disk transcript format of one agent and maps its records to a common set of events:

  ``turn_start`` — a new turn began. Codex writes ``task_started``; Claude Code has no such
                   record, so for it the bridge starts the turn from the inbound message.
  ``user``       — a user message (``Ev.text``). Used to detect whether the turn came from
                   Telegram (origin prefix) so only those turns are forwarded.
  ``text``       — assistant text to forward as a kept progress/final message. ``Ev.key`` is a
                   stable dedup id; ``Ev.final`` hints this is the final answer.
  ``tool``       — a tool/command call, summarized for the one-line status bubble. ``Ev.key`` is
                   the call id (so the same call isn't pushed twice).
  ``turn_end``   — the turn finished. Codex writes ``task_complete`` (so no Stop hook is needed!);
                   Claude Code has none, so the bridge ends the turn via its Stop-hook marker / idle.

Why an abstraction instead of an ``if agent == ...`` in the bridge: the two formats differ a lot
(Claude = one assistant record with text+tool_use blocks and a uuid; Codex = separate event_msg /
response_item lines, no uuid, but an explicit task_complete). Keeping that knowledge in small
readers makes the bridge identical for both and easy to extend to a third agent later.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from dataclasses import dataclass


@dataclass
class Ev:
    kind: str               # turn_start | user | text | tool | turn_end
    text: str = ""          # message / tool-summary text
    key: str = ""           # stable dedup id (text uuid/hash, tool call id)
    final: bool = False      # for 'text': hint that this is the final answer


def _short(s: str, n: int = 58) -> str:
    s = " ".join(str(s).split()).replace("**", "").replace("`", "")
    return s if len(s) <= n else s[:n - 1] + "…"


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]


# --------------------------------------------------------------------------- Claude Code

def _claude_tool_summary(name: str, inp: dict) -> str:
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return "🛠️ " + _short(inp.get("description") or inp.get("command", "command"))
    if name == "Read":
        return "📄 Reading " + _short(os.path.basename(inp.get("file_path", "")) or "file")
    if name in ("Edit", "Write", "NotebookEdit"):
        return "✏️ Editing " + _short(os.path.basename(inp.get("file_path", "")) or "file")
    if name in ("Grep", "Glob"):
        return "🔎 Searching " + _short(inp.get("pattern", ""))
    if name == "WebFetch":
        try:
            host = urllib.parse.urlparse(inp.get("url", "")).netloc or inp.get("url", "")
        except Exception:
            host = inp.get("url", "")
        return "🌐 Web " + _short(host)
    if name == "WebSearch":
        return "🔎 Web search: " + _short(inp.get("query", ""))
    if name in ("Agent", "Task"):
        return "🤖 " + _short(inp.get("description") or "subagent")
    if name.startswith("mcp__"):
        return "🔌 " + _short(name.replace("mcp__", "").replace("__", " "))
    return "🛠️ " + _short(name or "tool")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _codex_message_text(content, block_type: str) -> str:
    """Extract text from selected blocks of a current Codex ``message`` response item.

    Codex 0.147 records both user prompts and assistant replies as
    ``response_item/message`` records whose text lives in ``payload.content``
    blocks, rather than the legacy ``event_msg`` records.
    """
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == block_type
        and isinstance(block.get("text"), str)
    ).strip()


class ClaudeCodeReader:
    """Claude Code transcript (one JSONL record per message; assistant records carry text and
    tool_use blocks; user records may be real messages or tool results). No turn boundaries in
    the file — the bridge handles those via the Stop-hook marker and idle fallback."""

    name = "claude-code"
    emits_turn_end = False

    def user_text(self, rec: dict) -> str | None:
        if rec.get("type") != "user":
            return None
        return _text_of(rec.get("message", {}).get("content"))

    def parse(self, rec: dict):
        typ = rec.get("type")
        if typ == "user":
            t = _text_of(rec.get("message", {}).get("content"))
            if t.strip():
                yield Ev("user", text=t)
            return
        if typ != "assistant":
            return
        blocks = rec.get("message", {}).get("content")
        blocks = blocks if isinstance(blocks, list) else []
        # Text first, then tool calls — so a progress message clears the previous bubble before
        # the next call re-creates it below (the bridge relies on this order).
        text = "\n".join(b.get("text", "") for b in blocks
                         if isinstance(b, dict) and b.get("type") == "text").strip()
        if text:
            yield Ev("text", text=text, key=rec.get("uuid", "") or _hash(text))
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tid = b.get("id")
                if tid:
                    yield Ev("tool", text=_claude_tool_summary(b.get("name", ""), b.get("input")), key=tid)


# --------------------------------------------------------------------------- Codex

def _codex_tool_summary(payload: dict) -> str:
    pt = payload.get("type")
    if pt == "function_call":
        name = payload.get("name", "")
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except Exception:
            args = {}
        if name in ("exec_command", "shell", "local_shell", "container.exec"):
            cmd = args.get("cmd") or args.get("command") or ""
            if isinstance(cmd, list):
                cmd = " ".join(str(c) for c in cmd)
            return "🛠️ " + _short(cmd or name)
        if name in ("read_file", "view"):
            return "📄 Reading " + _short(os.path.basename(args.get("path", "")) or "file")
        if name.startswith("mcp"):
            return "🔌 " + _short(name)
        return "🛠️ " + _short(name or "tool")
    if pt == "custom_tool_call":
        name = payload.get("name", "tool")
        if name == "apply_patch":
            return "✏️ " + _short("apply_patch")
        return "🛠️ " + _short(name)
    if pt == "web_search_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        q = action.get("query") or (action.get("queries") or [""])[0] or ""
        return "🔎 Web search: " + _short(q) if q else "🔎 Searching the web"
    return "🛠️ tool"


class CodexReader:
    """Codex CLI rollout transcript (``~/.codex/sessions/.../rollout-*.jsonl``). Each line is an
    ``event_msg`` or ``response_item`` with a ``payload.type``. Crucially it records explicit
    ``task_started`` / ``task_complete`` events, so turn boundaries (and thus the typing
    indicator and bubble cleanup) need no external Stop hook."""

    name = "codex"
    emits_turn_end = True

    def user_text(self, rec: dict) -> str | None:
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if rec.get("type") == "event_msg" and p.get("type") == "user_message":
            return p.get("message", "")
        if (rec.get("type") == "response_item" and p.get("type") == "message"
                and p.get("role") == "user"):
            return _codex_message_text(p.get("content"), "input_text")
        return None

    def parse(self, rec: dict):
        t = rec.get("type")
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        pt = p.get("type")
        if t == "event_msg" and pt == "task_started":
            yield Ev("turn_start")
        elif t == "event_msg" and pt == "user_message":
            msg = p.get("message", "")
            if msg.strip():
                yield Ev("user", text=msg)
        elif t == "response_item" and pt == "message" and p.get("role") == "user":
            msg = _codex_message_text(p.get("content"), "input_text")
            if msg:
                yield Ev("user", text=msg)
        elif t == "event_msg" and pt == "agent_message":
            msg = (p.get("message") or "").strip()
            if msg:
                ts = rec.get("timestamp", "")
                yield Ev("text", text=msg, key=f"{ts}:{_hash(msg)}",
                         final=(p.get("phase") == "final_answer"))
        elif t == "response_item" and pt == "message" and p.get("role") == "assistant":
            msg = _codex_message_text(p.get("content"), "output_text")
            if msg:
                key = p.get("id") or _hash(msg)
                yield Ev("text", text=msg, key=key,
                         final=(p.get("phase") == "final_answer"))
        elif t == "response_item" and pt in ("function_call", "custom_tool_call", "web_search_call"):
            if pt == "web_search_call":
                action = p.get("action") if isinstance(p.get("action"), dict) else {}
                key = "web:" + (action.get("query") or "search")     # stable across status updates
            else:
                key = p.get("call_id") or _hash(json.dumps(p, sort_keys=True)[:200])
            yield Ev("tool", text=_codex_tool_summary(p), key=key)
        elif t == "event_msg" and pt == "task_complete":
            yield Ev("turn_end")


def for_agent(agent: str):
    """Return the reader for the configured agent (defaults to Claude Code)."""
    return CodexReader() if (agent or "").lower() == "codex" else ClaudeCodeReader()
