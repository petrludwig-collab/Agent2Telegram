"""The bridge: poll Telegram, dispatch each message to the agent, send the reply back.

Concurrency model
-----------------
A single poller thread reads updates and enqueues them. Each chat gets its own worker
thread and queue, so:
  * messages from the *same* chat are processed strictly in order (the agent is never
    hit twice concurrently for one conversation), and
  * different chats run in parallel.

The poller never blocks on a slow agent, so Telegram long-polling keeps flowing and the
bot stays responsive (e.g. to ``/status``) even while a long task runs elsewhere.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import signal
import threading
from dataclasses import dataclass
from pathlib import Path

from . import __version__, adapters
from .config import Config, _state_dir
from .redaction import message_metadata, update_metadata
from .telegram import TelegramClient, TelegramError, file_media, is_network_error

log = logging.getLogger("agent2telegram.bridge")


@dataclass
class Task:
    """One unit of work for a chat: the prompt text and an optional downloaded file."""
    text: str = ""
    attachment: str | None = None   # absolute path to a downloaded image/document, if any

_HELP = (
    "🤖 *Agent2Telegram*\n"
    "Send me a message and I'll pass it to the connected agent.\n\n"
    "Commands:\n"
    "/id — show your Telegram IDs (for the allow-list)\n"
    "/reset — start a fresh conversation\n"
    "/status — bridge status\n"
    "/help — this help"
)


class Bridge:
    def __init__(self, cfg: Config, *, client: TelegramClient | None = None) -> None:
        self.cfg = cfg
        self.tg = client or TelegramClient(cfg.token)
        self.adapter = adapters.build(cfg)
        self._allowed = set(cfg.allowed_user_ids)
        self._stt_key = cfg.elevenlabs_api_key
        self._stt_lang = getattr(cfg, "elevenlabs_language", "") or None
        self._stop = threading.Event()
        self._workers: dict[int, "_ChatWorker"] = {}
        self._workers_lock = threading.Lock()
        self._offset_file = _state_dir(cfg) / "offset"
        # Continuity is tracked on disk (a marker per chat dir), so it survives a restart
        # and a fresh conversation resumes correctly instead of starting over.

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        me = self._connect()
        if me and self.cfg.allowed_user_ids:
            from .notices import restart_notice
            restart_notice(self, self.cfg.allowed_user_ids[0])
        log.info("Connected as @%s — agent=%s, authorized users=%s",
                 me.get("username"), self.cfg.agent, sorted(self._allowed) or "(none!)")
        if not self._allowed:
            log.warning("No allowed_user_ids configured — the bot will refuse everyone. "
                        "Message the bot and check /id, then add your id to the config.")
        self._install_signal_handlers()
        offset = self._load_offset()
        transient_fails = 0
        outage_alerted = False
        while not self._stop.is_set():
            try:
                updates = self.tg.get_updates(offset, timeout=self.cfg.poll_timeout)
            except (TelegramError, OSError) as e:
                if is_network_error(e):
                    # Transient network/DNS problem (Errno 8, connection reset, timeout) — the
                    # bridge recovers on its own. WARNING + backoff; ERROR only ONCE during a
                    # longer outage, so monitoring doesn't alert on a self-healing blip.
                    transient_fails += 1
                    if transient_fails >= 10 and not outage_alerted:
                        log.error("getUpdates network outage (%d in a row) is lasting: %s",
                                  transient_fails, e)
                        outage_alerted = True
                    else:
                        log.warning("getUpdates transient network error (%d): %s", transient_fails, e)
                    self._stop.wait(min(3 * transient_fails, 30))
                    continue
                log.error("getUpdates failed: %s", e)     # a real (non-network) error
                self._stop.wait(3)
                continue
            if outage_alerted:
                log.info("getUpdates network recovered after %d errors", transient_fails)
            transient_fails = 0
            outage_alerted = False
            for upd in updates:
                try:
                    update_id = int(upd["update_id"])
                except (KeyError, TypeError, ValueError):
                    log.warning("skipping malformed Telegram update without a valid update_id: %s",
                                update_metadata(upd))
                    continue
                offset = max(offset, update_id + 1)
                try:
                    self._dispatch(upd)
                except Exception as e:
                    log.error("dispatch error: error=%s %s", type(e).__name__, update_metadata(upd))
            self._save_offset(offset)
        self._shutdown()

    def _connect(self) -> dict:
        """Verify the token at startup, retrying so a not-yet-ready network at boot
        doesn't crash the service (it just waits for connectivity)."""
        delay = 2
        while not self._stop.is_set():
            try:
                return self.tg.get_me()
            except Exception as e:
                log.warning("Telegram not reachable yet (%s); retrying in %ss", e, delay)
                self._stop.wait(delay)
                delay = min(delay * 2, 60)
        return {}

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except ValueError:
                pass  # not in main thread (e.g. tests) — caller drives _stop

    def _shutdown(self) -> None:
        log.info("Shutting down…")
        with self._workers_lock:
            for w in self._workers.values():
                w.stop()
        for w in list(self._workers.values()):
            w.join(timeout=5)

    # ---- dispatch ----------------------------------------------------------
    _SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

    def _dispatch(self, update: dict) -> None:
        msg = update.get("message")
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        user = msg.get("from", {})
        user_id = user.get("id")
        text = (msg.get("text") or msg.get("caption") or "").strip()

        from .mentions import accepts_message
        if not accepts_message(self.cfg, msg):
            return

        if text.startswith("/") and self._handle_command(chat_id, user_id, text):
            return

        if user_id not in self._allowed:
            log.warning("Refused message from unauthorized user %s (%s)", user_id, user.get("username"))
            self.tg.send_message(
                chat_id,
                "⛔ You're not authorized to use this bot.\n"
                f"Your user id is `{user_id}` — ask the owner to add it.",
                parse_mode="Markdown",
            )
            return

        task = self._build_task(chat_id, msg, text)
        if task is not None:
            self._enqueue(chat_id, task)

    def _build_task(self, chat_id: int, msg: dict, text: str) -> "Task | None":
        # Image → download the largest size and attach it.
        if msg.get("photo"):
            path = self._download(chat_id, msg["photo"][-1]["file_id"], default_name="image.jpg")
            if not path:
                self.tg.send_message(chat_id, "⚠️ Couldn't download the image.")
                return None
            return Task(text=text, attachment=path)
        # Document, video, video note or GIF: hand it over as a file (issue #12).
        found = file_media(msg)
        if found:
            doc, fallback = found
            path = self._download(chat_id, doc["file_id"], default_name=doc.get("file_name") or fallback)
            if not path:
                self.tg.send_message(chat_id, "⚠️ Couldn't download the file.")
                return None
            return Task(text=text, attachment=path)
        # Voice / audio → transcribe (only if a key is configured).
        media = msg.get("voice") or msg.get("audio")
        if media:
            if not self._stt_key:
                self.tg.send_message(
                    chat_id, "🎤 Voice messages aren't enabled. Add an ElevenLabs API key "
                    "(ELEVENLABS_API_KEY) to transcribe them.")
                return None
            transcript = self._transcribe(chat_id, media["file_id"])
            if not transcript:
                return None
            self.tg.send_message(chat_id, f"📝 _{transcript}_", parse_mode="Markdown")
            return Task(text=transcript)
        if text:
            return Task(text=text)
        self.tg.send_message(chat_id, "ℹ️ I can handle text, images, videos, files and (if enabled) voice.")
        return None

    def _download(self, chat_id: int, file_id: str, *, default_name: str) -> str | None:
        try:
            file_path = self.tg.get_file_path(file_id)
            data = self.tg.download(file_path)
        except Exception as e:
            log.error("attachment download failed: %s", e)
            return None
        name = self._SAFE_NAME.sub("_", Path(default_name).name) or "file"
        if "." not in name and (ext := Path(file_path).suffix):
            name += ext
        dest = self._unique(self.chat_dir(chat_id) / "attachments" / name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return str(dest)

    @staticmethod
    def _unique(p: Path) -> Path:
        if not p.exists():
            return p
        i = 1
        while (cand := p.with_name(f"{p.stem}-{i}{p.suffix}")).exists():
            i += 1
        return cand

    def _transcribe(self, chat_id: int, file_id: str) -> str | None:
        from . import stt
        try:
            file_path = self.tg.get_file_path(file_id)
            audio = self.tg.download(file_path)
            return stt.transcribe(audio, api_key=self._stt_key, language=self._stt_lang,
                                  filename=Path(file_path).name or "voice.ogg")
        except Exception as e:
            log.error("voice transcription failed: %s", e)
            self.tg.send_message(chat_id, f"⚠️ Couldn't transcribe the voice message: {e}")
            return None

    def _handle_command(self, chat_id: int, user_id: int | None, text: str) -> bool:
        cmd = text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in ("start", "help"):
            self.tg.send_message(chat_id, _HELP, parse_mode="Markdown")
            return True
        if cmd == "id":
            self.tg.send_message(
                chat_id, f"user id: `{user_id}`\nchat id: `{chat_id}`", parse_mode="Markdown")
            return True
        if cmd == "status":
            authed = "✅" if user_id in self._allowed else "⛔ (not authorized)"
            self.tg.send_message(
                chat_id,
                f"🤖 Agent2Telegram v{__version__}\nagent: {self.cfg.agent}\nyou: {authed}",
            )
            return True
        if cmd == "reset":
            if user_id in self._allowed:
                self._reset_chat(chat_id)
                self.tg.send_message(chat_id, "🔄 Fresh conversation started.")
            else:
                self.tg.send_message(chat_id, "⛔ You're not authorized to use this bot.")
            return True
        return False  # not a known command → treat as a normal prompt

    # ---- per-chat workers --------------------------------------------------
    def _enqueue(self, chat_id: int, task: "Task") -> None:
        with self._workers_lock:
            worker = self._workers.get(chat_id)
            if worker is None:
                worker = _ChatWorker(chat_id, self)
                self._workers[chat_id] = worker
                worker.start()
        worker.submit(task)

    def chat_dir(self, chat_id: int) -> Path:
        return self.cfg.path_workdir() / str(chat_id)

    def _reset_chat(self, chat_id: int) -> None:
        import shutil
        d = self.chat_dir(chat_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _marker(chat_dir: Path) -> Path:
        return chat_dir / ".a2t_started"

    def process(self, chat_id: int, task: "Task") -> None:
        """Run the agent for one task and reply. Runs inside a chat worker thread."""
        chat_dir = self.chat_dir(chat_id)
        prompt = task.text
        if task.attachment:
            note = (f"[The user attached a file, saved at: {task.attachment}\n"
                    f"Open and use it as appropriate.]")
            prompt = f"{prompt}\n\n{note}".strip() if prompt else note
        # Continue an existing conversation only after a *successful* first turn (marker).
        is_cont = self._marker(chat_dir).exists()
        with self._keep_typing(chat_id):
            try:
                reply = self.adapter.run(prompt, chat_dir=chat_dir, is_continuation=is_cont)
            except Exception as e:
                log.error("agent run failed for chat %s: error=%s %s", chat_id,
                          type(e).__name__, message_metadata(prompt, attachments=int(bool(task.attachment))))
                self.tg.send_message(chat_id, f"⚠️ Agent error: {e}")
                return
        try:
            self._marker(chat_dir).touch()
        except OSError:
            pass
        self.tg.send_message(chat_id, reply or "(the agent returned no output)")

    def _keep_typing(self, chat_id: int):
        """Context manager that keeps the Telegram 'typing…' indicator alive for the
        whole agent run (it otherwise expires after ~5s)."""
        bridge = self

        class _Typing:
            def __enter__(self):
                self._stop = threading.Event()
                self._t = threading.Thread(target=self._loop, daemon=True)
                self._t.start()
                return self

            def _loop(self):
                while not self._stop.is_set():
                    bridge.tg.send_chat_action(chat_id, "typing")
                    self._stop.wait(4)

            def __exit__(self, *exc):
                self._stop.set()
                self._t.join(timeout=1)

        return _Typing()

    # ---- offset persistence ------------------------------------------------
    def _load_offset(self) -> int:
        try:
            return int(json.loads(self._offset_file.read_text())["offset"])
        except Exception:
            return 0

    def _save_offset(self, offset: int) -> None:
        try:
            self._offset_file.parent.mkdir(parents=True, exist_ok=True)
            self._offset_file.write_text(json.dumps({"offset": offset}))
        except OSError as e:
            log.warning("could not persist offset: %s", e)


class _ChatWorker(threading.Thread):
    """Serializes processing for a single chat."""

    def __init__(self, chat_id: int, bridge: Bridge) -> None:
        super().__init__(daemon=True, name=f"chat-{chat_id}")
        self.chat_id = chat_id
        self.bridge = bridge
        self.q: queue.Queue = queue.Queue()

    def submit(self, task) -> None:
        self.q.put(task)

    def stop(self) -> None:
        self.q.put(None)

    def run(self) -> None:
        while True:
            task = self.q.get()
            if task is None:
                return
            try:
                self.bridge.process(self.chat_id, task)
            except Exception as e:  # belt and braces: a worker must never die silently
                log.error("worker %s crashed handling a message: error=%s %s", self.chat_id,
                          type(e).__name__, message_metadata(task.text, attachments=int(bool(task.attachment))))
