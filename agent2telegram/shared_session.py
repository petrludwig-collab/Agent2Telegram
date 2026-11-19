"""Telegram origin for background work in a SHARED session (opt-in, Claude Code attach mode).

A Telegram message and a message typed over SSH/tmux end up as the same transcript record
(``origin.kind == "human"``). So a background task launched during a Telegram turn cannot be told
apart from one launched in a terminal turn by looking at the text. This module gives the bridge a
correlation it can prove from the transcript order alone:

* the bridge wraps the WHOLE prompt it injects into a one-time envelope ``<a2t:N> ... </a2t:N>``
  (N = 128 random bits) and stores N *before* typing it;
* the first ``human`` record that is exactly that envelope consumes N and opens a Telegram turn;
* background tasks launched inside that turn are bound ``task_id -> {chat, nonce, session}``;
* a completion (``origin.kind == "task-notification"``) is claimed once, only when nothing else is
  running, and only while the chat is still authorised.

Scope (be honest about it): this is an OPERATIONAL correlation. It prevents accidental mixing of
terminal and Telegram work, a result reaching the wrong chat and replay of a stale marker. It does NOT
defend against someone who already has the same account, tmux, transcript or bridge state: they can
read or forge the envelope or edit the state file.

All state lives in ONE file that is replaced atomically, offset included. Any failure of that state
(unreadable file, failed write, replaced or truncated transcript) blocks only the automatic Telegram
delivery; it never touches the terminal path.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import secrets
import base64
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import readers

log = logging.getLogger("agent2telegram.shared_session")

VERSION = 1
DEFAULT_NONCE_TTL = 600.0       # T1: issue -> consumption
DEFAULT_JOB_TTL = 86400.0       # T2: launch observed in a Telegram window -> completion claimed
OFFSET_FLUSH_INTERVAL = 1.0     # an offset-only change is written at most this often
RESET_RETRY_INTERVAL = 5.0
EXPIRE_INTERVAL = 1.0

_L, _R = "‹", "›"
_NONCE = "[A-Z2-7]{26}"
_ENVELOPE = re.compile(f"^{_L}a2t:(?P<n>{_NONCE}){_R}(?P<inner>.*){_L}/a2t:(?P=n){_R}$", re.S)
_ANY_TAG = re.compile(f"{_L}/?a2t:")
_OPEN_TAG = re.compile(f"{_L}a2t:({_NONCE}){_R}")
_EDGE = re.compile("^[\\s​‌‍⁠﻿]+|[\\s​‌‍⁠﻿]+$")
_TASK_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_NOTIFICATION = re.compile(r"<task-notification>\s*<task-id>([A-Za-z0-9_.-]{1,80})</task-id>")

NONCE_STATES = ("issued", "consumed", "revoked", "expired")
JOB_STATES = ("pending", "claimed", "delivered", "uncertain", "expired", "revoked", "blocked")
TURN_KINDS = ("tg", "notif", "other")

#: Fixed text sent to the authorised inbound chat when a prompt is refused (R8). No dynamic content.
REFUSED_TEXT = ("\u26a0\ufe0f This message was NOT passed to the agent: the bridge cannot store the state it "
                "needs to track Telegram work safely. Please try again later.")
#: Fixed service lines when a queued input of ANOTHER chat arrives inside a running answer. Claude will very likely
#: answer both in the same turn, so the bridge stops forwarding that answer instead of guessing a recipient.
ROUTE_SWITCH_RUNNING = ("\u26a0\ufe0f This answer continues in the shared session together with a message of another chat "
                        "and is not forwarded here any further.")
ROUTE_SWITCH_QUEUED = ("\u26a0\ufe0f Your message was queued behind an answer for another chat. Its reply continues in the "
                       "shared session and is not forwarded to this chat.")


class Route:
    """The routing state of the running turn on the bridge side. kind: none | tg | notif | other. Only tg and
    notif that are not cut forward, and only to ``chat``."""
    __slots__ = ("kind", "chat", "job", "nonce", "cut", "prev_owner")

    def __init__(self, kind="none", chat=None, job=None, nonce=None, cut=False, prev_owner=None):
        self.kind, self.chat, self.job, self.nonce, self.cut = kind, chat, job, nonce, cut
        self.prev_owner = prev_owner                      # the owner chat a result route borrowed the variable from


class StateError(Exception):
    """The correlation state cannot be read, written or trusted."""


# ----------------------------------------------------------------------------- envelope

def new_nonce() -> str:
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def normalise(text: str) -> str:
    """Strip Unicode whitespace and zero-width characters at both ends — nothing else."""
    return _EDGE.sub("", text or "")


def wrap_prompt(nonce: str, text: str) -> str:
    """Wrap a whole prompt. The two guillemets are replaced inside the payload so that it can never
    contain a tag of its own."""
    body = text.replace(_L, "<").replace(_R, ">")
    return f"{_L}a2t:{nonce}{_R}{body}{_L}/a2t:{nonce}{_R}"


def fingerprint(body: str) -> str:
    """sha256 of the envelope body as it is typed (the guillemets are neutralised exactly like wrap_prompt).
    Only this hash is stored or logged, never the content."""
    return hashlib.sha256(body.replace(_L, "<").replace(_R, ">").encode("utf-8")).hexdigest()


#: An unreadable complete transcript line stands for a lost record (possibly a switch to the terminal).
UNREADABLE_RECORD = {"type": "user", "uuid": None, "message": {"role": "user", "content": ""}}


def parse_envelope(text: str):
    """``(nonce, inner)`` when the whole normalised text is exactly one envelope, else None."""
    m = _ENVELOPE.match(normalise(text))
    if not m or _ANY_TAG.search(m.group("inner")):
        return None
    return m.group("n"), m.group("inner")


_CLOSE_TAG = re.compile(f"{_L}/a2t:({_NONCE}){_R}")


def find_envelope(text: str):
    """``(nonce, inner, extra)`` when the text contains exactly ONE envelope (one open and one matching close tag, nothing
    else that looks like a tag) anywhere in it; ``extra`` says that other text (terminal typing) is around it. Two
    envelopes, a stray or mismatched tag, or no envelope give None."""
    norm = normalise(text)
    opens, closes = _OPEN_TAG.findall(norm), _CLOSE_TAG.findall(norm)
    if len(opens) != 1 or len(closes) != 1 or opens[0] != closes[0] or len(_ANY_TAG.findall(norm)) != 2:
        return None
    m = re.search(f"{_L}a2t:({_NONCE}){_R}(?P<inner>.*?){_L}/a2t:\\1{_R}", norm, re.S)
    if not m:
        return None
    return m.group(1), m.group("inner"), m.start() > 0 or m.end() < len(norm)


def strip_envelope(text: str) -> str:
    """Display/legacy normalisation only (NEVER a trust decision): the payload of a well-formed envelope."""
    parsed = parse_envelope(text) if isinstance(text, str) else None
    return parsed[1] if parsed else text


class EnvelopeReader:
    """Transcript reader whose prompts are shown without the envelope, so the older prefix-based turn
    bookkeeping keeps working. It delegates everything else. Which turn is Telegram's is decided by
    :class:`SharedSession`, not by what this reader shows."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def user_text(self, rec):
        text = self._inner.user_text(rec)
        return strip_envelope(text) if text else text

    def parse(self, rec):
        for ev in self._inner.parse(rec):
            if ev.kind == "user":
                ev.text = strip_envelope(ev.text)
            yield ev


