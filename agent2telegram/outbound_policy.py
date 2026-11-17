"""Explicit-only outbound policy (opt-in ``explicit_only_outbound``).

Threat model, stated honestly: the guard protects against the bridge's OWN passive paths
(text, bubbles, typing, files, voice, backstop, replay ... derived from the agent transcript).
Arbitrary code running inside the bridge process (a subclass, raw ``urllib``, a client object
the caller kept a reference to) is trusted and out of scope. So is the agent itself: it runs as
the same user and can call ``agent2telegram notify`` or use the token directly. The guard is
therefore a seatbelt for upstream's passive behaviour, not a sandbox.

The only things that may leave the bridge in this mode are typed ``ServiceReply`` objects whose
text is built HERE from fixed templates out of structured arguments (ints, enums). There is no
free-text entry point, and error replies never carry exception text.
"""
from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Exit code of ``agent2telegram run`` when a pending queue blocks the start (AK4).
EXIT_QUEUE_PRESENT = 3

#: Bot API methods the guard lets through untouched (read/control, no user-visible content).
_PASS_METHODS = frozenset({"getUpdates", "getMe", "getFile"})

_AGENT_LABELS = {"codex": "Codex", "claude-code": "Claude Code"}


class OutboundQueuePresent(RuntimeError):
    """A durable or legacy outbound queue already holds records; explicit-only must not touch them."""


def _int(value, name):
    if type(value) is not int:
        raise TypeError(f"{name} must be an int")
    return value


class ServiceReply:
    """Base of the typed allow-list. Subclasses carry only structured fields; the recipient is
    always the first field, ``chat_id`` (there is no second place to take it from)."""

    def render(self) -> str:                       # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True)
class NotAuthorized(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self): return "⛔ Not authorized."


@dataclass(frozen=True)
class NotAuthorizedHere(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self): return "⛔ Not authorized here – please use the shared group."


@dataclass(frozen=True)
class Help(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self):
        return ("👋 This bot is connected to an agent session in explicit-only mode: "
                "your messages reach the agent, replies are delivered by the operator's integration.\n\n"
                "Commands: /help · /status · /id")


@dataclass(frozen=True)
class ChatId(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self): return f"Your Telegram id: `{self.chat_id}`"


@dataclass(frozen=True)
class Status(ServiceReply):
    chat_id: int
    agent: str = ""            # only the known adapter names are rendered

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self):
        label = _AGENT_LABELS.get(self.agent, "agent")
        return f"✅ Connected — {label}, explicit-only outbound mode."


@dataclass(frozen=True)
class NotAvailableExplicitOnly(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    """/voice, /setkey and friends."""

    def render(self): return "Not available in explicit-only mode."


@dataclass(frozen=True)
class AudioTooLarge(ServiceReply):
    chat_id: int
    mb: int

    def __post_init__(self):
        _int(self.chat_id, "chat_id")
        _int(self.mb, "mb")

    def render(self):
        return (f"🎤 That audio is ~{self.mb} MB. Voice/audio limit is 25 MB — "
                "please send a shorter clip or write it as text.")


@dataclass(frozen=True)
class PromptTooLong(ServiceReply):
    chat_id: int
    chars: int
    limit: int

    def __post_init__(self):
        _int(self.chat_id, "chat_id")
        _int(self.chars, "chars")
        _int(self.limit, "limit")

    def render(self):
        return (f"⚠️ That message is too long after processing ({self.chars} characters). "
                f"Please shorten it to {self.limit} characters or less.")


@dataclass(frozen=True)
class SttNotEnabled(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self): return "🎤 Voice transcription isn't enabled."


@dataclass(frozen=True)
class SttNoSpeech(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self): return "🎤 I couldn't hear any speech in that voice note. Please record it again."


@dataclass(frozen=True)
class SttFailed(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self):
        return "🎤 Voice transcription failed. Please resend it or type your message instead."


@dataclass(frozen=True)
class FileTooBig(ServiceReply):
    chat_id: int
    mb: int

    def __post_init__(self):
        _int(self.chat_id, "chat_id")
        _int(self.mb, "mb")

    def render(self):
        return (f"⚠️ That file is ~{self.mb} MB. Telegram bots can only receive files up to 20 MB — "
                "please share a link or a smaller version.")


@dataclass(frozen=True)
class DownloadFailed(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self): return "⚠️ Couldn't download the attachment."


@dataclass(frozen=True)
class InjectFailed(ServiceReply):
    chat_id: int
    attempts: int

    def __post_init__(self):
        _int(self.chat_id, "chat_id")
        _int(self.attempts, "attempts")

    def render(self):
        return (f"⚠️ Couldn't deliver your message to the agent session after {self.attempts} "
                "attempts. Check the session, then send it again.")


@dataclass(frozen=True)
class UnsafePane(ServiceReply):
    chat_id: int

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self):
        return ("⚠️ A Telegram message was blocked because the configured session no longer "
                "appears to be running the expected agent. Restart it, then resend the message.")


