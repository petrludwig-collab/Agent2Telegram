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
import mimetypes
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("agent2telegram.telegram")

_ipv4_patched = False


def prefer_ipv4() -> None:
    """Prefer IPv4 for outbound connections (idempotent, process-wide).

    Some hosts have a broken/slow IPv6 route to Telegram — the connect hangs ~20s while IPv4 is
    instant — and Python's default resolver order tries IPv6 first, so every API call stalls.
    (curl avoids it via Happy Eyeballs; urllib doesn't.) We filter resolver results to IPv4 when
    any exist, falling back to the full list on IPv6-only hosts."""
    global _ipv4_patched
    if _ipv4_patched:
        return
    _orig = socket.getaddrinfo

    def _gai(host, port, family=0, *args, **kwargs):
        res = _orig(host, port, family, *args, **kwargs)
        v4 = [r for r in res if r[0] == socket.AF_INET]
        return v4 or res

    socket.getaddrinfo = _gai
    _ipv4_patched = True

API_ROOT = "https://api.telegram.org"
#: Telegram rejects text messages longer than 4096 UTF-16 code units. We keep a margin.
MAX_MESSAGE_LEN = 4000
#: Timeout for outbound sends. A healthy send is ~0.1s; on a flaky host a connection can hang,
#: so we cap it short and let the retry (a fresh connection) recover fast — far better than the
#: 65s default, which made one stuck connection delay a reply by over a minute.
SEND_TIMEOUT = 15


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
    # Markdown links [text](url) → <a href>; stashed so the URL isn't escaped/mangled.
    text = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
        lambda m: keep(f'<a href="{_html.escape(m.group(2), quote=True)}">{_html.escape(m.group(1))}</a>'),
        text)
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


def _probe_video(path: str) -> dict:
    """Display width, height and duration of a video via ffprobe ({} when unavailable).
    Rotation metadata is honoured, so a portrait phone clip is reported as portrait."""
    import shutil
    import subprocess
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,sample_aspect_ratio:stream_side_data=rotation:"
                            "format=duration", "-of", "json", path],
                           capture_output=True, text=True, timeout=30)
        info = json.loads(r.stdout or "{}")
        st = (info.get("streams") or [{}])[0]
        w, h = int(st["width"]), int(st["height"])
        sar = st.get("sample_aspect_ratio") or "1:1"
        if ":" in sar and sar not in ("0:1", "1:1"):
            num, den = (int(x) for x in sar.split(":"))
            if num and den:
                w = round(w * num / den)
        rot = next((abs(int(sd.get("rotation", 0))) for sd in st.get("side_data_list", [])
                    if "rotation" in sd), 0)
        if rot in (90, 270):
            w, h = h, w
        out = {"width": str(w), "height": str(h)}
        dur = (info.get("format") or {}).get("duration")
        if dur:
            out["duration"] = str(int(round(float(dur))))
        return out
    except Exception:  # noqa: BLE001 – best effort; sending without dimensions still works
        return {}


class TelegramError(Exception):
    def __init__(self, message: str, *, network_error: bool = False):
        super().__init__(message)
        self.network_error = network_error


def is_network_error(exc: BaseException) -> bool:
    """True for a transient network/DNS problem — whether direct (OSError/URLError/timeout,
    including gaierror "nodename nor servname"/Errno 8) or wrapped in ``TelegramError``
    (``__cause__``). ``json.JSONDecodeError`` = a malformed response = also transient. Used so
    self-healing outages are not logged as ERROR (which would trip a monitoring alert)."""
    if isinstance(exc, OSError) or getattr(exc, 'network_error', False):
        return True
    cause = getattr(exc, "__cause__", None)
    return isinstance(cause, (OSError, json.JSONDecodeError))