# ----------------------------------------------------------------------------- record structure

def _origin_kind(rec: dict):
    origin = rec.get("origin")
    return origin.get("kind") if isinstance(origin, dict) else None


def _queued_prompt(rec):
    """Claude Code records input that arrives WHILE a turn runs (typed or injected) as an `attachment` of type
    `queued_command`, not as a user record. Its prompt, or None when the record is not such an attachment."""
    att = rec.get("attachment")
    if rec.get("type") == "attachment" and isinstance(att, dict) and att.get("type") == "queued_command":
        prompt = att.get("prompt")
        return prompt if isinstance(prompt, str) else None
    return None


def record_text(rec: dict) -> str:
    queued = _queued_prompt(rec)
    if queued is not None:
        return queued
    return readers._text_of((rec.get("message") or {}).get("content"))


def classify(rec) -> str:
    """ignore | tool_result | human | queued | notification | foreign — from the record's own structure."""
    queued = _queued_prompt(rec) if isinstance(rec, dict) else None
    if queued is not None:
        # A queued command that carries our tag is the evidence that Claude took that input (consumption); it is
        # NOT the start of a new answer. One WITHOUT our tag is ignored (terminal text mixed into the turn is allowed).
        return "queued" if _ANY_TAG.search(queued) else "ignore"
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return "ignore"
    reader = readers.ClaudeCodeReader
    if reader._is_tool_result(rec):
        return "tool_result"
    if reader._is_harness_record(rec):
        return "ignore"
    kind = _origin_kind(rec)
    if kind == "human":
        return "human"
    if kind == "task-notification" and notification_task_id(record_text(rec)):
        return "notification"
    return "foreign"


def notification_task_id(text: str):
    m = _NOTIFICATION.match(text or "")        # the content must START with the tag
    return m.group(1) if m else None


def launch_task_id(rec: dict):
    """Task id of a background launch: a tool result with ``backgroundTaskId`` (shell) or
    ``agentId`` + ``isAsync: true`` (sub-agent)."""
    result = rec.get("toolUseResult")
    if not isinstance(result, dict):
        return None
    shell = result.get("backgroundTaskId")
    if isinstance(shell, str) and _TASK_ID.fullmatch(shell):
        return shell
    agent = result.get("agentId")
    if isinstance(agent, str) and result.get("isAsync") is True and _TASK_ID.fullmatch(agent):
        return agent
    return None


# ----------------------------------------------------------------------------- state

