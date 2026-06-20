"""Attach mode — drive an existing live agent session, the way a hand-rolled bridge does.

Async model (mirrors a proven setup):
  * **inbound**: poll Telegram → inject the message into the live tmux session via send-keys.
    No blocking wait.
  * **outbound** (background thread):
      - tail the agent transcript; lines starting with the progress marker (e.g. ``[tg]``)
        are sent **live** during the turn (interim/multi-part updates);
      - watch the Stop-hook signal file; when it appears it's the **final** answer of a turn
        that did *not* use the marker — send it.
  * a "typing…" indicator runs while a turn is in flight.

This keeps the agent's full session (context, persona, tools) and adds Telegram I/O around it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
from pathlib import Path

from .config import Config
from .session import TmuxSession
from .telegram import TelegramClient

log = logging.getLogger("agent2telegram.attach")

#: How long the transcript must be quiet before we consider a turn finished.
IDLE_DONE = 10.0


def _short(s: str, n: int = 58) -> str:
    s = " ".join(str(s).split()).replace("**", "").replace("`", "")
    return s if len(s) <= n else s[:n - 1] + "…"


def _tool_summary(name: str, inp: dict) -> str:
    """A short human line describing a tool call, for the live status bubble."""
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


class AttachBridge:
    def __init__(self, cfg: Config, *, client: TelegramClient | None = None) -> None:
        if not cfg.tmux_session:
            raise ValueError("attach mode requires 'tmux_session' in config")
        self.cfg = cfg
        self.tg = client or TelegramClient(cfg.token)
        self._allowed = set(cfg.allowed_user_ids)
        self._marker = cfg.progress_marker
        self._origin = cfg.origin_prefix
        self._owner_chat = cfg.allowed_user_ids[0] if cfg.allowed_user_ids else None
        self._signal = Path(cfg.signal_file) if cfg.signal_file else None
        self._transcript = Path(cfg.transcript_path) if cfg.transcript_path else None
        self._session = TmuxSession([], name=cfg.tmux_session, cwd=Path.home(),
                                    origin_prefix=cfg.origin_prefix, boot_wait=0)
        self._stop = threading.Event()
        self._sent_keys: set = set()
        self._tpos = 0
        self._turn_active = threading.Event()
        self._turn_from_tg = False           # is the current transcript turn Telegram-originated?
        self._last_activity = 0.0            # monotonic ts of last transcript activity (for typing)
        self._status = {"mid": None, "lines": [], "shown": ""}   # live tool-call status bubble
        self._seen_tools: set = set()

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        me = self.tg.get_me()
        log.info("Attach bridge live as @%s → tmux '%s', owner=%s",
                 me.get("username"), self.cfg.tmux_session, self._owner_chat)
        if not self._session.alive:
            raise RuntimeError(f"tmux session '{self.cfg.tmux_session}' not found")
        # Start tailing the transcript from its current end (don't replay history),
        # but recover the current turn's origin so a restart mid-turn still forwards the rest.
        if self._transcript and self._transcript.exists():
            self._tpos = self._transcript.stat().st_size
            self._detect_initial_origin()
        threading.Thread(target=self._outbound_loop, daemon=True).start()
        threading.Thread(target=self._typing_loop, daemon=True).start()
        self._inbound_loop()

    def _detect_initial_origin(self) -> None:
        """Recover whether the in-progress turn is Telegram-originated by scanning the tail
        for the most recent non-empty user message (so a restart mid-turn keeps forwarding)."""
        origin = self._origin.strip()
        try:
            with open(self._transcript, "rb") as f:
                f.seek(max(0, self._tpos - 65536))
                tail = f.read()
        except OSError:
            return
        for raw in reversed(tail.split(b"\n")):
            try:
                rec = json.loads(raw.decode("utf-8", "ignore"))
            except (json.JSONDecodeError, ValueError):
                continue
            if rec.get("type") != "user":
                continue
            content = rec.get("message", {}).get("content")
            utext = content if isinstance(content, str) else "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            ) if isinstance(content, list) else ""
            if utext.strip():
                self._turn_from_tg = utext.lstrip().startswith(origin) if origin else True
                return

    # ---- inbound (Telegram → session) -------------------------------------
    def _inbound_loop(self) -> None:
        offset = 0
        allowed_updates = json.dumps(["message", "edited_message", "message_reaction"])
        while not self._stop.is_set():
            try:
                updates = self.tg._call(
                    "getUpdates",
                    {"offset": offset, "timeout": self.cfg.poll_timeout,
                     "allowed_updates": allowed_updates},
                    timeout=self.cfg.poll_timeout + 15,
                )
            except Exception as e:
                log.error("getUpdates failed: %s", e)
                self._stop.wait(3)
                continue
            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)
                try:
                    self._handle(upd)
                except Exception as e:
                    log.exception("inbound error: %s", e)

    def _handle(self, upd: dict) -> None:
        # Reactions (e.g. ❤️) → quick-feedback line.
        mr = upd.get("message_reaction")
        if mr:
            if mr.get("user", {}).get("id") not in self._allowed:
                return
            emojis = "".join(r.get("emoji", "") for r in mr.get("new_reaction", [])
                             if r.get("type") == "emoji")
            if emojis:
                self._inject(f"{emojis} reacted {emojis} to your message #{mr.get('message_id')} "
                             f"— quick feedback; no need to reply unless relevant.")
            return

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        user_id = msg.get("from", {}).get("id")
        chat_id = msg["chat"]["id"]
        if user_id not in self._allowed:
            self.tg.send_message(chat_id, "⛔ Not authorized.")
            return

        text = (msg.get("text") or msg.get("caption") or "").strip()
        if msg.get("voice") or msg.get("audio"):
            text = self._transcribe(msg.get("voice") or msg.get("audio"), chat_id) or text
            if not text:
                return
        elif msg.get("photo") or msg.get("document"):
            note = self._download_note(msg, chat_id)
            text = f"{text}\n{note}".strip() if note else text
        if text:
            self._inject(text)

    def _inject(self, text: str) -> None:
        self._turn_active.set()
        self._last_activity = time.monotonic()   # keep typing lit from the very start
        try:
            self._session.inject(text)
        except Exception as e:
            log.error("inject failed: %s", e)
            self._turn_active.clear()

    # ---- outbound (session → Telegram) ------------------------------------
    def _outbound_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_transcript()
                self._drain_signal()
                # Turn finished (transcript quiet) → remove the live status bubble, stop typing.
                if self._turn_active.is_set() and time.monotonic() - self._last_activity > IDLE_DONE:
                    self._status_clear()
                    self._turn_active.clear()
            except Exception as e:
                log.error("outbound error: %s", e)
            self._stop.wait(0.4)

    # ---- live tool-call status bubble (shown during the turn, deleted at the end) ------
    def _status_push(self, line: str) -> None:
        if self._owner_chat is None:
            return
        self._status["lines"].append(line)
        text = "🛠️ Working…\n" + "\n".join(self._status["lines"][-8:])
        if text == self._status["shown"]:
            return
        if self._status["mid"] is None:
            mid = self.tg.send_plain_id(self._owner_chat, text)
            if mid:
                self._status["mid"] = mid
                self._status["shown"] = text
        else:
            self.tg.edit_plain(self._owner_chat, self._status["mid"], text)
            self._status["shown"] = text

    def _status_clear(self) -> None:
        if self._status["mid"] is not None and self._owner_chat is not None:
            self.tg.delete_message(self._owner_chat, self._status["mid"])
        self._status = {"mid": None, "lines": [], "shown": ""}
        self._seen_tools.clear()

    def _drain_signal(self) -> None:
        if not self._signal or not self._signal.exists():
            return
        try:
            answer = self._signal.read_text("utf-8").strip()
            self._signal.unlink()
        except OSError:
            return
        if answer and self._owner_chat is not None:
            self.tg.send_message(self._owner_chat, answer)
            self._turn_active.clear()

    def _drain_transcript(self) -> None:
        if not self._transcript or not self._transcript.exists():
            return
        size = self._transcript.stat().st_size
        if size < self._tpos:          # file rotated/truncated
            self._tpos = 0
        if size == self._tpos:
            return
        with open(self._transcript, "rb") as f:
            f.seek(self._tpos)
            chunk = f.read()
        # Only consume up to the last complete line; keep a partial trailing line for next time.
        nl = chunk.rfind(b"\n")
        if nl == -1:
            return
        self._tpos += nl + 1
        origin = self._origin.strip()
        for raw in chunk[:nl].split(b"\n"):
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = rec.get("type")
            content = rec.get("message", {}).get("content")
            if typ == "user":
                # New turn — remember if it came from Telegram, so we forward EVERY progress
                # message of this turn (terminal-originated turns stay local).
                utext = content if isinstance(content, str) else "\n".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                ) if isinstance(content, list) else ""
                if utext.strip():
                    self._turn_from_tg = utext.lstrip().startswith(origin) if origin else True
                continue
            if typ != "assistant" or not self._turn_from_tg:
                continue
            blocks = content if isinstance(content, list) else []
            # Tool calls → live status bubble (edited in place, deleted at turn end).
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tid = b.get("id")
                    if tid and tid not in self._seen_tools:
                        self._seen_tools.add(tid)
                        self._status_push(_tool_summary(b.get("name", ""), b.get("input")))
            # Text → a kept progress message.
            text = "\n".join(b.get("text", "") for b in blocks
                             if isinstance(b, dict) and b.get("type") == "text").strip()
            if not text:
                continue
            uuid = rec.get("uuid", "")
            if uuid in self._sent_keys:
                continue
            self._sent_keys.add(uuid)
            lines = text.splitlines()
            if lines and lines[0].lstrip().startswith(self._marker):
                lines[0] = lines[0].lstrip()[len(self._marker):].lstrip()   # strip internal cue
            out = "\n".join(lines).strip()
            if out and self._owner_chat is not None:
                self.tg.send_message(self._owner_chat, out)
        # Any new transcript content during a Telegram turn = the agent is still working;
        # refresh activity so the typing indicator stays lit until the turn goes quiet.
        if self._turn_from_tg:
            self._last_activity = time.monotonic()

    # ---- typing indicator --------------------------------------------------
    def _typing_loop(self) -> None:
        while not self._stop.is_set():
            if self._turn_active.is_set() and self._owner_chat is not None:
                self.tg.send_chat_action(self._owner_chat, "typing")
            self._stop.wait(3)

    # ---- media helpers (reuse the same download/STT as one-shot mode) ------
    def _transcribe(self, media: dict, chat_id: int) -> str | None:
        from . import stt
        if not self.cfg.elevenlabs_api_key:
            self.tg.send_message(chat_id, "🎤 Voice isn't enabled (no ElevenLabs key).")
            return None
        try:
            fp = self.tg.get_file_path(media["file_id"])
            audio = self.tg.download(fp)
            return stt.transcribe(audio, api_key=self.cfg.elevenlabs_api_key,
                                  filename=Path(fp).name or "voice.ogg")
        except Exception as e:
            log.error("transcription failed: %s", e)
            self.tg.send_message(chat_id, f"⚠️ Couldn't transcribe: {e}")
            return None

    def _download_note(self, msg: dict, chat_id: int) -> str:
        import re
        if msg.get("photo"):
            file_id, default = msg["photo"][-1]["file_id"], "image.jpg"
        else:
            doc = msg["document"]
            file_id, default = doc["file_id"], doc.get("file_name") or "file"
        try:
            fp = self.tg.get_file_path(file_id)
            data = self.tg.download(fp)
        except Exception:
            self.tg.send_message(chat_id, "⚠️ Couldn't download the attachment.")
            return ""
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(default)) or "file"
        if "." not in name and (ext := Path(fp).suffix):
            name += ext
        d = Path.home() / ".local/state/agent2telegram/attachments"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / name
        dest.write_bytes(data)
        return f"[The user attached a file saved at: {dest} — open and use it as appropriate.]"
