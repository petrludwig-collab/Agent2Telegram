"""A small, robust Telegram Bot API client built on the standard library only.

Why no `python-telegram-bot`? Fewer dependencies means fewer install failures on a
stranger's machine — which is the whole point of this project. We only need a handful
of methods and we want full control over retries and flood-control handling.

Transport notes:
  * We use long polling (``getUpdates``), so the host needs no public IP / webhook —
    it works behind NAT, a home router, or a strict firewall.
  * Every call retries with exponential backoff on transient network/5xx errors and
    honours Telegram's ``429 retry_after`` flood control.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("agent2telegram.telegram")

API_ROOT = "https://api.telegram.org"
#: Telegram rejects text messages longer than 4096 UTF-16 code units. We keep a margin.
MAX_MESSAGE_LEN = 4000


def markdown_to_html(text: str) -> str:
    """Convert the common Markdown subset (``**bold**``, ``*italic*``/``_italic_``,
    `` `code` ``, fenced ``` blocks) to the HTML Telegram supports. Without this, agents'
    Markdown shows literally (asterisks/backticks) in Telegram."""
    stash: list[str] = []

    def keep(s: str) -> str:
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"```(?:\w+)?\n?(.*?)```",
                  lambda m: keep(f"<pre>{_html.escape(m.group(1))}</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`",
                  lambda m: keep(f"<code>{_html.escape(m.group(1))}</code>"), text)
    text = _html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)
    for i, s in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", s)
    return text


def _strip_markdown(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split *text* into Telegram-sized chunks, preferring paragraph then line then
    word boundaries, and hard-splitting only as a last resort. Pure function — tested."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer the latest natural boundary inside the window.
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > limit * 0.5:        # only if it doesn't waste too much of the window
                break
        else:
            cut = limit                  # no good boundary: hard cut
        cut = cut if cut > 0 else limit
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, token: str, *, max_retries: int = 5, opener=None) -> None:
        if not token or ":" not in token:
            raise TelegramError("Invalid bot token.")
        self._token = token
        self._max_retries = max_retries
        # `opener` is injectable so tests can run without touching the network.
        self._opener = opener or urllib.request.build_opener()

    # ---- low-level ---------------------------------------------------------
    def _call(self, method: str, params: dict | None = None, *, timeout: float = 65) -> dict:
        url = f"{API_ROOT}/bot{self._token}/{method}"
        data = urllib.parse.urlencode(params or {}, doseq=True).encode()
        attempt = 0
        while True:
            attempt += 1
            try:
                req = urllib.request.Request(url, data=data, method="POST")
                with self._opener.open(req, timeout=timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                if not body.get("ok"):
                    raise TelegramError(f"{method}: {body.get('description', 'unknown error')}")
                return body["result"]
            except urllib.error.HTTPError as e:
                retry_after = self._retry_after(e)
                if retry_after is not None:
                    log.warning("Flood control on %s, waiting %ss", method, retry_after)
                    time.sleep(retry_after + 0.5)
                    continue                         # do not count flood waits as failures
                if e.code >= 500 and attempt <= self._max_retries:
                    self._backoff(attempt)
                    continue
                raise TelegramError(f"{method}: HTTP {e.code} {e.reason}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                if attempt <= self._max_retries:
                    self._backoff(attempt)
                    continue
                raise TelegramError(f"{method}: {e}") from e

    @staticmethod
    def _retry_after(err: urllib.error.HTTPError) -> int | None:
        if err.code != 429:
            return None
        try:
            payload = json.loads(err.read().decode("utf-8"))
            return int(payload.get("parameters", {}).get("retry_after", 1))
        except Exception:
            return int(err.headers.get("Retry-After", 1) or 1)

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(2 ** attempt, 30))

    # ---- high-level --------------------------------------------------------
    def get_me(self) -> dict:
        return self._call("getMe", timeout=15)

    def get_updates(self, offset: int, *, timeout: int = 50) -> list[dict]:
        # Network timeout must exceed the long-poll timeout, else we'd cancel mid-poll.
        return self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(["message"])},
            timeout=timeout + 15,
        )

    def get_file_path(self, file_id: str) -> str:
        return self._call("getFile", {"file_id": file_id}, timeout=20)["file_path"]

    def download(self, file_path: str, *, timeout: float = 120) -> bytes:
        """Download a file the bot has access to (returned by getFile)."""
        url = f"{API_ROOT}/file/bot{self._token}/{file_path}"
        last = None
        for attempt in range(1, 4):
            try:
                with self._opener.open(urllib.request.Request(url), timeout=timeout) as resp:
                    return resp.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
                self._backoff(attempt)
        raise TelegramError(f"download failed: {last}")

    def send_plain_id(self, chat_id: int, text: str) -> int | None:
        """Send a plain-text message and return its message_id (for editable status bubbles)."""
        try:
            r = self._call("sendMessage", {"chat_id": chat_id, "text": text,
                                           "disable_web_page_preview": "true"})
            return r.get("message_id")
        except TelegramError:
            return None

    def edit_plain(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text})
        except TelegramError:
            pass

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError:
            pass

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=15)
        except TelegramError:
            pass  # purely cosmetic; never let it break a turn

    def send_message(self, chat_id: int, text: str, *, parse_mode: str = "auto") -> None:
        """Send text, splitting to Telegram's size limit. By default (``parse_mode="auto"``)
        the agent's Markdown is rendered via HTML; on any parse failure we fall back to plain
        text so a message is never lost to a formatting glitch."""
        for chunk in split_message(text) or ["(empty response)"]:
            base = {"chat_id": chat_id, "disable_web_page_preview": "true"}
            if parse_mode == "auto":
                try:
                    self._call("sendMessage", {**base, "text": markdown_to_html(chunk), "parse_mode": "HTML"})
                except TelegramError as e:
                    log.warning("HTML send failed, falling back to plain text: %s", e)
                    self._call("sendMessage", {**base, "text": _strip_markdown(chunk)})
            elif parse_mode:
                try:
                    self._call("sendMessage", {**base, "text": chunk, "parse_mode": parse_mode})
                except TelegramError as e:
                    log.warning("send failed with parse_mode=%s, retrying plain: %s", parse_mode, e)
                    self._call("sendMessage", {**base, "text": chunk})
            else:
                self._call("sendMessage", {**base, "text": chunk})
