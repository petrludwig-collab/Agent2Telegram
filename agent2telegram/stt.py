"""Optional speech-to-text for Telegram voice messages.

Currently supports **ElevenLabs Scribe** (`scribe_v1`). It is enabled only when the user
provides their own API key (``elevenlabs_api_key`` in config or ``ELEVENLABS_API_KEY`` in
the environment) — there is no shared/default key and no third-party Python dependency:
the multipart upload is built by hand on top of ``urllib``.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
import uuid

from .offline import OfflineError, require_online

log = logging.getLogger("agent2telegram.stt")

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL_ID = "scribe_v1"
TRANSIENT_BACKOFFS = (1.0, 3.0)


class STTError(Exception):
    pass


class STTOfflineError(OfflineError, STTError):
    """Transcription was refused because offline mode is on."""


class EmptyTranscript(STTError):
    """The audio was received fine but contained no speech (silent or accidental tap)."""


#: Anything that looks like a credential is stripped before an upstream body is logged
#: or shown to the user. ElevenLabs does not echo the key today, but an error body is
#: attacker-influencable text on its way into a log and a chat message.
_SECRET_RE = re.compile(r"\b(?:sk_|xi-)[A-Za-z0-9_\-]{8,}|\b[A-Fa-f0-9]{32,}\b")


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text)


def _error_detail(err: urllib.error.HTTPError) -> str:
    """The upstream explanation, without which an HTTP code says nothing.

    A bare ``HTTP 400: Bad Request`` cost an hour of digging on 2026-08-07; the body
    said ``API key must start with 'sk_'`` all along. Read at most a few KB, never
    fail because of the body, and redact anything credential-shaped.
    """
    try:
        raw = err.read(4096).decode("utf-8", "replace")
    except Exception:
        return ""
    if not raw.strip():
        return ""
    try:
        payload = json.loads(raw)
    except ValueError:
        return _redact(" ".join(raw.split()))[:300]
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("status") or json.dumps(detail)
    elif isinstance(detail, str):
        message = detail
    else:
        message = json.dumps(payload)
    return _redact(" ".join(str(message).split()))[:300]


def _describe_error(err: BaseException) -> str:
    if isinstance(err, urllib.error.HTTPError):
        msg = getattr(err, "reason", None) or getattr(err, "msg", "") or ""
        popis = f"HTTP {err.code}: {msg}".strip()
        detail = _error_detail(err)
        return f"{popis} — {detail}" if detail else popis
    return _redact(str(err)) or err.__class__.__name__


def _is_retryable_http(err: urllib.error.HTTPError) -> bool:
    return 500 <= err.code <= 599


def _multipart(fields: dict[str, str], filename: str, audio: bytes,
               content_type: str = "audio/ogg") -> tuple[str, bytes]:
    boundary = "----a2t" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f'Content-Type: {content_type}\r\n\r\n'.encode()
    )
    parts.append(audio)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    return boundary, b"".join(parts)


def transcribe_elevenlabs(audio: bytes, *, api_key: str, filename: str = "voice.ogg",
                          language: str | None = None,
                          opener=None, timeout: float = 120,
                          retry_backoffs: tuple[float, ...] = TRANSIENT_BACKOFFS,
                          sleeper=time.sleep) -> str:
    """Transcribe *audio* bytes with ElevenLabs Scribe. Returns the recognized text.

    *language* is an ISO-639 code (``cs``, ``de``, …). Without it Scribe guesses, and on a
    short clip it guesses badly — a three-second Czech note came back as the English word
    "Down". Telling it the language costs nothing and removes a whole class of nonsense.
    """
    if not api_key:
        raise STTError("no ElevenLabs API key configured")
    pole = {"model_id": MODEL_ID}
    if language:
        pole["language_code"] = language
    boundary, body = _multipart(pole, filename, audio)
    req = urllib.request.Request(
        ELEVENLABS_URL,
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )
    op = opener or urllib.request.build_opener()
    attempts = len(retry_backoffs) + 1
    for attempt in range(attempts):
        require_online("ElevenLabs STT", STTOfflineError)   # before every attempt
        try:
            with op.open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            retryable = _is_retryable_http(e)
            if not retryable or attempt == attempts - 1:
                detail = _describe_error(e)
                if retryable and attempts > 1:
                    raise STTError(
                        f"ElevenLabs request failed after {attempt + 1} attempts: {detail}"
                    ) from e
                raise STTError(f"ElevenLabs request failed: {detail}") from e
            detail = _describe_error(e)
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as e:
            if attempt == attempts - 1:
                raise STTError(
                    f"ElevenLabs request failed after {attempt + 1} attempts: {_describe_error(e)}"
                ) from e
            detail = _describe_error(e)
        except Exception as e:
            raise STTError(f"ElevenLabs request failed: {_describe_error(e)}") from e

        backoff = retry_backoffs[attempt]
        # ElevenLabs STT occasionally drops long uploads; retry only transient failures.
        log.warning("ElevenLabs STT transient failure (%d/%d): %s; retrying in %.1fs",
                    attempt + 1, attempts, detail, backoff)
        sleeper(backoff)
    else:  # pragma: no cover - for type-checkers; the loop always returns or raises
        raise STTError("ElevenLabs request failed")
    text = (payload.get("text") or "").strip()
    if not text:
        raise EmptyTranscript("transcription returned no text")
    return text


def looks_like_api_key(key: str) -> bool:
    """Cheap shape check so a wrong key is caught at entry, not at the first voice message.

    ElevenLabs rejects anything without the ``sk_`` prefix with a bare HTTP 400. Deliberately
    only the prefix: length and alphabet are theirs to change, and a check that guesses too
    much would start rejecting valid keys.
    """
    return bool(key) and key.strip().startswith("sk_")


def transcribe(audio: bytes, *, api_key: str, filename: str = "voice.ogg",
               language: str | None = None) -> str:
    """Provider dispatcher (only ElevenLabs Scribe today)."""
    return transcribe_elevenlabs(audio, api_key=api_key, filename=filename, language=language)
