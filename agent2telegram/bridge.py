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
import signal
import threading
from pathlib import Path

from . import __version__, adapters
from .config import Config, _state_dir
from .telegram import TelegramClient

log = logging.getLogger("agent2telegram.bridge")

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
        self._stop = threading.Event()
        self._workers: dict[int, "_ChatWorker"] = {}
        self._workers_lock = threading.Lock()
        self._offset_file = _state_dir() / "offset"
        # Continuity is tracked on disk (a marker per chat dir), so it survives a restart
        # and a fresh conversation resumes correctly instead of starting over.

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        me = self._connect()
        log.info("Connected as @%s — agent=%s, authorized users=%s",
                 me.get("username"), self.cfg.agent, sorted(self._allowed) or "(none!)")
        if not self._allowed:
            log.warning("No allowed_user_ids configured — the bot will refuse everyone. "
                        "Message the bot and check /id, then add your id to the config.")
        self._install_signal_handlers()
        offset = self._load_offset()
        while not self._stop.is_set():
            try:
                updates = self.tg.get_updates(offset, timeout=self.cfg.poll_timeout)
            except Exception as e:                       # never let the loop die
                log.error("getUpdates failed: %s", e)
                self._stop.wait(3)
                continue
            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)
                try:
                    self._dispatch(upd)
                except Exception as e:
                    log.exception("dispatch error: %s", e)
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
    def _dispatch(self, update: dict) -> None:
        msg = update.get("message")
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        user = msg.get("from", {})
        user_id = user.get("id")
        text = (msg.get("text") or "").strip()

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

        if not text:
            self.tg.send_message(chat_id, "ℹ️ I can only handle text messages right now.")
            return

        self._enqueue(chat_id, text)

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
            return True
        return False  # not a known command → treat as a normal prompt

    # ---- per-chat workers --------------------------------------------------
    def _enqueue(self, chat_id: int, text: str) -> None:
        with self._workers_lock:
            worker = self._workers.get(chat_id)
            if worker is None:
                worker = _ChatWorker(chat_id, self)
                self._workers[chat_id] = worker
                worker.start()
        worker.submit(text)

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

    def process(self, chat_id: int, text: str) -> None:
        """Run the agent for one message and reply. Runs inside a chat worker thread."""
        chat_dir = self.chat_dir(chat_id)
        # Continue an existing conversation only after a *successful* first turn (marker).
        is_cont = self._marker(chat_dir).exists()
        with self._keep_typing(chat_id):
            try:
                reply = self.adapter.run(text, chat_dir=chat_dir, is_continuation=is_cont)
            except Exception as e:
                log.error("agent run failed for chat %s: %s", chat_id, e)
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
        self.q: queue.Queue[str | None] = queue.Queue()

    def submit(self, text: str) -> None:
        self.q.put(text)

    def stop(self) -> None:
        self.q.put(None)

    def run(self) -> None:
        while True:
            text = self.q.get()
            if text is None:
                return
            try:
                self.bridge.process(self.chat_id, text)
            except Exception as e:  # belt and braces: a worker must never die silently
                log.exception("worker %s crashed handling a message: %s", self.chat_id, e)