@dataclass
class Outcome:
    turn: dict | None = None     # the turn after this record (a copy), None = idle
    blocked: bool = False        # state unusable: treat the record's turn as NOT Telegram
    claim: dict | None = None    # {"task", "chat", "uuid"}: this notification record started a claimed turn
    noop: bool = False           # the same record was already applied before a restart
    changed: bool = False        # the turn differs from the one before this record
    cut: bool = False            # forwarding of the running turn must stop now (fail closed)
    cut_chats: tuple = ()        # chats that get a fixed service line because of that cut
    unvalidated: bool = False    # the record carried a bridge tag that could not be validated
    again: bool = False          # a second appearance of an already consumed input (idempotent, not a replay)


@dataclass
class Sync:
    resume: int                  # byte offset to continue reading from
    changed: bool                # the transcript is new/replaced: the caller must move its cursor


def _fresh() -> dict:
    return {"v": VERSION, "transcript": None, "turn": None, "nonces": {}, "jobs": {}, "sends": {}, "boundary_uuid": None}


def _validate(st) -> None:
    def need(cond):
        if not cond:
            raise ValueError("invalid shared-session state")
    need(isinstance(st, dict) and st.get("v") == VERSION)
    t = st.get("transcript")
    need(t is None or (isinstance(t, dict) and isinstance(t.get("path"), str)
                       and all(type(t.get(k)) is int for k in ("ino", "dev", "offset")) and t["offset"] >= 0
                       and isinstance(t.get("head"), str) and isinstance(t.get("session"), str)))
    turn = st.get("turn")
    need(turn is None or (isinstance(turn, dict) and turn.get("kind") in TURN_KINDS))
    need(isinstance(st.get("nonces"), dict) and isinstance(st.get("jobs"), dict))
    for n, rec in st["nonces"].items():
        need(re.fullmatch(_NONCE, str(n)) and isinstance(rec, dict) and rec.get("state") in NONCE_STATES
             and rec.get("purpose") == "inbound" and type(rec.get("chat")) is int
             and isinstance(rec.get("at"), (int, float)) and isinstance(rec.get("session"), str))
    for tid, job in st["jobs"].items():
        need(_TASK_ID.fullmatch(str(tid)) and isinstance(job, dict) and job.get("state") in JOB_STATES
             and type(job.get("chat")) is int and isinstance(job.get("bound_at"), (int, float))
             and isinstance(job.get("at"), (int, float)) and isinstance(job.get("session"), str))


def _generation(st: dict) -> str:
    """Identity of the transcript the offsets belong to: an offset means nothing in another generation."""
    t = st["transcript"]
    return f"{t['ino']}:{t['dev']}:{t['head']}"


def _identity(path: Path) -> dict | None:
    try:
        st = path.stat()
        head = ""
        with open(path, "rb") as f:
            first = f.readline(4096)
        if first.endswith(b"\n"):                 # only a COMPLETE first line is a stable identity
            head = hashlib.sha256(first).hexdigest()[:32]
    except OSError:
        return None
    return {"path": str(path.resolve()), "ino": st.st_ino, "dev": st.st_dev, "head": head,
            "size": st.st_size, "session": path.stem}


