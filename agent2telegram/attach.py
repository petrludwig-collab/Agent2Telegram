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
from pathlib import Path

from .config import Config
from .session import TmuxSession
from .telegram import TelegramClient

log = logging.getLogger("agent2telegram.attach")


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
        self._sent_marker_keys: set = set()
        self._tpos = 0
        self._turn_active = threading.Event()

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        me = self.tg.get_me()
        log.info("Attach bridge live as @%s → tmux '%s', owner=%s",
                 me.get("username"), self.cfg.tmux_session, self._owner_chat)
        if not self._session.alive:
            raise RuntimeError(f"tmux session '{self.cfg.tmux_session}' not found")
        # Start tailing the transcript from its current end (don't replay history).
        if self._transcript and self._transcript.exists():
            self._tpos = self._transcript.stat().st_size
        threading.Thread(target=self._outbound_loop, daemon=True).start()
        threading.Thread(target=self._typing_loop, daemon=True).start()
        self._inbound_loop()

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
            except Exception as e:
                log.error("outbound error: %s", e)
            self._stop.wait(0.4)

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
        with open(self._transcript, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(self._tpos)
            chunk = f.read()
            self._tpos = f.tell()
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            uuid = rec.get("uuid", "")
            for block in rec.get("message", {}).get("content", []):
                if block.get("type") != "text":
                    continue
                for tline in block.get("text", "").splitlines():
                    if tline.lstrip().startswith(self._marker):
                        key = (uuid, tline)
                        if key in self._sent_marker_keys:
                            continue
                        self._sent_marker_keys.add(key)
                        clean = tline.lstrip()[len(self._marker):].strip()
                        if clean and self._owner_chat is not None:
                            self.tg.send_message(self._owner_chat, clean)
                            self._turn_active.clear()

    # ---- typing indicator --------------------------------------------------
    def _typing_loop(self) -> None:
        while not self._stop.is_set():
            if self._turn_active.is_set() and self._owner_chat is not None:
                self.tg.send_chat_action(self._owner_chat, "typing")
            self._stop.wait(4)

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