@dataclass(frozen=True)
class RestartNotice(ServiceReply):
    chat_id: int
    language: str = "en"

    def __post_init__(self): _int(self.chat_id, "chat_id")

    def render(self):
        from .notices import default_restart_text
        return default_restart_text(self.language)


@dataclass(frozen=True)
class ReactionAck:
    """Fixed-emoji acknowledgement of an inbound message (setMessageReaction)."""
    chat_id: int
    message_id: int
    EMOJI = "👍"

    def __post_init__(self):
        _int(self.chat_id, "chat_id")
        _int(self.message_id, "message_id")


@dataclass(frozen=True)
class CallbackAck:
    """answerCallbackQuery WITHOUT text."""
    callback_query_id: str

    def __post_init__(self):
        if type(self.callback_query_id) is not str or not self.callback_query_id.isdigit():
            raise TypeError("callback_query_id must be a numeric string")


class OutboundGuard:
    """Wraps whatever client the bridge was given. Default deny; the only way to send is
    ``send_service``. A denied call is logged (metadata only) and handled, never raised, so it
    cannot take the inbound loop down."""

    def __init__(self, inner, static_commands):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_commands", list(static_commands))
        object.__setattr__(self, "denied", [])

    def _deny(self, what):
        self.denied.append(what)
        log.warning("explicit-only outbound: %s blocked", what)

    # ---- pass-through (read / control, no content) ---------------------------------
    def get_me(self): return self._inner.get_me()

    def get_updates(self, *a, **k): return self._inner.get_updates(*a, **k)

    def get_file_path(self, file_id): return self._inner.get_file_path(file_id)

    def download(self, *a, **k): return self._inner.download(*a, **k)

    def _call(self, method, params=None, **kw):
        if method in _PASS_METHODS:
            return self._inner._call(method, params, **kw)
        self._deny(f"_call({method})")
        return []

    def set_my_commands(self, commands):
        if list(commands) == self._commands:          # the constant from the code, nothing else
            return self._inner.set_my_commands(commands)
        self._deny("setMyCommands with a non-static payload")

    # ---- the single door -----------------------------------------------------------
    def send_service(self, reply):
        """The recipient comes only from the typed reply (``reply.chat_id``)."""
        if isinstance(reply, ServiceReply):
            return self._inner.send_message(reply.chat_id, reply.render())
        if isinstance(reply, ReactionAck):
            return self._inner._call("setMessageReaction", {
                "chat_id": reply.chat_id, "message_id": reply.message_id,
                "reaction": '[{"type":"emoji","emoji":"%s"}]' % ReactionAck.EMOJI})
        if isinstance(reply, CallbackAck):             # no chat id, no text
            return self._inner._call("answerCallbackQuery",
                                     {"callback_query_id": reply.callback_query_id})
        self._deny(f"send_service({type(reply).__name__})")

    # ---- everything else that can put content in a chat is denied -------------------
    def send_message(self, *a, **k): self._deny("send_message")

    def send_file(self, *a, **k): self._deny("send_file")

    def send_voice(self, *a, **k): self._deny("send_voice")

    def send_plain_id(self, *a, **k): self._deny("send_plain_id")

    def edit_plain(self, *a, **k): self._deny("edit_plain")

    def send_chat_action(self, *a, **k): self._deny("send_chat_action")

    def delete_message(self, *a, **k): self._deny("delete_message")

    def __getattr__(self, name):
        """Unknown attribute: logged by name, answered by a no-op returning None. Never raises
        (so it cannot break the inbound loop); dunder probes keep the normal AttributeError."""
        if name.startswith("__"):
            raise AttributeError(name)

        def _denied(*a, **k):
            self._deny(f"unknown method {name}")
        return _denied


def scan_queues(queue_root, legacy_queue_path) -> list[str]:
    """Raw read-only look for pending outbound records. Never constructs ``DurableOutbox``
    (its ``_maintain`` moves and quarantines files), never creates a directory, never follows a
    symlink, never opens anything for writing. EVERY entry of the durable outbox counts (a
    symlink, fifo, directory or unreadable entry too), as does a legacy queue that is not an
    empty regular file. Returns human-readable locations (no content)."""
    found = []
    outbox = os.path.join(os.fspath(queue_root), "outbox")
    try:
        st = os.lstat(outbox)
    except FileNotFoundError:
        st = None
    except OSError:
        found.append(outbox)
        st = None
    if st is not None:
        if not stat.S_ISDIR(st.st_mode):
            found.append(outbox)
        else:
            try:
                with os.scandir(outbox) as it:
                    found.extend(e.path for e in it)
            except OSError:
                found.append(outbox)
    if legacy_queue_path is not None:
        legacy = os.fspath(legacy_queue_path)
        try:
            lst = os.lstat(legacy)
        except FileNotFoundError:
            lst = None
        except OSError:
            found.append(legacy)
            lst = None
        if lst is not None:
            if not stat.S_ISREG(lst.st_mode):
                found.append(legacy)
            else:
                try:
                    with open(legacy, "rb") as fh:
                        if fh.read().strip():
                            found.append(legacy)
                except OSError:
                    found.append(legacy)
    return sorted(set(found))
