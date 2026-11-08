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
from collections import deque
import os
import urllib.parse
from dataclasses import dataclass
from .redaction import redact


@dataclass
class Ev:
    kind: str               # turn_start | user | text | tool | files | turn_end
    text: str = ""          # message / tool-summary text
    key: str = ""           # stable dedup id (text uuid/hash, tool call id)
    final: bool = False      # for 'text': hint that this is the final answer
    files: tuple = ()        # for 'files': paths the agent wants delivered to the user
    interim: bool = False    # explicit narration; must never be promoted to a group final answer


def _short(s: str, n: int = 58) -> str:
    s = " ".join(redact(s).split()).replace("**", "").replace("`", "")
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
            host = urllib.parse.urlparse(inp.get("url", "")).hostname or inp.get("url", "")
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



#: Tools by which an agent hands a FILE to the user. The harness runs them itself, so the bridge
#: never sees a call — only its record in the transcript. Without this the file silently vanishes:
#: the reply arrives, the attachment does not, and nothing anywhere says so (2026-08-24).
#: Recognising the tool by name is deliberate — it beats guessing from prose, which would be
#: language-dependent and full of false positives.
FILE_SEND_TOOLS = {"senduserfile"}


def _file_tool_paths(name: str, inp) -> tuple:
    """Paths a file-sending tool call asks to deliver, or () when it is not such a call."""
    if str(name or "").strip().lower() not in FILE_SEND_TOOLS:
        return ()
    if not isinstance(inp, dict):
        return ()
    raw = inp.get("files") or inp.get("file") or inp.get("paths") or inp.get("path")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(p for p in raw if isinstance(p, str) and p.strip())


class ClaudeCodeReader:
    """Claude Code transcript (one JSONL record per message; assistant records carry text and
    tool_use blocks; user records may be real messages or tool results). No turn boundaries in
    the file — the bridge handles those via the Stop-hook marker and idle fallback."""

    name = "claude-code"
    emits_turn_end = False

    @staticmethod
    def _is_tool_result(rec: dict) -> bool:
        """A ``user`` record that is really a TOOL RESULT, not something the person typed.

        Claude Code files tool results under ``type: "user"``. They are indistinguishable from a
        real prompt by type alone, and treating one as a prompt is not cosmetic: the bridge
        decides from the last user text whether the turn came from Telegram, so a tool result
        silently reclassified a live Telegram turn as terminal-originated and the answer was
        never forwarded — no error, no backstop, nothing in the log (2026-08-23, an image the
        agent read mid-turn).
        """
        if "toolUseResult" in rec:
            return True
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        return False

    @staticmethod
    def _is_harness_record(rec: dict) -> bool:
        """A ``user`` record the HARNESS wrote, not the person: the compaction summary
        (``isCompactSummary``), transcript-only notes (``isVisibleInTranscriptOnly``) and meta
        records (``isMeta``). Treating one as a prompt is the same trap as a tool result: it has
        no origin prefix, so it reclassified a live Telegram turn as terminal-originated and the
        real answer that followed was dropped without a log line (2026-09-02 22:25, right after
        a context compaction)."""
        return any(rec.get(k) for k in ("isCompactSummary", "isVisibleInTranscriptOnly", "isMeta"))

    def user_text(self, rec: dict) -> str | None:
        if rec.get("type") != "user" or self._is_tool_result(rec) or self._is_harness_record(rec):
            return None
        return _text_of(rec.get("message", {}).get("content"))

    def parse(self, rec: dict):
        typ = rec.get("type")
        if typ == "user":
            if self._is_tool_result(rec) or self._is_harness_record(rec):
                return                      # a tool result / harness note is not something the person typed
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
            stop = rec.get("message", {}).get("stop_reason")
            yield Ev("text", text=text, key=rec.get("uuid", "") or _hash(text),
                     final=stop == "end_turn", interim=stop == "tool_use")
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tid = b.get("id")
                if not tid:
                    continue
                paths = _file_tool_paths(b.get("name", ""), b.get("input"))
                if paths:
                    yield Ev("files", key=tid, files=paths)
                    continue                      # not a tool bubble — it is an attachment
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


def _codex_message_text(payload: dict, block_type: str) -> str:
    """Join the ``block_type`` parts of a Codex ``response_item/message`` payload.

    Codex >= 0.149 records user prompts (``input_text``) and assistant replies (``output_text``)
    only in this form; the legacy ``event_msg`` records are gone.
    """
    parts = payload.get("content")
    if not isinstance(parts, list):
        return ""
    return "".join(
        part.get("text") or "" for part in parts
        if isinstance(part, dict) and part.get("type") == block_type
    ).strip()


class CodexReader:
    """Codex CLI rollout transcript (``~/.codex/sessions/.../rollout-*.jsonl``). Each line is an
    ``event_msg`` or ``response_item`` with a ``payload.type``. Crucially it records explicit
    ``task_started`` / ``task_complete`` events, so turn boundaries (and thus the typing
    indicator and bubble cleanup) need no external Stop hook."""

    name = "codex"
    emits_turn_end = True

    def __init__(self) -> None:
        # Codex <= 0.144 logs an agent reply TWICE: first as ``event_msg/agent_message``, then as
        # ``response_item/message`` (role=assistant). Newer Codex (>= 0.149) logs ONLY the second
        # form. We therefore read both and remember what we already emitted, so old versions do not
        # double-send and new versions do not go silent. Silence is the dangerous failure here: the
        # bridge keeps showing "typing" while the reply never arrives (reported 2026-08-23).
        # A bounded window, not a session-wide set: the two records of one reply sit next to each
        # other in the log, while an agent legitimately repeating the same sentence later in the
        # session must still be delivered.
        self._recent_msgs: deque = deque(maxlen=8)
        self._recent_users: deque = deque(maxlen=8)

    def user_text(self, rec: dict) -> str | None:
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if rec.get("type") == "event_msg" and p.get("type") == "user_message":
            return p.get("message", "")
        if (rec.get("type") == "response_item" and p.get("type") == "message"
                and p.get("role") == "user"):
            return _codex_message_text(p, "input_text") or None
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
                h = _hash(msg.strip())
                if h in self._recent_users:
                    return
                self._recent_users.append(h)
                yield Ev("user", text=msg)
        elif t == "response_item" and pt == "message" and p.get("role") == "user":
            msg = _codex_message_text(p, "input_text")
            if msg and _hash(msg) not in self._recent_users:
                self._recent_users.append(_hash(msg))
                yield Ev("user", text=msg)
        elif t == "event_msg" and pt == "agent_message":
            msg = (p.get("message") or "").strip()
            if msg:
                ts = rec.get("timestamp", "")
                self._recent_msgs.append(_hash(msg))
                yield Ev("text", text=msg, key=f"{ts}:{_hash(msg)}",
                         final=(p.get("phase") == "final_answer"),
                         interim=(p.get("phase") == "commentary"))
        elif t == "response_item" and pt == "message" and p.get("role") == "assistant":
            # Fallback for Codex >= 0.149, which no longer emits event_msg/agent_message.
            msg = _codex_message_text(p, "output_text")
            if msg and _hash(msg) not in self._recent_msgs:
                ts = rec.get("timestamp", "")
                self._recent_msgs.append(_hash(msg))
                yield Ev("text", text=msg, key=f"{ts}:{_hash(msg)}",
                         final=(p.get("phase") == "final_answer" or p.get("phase") is None),
                         interim=(p.get("phase") == "commentary"))
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