class SharedSession:
    def __init__(self, path, *, nonce_ttl: float = DEFAULT_NONCE_TTL, job_ttl: float = DEFAULT_JOB_TTL,
                 clock=time.time, authorised=lambda chat: True) -> None:
        self.path = Path(path)
        self.nonce_ttl = float(nonce_ttl)
        self.job_ttl = float(job_ttl)
        self._clock = clock
        self._authorised = authorised
        self._lock = threading.RLock()
        self._s: dict | None = None
        self.degraded: str | None = None
        self._pending_off: int | None = None
        self._last_off_write = 0.0
        self._last_reset_try = 0.0
        self._last_expire = float("-inf")

    # ---- persistence -------------------------------------------------------
    def _write(self, state: dict) -> None:
        data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            dfd = os.open(self.path.parent, os.O_RDONLY)      # a failure here is a failed write: no effect
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise StateError(type(e).__name__) from None

    def _degrade(self, reason: str) -> None:
        if self.degraded is None:
            log.error("shared session: state unusable (%s) -> automatic Telegram delivery blocked", reason)
        self.degraded = reason

    def _commit(self, fn):
        """Apply ``fn`` to a copy, write it, then swap it in. A failed write changes nothing."""
        new = copy.deepcopy(self._s)
        result = fn(new)
        try:
            self._write(new)
        except StateError as e:
            self._degrade(f"write failed: {e}")
            raise
        self._s = new
        self._pending_off = None
        self._last_off_write = self._clock()
        return result

    @property
    def healthy(self) -> bool:
        return self.degraded is None and self._s is not None

    def turn(self) -> dict | None:
        with self._lock:
            t = self._s["turn"] if self._s else None
            return dict(t) if t else None

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._s)

    # ---- lifecycle ---------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                raw = self.path.read_text("utf-8")
            except FileNotFoundError:
                self._s = _fresh()
                return
            except OSError as e:
                self._s = _fresh()
                self._degrade(f"unreadable: {type(e).__name__}")
                return
            try:
                st = json.loads(raw)
                _validate(st)
            except (ValueError, TypeError):
                log.error("shared session: state file is not valid -> all bindings revoked")
                try:
                    self.path.replace(self.path.with_name(self.path.name + f".corrupt-{int(self._clock())}"))
                except OSError:
                    pass
                self._s = _fresh()
                self._reset_locked("invalid state file")
                return
            st.setdefault("sends", {})
            st.setdefault("boundary_uuid", None)
            self._s = st

            def recover(new):
                # R5: a job that was claimed when we died is uncertain, never retried.
                for job in new["jobs"].values():
                    if job["state"] == "claimed":
                        job["state"], job["at"] = "uncertain", self._clock()
                if new["turn"] and new["turn"]["kind"] == "notif":
                    new["turn"] = None
            if any(j["state"] == "claimed" for j in st["jobs"].values()) or (
                    st["turn"] and st["turn"]["kind"] == "notif"):
                try:
                    self._commit(recover)
                except StateError:
                    pass

    def _reset_locked(self, reason: str) -> bool:
        """Fresh state, every earlier binding gone. The cursor is set by the next sync."""
        keep = self._s["transcript"] if self._s else None
        fresh = _fresh()
        fresh["transcript"] = keep
        try:
            self._write(fresh)
        except StateError as e:
            self._degrade(f"{reason}; reset failed: {e}")
            self._s = fresh
            return False
        self._s = fresh
        self.degraded = None
        log.warning("shared session: state reset (%s); earlier Telegram bindings are gone", reason)
        return True

    def try_recover(self) -> bool:
        with self._lock:
            if self.degraded is None:
                return True
            now = self._clock()
            if now - self._last_reset_try < RESET_RETRY_INTERVAL:
                return False
            self._last_reset_try = now
            if self._s is None:
                self._s = _fresh()
            # The cursor of a degraded store is not trustworthy: forget it, the next sync restarts at EOF.
            self._s["transcript"] = None
            return self._reset_locked("recovering from: " + str(self.degraded))

    def sync_transcript(self, path, read: int = 0) -> Sync | None:
        """Check the transcript identity and say where to read from.

        * first ever transcript: history is history, resume at EOF (offset 0 only if a prompt was issued
          before the file existed — the agent writes it with the first message);
        * same file: resume at the stored offset (records written while we were down are seen);
        * replaced (other inode / first line) or truncated: every binding is revoked, resume at EOF."""
        with self._lock:
            if self._s is None:
                self.load()
            if path is None:
                return None
            ident = _identity(Path(path))
            if ident is None:
                if self.degraded is None and self._s["transcript"] is None:
                    try:
                        self._commit(lambda st: st.update(
                            transcript={"path": "", "ino": 0, "dev": 0, "head": "", "offset": 0, "session": ""}))
                    except StateError:
                        pass
                return None
            if self.degraded is not None and not self.try_recover():
                return Sync(ident["size"], False)
            return self._sync_locked(ident, read)

    def _sync_locked(self, ident: dict, read: int = 0) -> Sync:
        t = self._s["transcript"]
        new_t = {k: ident[k] for k in ("path", "ino", "dev", "head", "session")}

        def adopt(offset):
            def f(st):
                if st["transcript"] and st["transcript"]["path"] == "":
                    for n in st["nonces"].values():       # issued before the file existed
                        if n["state"] == "issued":
                            n["session"] = new_t["session"]
                st["transcript"] = dict(new_t, offset=offset)
            return f

        try:
            if t is None:
                self._commit(adopt(ident["size"]))
                return Sync(ident["size"], True)
            if t["path"] == "":                  # no file existed at the last sync
                offset = 0 if any(n["state"] == "issued" for n in self._s["nonces"].values()) else ident["size"]
                self._commit(adopt(offset))
                return Sync(offset, True)
            same_file = (t["path"] == ident["path"] and t["ino"] == ident["ino"]
                         and t["dev"] == ident["dev"])
            head_ok = t["head"] == ident["head"] or (t["head"] == "" and ident["head"] != "")
            same = same_file and head_ok
            highest = max(t["offset"], read or 0, self._pending_off or 0)    # what was already READ
            if same and ident["size"] >= highest:
                if t["head"] != ident["head"]:
                    self._commit(lambda st: st["transcript"].update(head=ident["head"]))
                return Sync(t["offset"], False)
            self._commit(lambda st: self._revoke_all(st, adopt(ident["size"])))
            log.warning("shared session: transcript replaced or truncated -> all bindings revoked")
            return Sync(ident["size"], True)
        except StateError:
            return Sync(ident["size"], True)

    def _revoke_all(self, st: dict, adopt) -> None:
        now = self._clock()
        for rec in st["nonces"].values():
            if rec["state"] == "issued":
                rec["state"], rec["at"] = "revoked", now
            elif rec["state"] == "consumed":
                rec["closed"] = True                          # no second appearance after a transcript reset
        for job in st["jobs"].values():
            if job["state"] == "pending":
                job["state"], job["at"] = "revoked", now
            elif job["state"] == "claimed":
                job["state"], job["at"] = "uncertain", now
        st["turn"] = None
        st["boundary_uuid"] = None                        # nothing older than a reset may anchor a route
        adopt(st)

    # ---- issuing (R8) ------------------------------------------------------
    def issue(self, chat: int, fp: str) -> str:
        """Store a new one-time nonce for ``chat`` BEFORE the prompt is typed. Raises StateError when it
        cannot be stored safely; the caller must then not inject."""
        with self._lock:
            return self._issue_locked(chat, fp)

    def _issue_locked(self, chat, fp) -> str:
        if self.degraded is not None or self._s is None or self._s["transcript"] is None:
            raise StateError("state not usable")
        if type(chat) is not int:
            raise StateError("no chat")
        nonce = new_nonce()
        now = self._clock()

        def f(st):
            st["nonces"][nonce] = {"chat": chat, "state": "issued", "purpose": "inbound", "job": None,
                                   "session": st["transcript"]["session"], "at": now, "off": None,
                                   "replays": 0, "fp": fp, "typed": False, "appearances": 0, "offs": []}
        self._commit(f)
        return nonce

    def cancel(self, nonce: str) -> None:
        """The injection failed: the nonce must not stay valid for a later accidental occurrence."""
        with self._lock:
            if self._s is None or nonce not in self._s["nonces"]:
                return

            def f(st):
                rec = st["nonces"][nonce]
                if rec["state"] == "issued":
                    rec["state"], rec["at"] = "revoked", self._clock()
            try:
                self._commit(f)
            except StateError:
                pass

    def mark_typed(self, nonce: str) -> None:
        """The injection of this prompt completed: from now on only T2 (not T1) bounds its consumption."""
        with self._lock:
            if self._s is None or self.degraded is not None or nonce not in self._s["nonces"]:
                return
            try:
                self._commit(lambda st: st["nonces"][nonce].update(typed=True))
            except StateError:
                pass

    def bind_send(self, key: str, job: str, chat: int) -> bool:
        """Persist the INTENT to send a result reply of ``job`` to ``chat`` under the outbox key ``key``. Written BEFORE
        the reply is stored in the outbox, so a crash between the two cannot lose it. False = not stored: do not send."""
        with self._lock:
            if self._s is None or self.degraded is not None:
                return False
            now = self._clock()
            try:
                self._commit(lambda st: st["sends"].update(
                    {key: {"job": job, "chat": chat, "session": st["transcript"]["session"], "at": now}}))
            except StateError:
                return False
            return True

    def confirm_key(self, key: str, chat: int) -> None:
        """Telegram CONFIRMED a part of the stored reply ``key``: the binding is removed and the job is marked delivered
        in ONE state write (a crash cannot leave one without the other)."""
        with self._lock:
            if self._s is None or self.degraded is not None or key not in self._s["sends"]:
                return

            def f(st):
                bound = st["sends"].pop(key)
                job = st["jobs"].get(bound["job"])
                if job and job["chat"] == bound["chat"] == chat and job["state"] in ("claimed", "uncertain"):
                    job["state"], job["at"] = "delivered", self._clock()
            try:
                self._commit(f)
            except StateError:
                pass

    def abort_send(self, key: str) -> None:
        """The stored reply ``key`` must not be sent (authorisation gone, enqueue failed): binding removed and a claimed
        job becomes uncertain, in one write. Never resent by us."""
        with self._lock:
            if self._s is None or self.degraded is not None or key not in self._s["sends"]:
                return

            def f(st):
                bound = st["sends"].pop(key)
                job = st["jobs"].get(bound["job"])
                if job and job["state"] == "claimed":
                    job["state"], job["at"] = "uncertain", self._clock()
            try:
                self._commit(f)
            except StateError:
                pass

    def reconcile(self, state_of) -> None:
        """After a restart: resolve every stored intent against the outbox. ``state_of(key)`` is "delivered" (a part was
        confirmed or the key is in the delivered ledger), "pending" (still stored, may be confirmed later) or "orphan"
        (no stored reply: the intent is dropped, the job stays uncertain)."""
        with self._lock:
            if self._s is None or self.degraded is not None or not self._s["sends"]:
                return
            verdict = {key: state_of(key) for key in self._s["sends"]}

            def f(st):
                for key, v in verdict.items():
                    if v == "pending" or key not in st["sends"]:
                        continue
                    bound = st["sends"].pop(key)
                    job = st["jobs"].get(bound["job"])
                    if v == "delivered" and job and job["chat"] == bound["chat"] and job["state"] in ("claimed", "uncertain"):
                        job["state"], job["at"] = "delivered", self._clock()
            try:
                self._commit(f)
            except StateError:
                pass

    def mark_uncertain(self, task: str) -> None:
        """A claimed result that must not be sent any more (authorisation gone) is uncertain, never resent by us."""
        with self._lock:
            job = (self._s or {}).get("jobs", {}).get(task)
            if self.degraded is not None or not job or job["state"] != "claimed":
                return

            def f(st):
                st["jobs"][task]["state"], st["jobs"][task]["at"] = "uncertain", self._clock()
            try:
                self._commit(f)
            except StateError:
                pass

    # ---- transcript records ------------------------------------------------
    def on_record(self, offset: int, end: int, rec) -> Outcome:
        with self._lock:
            if self._s is None or self.degraded is not None or self._s["transcript"] is None:
                return Outcome(blocked=True)
            kind = classify(rec)
            if kind == "ignore":
                self._note_offset(end)
                return Outcome(turn=self.turn())
            new = copy.deepcopy(self._s)
            out = Outcome()
            handler = {"human": self._on_human, "foreign": self._on_human, "queued": self._on_queued,
                       "tool_result": self._on_tool_result, "notification": self._on_notification}[kind]
            handler(new, offset, rec, out, kind)
            out.turn = dict(new["turn"]) if new["turn"] else None
            out.changed = new["turn"] != self._s["turn"]
            if out.noop:
                return out
            if new == self._s:
                self._note_offset(end)
                return out
            new["transcript"]["offset"] = max(new["transcript"]["offset"], end)
            try:
                self._write(new)
            except StateError as e:
                self._degrade(f"write failed: {e}")
                return Outcome(blocked=True)
            self._s = new
            self._pending_off = None
            self._last_off_write = self._clock()
            return out

    def _note_offset(self, end: int) -> None:
        if end > self._s["transcript"]["offset"]:
            self._pending_off = max(end, self._pending_off or 0)

    def advance(self, end: int) -> None:
        """Everything up to ``end`` has been read (records that change nothing included)."""
        with self._lock:
            if self._s is not None and self.degraded is None and self._s["transcript"] is not None:
                self._note_offset(end)
        self.flush_offset()

    def job_state(self, task: str):
        with self._lock:
            job = (self._s or {}).get("jobs", {}).get(task)
            return job["state"] if job else None

    def nonce_state(self, nonce: str) -> str | None:
        with self._lock:
            rec = (self._s or {}).get("nonces", {}).get(nonce)
            return rec["state"] if rec else None

    def needs_sync(self) -> bool:
        with self._lock:
            return self._s is not None and (self.degraded is not None or self._s["transcript"] is None)

    def flush_offset(self, force: bool = False) -> None:
        with self._lock:
            if (self._pending_off is None or self.degraded is not None or self._s is None
                    or self._s["transcript"] is None):
                return
            if not force and self._clock() - self._last_off_write < OFFSET_FLUSH_INTERVAL:
                return
            end = self._pending_off
            try:
                self._commit(lambda st: st["transcript"].update(offset=max(st["transcript"]["offset"], end)))
            except StateError:
                pass

    def _end_route(self, st, now) -> None:
        """A turn boundary: a result route that ends without a confirmed send is uncertain (R5, never retried)."""
        prev = st["turn"]
        if prev and prev["kind"] == "notif":
            job = st["jobs"].get(prev.get("job") or "")
            if job and job["state"] == "claimed":
                job["state"], job["at"] = "uncertain", now

    def _consume(self, st, offset, text, now, out, kind):
        """The consumption checks for an envelope in an input record (human or queued). Returns the consumed nonce or
        None. Exactly ONE envelope with a matching body may sit among other text (terminal typing: mixing is allowed);
        two envelopes, a changed body, an unknown/revoked/expired nonce or a replay are not Telegram input.
        ``out.unvalidated`` is set when the record carried a bridge tag that could not be validated."""
        found = find_envelope(text)
        if not found:
            for nonce in _OPEN_TAG.findall(text):         # two envelopes / mangled tags: revoke what is still open
                nrec = st["nonces"].get(nonce)
                if nrec and nrec["state"] == "issued":
                    nrec["state"], nrec["at"] = "revoked", now
                    log.warning("shared session: mixed input -> envelope revoked")
            out.unvalidated = bool(_ANY_TAG.search(text or ""))
            return None
        nonce, inner, _extra = found
        nrec = st["nonces"].get(nonce)
        if nrec and nrec["state"] == "issued":
            if nrec.get("fp") != fingerprint(inner):
                # right tags, other body: not the prompt that was typed (edited, shortened)
                nrec["state"], nrec["at"] = "revoked", now
                log.warning("shared session: envelope body differs from the typed prompt -> not Telegram")
            elif now - nrec["at"] <= self.job_ttl and nrec["session"] == st["transcript"]["session"]:
                # Bound by the session and the long orphan bound T2, NOT by T1: an input may wait in Claude's queue.
                nrec["appearances"], nrec["offs"], nrec["first_kind"] = 1, [offset], kind
                nrec["gen"] = _generation(st)
                return nonce
            else:
                nrec["state"], nrec["at"] = "expired", now
        elif nrec and nrec["state"] == "consumed":
            if (not nrec.get("closed") and nrec["session"] == st["transcript"]["session"]
                    and nrec.get("gen") == _generation(st)
                    and offset in nrec.get("offs", [nrec.get("off")])):
                out.noop = True                           # this very record, re-read after a restart (same generation only)
                return None
            if (nrec.get("fp") == fingerprint(inner) and nrec.get("appearances", 1) == 1
                    and nrec.get("first_kind") == "queued" and kind == "human" and not nrec.get("closed")
                    and nrec["session"] == st["transcript"]["session"] and now - nrec["at"] <= self.job_ttl):
                # the SAME input once more, ONLY as the queued_command -> human transition (Claude runs what it queued),
                # same session, same body, within T2 and before any transcript reset; anything else is a replay
                nrec["appearances"] = 2
                nrec["offs"] = list(nrec.get("offs", [])) + [offset]
                out.again = True
                return nonce
            nrec["replays"] = nrec.get("replays", 0) + 1
            log.warning("shared session: replay of a consumed envelope (no binding through it)")
        out.unvalidated = True
        return None

    def _on_human(self, st, offset, rec, out, kind) -> None:
        """A user record: a turn boundary. An exact bridge envelope starts a Telegram route, anything else is
        terminal (no route)."""
        now = self._clock()
        text = record_text(rec) if kind == "human" else ""
        consumed = self._consume(st, offset, text, now, out, "human") if kind == "human" else None
        if out.noop:
            return
        self._end_route(st, now)
        uuid = rec.get("uuid") if isinstance(rec.get("uuid"), str) else None
        st["boundary_uuid"] = uuid                        # the record that started the turn Claude is running now
        if consumed:
            nrec = st["nonces"][consumed]
            nrec["state"], nrec["off"] = "consumed", nrec.get("off") if out.again else offset
            st["turn"] = {"kind": "tg", "chat": nrec["chat"], "nonce": consumed, "job": None, "uuid": uuid}
            return
        st["turn"] = {"kind": "other", "chat": None, "nonce": None, "job": None, "uuid": uuid}

    def _on_queued(self, st, offset, rec, out, kind) -> None:
        """An attachment `queued_command` that carries a bridge tag: Claude took that input while a turn runs. A valid
        envelope is consumed here, but this is NOT a new answer, so the running route keeps its recipient: same chat
        continues; no route yet (a terminal turn is running) starts one for that chat; ANOTHER chat inside an active
        route never takes the route over. A tag that cannot be validated (expired, revoked, unknown, replayed, two
        envelopes, changed body) is input Claude has seen that we cannot attribute: an active route is cut as well."""
        now = self._clock()
        consumed = self._consume(st, offset, record_text(rec), now, out, "queued")
        if out.noop:
            return
        turn = st["turn"]
        active = turn is not None and turn["kind"] in ("tg", "notif")
        if not consumed:
            if out.unvalidated and active and not turn.get("cut"):
                turn["cut"] = True
                out.cut = True
                out.cut_chats = (turn["chat"],)
                log.warning("shared session: unvalidated bridge input inside a running answer -> forwarding stopped")
            return
        nrec = st["nonces"][consumed]
        nrec["state"] = "consumed"
        if not out.again:
            nrec["off"] = offset
        uuid = rec.get("uuid") if isinstance(rec.get("uuid"), str) else None
        if not active:
            if turn is None or turn["kind"] == "other":
                # The route is opened by a queued_command INSIDE a running turn: that turn's own prompt record (the
                # `other` turn's opening record) is the anchor whose Stop ends it; nothing older may.
                st["turn"] = {"kind": "tg", "chat": nrec["chat"], "nonce": consumed, "job": None, "uuid": uuid,
                              "anchor": st.get("boundary_uuid")}
        elif turn["chat"] != nrec["chat"]:
            was_cut = bool(turn.get("cut"))
            turn["cut"] = True
            out.cut = True
            out.cut_chats = () if out.again else ((nrec["chat"],) if was_cut else (turn["chat"], nrec["chat"]))
            log.warning("shared session: input of another chat inside a running answer -> forwarding stopped")

    def _on_tool_result(self, st, offset, rec, out, kind) -> None:
        task = launch_task_id(rec)
        turn = st["turn"]
        if not task or not turn or turn["kind"] != "tg" or turn.get("cut") or task in st["jobs"]:
            return                                        # terminal / unbound turn: never inherits
        nrec = st["nonces"].get(turn["nonce"] or "")
        if not nrec or nrec["state"] != "consumed":
            return
        now = self._clock()
        st["jobs"][task] = {"chat": turn["chat"], "nonce": turn["nonce"], "session": nrec["session"],
                            "state": "pending", "bound_at": now, "at": now}

    def _valid_pending(self, st, job, now) -> bool:
        """A pending job that may still be delivered (TTL and session are checked and recorded)."""
        if job is None or job["state"] != "pending":
            return False
        if now - job["bound_at"] > self.job_ttl:
            job["state"], job["at"] = "expired", now
            return False
        if job["session"] != st["transcript"]["session"]:
            job["state"], job["at"] = "revoked", now
            return False
        return True

    def _on_notification(self, st, offset, rec, out, kind) -> None:
        """The harness writes the completion when it STARTS processing it: a turn boundary. A bound, valid, authorised
        job starts a result route for ITS OWN chat (durable claim first); anything else is terminal-only."""
        now = self._clock()
        task = notification_task_id(record_text(rec))
        job = st["jobs"].get(task or "")
        turn = st["turn"]
        if turn and turn["kind"] == "notif" and turn.get("job") == task:
            out.noop = True                               # a repeat inside its own route: nothing changes
            return
        uuid = rec.get("uuid") if isinstance(rec.get("uuid"), str) else None
        self._end_route(st, now)
        st["boundary_uuid"] = uuid
        other = {"kind": "other", "chat": None, "nonce": None, "job": None, "uuid": uuid}
        if not self._valid_pending(st, job, now):
            st["turn"] = other                            # terminal / unbound / expired / foreign / already claimed
            return
        if not self._authorised(job["chat"]):
            job["state"], job["at"] = "blocked", now
            st["turn"] = other
            log.warning("shared session: chat no longer authorised -> result not delivered")
            return
        job["state"], job["at"] = "claimed", now          # durable claim BEFORE anything is forwarded
        st["turn"] = {"kind": "notif", "chat": job["chat"], "nonce": None, "job": task, "uuid": uuid}
        out.claim = {"task": task, "chat": job["chat"], "uuid": uuid}

    # ---- turn end, confirmed sends, expiry -----------------------------------
    def turn_end(self) -> None:
        """The Stop hook (or the last-resort idle fallback) ended the turn. A result route that never had a confirmed
        send is uncertain; `delivered` is only ever set by :meth:`confirm_delivered`."""
        with self._lock:
            if self._s is None or self.degraded is not None or self._s["turn"] is None:
                return

            def f(st):
                self._end_route(st, self._clock())
                st["turn"] = None
            try:
                self._commit(f)
            except StateError:
                pass

    def confirm_delivered(self, task: str, chat: int) -> None:
        """Telegram CONFIRMED a send of a result text of ``task`` to ``chat``."""
        with self._lock:
            job = (self._s or {}).get("jobs", {}).get(task)
            if self.degraded is not None or not job or job["chat"] != chat or job["state"] not in ("claimed", "uncertain"):
                return

            def f(st):
                st["jobs"][task]["state"], st["jobs"][task]["at"] = "delivered", self._clock()
            try:
                self._commit(f)
            except StateError:
                pass

    def expire(self) -> None:
        with self._lock:
            if self._s is None or self.degraded is not None:
                return
            now = self._clock()
            if now - self._last_expire < EXPIRE_INTERVAL:
                return
            self._last_expire = now
            t1, t2 = self.nonce_ttl, self.job_ttl

            def f(st):
                for rec in st["nonces"].values():
                    # T1 only cleans up nonces whose injection was never completed; a typed input may wait in Claude's
                    # own queue for as long as it likes, bounded only by the orphan bound T2.
                    limit = t2 if rec.get("typed") else t1
                    if rec["state"] == "issued" and now - rec["at"] > limit:
                        rec["state"], rec["at"] = "expired", now
                for job in st["jobs"].values():
                    if job["state"] == "pending" and now - job["bound_at"] > t2:
                        job["state"], job["at"] = "expired", now
                    elif job["state"] == "claimed" and now - job["at"] > t2:
                        job["state"], job["at"] = "uncertain", now
                turn = st["turn"]
                if turn and turn["kind"] == "notif" and st["jobs"].get(turn.get("job") or "", {}).get("state") not in (
                        "claimed", "delivered"):
                    st["turn"] = None                         # its job is dead: the turn ends with it
                for n in [n for n, r in st["nonces"].items() if r["state"] != "issued" and now - r["at"] > 2 * t2]:
                    del st["nonces"][n]
                for rid in [k for k, v in st["sends"].items() if now - v["at"] > 2 * t2]:
                    del st["sends"][rid]
                for tid in [k for k, j in st["jobs"].items()
                            if j["state"] not in ("pending", "claimed") and now - j["at"] > 2 * t2]:
                    del st["jobs"][tid]
            probe = copy.deepcopy(self._s)
            f(probe)
            if probe == self._s:
                return
            try:
                self._commit(f)
            except StateError:
                pass

    def close_turn(self) -> None:
        """Idle fallback: close a turn nobody ended (no Stop hook); a claimed job becomes uncertain."""
        self.turn_end()


