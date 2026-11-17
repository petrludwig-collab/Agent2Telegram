"""Idempotent ``notify --key``: a small per-key ledger so a repeated call cannot duplicate a message.

One JSON record per key lives in ``<state dir>/notify-keys/``, guarded by a per-key ``flock`` so
separate processes (cron jobs, scripts) agree on who may send. The record stores a *fingerprint*
of the target and text (never the text itself).

Record states and what a repeated ``notify --key`` does with them:

  sending    intent persisted BEFORE the first request. If the lock is free again, the sender died
             mid-send; the request may or may not have reached Telegram -> ``uncertain``.
  delivered  every part returned a ``message_id`` -> nothing is sent, the original result is reported.
  failed     Telegram definitely rejected the first request, nothing was delivered -> a rerun may send.
  uncertain  a request may have been accepted (timeout, network/5xx error, partial multi-part
             success, crash, unreadable record) -> NEVER resent automatically. There is no force
             option: wait for the record to expire or use a new key.

A key is a bounded dedup window, not a permanent identity: ``expires_at`` is fixed when the key is
first used. Expired records are removed only by the sending path, never by ``notify-status``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import time
from pathlib import Path

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - non-POSIX is not a target
    fcntl = None  # type: ignore[assignment]

from .telegram import TelegramError, definitely_rejected, split_message

DEFAULT_TTL = 7 * 24 * 3600
MAX_TTL = 10 * 365 * 24 * 3600      # sane upper bound (10 years); keeps every timestamp printable
MAX_KEY_LEN = 200
RECORD_VERSION = 1

EXIT_OK = 0
EXIT_FAILED = 1          # certain that nothing was delivered
EXIT_COLLISION = 3       # same key, different target/text; nothing sent
EXIT_UNCERTAIN = 4       # may or may not have been delivered; do not resend blindly
EXIT_IN_PROGRESS = 5     # another process is sending under this key right now


class NoMessageId(Exception):
    """Telegram answered ok but without a message_id: the message may well have been delivered."""


class InvalidKey(ValueError):
    """Invalid ``--key`` value."""


def check_key(key: str) -> str:
    if not key or len(key) > MAX_KEY_LEN or not key.isprintable():
        raise InvalidKey(f"--key must be 1-{MAX_KEY_LEN} printable characters")
    return key


def available() -> bool:
    return fcntl is not None


def _now() -> float:
    return time.time()


def bot_id(token: str) -> str | None:
    """The numeric bot id (the part of the token before ':'), or None when it cannot be derived.
    It is not a secret. Nothing after the ':' is ever read into the ledger."""
    head, sep, _ = (token or "").partition(":")
    return head if sep and head.isascii() and head.isdigit() else None


def _is_bot_id(bot) -> bool:
    return isinstance(bot, str) and bot.isascii() and bot.isdigit()     # non-empty ASCII digits


def _base(ledger_dir: Path, bot: str, key: str) -> Path:
    """Records are namespaced by bot: one state directory may serve several bots."""
    return ledger_dir / hashlib.sha256(f"{bot}\0{key}".encode("utf-8")).hexdigest()[:40]


def fingerprint(bot: str, target: int, text: str) -> str:
    blob = json.dumps({"bot": bot, "target": target, "text": text}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


STATES = ("sending", "delivered", "failed", "uncertain")


def _number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and -math.inf < x < math.inf


def _consistent(rec: dict) -> bool:
    """The state must agree with the recorded message_ids: failed means none were sent, delivered
    means every part was confirmed, uncertain may hold a partial set."""
    ids, total = rec["message_ids"], rec["parts_total"]
    if rec["state"] == "failed":
        return not ids
    if rec["state"] == "delivered":
        return total >= 1 and len(ids) == total
    return True


def _valid(rec) -> bool:
    """Every field the code relies on has the right type; anything else is an unusable record."""
    return (isinstance(rec, dict) and rec.get("v") == RECORD_VERSION
            and rec.get("state") in STATES
            and isinstance(rec.get("key"), str) and isinstance(rec.get("fingerprint"), str)
            and _is_bot_id(rec.get("bot"))
            and all(_number(rec.get(f)) for f in ("created_at", "updated_at", "expires_at"))
            and isinstance(rec.get("message_ids"), list)
            and all(type(i) is int for i in rec["message_ids"])
            and type(rec.get("parts_total")) is int and rec["parts_total"] >= 0
            and isinstance(rec.get("detail"), str)
            and _consistent(rec))


def _read(path: Path, bot: str | None = None, key: str | None = None):
    """The record dict, None when absent; raises ValueError when present but unusable. With *bot* and
    *key* the record must also belong to exactly that bot and key (a record planted or copied under
    another path is unusable, whatever it says)."""
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return None
    rec = json.loads(raw)
    if not _valid(rec):
        raise ValueError("unusable ledger record")
    if (bot is not None and rec["bot"] != bot) or (key is not None and rec["key"] != key):
        raise ValueError("ledger record belongs to another bot or key")
    return rec


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dir(path: Path) -> None:
    """Create the ledger directory (and missing parents) and make each new entry durable."""
    if not path.name:
        raise ValueError("empty ledger path")
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            pass
    # Unconditionally, also when everything already existed: another process (or an earlier,
    # interrupted attempt) may have created these entries without making them durable. Mandatory:
    # the ledger directory, the entry of the ledger in the state directory, and the entry of the
    # state directory in its parent. A failure stops the call before the intent is written.
    _fsync_dir(path)
    _fsync_dir(path.parent)
    _fsync_dir(path.parent.parent)


def _write(path: Path, rec: dict) -> None:
    """Atomic replace, fsynced, so the intent survives a crash right after this returns."""
    tmp = path.with_name(path.name + ".tmp")     # fixed name: only ever written under the key lock
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _try_lock(lock_path: Path):
    """Non-blocking exclusive lock; the open fd, or None when someone else holds it. Re-checks that
    the lock file was not unlinked (pruned) between open and flock, else two holders could coexist."""
    while True:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        try:
            if os.fstat(fd).st_ino == os.stat(lock_path).st_ino:
                return fd
        except FileNotFoundError:
            pass
        os.close(fd)


def _expired(rec_path: Path, now: float) -> bool:
    try:
        rec = _read(rec_path)
    except (ValueError, OSError):
        return False                                      # unusable: leave it for a human
    return rec is not None and rec["expires_at"] <= now


def _prune(ledger_dir: Path, now: float) -> None:
    """Drop expired records of other keys. Skips any key whose lock is busy."""
    for rec_path in ledger_dir.glob("*.json"):
        if not _expired(rec_path, now):
            continue
        lock_path = rec_path.with_suffix(".lock")
        fd = _try_lock(lock_path)
        if fd is None:
            continue
        try:
            if not _expired(rec_path, now):               # re-check under the lock: it may have been renewed
                continue
            for p in (rec_path, rec_path.with_name(rec_path.name + ".tmp"), lock_path):
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()
        finally:
            os.close(fd)


def _result(state: str, code: int, message: str, rec: dict | None) -> dict:
    return {"state": state, "exit_code": code, "message": message, "record": rec}


def _send_parts(client, target: int, text: str, rec: dict) -> dict:
    """Send all parts, at most one request per part. Updates *rec* in place with the final state."""
    chunks = split_message(text)
    rec["parts_total"] = len(chunks)
    ids: list[int] = []
    try:
        for chunk in chunks:
            message_id = client.send_part_once(target, chunk)
            if message_id is None:
                raise NoMessageId()
            ids.append(message_id)
    except TelegramError as e:
        rec["message_ids"] = ids
        if not ids and definitely_rejected(e):
            rec.update(state="failed", detail=str(e))
        elif ids:
            rec.update(state="uncertain", detail=f"partial: {len(ids)} of {len(chunks)} parts sent; {e}")
        else:
            rec.update(state="uncertain", detail=str(e))
    except Exception as e:  # noqa: BLE001 - anything unexpected after a send started is unclear
        detail = "answer without a message_id" if isinstance(e, NoMessageId) else f"unexpected {e.__class__.__name__}"
        rec.update(message_ids=ids, state="uncertain", detail=detail)
    else:
        rec.update(message_ids=ids, state="delivered", detail="")
    return rec


def notify_keyed(client, ledger_dir: Path, bot: str, key: str, target: int, text: str, *, ttl: float = DEFAULT_TTL) -> dict:
    """Send *text* to *target* at most once per *key*. Returns ``{state, exit_code, message, record}``."""
    if not _is_bot_id(bot):
        raise ValueError("bot id must be numeric")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("nothing to send")                # before anything is recorded
    if not (isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and 0 < ttl <= MAX_TTL):
        raise ValueError("ttl must be a positive number of seconds, at most 10 years")
    _ensure_dir(ledger_dir)
    base = _base(ledger_dir, bot, key)
    rec_path = base.with_suffix(".json")
    lock_fd = _try_lock(base.with_suffix(".lock"))
    if lock_fd is None:
        return _result("sending", EXIT_IN_PROGRESS,
                       "another process is sending under this key; not sending again (see notify-status)", None)
    try:
        now = _now()
        with contextlib.suppress(OSError):               # housekeeping must never block a send
            _prune(ledger_dir, now)
        fp = fingerprint(bot, target, text)
        try:
            rec = _read(rec_path, bot, key)
        except (ValueError, OSError):
            return _result("uncertain", EXIT_UNCERTAIN,
                           "ledger record for this key is unreadable; not sending (remove it to start over)", None)
        if rec is not None and rec["expires_at"] <= now:
            rec = None                                    # dedup window over: the key is free again
        if rec is not None:
            if rec.get("fingerprint") != fp:
                return _result("collision", EXIT_COLLISION,
                               "key already used for a different target or text; nothing sent", rec)
            state = rec["state"]
            if state == "delivered":
                return _result("delivered", EXIT_OK, "already delivered; nothing sent", rec)
            if state == "sending":                        # lock is free, so the sender died mid-send
                rec.update(state="uncertain", updated_at=now, detail="sender interrupted mid-send")
                _write(rec_path, rec)
                state = "uncertain"
            if state == "uncertain":
                return _result("uncertain", EXIT_UNCERTAIN,
                               "an earlier send may have been delivered; not resending", rec)
            # state == "failed": a definite rejection, nothing was delivered -> send again, but the
            # window stays the one fixed when the key was first used
            rec.update(state="sending", updated_at=now, message_ids=[], parts_total=0, detail="")
        else:
            rec = {"v": RECORD_VERSION, "key": key, "fingerprint": fp, "state": "sending",
                   "bot": bot, "created_at": now, "updated_at": now, "expires_at": now + ttl,
                   "message_ids": [], "parts_total": 0, "detail": ""}
        _write(rec_path, rec)                             # intent first, durable, BEFORE any request
        _send_parts(client, target, text, rec)
        rec["updated_at"] = _now()
        try:
            _write(rec_path, rec)
        except OSError as e:                              # outcome known but not recorded: stay safe
            rec.update(state="uncertain", detail=f"result could not be recorded ({e.__class__.__name__})")
        state = rec["state"]
        if state == "delivered":
            return _result(state, EXIT_OK, "sent", rec)
        if state == "failed":
            return _result(state, EXIT_FAILED, f"not delivered: {rec['detail']}", rec)
        return _result(state, EXIT_UNCERTAIN, f"may or may not be delivered: {rec['detail']}", rec)
    finally:
        os.close(lock_fd)


def read_status(ledger_dir: Path, bot: str, key: str) -> dict:
    """Strictly read-only: opens one file, takes no lock, creates and removes nothing."""
    if not _is_bot_id(bot):
        raise ValueError("bot id must be numeric")
    now = _now()
    try:
        rec = _read(_base(ledger_dir, bot, key).with_suffix(".json"), bot, key)
    except (ValueError, OSError):
        return {"key": key, "state": "uncertain", "detail": "ledger record is unusable"}
    if rec is None:
        return {"key": key, "state": "unknown"}
    out = {"key": key, "state": rec["state"], "message_ids": rec.get("message_ids", []),
           "parts_total": rec.get("parts_total", 0), "created_at": rec.get("created_at"),
           "updated_at": rec.get("updated_at"), "expires_at": rec.get("expires_at"),
           "expired": rec["expires_at"] <= now, "detail": rec.get("detail", "")}
    if rec["state"] == "sending":
        out["note"] = "send started, no result recorded: in flight or interrupted; treat as uncertain"
    return out
