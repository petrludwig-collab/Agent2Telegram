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
#: Fixed prompt for a deferred completion (the agent already saw the notification elsewhere).
REPORT_PROMPT = "Background task {task} has finished. Report its result to the user."


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


def parse_envelope(text: str):
    """``(nonce, inner)`` when the whole normalised text is exactly one envelope, else None."""
    m = _ENVELOPE.match(normalise(text))
    if not m or _ANY_TAG.search(m.group("inner")):
        return None
    return m.group("n"), m.group("inner")


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


def record_text(rec: dict) -> str:
    return readers._text_of((rec.get("message") or {}).get("content"))


def classify(rec) -> str:
    """ignore | tool_result | human | notification | foreign — from the record's own structure."""
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


@dataclass
class Sync:
    resume: int                  # byte offset to continue reading from
    changed: bool                # the transcript is new/replaced: the caller must move its cursor


def _fresh() -> dict:
    return {"v": VERSION, "transcript": None, "turn": None, "nonces": {}, "jobs": {}, "completions": []}


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
    need(isinstance(st.get("nonces"), dict) and isinstance(st.get("jobs"), dict)
         and isinstance(st.get("completions"), list))
    for n, rec in st["nonces"].items():
        need(re.fullmatch(_NONCE, str(n)) and isinstance(rec, dict) and rec.get("state") in NONCE_STATES
             and rec.get("purpose") in ("inbound", "report") and type(rec.get("chat")) is int
             and isinstance(rec.get("at"), (int, float)) and isinstance(rec.get("session"), str))
    for tid, job in st["jobs"].items():
        need(_TASK_ID.fullmatch(str(tid)) and isinstance(job, dict) and job.get("state") in JOB_STATES
             and type(job.get("chat")) is int and isinstance(job.get("bound_at"), (int, float))
             and isinstance(job.get("at"), (int, float)) and isinstance(job.get("session"), str))
    for c in st["completions"]:
        need(isinstance(c, dict) and isinstance(c.get("task"), str) and type(c.get("offset")) is int)


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
                 clock=time.time, authorised=lambda chat: True, busy=lambda: False) -> None:
        self.path = Path(path)
        self.nonce_ttl = float(nonce_ttl)
        self.job_ttl = float(job_ttl)
        self._clock = clock
        self._authorised = authorised
        self._busy = busy
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
            try:
                dfd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
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

    def sync_transcript(self, path) -> Sync | None:
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
            return self._sync_locked(ident)

    def _sync_locked(self, ident: dict) -> Sync:
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
            if same and ident["size"] >= t["offset"]:
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
        for job in st["jobs"].values():
            if job["state"] == "pending":
                job["state"], job["at"] = "revoked", now
            elif job["state"] == "claimed":
                job["state"], job["at"] = "uncertain", now
        st["completions"] = []
        st["turn"] = None
        adopt(st)

    # ---- issuing (R8) ------------------------------------------------------
    def issue(self, chat: int) -> str:
        """Store a new one-time nonce for ``chat`` BEFORE the prompt is typed. Raises StateError when it
        cannot be stored safely; the caller must then not inject."""
        with self._lock:
            return self._issue_locked(chat, "inbound", None)

    def _issue_locked(self, chat, purpose, job) -> str:
        if self.degraded is not None or self._s is None or self._s["transcript"] is None:
            raise StateError("state not usable")
        if type(chat) is not int:
            raise StateError("no chat")
        nonce = new_nonce()
        now = self._clock()

        def f(st):
            st["nonces"][nonce] = {"chat": chat, "state": "issued", "purpose": purpose, "job": job,
                                   "session": st["transcript"]["session"], "at": now, "off": None,
                                   "replays": 0}
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
                job = st["jobs"].get(rec.get("job") or "")
                if rec["purpose"] == "report" and job and job["state"] == "claimed":
                    job["state"], job["at"] = "uncertain", self._clock()
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
            handler = {"human": self._on_human, "foreign": self._on_human,
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

    def _on_human(self, st, offset, rec, out, kind) -> None:
        now = self._clock()
        text = record_text(rec) if kind == "human" else ""
        consumed = None
        parsed = parse_envelope(text) if kind == "human" else None
        if parsed:
            nonce = parsed[0]
            nrec = st["nonces"].get(nonce)
            if nrec and nrec["state"] == "issued":
                if now - nrec["at"] <= self.nonce_ttl and nrec["session"] == st["transcript"]["session"]:
                    consumed = nonce
                else:
                    nrec["state"], nrec["at"] = "expired", now
            elif nrec and nrec["state"] == "consumed":
                if nrec.get("off") == offset:
                    out.noop = True                       # this very record, re-read after a restart
                    return
                nrec["replays"] = nrec.get("replays", 0) + 1
                log.warning("shared session: replay of a consumed envelope (no binding through it)")
        elif kind == "human":
            for nonce in _OPEN_TAG.findall(text):         # mixed input: our tag inside other text
                nrec = st["nonces"].get(nonce)
                if nrec and nrec["state"] == "issued":
                    nrec["state"], nrec["at"] = "revoked", now
                    log.warning("shared session: mixed input -> envelope revoked, turn is not Telegram")
        uuid = rec.get("uuid") if isinstance(rec.get("uuid"), str) else None
        if consumed:
            nrec = st["nonces"][consumed]
            job = st["jobs"].get(nrec.get("job") or "")
            nrec["state"], nrec["off"] = "consumed", offset
            if nrec["purpose"] == "inbound":
                st["turn"] = {"kind": "tg", "chat": nrec["chat"], "nonce": consumed, "job": None, "uuid": uuid}
                return
            if job and job["state"] == "claimed":
                st["turn"] = {"kind": "notif", "chat": nrec["chat"], "nonce": consumed,
                              "job": nrec["job"], "uuid": uuid}
                return
        st["turn"] = {"kind": "other", "chat": None, "nonce": None, "job": None, "uuid": uuid}

    def _on_tool_result(self, st, offset, rec, out, kind) -> None:
        task = launch_task_id(rec)
        turn = st["turn"]
        if not task or not turn or turn["kind"] != "tg" or task in st["jobs"]:
            return                                        # terminal / unbound turn: never inherits
        nrec = st["nonces"].get(turn["nonce"] or "")
        if not nrec or nrec["state"] != "consumed":
            return
        now = self._clock()
        st["jobs"][task] = {"chat": turn["chat"], "nonce": turn["nonce"], "session": nrec["session"],
                            "state": "pending", "bound_at": now, "at": now}

    def _on_notification(self, st, offset, rec, out, kind) -> None:
        now = self._clock()
        task = notification_task_id(record_text(rec))
        job = st["jobs"].get(task or "")
        turn = st["turn"]
        uuid = rec.get("uuid") if isinstance(rec.get("uuid"), str) else None
        other = {"kind": "other", "chat": None, "nonce": None, "job": None, "uuid": uuid}
        if job is None or job["state"] != "pending":
            if turn is None:
                st["turn"] = other                        # the harness started a turn that is not ours
            return
        if now - job["bound_at"] > self.job_ttl:
            job["state"], job["at"] = "expired", now
            st["turn"] = turn or other
            return
        if job["session"] != st["transcript"]["session"]:
            job["state"], job["at"] = "revoked", now
            st["turn"] = turn or other
            return
        if not any(c["task"] == task for c in st["completions"]):
            st["completions"].append({"task": task, "offset": offset, "seen_at": now})
        if turn is not None or self._busy():
            st["turn"] = turn or other                    # deferred: claimed only when nothing is running
            return
        st["completions"] = [c for c in st["completions"] if c["task"] != task]
        if not self._authorised(job["chat"]):
            job["state"], job["at"] = "blocked", now
            st["turn"] = other
            log.warning("shared session: chat no longer authorised -> result not delivered")
            return
        job["state"], job["at"] = "claimed", now          # durable intention BEFORE the turn is opened
        st["turn"] = {"kind": "notif", "chat": job["chat"], "nonce": None, "job": task, "uuid": uuid}
        out.claim = {"task": task, "chat": job["chat"], "uuid": uuid}

    # ---- turn end, deferred claims, expiry ----------------------------------
    def turn_end(self, *, text_sent: bool) -> None:
        with self._lock:
            if self._s is None or self.degraded is not None or self._s["turn"] is None:
                return

            def f(st):
                turn = st["turn"]
                job = st["jobs"].get(turn.get("job") or "")
                if turn["kind"] == "notif" and job and job["state"] == "claimed":
                    job["state"], job["at"] = ("delivered" if text_sent else "uncertain"), self._clock()
                st["turn"] = None
            try:
                self._commit(f)
            except StateError:
                pass

    def claim_deferred(self) -> dict | None:
        """The oldest deferred completion that is still valid, claimed in ONE write together with the
        one-time envelope of its report prompt. None when nothing may be claimed now."""
        with self._lock:
            if (self._s is None or self.degraded is not None or self._s["turn"] is not None
                    or self._busy() or not self._s["completions"]):
                return None
            now = self._clock()
            result: dict = {}

            def f(st):
                keep = []
                for c in st["completions"]:
                    job = st["jobs"].get(c["task"])
                    if result or not job or job["state"] != "pending":
                        if result and job and job["state"] == "pending":
                            keep.append(c)
                        continue
                    if now - job["bound_at"] > self.job_ttl:
                        job["state"], job["at"] = "expired", now
                        continue
                    if not self._authorised(job["chat"]):
                        job["state"], job["at"] = "blocked", now
                        log.warning("shared session: chat no longer authorised -> result not delivered")
                        continue
                    nonce = new_nonce()
                    st["nonces"][nonce] = {"chat": job["chat"], "state": "issued", "purpose": "report",
                                           "job": c["task"], "session": st["transcript"]["session"],
                                           "at": now, "off": None, "replays": 0}
                    job["state"], job["at"] = "claimed", now
                    result.update(task=c["task"], chat=job["chat"], nonce=nonce)
                st["completions"] = keep
            try:
                self._commit(f)
            except StateError:
                return None
            return dict(result) if result else None

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
                    if rec["state"] == "issued" and now - rec["at"] > t1:
                        rec["state"], rec["at"] = "expired", now
                        job = st["jobs"].get(rec.get("job") or "")
                        if rec["purpose"] == "report" and job and job["state"] == "claimed":
                            job["state"], job["at"] = "uncertain", now
                for job in st["jobs"].values():
                    if job["state"] == "pending" and now - job["bound_at"] > t2:
                        job["state"], job["at"] = "expired", now
                    elif job["state"] == "claimed" and now - job["at"] > t2:
                        job["state"], job["at"] = "uncertain", now
                st["completions"] = [c for c in st["completions"]
                                     if st["jobs"].get(c["task"], {}).get("state") == "pending"]
                for n in [n for n, r in st["nonces"].items() if r["state"] != "issued" and now - r["at"] > 2 * t2]:
                    del st["nonces"][n]
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
        self.turn_end(text_sent=False)


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
