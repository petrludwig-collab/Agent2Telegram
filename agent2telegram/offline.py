"""Opt-in offline mode: refuse every outbound HTTP request before a connection is opened.

Export ``AGENT2TELEGRAM_OFFLINE=1`` (also ``true``/``yes``/``on``) to make the bridge safe to
run in local tests, CI or demos: nothing reaches Telegram or ElevenLabs, so a real bot is never
touched and a second poller can never compete with a live bridge for ``getUpdates``.

The switch is read from the environment on every request (no cached state) and is off by
default, in which case this module changes nothing. A refusal is a typed :class:`OfflineError`
that also subclasses the caller's usual error (``TelegramError``, ``STTError``, ``TTSError``),
so existing handlers treat it like any other failed request. Best-effort cosmetic helpers
(typing action, edit/delete, command menu) swallow it exactly as they swallow any failure.
"""
from __future__ import annotations

import os

OFFLINE_ENV = "AGENT2TELEGRAM_OFFLINE"
_TRUE = ("1", "true", "yes", "on")


class OfflineError(Exception):
    """A network request was refused because offline mode is on."""


def is_offline() -> bool:
    return os.environ.get(OFFLINE_ENV, "").strip().lower() in _TRUE


def require_online(what: str, error: type[Exception] = OfflineError) -> None:
    """Raise *error* (an :class:`OfflineError` subclass) if offline mode is on. Call this
    BEFORE building or opening any connection."""
    if is_offline():
        raise error(f"{what}: refused - offline mode is on ({OFFLINE_ENV}); no request was made")