class TelegramClient:
    def __init__(self, token: str, *, max_retries: int = 5, opener=None) -> None:
        if not token or ":" not in token:
            raise TelegramError("Invalid bot token.")
        prefer_ipv4()                       # dodge slow/broken IPv6 routes to Telegram
        self._token = token
        self._max_retries = max_retries
        # `opener` is injectable so tests can run without touching the network.
        self._opener = opener or urllib.request.build_opener()

    def _safe_error(self, value) -> str:
        from .redaction import redact
        text = str(value)
        for token in (self._token, urllib.parse.quote(self._token, safe='')):
            text = text.replace(token, '[REDACTED]')
        return redact(text)

    def _http_error(self, method: str, code: int, reason) -> TelegramError:
        if code == 401:
            return TelegramError('Telegram rejected the token – copy it again in full from @BotFather')
        return TelegramError(f'{method}: HTTP {code} {self._safe_error(reason)}')

    # ---- low-level ---------------------------------------------------------
    def _call(self, method: str, params: dict | None = None, *, timeout: float = 65,
              retries: int | None = None) -> dict:
        url = f"{API_ROOT}/bot{self._token}/{method}"
        data = urllib.parse.urlencode(params or {}, doseq=True).encode()
        mx = self._max_retries if retries is None else retries
        attempt = 0
        while True:
            attempt += 1
            try:
                req = urllib.request.Request(url, data=data, method="POST")
                with self._opener.open(req, timeout=timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                if not body.get("ok"):
                    if body.get('error_code') == 401:
                        raise self._http_error(method, 401, '')
                    raise TelegramError(f"{method}: {self._safe_error(body.get('description', 'unknown error'))}")
                return body["result"]
            except urllib.error.HTTPError as e:
                retry_after = self._retry_after(e)
                if retry_after is not None:
                    log.warning("Flood control on %s, waiting %ss", method, retry_after)
                    time.sleep(retry_after + 0.5)
                    continue                         # do not count flood waits as failures
                if e.code >= 500 and attempt <= mx:
                    log.warning("%s: HTTP %s, retry %d/%d (backoff)", method, e.code, attempt, mx)
                    self._backoff(attempt)
                    continue
                raise self._http_error(method, e.code, e.reason) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                if attempt <= mx:
                    log.warning("%s: %s, retry %d/%d (backoff)", method, self._safe_error(e), attempt, mx)
                    self._backoff(attempt)
                    continue
                raise TelegramError(f"{method}: {self._safe_error(e)}", network_error=True) from None

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

    def set_my_commands(self, commands: list[dict]) -> None:
        """Register the bot's command list so Telegram shows the ``/`` autocomplete menu."""
        try:
            self._call("setMyCommands", {"commands": json.dumps(commands)}, timeout=15)
        except TelegramError as e:
            log.warning("setMyCommands failed: %s", e)   # cosmetic; never block startup

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
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise self._http_error('download', e.code, e.reason) from None
                last = self._safe_error(e)
                self._backoff(attempt)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = self._safe_error(e)
                self._backoff(attempt)
        raise TelegramError(f"download failed: {last}", network_error=True) from None

    def send_plain_id(self, chat_id: int, text: str, *, parse_mode: str | None = None) -> int | None:
        """Send a message and return its message_id (for editable status bubbles)."""
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            return self._call("sendMessage", params, timeout=SEND_TIMEOUT).get("message_id")
        except TelegramError:
            return None

    def edit_plain(self, chat_id: int, message_id: int, text: str, *, parse_mode: str | None = None) -> None:
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            self._call("editMessageText", params, timeout=SEND_TIMEOUT)
        except TelegramError:
            pass

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=SEND_TIMEOUT)
        except TelegramError:
            pass

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            # Cosmetic — fail fast (short timeout, no backoff) so a hiccup can't stall the turn.
            self._call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=8, retries=0)
        except TelegramError:
            pass  # purely cosmetic; never let it break a turn

    # ---- files ---------------------------------------------------------------
    # Telegram accepts up to 50 MB per file for bots. Anything larger has to be shared
    # some other way (link), so we fail early with a clear message instead of uploading
    # for minutes and then getting a 413.
    MAX_UPLOAD = 50 * 1024 * 1024

    #: extension -> (API method, form field). Order matters only for readability.
    _KINDS = {
        ".mp4": ("sendVideo", "video"), ".mov": ("sendVideo", "video"),
        ".m4v": ("sendVideo", "video"), ".webm": ("sendVideo", "video"),
        ".mp3": ("sendAudio", "audio"), ".m4a": ("sendAudio", "audio"),
        ".aac": ("sendAudio", "audio"), ".flac": ("sendAudio", "audio"),
        ".ogg": ("sendAudio", "audio"), ".opus": ("sendAudio", "audio"),
        ".wav": ("sendAudio", "audio"),
        ".jpg": ("sendPhoto", "photo"), ".jpeg": ("sendPhoto", "photo"),
        ".png": ("sendPhoto", "photo"), ".webp": ("sendPhoto", "photo"),
    }

    @classmethod
    def kind_for(cls, path) -> tuple[str, str]:
        """Pick the API method for a file. Unknown types go as a document, which always
        works and keeps the original bytes intact."""
        return cls._KINDS.get(os.path.splitext(str(path))[1].lower(), ("sendDocument", "document"))

    def _call_multipart(self, method: str, fields: dict, file_field: str, filename: str,
                        payload: bytes, *, timeout: float = 300) -> dict:
        """One multipart/form-data POST. Written by hand so the project keeps its
        stdlib-only promise (see module docstring)."""
        boundary = "----a2t" + secrets.token_hex(16)
        crlf = b"\r\n"
        body = bytearray()
        for key, val in fields.items():
            if val is None:
                continue
            body += b"--" + boundary.encode() + crlf
            body += f'Content-Disposition: form-data; name="{key}"'.encode() + crlf + crlf
            body += str(val).encode("utf-8") + crlf
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body += b"--" + boundary.encode() + crlf
        body += (f'Content-Disposition: form-data; name="{file_field}"; '
                 f'filename="{filename}"').encode() + crlf
        body += f"Content-Type: {ctype}".encode() + crlf + crlf
        body += payload + crlf
        body += b"--" + boundary.encode() + b"--" + crlf

        url = f"{API_ROOT}/bot{self._token}/{method}"
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=bytes(body), method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            try:
                with self._opener.open(req, timeout=timeout) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                if not out.get("ok"):
                    if out.get('error_code') == 401:
                        raise self._http_error(method, 401, '')
                    raise TelegramError(f"{method}: {self._safe_error(out.get('description', 'unknown error'))}")
                return out["result"]
            except urllib.error.HTTPError as e:
                retry_after = self._retry_after(e)
                if retry_after is not None:
                    log.warning("Flood control on %s, waiting %ss", method, retry_after)
                    time.sleep(retry_after + 0.5)
                    continue
                if e.code >= 500 and attempt <= self._max_retries:
                    log.warning("%s: HTTP %s, retry %d", method, e.code, attempt)
                    self._backoff(attempt)
                    continue
                raise self._http_error(method, e.code, e.reason) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                if attempt <= self._max_retries:
                    log.warning("%s: %s, retry %d", method, self._safe_error(e), attempt)
                    self._backoff(attempt)
                    continue
                raise TelegramError(f"{method}: {self._safe_error(e)}", network_error=True) from None

    def send_file(self, chat_id: int, path, *, caption: str | None = None,
                  meta: dict | None = None) -> None:
        """Upload one local file. The method is chosen from the extension, so a video
        arrives as a playable video and audio as a track, not as a nondescript blob."""
        path = os.fspath(path)
        size = os.path.getsize(path)
        if size > self.MAX_UPLOAD:
            raise TelegramError(
                f"{os.path.basename(path)} is {size // 1048576} MB; bots can send at most "
                f"{self.MAX_UPLOAD // 1048576} MB — share a link instead")
        method, field = self.kind_for(path)
        fields = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption[:1024]
        if method == "sendVideo":
            fields["supports_streaming"] = "true"
            if not (meta or {}).get("width"):
                # Without explicit dimensions Telegram guesses the aspect ratio and often
                # shows a 16:9 video squashed or square. Probe the file and send them.
                meta = {**_probe_video(path), **(meta or {})}
        for k, v in (meta or {}).items():          # width/height/duration/title/performer
            fields[k] = v
        with open(path, "rb") as fh:
            payload = fh.read()
        try:
            self._call_multipart(method, fields, field, os.path.basename(path), payload)
        except TelegramError:
            # Telegram rejects perfectly valid files when they do not fit the *display*
            # rules of the specialised method: a photo may be at most 10 MB with
            # width + height <= 10000 and a side ratio <= 20, a video must be decodable,
            # and so on. A tall screenshot or a stitched chart trips this routinely.
            # The bytes are fine, only the presentation is not, so fall back to a plain
            # document. That keeps the file, and the alternative is losing it outright.
            if method == "sendDocument":
                raise
            log.warning("%s rejected %s, retrying as a document",
                        method, os.path.basename(path))
            fields.pop("supports_streaming", None)
            for k in ("width", "height", "duration", "title", "performer"):
                fields.pop(k, None)
            self._call_multipart("sendDocument", fields, "document",
                                 os.path.basename(path), payload)
            log.info("sent %s (sendDocument after %s, %d bytes)",
                     os.path.basename(path), method, size)
            return
        log.info("sent %s (%s, %d bytes)", os.path.basename(path), method, size)

    def send_voice(self, chat_id: int, path, *, duration: int | None = None) -> dict:
        """Send an OGG/OPUS file as a Telegram VOICE note (a bubble with a waveform), not a
        nondescript audio attachment. The file must already be OGG/OPUS; conversion is the
        caller's job (ffmpeg). *duration* (seconds) is passed through when known so the client
        does not have to guess the length of a long note (2026-09-05: clipped long narrations)."""
        path = os.fspath(path)
        with open(path, "rb") as fh:
            payload = fh.read()
        fields = {"chat_id": chat_id}
        if duration:
            fields["duration"] = str(int(duration))
        out = self._call_multipart("sendVoice", fields, "voice", os.path.basename(path), payload)
        log.info("sent voice note (%d bytes, duration=%s)", len(payload), duration)
        return out

    def send_message(self, chat_id: int, text: str, *, parse_mode: str = "auto") -> None:
        """Send text, splitting to Telegram's size limit. By default (``parse_mode="auto"``)
        the agent's Markdown is rendered via HTML; on any parse failure we fall back to plain
        text so a message is never lost to a formatting glitch."""
        for chunk in split_message(text) or ["(empty response)"]:
            base = {"chat_id": chat_id, "disable_web_page_preview": "true"}
            if parse_mode == "auto":
                try:
                    self._call("sendMessage", {**base, "text": markdown_to_html(chunk), "parse_mode": "HTML"}, timeout=SEND_TIMEOUT)
                except TelegramError as e:
                    log.warning("HTML send failed, falling back to plain text: %s", e)
                    self._call("sendMessage", {**base, "text": _strip_markdown(chunk)}, timeout=SEND_TIMEOUT)
            elif parse_mode:
                try:
                    self._call("sendMessage", {**base, "text": chunk, "parse_mode": parse_mode}, timeout=SEND_TIMEOUT)
                except TelegramError as e:
                    log.warning("send failed with parse_mode=%s, retrying plain: %s", parse_mode, e)
                    self._call("sendMessage", {**base, "text": chunk}, timeout=SEND_TIMEOUT)
            else:
                self._call("sendMessage", {**base, "text": chunk}, timeout=SEND_TIMEOUT)