# ----------------------------------------------------------------------------- config

def validate_config(cfg) -> None:
    """Reject an unusable configuration before any effect. Raises ``config.ConfigError``."""
    from .config import ConfigError
    on = getattr(cfg, "shared_session_delivery", False)
    if type(on) is not bool:
        raise ConfigError("'shared_session_delivery' must be a boolean.")
    for name in ("shared_session_nonce_ttl", "shared_session_job_ttl"):
        v = getattr(cfg, name, None)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise ConfigError(f"'{name}' must be a positive finite number.")
    if cfg.shared_session_nonce_ttl > cfg.shared_session_job_ttl:
        raise ConfigError("'shared_session_nonce_ttl' must not exceed 'shared_session_job_ttl'.")
    if not on:
        return
    if getattr(cfg, "mode", "") != "attach" or getattr(cfg, "agent", "") != "claude-code":
        raise ConfigError("'shared_session_delivery' requires mode 'attach' and agent 'claude-code'.")
    if getattr(cfg, "reply_to_origin_chat", False) is not True:
        raise ConfigError("'shared_session_delivery' requires 'reply_to_origin_chat': true.")
    if not getattr(cfg, "tmux_session", "") or not getattr(cfg, "signal_file", ""):
        raise ConfigError("'shared_session_delivery' requires 'tmux_session' and 'signal_file'.")
