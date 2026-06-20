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

import glob
import html
import json
import logging
import threading
import time
from pathlib import Path

from . import readers
from .config import Config
from .session import TmuxSession
from .telegram import TelegramClient

log = logging.getLogger("agent2telegram.attach")

#: Fallback only: how long the transcript may be quiet before we force-end a turn, in case
#: the Stop-hook turn-end marker never arrives. The marker is the primary, precise signal —
#: this just stops "typing…" from hanging forever if the hook is missing/misconfigured.
IDLE_DONE = 90.0
#: How often we re-assert the "typing…" chat action (Telegram shows it for ~5s). Kept well
#: under that window so a turn never shows a gap, even right after a sent message clears it.
TYPING_INTERVAL = 1.5




class AttachBridge:
    def __init__(self, cfg: Config, *, client: TelegramClient | None = None) -> None:
        if not cfg.tmux_session:
            raise ValueError("attach mode requires 'tmux_session' in config")
        self.cfg = cfg
        self.tg = client or TelegramClient(cfg.token)
        self._allowed = set(cfg.allowed_user_ids)
        self._marker = cfg.progress_marker
        self._origin = cfg.origin_prefix
        # The reader knows the agent's transcript format and turns it into a common event stream.
        self._reader = readers.for_agent(cfg.agent)
        self._pending_turn_end = False       # set when the reader signals end-of-turn (Codex)
        # Accept the configured prefix plus the legacy "Telegram:" one, so a prefix change
        # mid-conversation doesn't drop the turn in flight.
        self._origins = tuple({p for p in (cfg.origin_prefix.strip(), "Telegram:", "[TG]") if p})
        self._owner_chat = cfg.allowed_user_ids[0] if cfg.allowed_user_ids else None
        self._signal = Path(cfg.signal_file) if cfg.signal_file else None
        # Precise end-of-turn marker written by the agent's Stop hook; lets "typing…" stay lit
        # continuously through long thinking/tool runs and switch off exactly when the turn ends.
        # Claude Code only: end-of-turn marker its Stop hook writes. Codex has no hook — its
        # rollout log records task_complete, so the reader signals turn end directly.
        self._turn_end = (self._signal.parent / "turn_end") if self._signal else None
        # Codex writes a fresh rollout-*.jsonl per session under ~/.codex/sessions; auto-detect
        # the newest one (and re-detect if the session restarts). Claude Code uses a fixed path.
        self._transcript = self._resolve_transcript()
        self._last_resolve = 0.0
        self._session = TmuxSession([], name=cfg.tmux_session, cwd=Path.home(),
                                    origin_prefix=cfg.origin_prefix, boot_wait=0)
        self._stop = threading.Event()
        # Persisted ledger of already-forwarded message uuids — survives restarts/crashes/reboots
        # so resuming an interrupted turn never re-sends what was already delivered.
        self._sent_path = Path.home() / ".config" / "agent2telegram" / "attach_sent.txt"
        try:
            self._sent_keys: set = set(self._sent_path.read_text("utf-8").split())
        except OSError:
            self._sent_keys = set()
        self._tpos = 0
        self._turn_active = threading.Event()
        self._turn_from_tg = False           # is the current transcript turn Telegram-originated?
        self._last_activity = 0.0            # monotonic ts of last transcript activity (for typing)
        self._status = {"mid": None, "shown": ""}   # live one-line tool-call status bubble
        self._last_typing = 0.0                      # monotonic ts of last "typing…" chat action
        self._typing_count = 0                       # diagnostics: typing actions in current turn
        self._turn_started = 0.0                     # diagnostics: monotonic ts of turn start
        self._max_gap = 0.0                          # diagnostics: largest gap between typing actions
        # Persist the bubble's message_id so a restart/crash mid-turn can delete the orphan it
        # would otherwise leave behind in the chat.
        self._status_path = (self._signal.parent / "status_bubble") if self._signal else None
        self._seen_tools: set = set()

    # ---- transcript resolution --------------------------------------------
    def _codex_sessions_dir(self) -> Path:
        tp = (self.cfg.transcript_path or "").strip()
        if tp and tp.lower() != "auto":
            p = Path(tp).expanduser()
            if p.is_dir():
                return p
        return Path.home() / ".codex" / "sessions"

    @staticmethod
    def _newest_under(base: Path, *patterns: str) -> Path | None:
        files: list[str] = []
        for pat in (patterns or ("*.jsonl",)):
            files = glob.glob(str(base / "**" / pat), recursive=True)
            if files:
                break
        if not files:
            return None
        try:
            return Path(max(files, key=lambda f: Path(f).stat().st_mtime))
        except OSError:
            return None

    def _newest_rollout(self) -> Path | None:
        return self._newest_under(self._codex_sessions_dir(), "rollout-*.jsonl", "*.jsonl")

    def _resolve_transcript(self) -> Path | None:
        """Resolve the transcript to tail. An explicit path is used as-is; ``""``/``"auto"``
        auto-detects the newest transcript for the agent (Codex rollout / Claude Code session)."""
        tp = (self.cfg.transcript_path or "").strip()
        if tp and tp.lower() != "auto":
            p = Path(tp).expanduser()
            return self._newest_under(p) if p.is_dir() else p
        if self.cfg.agent == "codex":
            return self._newest_rollout()
        if self.cfg.agent == "claude-code":
            return self._newest_under(Path.home() / ".claude" / "projects")
        return None

    def _maybe_reresolve_codex(self) -> None:
        """If Codex started a new session (newer rollout file), follow it from its start."""
        if self.cfg.agent != "codex":
            return
        now = time.monotonic()
        if now - self._last_resolve < 5.0:
            return
        self._last_resolve = now
        newest = self._newest_rollout()
        if newest and newest != self._transcript:
            log.info("Codex session switched → %s", newest.name)
            self._transcript = newest
            self._tpos = 0
            self._resume_position()

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        me = self.tg.get_me()
        log.info("Attach bridge live as @%s → tmux '%s', owner=%s",
                 me.get("username"), self.cfg.tmux_session, self._owner_chat)
        if not self._session.alive:
            raise RuntimeError(f"tmux session '{self.cfg.tmux_session}' not found")
        # Resume at the start of the current turn (right after the last user message) rather
        # than at EOF, so a reply written while we were restarting still gets forwarded. The
        # persisted ledger dedups, so already-delivered progress/final messages aren't re-sent.
        if self._transcript and self._transcript.exists():
            self._tpos = self._transcript.stat().st_size
            self._resume_position()
        self._cleanup_orphan_status()       # remove a bubble orphaned by a prior crash/restart
        # Typing runs in its own thread so a flood-control sleep in the send path never starves it.
        threading.Thread(target=self._outbound_loop, daemon=True).start()
        threading.Thread(target=self._typing_loop, daemon=True).start()
        self._inbound_loop()

    def _resume_position(self) -> None:
        """Find the most recent non-empty user message and rewind ``_tpos`` to just after it,
        so the current turn's assistant messages are re-read on startup. Combined with the
        persisted ledger this re-delivers a reply that was written while we were down, without
        re-sending anything already delivered. Also recovers the turn's Telegram origin."""
        size = self._tpos
        start = max(0, size - 5_000_000)        # large window: tool outputs can be big
        try:
            with open(self._transcript, "rb") as f:
                f.seek(start)
                tail = f.read()
        except OSError:
            return
        pos = start
        last_user_end = None
        from_tg = self._turn_from_tg
        for raw in tail.split(b"\n"):
            line_end = pos + len(raw) + 1       # +1 for the newline separator
            pos = line_end
            try:
                rec = json.loads(raw.decode("utf-8", "ignore"))
            except (json.JSONDecodeError, ValueError):
                continue
            utext = self._reader.user_text(rec)
            if utext and utext.strip():
                from_tg = utext.lstrip().startswith(self._origins)
                last_user_end = min(line_end, size)
        if last_user_end is not None:
            self._tpos = last_user_end
            self._turn_from_tg = from_tg

    def _mark_sent(self, uuid: str) -> None:
        """Record a forwarded message uuid in memory and on disk (append-only ledger)."""
        if not uuid or uuid in self._sent_keys:
            return
        self._sent_keys.add(uuid)
        try:
            self._sent_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._sent_path, "a", encoding="utf-8") as f:
                f.write(uuid + "\n")
        except OSError:
            pass

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

        # Light "typing…" from the very first moment — including the voice-transcription /
        # file-download window (seconds), so the indicator never has a gap at the start.
        self._consume_turn_end()                 # drop any stale end-marker from a prior turn
        now = time.monotonic()
        self._turn_active.set()
        self._last_activity = now
        self._turn_started = now
        self._typing_count = 1
        self._max_gap = 0.0
        self._last_typing = now
        self.tg.send_chat_action(self._owner_chat, "typing")   # instant, don't wait for the loop
        log.info("TURN START t=%.2f", time.time())

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

    def _typing_loop(self) -> None:
        """Dedicated thread: assert "typing…" every TYPING_INTERVAL while a turn is active.

        It runs independently of the outbound/send loop, so a flood-control sleep in the send path
        (which happens during a burst of messages) can never starve the indicator — that was the
        cause of mid-turn typing gaps. It stops the instant the turn ends (turn_active cleared), so
        no action fires after the final message and typing stops with it (bar Telegram's ~5s decay)."""
        while not self._stop.is_set():
            if self._turn_active.is_set() and self._owner_chat is not None:
                now = time.monotonic()
                gap = now - self._last_typing
                if gap > self._max_gap:
                    self._max_gap = gap
                self.tg.send_chat_action(self._owner_chat, "typing")
                self._last_typing = now
                self._typing_count += 1
            self._stop.wait(TYPING_INTERVAL)

    def _consume_turn_end(self) -> None:
        if self._turn_end is not None:
            try:
                self._turn_end.unlink()
            except OSError:
                pass

    def _finish_turn(self) -> None:
        """Drop the technical bubble and stop the typing indicator at the real end of a turn."""
        self._status_clear()
        was_active = self._turn_active.is_set()
        self._turn_active.clear()
        self._pending_turn_end = False
        self._consume_turn_end()
        if was_active:
            log.info("TURN END t=%.2f dur=%.1fs typing_fired=%d max_gap=%.2fs",
                     time.time(), time.monotonic() - self._turn_started,
                     self._typing_count, self._max_gap)

    def _end_turn(self) -> None:
        # Claude Stop-hook path: catch anything written just before the hook fired, then finish.
        self._drain_transcript()
        self._finish_turn()

    # ---- outbound (session → Telegram) ------------------------------------
    def _outbound_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._maybe_reresolve_codex()
                self._drain_transcript()      # may set _pending_turn_end (Codex task_complete)
                self._drain_signal()
                # End-of-turn detection, in priority order:
                #   * Codex: the reader saw task_complete → end now (no hook needed).
                #   * Claude Code: the Stop hook wrote the end-of-turn marker. Authoritative even
                #     if turn_active is unset (e.g. a restart mid-turn that would orphan a bubble).
                #   * Fallback: force-end if the transcript went quiet too long (hook missing).
                if self._pending_turn_end:
                    self._finish_turn()
                elif self._turn_end is not None and self._turn_end.exists():
                    self._end_turn()
                elif self._turn_active.is_set() and time.monotonic() - self._last_activity > IDLE_DONE:
                    self._status_clear()
                    self._turn_active.clear()
            except Exception as e:
                log.error("outbound error: %s", e)
            self._stop.wait(0.4)

    # ---- live tool-call status bubble (shown during the turn, deleted at the end) ------
    def _status_push(self, line: str) -> None:
        # Single line, emoji at the start, rendered in italics. One bubble is edited in place
        # across a run of consecutive tool calls; it's deleted when the next progress message
        # arrives (then re-created below it) and at turn end — so it always trails at the bottom.
        if self._owner_chat is None or not line or line == self._status["shown"]:
            return
        body = f"<i>{html.escape(line)}</i>"
        if self._status["mid"] is None:
            mid = self.tg.send_plain_id(self._owner_chat, body, parse_mode="HTML")
            if mid:
                self._status["mid"] = mid
                self._status["shown"] = line
                self._persist_status(mid)
        else:
            self.tg.edit_plain(self._owner_chat, self._status["mid"], body, parse_mode="HTML")
            self._status["shown"] = line

    def _status_clear(self) -> None:
        if self._status["mid"] is not None and self._owner_chat is not None:
            self.tg.delete_message(self._owner_chat, self._status["mid"])
        self._status = {"mid": None, "shown": ""}
        self._seen_tools.clear()
        self._persist_status(None)

    def _persist_status(self, mid: int | None) -> None:
        if self._status_path is None:
            return
        try:
            if mid is None:
                self._status_path.unlink()
            else:
                self._status_path.parent.mkdir(parents=True, exist_ok=True)
                self._status_path.write_text(str(mid), "utf-8")
        except OSError:
            pass

    def _cleanup_orphan_status(self) -> None:
        """Delete a status bubble left over from a previous run that died mid-turn."""
        if self._status_path is None or self._owner_chat is None:
            return
        try:
            mid = int(self._status_path.read_text("utf-8").strip())
        except (OSError, ValueError):
            return
        self.tg.delete_message(self._owner_chat, mid)
        try:
            self._status_path.unlink()
        except OSError:
            pass

    def _drain_signal(self) -> None:
        if not self._signal or not self._signal.exists():
            return
        try:
            answer = self._signal.read_text("utf-8").strip()
            self._signal.unlink()
        except OSError:
            return
        if answer and self._owner_chat is not None:
            self._status_clear()                         # final message → drop the technical bubble
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
        for raw in chunk[:nl].split(b"\n"):
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for ev in self._reader.parse(rec):
                self._handle_event(ev)
        # Any new transcript content during a Telegram turn = the agent is still working;
        # refresh activity so the idle fallback doesn't fire prematurely.
        if self._turn_from_tg:
            self._last_activity = time.monotonic()

    def _strip_marker(self, text: str) -> str:
        """Remove a leading progress marker (e.g. ``[TG]``) from the first line, if present."""
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith(self._marker):
            lines[0] = lines[0].lstrip()[len(self._marker):].lstrip()
        return "\n".join(lines).strip()

    def _handle_event(self, ev) -> None:
        """Apply one normalized reader event to the Telegram side."""
        if ev.kind == "user":
            # Remember whether this turn came from Telegram (origin prefix) — only those are
            # forwarded; terminal-originated turns stay local.
            self._turn_from_tg = ev.text.lstrip().startswith(self._origins)
            return
        if ev.kind == "turn_start":
            return                              # inbound already lit typing; nothing else to do
        if ev.kind == "turn_end":
            self._pending_turn_end = True       # outbound loop finishes the turn after this drain
            return
        if not self._turn_from_tg or self._owner_chat is None:
            return
        if ev.kind == "text":
            out = self._strip_marker(ev.text)
            if out and ev.key not in self._sent_keys:
                self._mark_sent(ev.key)         # ledger dedups across restarts
                # A new progress message → delete the current technical bubble so the next tool
                # calls re-create it BELOW this message (the bubble always trails at the bottom).
                self._status_clear()
                self.tg.send_message(self._owner_chat, out)
        elif ev.kind == "tool":
            if ev.key and ev.key not in self._seen_tools:
                self._seen_tools.add(ev.key)
                self._status_push(ev.text)

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
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(default).name) or "file"
        if "." not in name and (ext := Path(fp).suffix):
            name += ext
        d = Path.home() / ".local/state/agent2telegram/attachments"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / name
        dest.write_bytes(data)
        return f"[The user attached a file saved at: {dest} — open and use it as appropriate.]"
