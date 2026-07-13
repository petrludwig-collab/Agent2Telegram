"""Attach mode — drive an existing live agent session, the way a hand-rolled bridge does.

Async model:
  * **inbound** (main thread): poll Telegram → inject each message into the live tmux session
    via send-keys. No blocking wait.
  * **outbound** (background thread): tail the agent transcript and, via an agent-specific
    :mod:`reader`, forward every assistant message of Telegram-originated turns, drive a live
    one-line tool-call status bubble, and detect end-of-turn (Codex: ``task_complete`` in the
    log; Claude Code: a marker its Stop hook writes; plus an idle fallback).
  * **typing** (background thread): assert "typing…" while a turn is in flight, independent of
    the send path so a flood-control sleep can't starve it.

This keeps the agent's full session (context, persona, tools) and adds Telegram I/O around it.
"""
from __future__ import annotations

import glob
import html
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from . import readers
from .config import Config
from .session import TmuxSession
from .telegram import TelegramClient

log = logging.getLogger("agent2telegram.attach")

#: Fallback only: how long the transcript may be quiet before we force-end a turn, in case
#: the Stop-hook turn-end marker never arrives. The marker is the primary, precise signal —
#: this just stops "typing…" from hanging forever if the hook is missing/misconfigured.
IDLE_DONE = 90.0
#: How often we re-assert the "typing…" chat action (Telegram shows it for ~5s). Kept well
#: under that window so a turn never shows a gap, even right after a sent message clears it.
TYPING_INTERVAL = 1.5

#: Codex only: hold the first scraped tool bubble until the turn's intro text has been forwarded
#: (agents usually say what they'll do, THEN call the tool). The scraper is live but the Codex
#: transcript text lags, so without this the bubble jumps ahead of the intro line. After this
#: grace we show bubbles anyway, since some turns call a tool with no intro text.
TUI_BUBBLE_GRACE = 3.0

#: Registered with Telegram (setMyCommands) so typing "/" shows the command autocomplete menu.
BOT_COMMANDS = [
    {"command": "start", "description": "Intro and what you can send"},
    {"command": "help", "description": "Intro and what you can send"},
    {"command": "status", "description": "Connection and voice status"},
    {"command": "health", "description": "Quick bridge and session health"},
    {"command": "diag", "description": "Safe diagnostics for the owner"},
    {"command": "setkey", "description": "Enable voice (your ElevenLabs API key)"},
    {"command": "id", "description": "Show your Telegram id"},
]

import re as _re  # noqa: E402
from .readers import _short  # noqa: E402

#: Codex renders tool activity live in its TUI but only writes it to the rollout at completion.
#: For Codex (attach), we scrape the tmux pane for these lines so tool bubbles appear LIVE —
#: matching Claude Code (which logs tool_use to its transcript immediately). Claude needs no scrape.
_TUI_VERBS = {"Read": "📄", "List": "📂", "Search": "🔎", "Ran": "🛠️",
              "Edit": "✏️", "Wrote": "✏️", "Added": "✏️", "Updated": "✏️",
              "Deleted": "🗑️", "Removed": "🗑️"}


def _blocking_session_prompt(pane: str) -> str | None:
    """Identify known interactive screens that are unsafe for Telegram key injection."""
    text = (pane or "").lower()
    if "update available!" in text and "press enter to continue" in text:
        return "codex_update_prompt"
    if "trust" in text and ("press enter" in text or "continue" in text):
        return "workspace_trust_prompt"
    return None


def _empty_turn_notice(pane: str) -> tuple[str, str]:
    """Classify a Codex turn that completed without an assistant message.

    Codex prints quota failures in the TUI but records only ``task_complete`` with a null
    ``last_agent_message`` in its rollout. Inspect only the bottom of the pane, where the result
    of the just-finished prompt is rendered, so an older visible quota error cannot taint a later
    empty turn.
    """
    tail = "\n".join((pane or "").splitlines()[-12:])
    lower = tail.lower()
    if "you've hit your usage limit" in lower or "you have hit your usage limit" in lower:
        retry = _re.search(r"try\s+again\s+at\s+([^\n.]+(?:\.[mM]\.)?)", tail, _re.IGNORECASE)
        when = f" Další pokus je možný přibližně v {retry.group(1).strip()}." if retry else ""
        return (
            "usage_limit",
            "⚠️ Codex narazil na limit používání a požadavek proto nedokončil." + when
            + " Zadání nebylo potichu zahazeno; po obnovení kapacity jej pošlete znovu.",
        )
    if "rate limit" in lower or "too many requests" in lower:
        return (
            "rate_limit",
            "⚠️ Codex narazil na dočasný rate limit a požadavek nedokončil. "
            "Po krátké době jej prosím pošlete znovu.",
        )
    return (
        "empty_response",
        "⚠️ Codex ukončil požadavek bez odpovědi. Bridge zůstal aktivní, ale nemá žádný "
        "výsledek k doručení; požadavek prosím zopakujte.",
    )


def _extract_tui_tools(pane: str) -> list:
    """Pull live tool/web-search lines out of a Codex TUI capture, as bubble summaries."""
    out = []
    for raw in pane.splitlines():
        s = raw.strip()
        m = _re.search(r"Searched the web for\s+(.+)", s)
        if m:
            out.append("🔎 Web search: " + _short(m.group(1)))
            continue
        if "Searching the web" in s:
            out.append("🔎 Searching the web")
            continue
        # Codex prints the call on a bullet line ("● Ran df -h /", "● Read foo.py") and its
        # output nested under "└ …". Strip any leading bullet/branch markers so we catch the
        # verb on either line; the verb whitelist keeps plain agent text (other words) out.
        body = s.lstrip("└├│•●▪▸·*- \t")
        if not body:
            continue
        verb = body.split(" ", 1)[0]
        if verb in _TUI_VERBS:
            rest = body[len(verb):].strip()
            out.append(f"{_TUI_VERBS[verb]} {verb} {_short(rest)}".rstrip())
    return out


class AttachBridge:
    def __init__(self, cfg: Config, *, client: TelegramClient | None = None) -> None:
        if not cfg.tmux_session:
            raise ValueError("attach mode requires 'tmux_session' in config")
        self.cfg = cfg
        self.tg = client or TelegramClient(cfg.token)
        self._allowed = set(cfg.allowed_user_ids)
        self._marker = cfg.progress_marker
        self._origin = cfg.origin_prefix
        # The reader knows the agent's transcript format and turns it into a common event stream.
        self._reader = readers.for_agent(cfg.agent)
        self._pending_turn_end = False       # set when the reader signals end-of-turn (Codex)
        # Accept the configured prefix plus the legacy "Telegram:" one, so a prefix change
        # mid-conversation doesn't drop the turn in flight.
        self._origins = tuple({p for p in (cfg.origin_prefix.strip(), "Telegram:", "[TG]") if p})
        self._owner_chat = cfg.allowed_user_ids[0] if cfg.allowed_user_ids else None
        self._signal = Path(cfg.signal_file) if cfg.signal_file else None
        # Claude Code only: end-of-turn marker its Stop hook writes (keeps "typing…" lit through
        # long thinking and off exactly at turn end). Codex needs none — its rollout records
        # task_complete, so the reader signals turn end directly.
        self._turn_end = (self._signal.parent / "turn_end") if self._signal else None
        # Outbound-loop heartbeat: touched at the end of every forward cycle (see _outbound_loop).
        # The process and the inbound poller can stay alive while forwarding is wedged — a blocking
        # send or a persistent exception freezes replies silently. A watchdog notices this file go
        # stale and restarts the bridge. Per-bridge (keyed on the tmux session) so several bridges
        # from one install don't share a heartbeat.
        _slug = "".join(c if (c.isalnum() or c in "._-") else "_" for c in (cfg.tmux_session or "bridge"))
        self._heartbeat = (self._signal.parent / f"outbound_heartbeat_{_slug}") if self._signal else None
        # Codex writes a fresh rollout-*.jsonl per session under ~/.codex/sessions; auto-detect
        # the newest one (and re-detect if the session restarts). Claude Code uses a fixed path.
        self._transcript = self._resolve_transcript()
        self._last_resolve = 0.0
        self._session = TmuxSession([], name=cfg.tmux_session, cwd=Path.home(),
                                    origin_prefix=cfg.origin_prefix, boot_wait=0)
        self._stop = threading.Event()
        # Persisted ledger of already-forwarded message uuids — survives restarts/crashes/reboots
        # so resuming an interrupted turn never re-sends what was already delivered.
        self._sent_path = Path.home() / ".config" / "agent2telegram" / "attach_sent.txt"
        try:
            self._sent_keys: set = set(self._sent_path.read_text("utf-8").split())
        except OSError:
            self._sent_keys = set()
        # Durable outbound queue: a reply whose send HARD-fails (network reset, etc.) is persisted
        # here and re-sent by the outbound loop until Telegram confirms — so a turn's answer is
        # NEVER silently lost. Survives restarts (re-loaded below). Per-bridge (tmux slug).
        self._queue_path = (self._signal.parent / f"outbound_queue_{_slug}.jsonl") if self._signal else None
        self._pending_send: list = self._load_queue()
        # In-flight Telegram task ledger.  Unlike the Codex transcript this is tiny, explicit and
        # independent of the agent process, so a brand-new tmux session after a machine reboot can
        # continue the task that was active when power went away.  boot_id distinguishes a bridge
        # crash (same tmux/task still running: do not inject twice) from a real reboot.
        self._task_path = (self._signal.parent / f"inflight_task_{_slug}.json") if self._signal else None
        self._inflight_task = self._load_inflight_task()
        self._tpos = 0
        # Byte offset at which the current injected Telegram turn began. The turn-end backstop
        # must never recover assistant text from before this boundary: Codex can legitimately
        # complete an acknowledgement-only turn with last_agent_message=null.
        self._turn_transcript_start = 0
        self._turn_active = threading.Event()
        self._task_started = threading.Event()  # Codex transcript acknowledgement of submission
        self._turn_from_tg = False           # is the current transcript turn Telegram-originated?
        self._last_activity = 0.0            # monotonic ts of last transcript activity (for typing)
        self._status = {"mid": None, "shown": ""}   # live one-line tool-call status bubble
        self._last_typing = 0.0                      # monotonic ts of last "typing…" chat action
        self._typing_count = 0                       # diagnostics: typing actions in current turn
        self._turn_started = 0.0                     # diagnostics: monotonic ts of turn start
        self._max_gap = 0.0                          # diagnostics: largest gap between typing actions
        # Persist the bubble's message_id so a restart/crash mid-turn can delete the orphan it
        # would otherwise leave behind in the chat.
        self._status_path = (self._signal.parent / "status_bubble") if self._signal else None
        self._seen_tools: set = set()
        self._tui_seen: set = set()          # Codex TUI scrape: tool lines already shown this turn
        self._turn_text_sent = False         # has any text been forwarded this turn (bubble gate)
        self._bridge_started = time.monotonic()
        self._idle_warned = False

    # ---- transcript resolution --------------------------------------------
    def _codex_sessions_dir(self) -> Path:
        tp = (self.cfg.transcript_path or "").strip()
        if tp and tp.lower() != "auto":
            p = Path(tp).expanduser()
            if p.is_dir():
                return p
        return Path.home() / ".codex" / "sessions"

    @staticmethod
    def _newest_under(base: Path, *patterns: str) -> Path | None:
        files: list[str] = []
        for pat in (patterns or ("*.jsonl",)):
            files = glob.glob(str(base / "**" / pat), recursive=True)
            if files:
                break
        if not files:
            return None
        try:
            return Path(max(files, key=lambda f: Path(f).stat().st_mtime))
        except OSError:
            return None

    def _session_cwd(self) -> str | None:
        """Working directory of the driven tmux session — used to pick the matching Codex
        rollout even when other `codex` processes (e.g. cron jobs) write newer rollouts."""
        try:
            out = subprocess.run(
                ["tmux", "display-message", "-p", "-t", self.cfg.tmux_session, "#{pane_current_path}"],
                capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or None
        except (subprocess.SubprocessError, OSError):
            return None

    @staticmethod
    def _rollout_cwd(path: Path) -> str | None:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                rec = json.loads(f.readline() or "{}")
            return (rec.get("payload") or {}).get("cwd")
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _norm(p: str | None) -> str:
        """Normalize a path for comparison — resolves symlinks like /tmp → /private/tmp (macOS)."""
        if not p:
            return ""
        try:
            return str(Path(p).resolve())
        except (OSError, RuntimeError):
            return p

    def _cwd_matches(self, rollout: Path) -> bool:
        sc = self._session_cwd()
        return bool(sc) and self._norm(self._rollout_cwd(rollout)) == self._norm(sc)

    def _newest_rollout(self) -> Path | None:
        base = self._codex_sessions_dir()
        files = (glob.glob(str(base / "**" / "rollout-*.jsonl"), recursive=True)
                 or glob.glob(str(base / "**" / "*.jsonl"), recursive=True))
        if not files:
            return None
        try:
            files.sort(key=lambda f: Path(f).stat().st_mtime, reverse=True)
        except OSError:
            return None
        cwd = self._norm(self._session_cwd())
        if cwd:
            for f in files:                       # newest first → our session's own rollout
                if self._norm(self._rollout_cwd(Path(f))) == cwd:
                    return Path(f)
            return None                           # cwd known but no rollout yet → wait, don't grab
        return Path(files[0])                     # cwd unknown → best-effort newest overall

    def _resolve_transcript(self) -> Path | None:
        """Resolve the transcript to tail. An explicit path is used as-is; ``""``/``"auto"``
        auto-detects the newest transcript for the agent (Codex rollout / Claude Code session)."""
        tp = (self.cfg.transcript_path or "").strip()
        if tp and tp.lower() != "auto":
            p = Path(tp).expanduser()
            return self._newest_under(p) if p.is_dir() else p
        if self.cfg.agent == "codex":
            return self._newest_rollout()
        if self.cfg.agent == "claude-code":
            return self._newest_claude()
        return None

    def _newest_claude(self) -> Path | None:
        """Newest Claude Code transcript for the driven session, scoped by cwd so it never picks
        up another concurrent Claude session (Claude stores transcripts under a per-cwd project
        dir: ``~/.claude/projects/<cwd-with-slashes-as-dashes>/``)."""
        base = Path.home() / ".claude" / "projects"
        cwd = self._session_cwd()
        dirs: list[Path] = []
        if cwd:
            for c in {cwd, self._norm(cwd)}:
                d = base / c.replace("/", "-")
                if d.is_dir():
                    dirs.append(d)
        if not dirs:
            return self._newest_under(base) if not cwd else None
        best, best_m = None, -1.0
        for d in dirs:
            p = self._newest_under(d, "*.jsonl")
            try:
                if p and p.stat().st_mtime > best_m:
                    best, best_m = p, p.stat().st_mtime
            except OSError:
                pass
        return best

    def _maybe_reresolve(self) -> None:
        """Keep the tailed transcript pointed at our tmux session's own log (auto mode only).

        Agents write the transcript on the first message (not at launch), and a session restart
        starts a new one — so we re-check periodically. We switch when a better match appears, but
        never abandon a transcript we're already on for an in-flight turn. A no-op when the config
        gives an explicit transcript path (the path resolves to itself)."""
        if (self.cfg.transcript_path or "").strip().lower() not in ("", "auto"):
            return                                # explicit path → nothing to re-resolve
        now = time.monotonic()
        if now - self._last_resolve < 3.0:
            return
        self._last_resolve = now
        newest = self._resolve_transcript()
        if not newest or newest == self._transcript:
            return
        # cwd-scoped resolution returns OUR session's own log, so follow it even mid-turn: a new
        # rollout for the same session means the current turn is being written THERE. Blocking the
        # switch while a turn was active caused a ~90s lag (it only switched after the idle timeout)
        # — the first message looked like it took ~2 minutes. Only the cwd-unknown best-effort
        # fallback still avoids jumping away during a live turn.
        if self._session_cwd() is None and self._transcript is not None and self._turn_active.is_set():
            return
        log.info("transcript → %s", newest.name)
        self._transcript = newest
        self._tpos = 0
        self._resume_position()

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        me = self.tg.get_me()
        log.info("BRIDGE_STARTED bot=@%s tmux=%s owner=%s agent=%s",
                 me.get("username"), self.cfg.tmux_session, self._owner_chat, self.cfg.agent)
        self.tg.set_my_commands(BOT_COMMANDS)    # enable the "/" command menu in Telegram
        if not self._session.alive:
            raise RuntimeError(f"tmux session '{self.cfg.tmux_session}' not found")
        blocked = _blocking_session_prompt(self._session._capture())
        if blocked:
            raise RuntimeError(
                f"tmux session '{self.cfg.tmux_session}' is not ready: {blocked}; "
                "resolve the prompt in tmux before starting the bridge"
            )
        # Start tailing at EOF. If we've run before (the ledger has entries), rewind to the start
        # of the current turn so a reply written while we were restarting still gets forwarded —
        # the ledger dedups, so nothing already delivered is re-sent. On the very first run we do
        # NOT rewind, so attaching to an already-busy session never re-posts its prior turn.
        if self._transcript and self._transcript.exists():
            self._tpos = self._transcript.stat().st_size
            if self._sent_keys:
                self._resume_position()
        self._cleanup_orphan_status()       # remove a bubble orphaned by a prior crash/restart
        # Typing runs in its own thread so a flood-control sleep in the send path never starves it.
        threading.Thread(target=self._outbound_loop, daemon=True).start()
        threading.Thread(target=self._typing_loop, daemon=True).start()
        if self.cfg.agent == "codex":
            # Codex logs tools to the rollout only at completion → scrape the TUI for LIVE bubbles.
            threading.Thread(target=self._tui_scrape_loop, daemon=True).start()
        self._restore_inflight_task()
        try:
            self._inbound_loop()
        except BaseException:
            log.exception("BRIDGE_STOPPED reason=unhandled_exception")
            raise
        else:
            log.info("BRIDGE_STOPPED reason=normal")

    def _resume_position(self) -> None:
        """Find the most recent non-empty user message and rewind ``_tpos`` to just after it,
        so the current turn's assistant messages are re-read on startup. Combined with the
        persisted ledger this re-delivers a reply that was written while we were down, without
        re-sending anything already delivered. Also recovers the turn's Telegram origin."""
        size = self._tpos
        start = max(0, size - 5_000_000)        # large window: tool outputs can be big
        try:
            with open(self._transcript, "rb") as f:
                f.seek(start)
                tail = f.read()
        except OSError:
            return
        pos = start
        last_user_end = None
        from_tg = self._turn_from_tg
        for raw in tail.split(b"\n"):
            line_end = pos + len(raw) + 1       # +1 for the newline separator
            pos = line_end
            try:
                rec = json.loads(raw.decode("utf-8", "ignore"))
            except (json.JSONDecodeError, ValueError):
                continue
            utext = self._reader.user_text(rec)
            if utext and utext.strip():
                from_tg = utext.lstrip().startswith(self._origins)
                last_user_end = min(line_end, size)
        if last_user_end is not None:
            self._tpos = last_user_end
            self._turn_from_tg = from_tg

    def _mark_sent(self, uuid: str) -> None:
        """Record a forwarded message uuid in memory and on disk (append-only ledger)."""
        if not uuid or uuid in self._sent_keys:
            return
        self._sent_keys.add(uuid)
        try:
            self._sent_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._sent_path, "a", encoding="utf-8") as f:
                f.write(uuid + "\n")
        except OSError:
            pass

    # ---- durable outbound delivery (never drop a reply) --------------------
    @staticmethod
    def _boot_id() -> str:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text("utf-8").strip()
        except OSError:
            return "unknown"

    def _load_inflight_task(self) -> dict | None:
        if self._task_path is None or not self._task_path.exists():
            return None
        try:
            value = json.loads(self._task_path.read_text("utf-8"))
            return value if isinstance(value, dict) and value.get("text") else None
        except (OSError, ValueError) as e:
            log.error("TASK_LOAD_FAILED path=%s error=%s", self._task_path, e)
            return None

    def _persist_inflight_task(self, text: str, *, message_id: int | None = None) -> None:
        if self._task_path is None:
            return
        task = {
            "text": text,
            "message_id": message_id,
            "accepted_at": int(time.time()),
            "accepted_boot_id": self._boot_id(),
            "resumed_boot_id": None,
        }
        try:
            self._task_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._task_path.parent / (self._task_path.name + ".tmp")
            tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", "utf-8")
            tmp.replace(self._task_path)
            self._inflight_task = task
        except OSError as e:
            # Never inject a task that we failed to journal: that would recreate the original
            # silent-loss window on reboot.
            raise RuntimeError(f"could not persist in-flight task: {e}") from e

    def _write_inflight_task(self) -> None:
        if self._task_path is None or not self._inflight_task:
            return
        tmp = self._task_path.parent / (self._task_path.name + ".tmp")
        tmp.write_text(json.dumps(self._inflight_task, ensure_ascii=False, indent=2) + "\n", "utf-8")
        tmp.replace(self._task_path)

    def _clear_inflight_task(self) -> None:
        self._inflight_task = None
        if self._task_path is not None:
            try:
                self._task_path.unlink()
            except OSError:
                pass

    def _restore_inflight_task(self) -> None:
        """Restore turn routing after a bridge crash; inject once after an actual machine reboot."""
        task = self._inflight_task
        if not task:
            return
        self._turn_active.set()
        self._turn_from_tg = True
        self._last_activity = time.monotonic()
        current_boot = self._boot_id()
        if task.get("accepted_boot_id") == current_boot or task.get("resumed_boot_id") == current_boot:
            log.info("TASK_RESTORE mode=same_boot action=follow_existing_turn")
            return

        # Claim this boot before injection.  If the bridge itself crashes during resume, its next
        # start follows the existing tmux turn instead of submitting a duplicate.
        task["resumed_boot_id"] = current_boot
        task["resumed_at"] = int(time.time())
        self._write_inflight_task()
        if self._owner_chat is not None:
            self.tg.send_message(
                self._owner_chat,
                "♻️ Server znovu naběhl. Automaticky navazuji na rozpracovaný úkol.",
            )
        resume = (
            "Server/process byl během tohoto úkolu restartován. Automaticky pokračuj v původním "
            "úkolu níže. Nejdřív zkontroluj aktuální stav a již provedené změny; neopakuj "
            "nevratné kroky. Dokonči bezpečně zbývající práci, ověř výsledek a pošli uživateli "
            "závěrečný stav.\n\nPŮVODNÍ ÚKOL:\n" + str(task["text"])
        )
        log.info("TASK_RESTORE mode=new_boot action=resume message_id=%s", task.get("message_id"))
        self._inject(resume)

    def _load_queue(self) -> list:
        if self._queue_path is None or not self._queue_path.exists():
            return []
        out = []
        try:
            for line in self._queue_path.read_text("utf-8").splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except (OSError, ValueError) as e:
            log.error("QUEUE_LOAD_FAILED path=%s error=%s", self._queue_path, e)
            return []
        return out

    def _persist_queue(self) -> None:
        if self._queue_path is None:
            return
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._queue_path.parent / (self._queue_path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for item in self._pending_send:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            tmp.replace(self._queue_path)          # atomic, no os import needed
        except OSError as e:
            log.error("QUEUE_PERSIST_FAILED path=%s queued=%d error=%s",
                      self._queue_path, len(self._pending_send), e)

    def _enqueue(self, text: str, key: str | None) -> None:
        self._pending_send.append({"text": text, "key": key})
        self._persist_queue()

    def _send_final(self, text: str, key: str | None = None) -> None:
        """Forward one reply RELIABLY. Marks the dedup ledger only AFTER a confirmed send; on any
        send failure the reply is queued to disk and the outbound loop keeps retrying until Telegram
        confirms. Order is preserved: if anything is already queued, this appends behind it."""
        if not text or self._owner_chat is None:
            return
        if key and key in self._sent_keys:
            return
        if self._pending_send:                       # something already waiting → keep FIFO order
            self._enqueue(text, key)
            return
        try:
            self.tg.send_message(self._owner_chat, text)
        except Exception as e:                        # network reset, 5xx after retries, etc.
            log.warning("forward failed → queued for re-delivery: %s", e)
            self._enqueue(text, key)
            return
        if key:
            self._mark_sent(key)
        self._turn_text_sent = True
        log.info("FWD (send) %r", text[:30])

    def _flush_pending(self) -> None:
        """Re-send queued replies FIFO until one fails (then stop, preserving order for next pass).
        Called every outbound cycle, so a transient network failure self-heals within ~0.4 s."""
        while self._pending_send and self._owner_chat is not None:
            item = self._pending_send[0]
            try:
                self.tg.send_message(self._owner_chat, item.get("text", ""))
            except Exception as e:
                log.warning("re-delivery still failing (%d queued): %s", len(self._pending_send), e)
                return
            self._pending_send.pop(0)
            self._persist_queue()
            if item.get("key"):
                self._mark_sent(item["key"])
            self._turn_text_sent = True
            log.info("FWD (re-delivered) %r", str(item.get("text", ""))[:30])

    # ---- inbound (Telegram → session) -------------------------------------
    def _inbound_loop(self) -> None:
        offset = 0
        allowed_updates = json.dumps(["message", "edited_message", "message_reaction"])
        while not self._stop.is_set():
            try:
                updates = self.tg._call(
                    "getUpdates",
                    {"offset": offset, "timeout": self.cfg.poll_timeout,
                     "allowed_updates": allowed_updates},
                    timeout=self.cfg.poll_timeout + 15,
                )
            except Exception as e:
                log.error("getUpdates failed: %s", e)
                self._stop.wait(3)
                continue
            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)
                try:
                    self._handle(upd)
                except Exception as e:
                    log.exception("inbound error: %s", e)

    def _handle(self, upd: dict) -> None:
        # Reactions (e.g. ❤️) → quick-feedback line.
        mr = upd.get("message_reaction")
        if mr:
            if mr.get("user", {}).get("id") not in self._allowed:
                return
            emojis = "".join(r.get("emoji", "") for r in mr.get("new_reaction", [])
                             if r.get("type") == "emoji")
            if emojis:
                self._inject(f"{emojis} reacted {emojis} to your message #{mr.get('message_id')} "
                             f"— quick feedback; no need to reply unless relevant.")
            return

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        user_id = msg.get("from", {}).get("id")
        chat_id = msg["chat"]["id"]
        if user_id not in self._allowed:
            self.tg.send_message(chat_id, "⛔ Not authorized.")
            return

        # Bridge-level slash commands (e.g. /start, /help) are answered here instead of being
        # forwarded to the agent — so the first contact is a friendly intro, not the agent
        # puzzling over "/start". Only plain-text commands, never media captions.
        text0 = (msg.get("text") or "").strip()
        if text0.startswith("/") and not (msg.get("voice") or msg.get("audio")
                                           or msg.get("photo") or msg.get("document")):
            if self._handle_command(text0, chat_id, msg.get("message_id")):
                return

        # Light "typing…" from the very first moment — including the voice-transcription /
        # file-download window (seconds), so the indicator never has a gap at the start.
        self._consume_turn_end()                 # drop any stale end-marker from a prior turn
        now = time.monotonic()
        self._turn_active.set()
        self._last_activity = now
        self._turn_started = now
        self._typing_count = 1
        self._max_gap = 0.0
        self._last_typing = now
        self._turn_text_sent = False             # gate TUI bubbles until intro text lands
        self._idle_warned = False
        # Seed the TUI dedup with tool lines ALREADY on screen from previous turns, so the
        # scraper only emits calls that appear DURING this turn — otherwise stale lines still
        # visible in the pane get re-sent as bubbles under the new turn.
        if self.cfg.agent == "codex":
            try:
                self._tui_seen = set(_extract_tui_tools(self._session._capture()))
            except Exception:
                self._tui_seen = set()
        else:
            self._tui_seen = set()
        self.tg.send_chat_action(self._owner_chat, "typing")   # instant, don't wait for the loop
        log.info("TURN START t=%.2f", time.time())

        text = (msg.get("text") or msg.get("caption") or "").strip()
        if msg.get("voice") or msg.get("audio"):
            text = self._transcribe(msg.get("voice") or msg.get("audio"), chat_id) or text
            if not text:
                return
        elif msg.get("photo") or msg.get("document"):
            note = self._download_note(msg, chat_id)
            text = f"{text}\n{note}".strip() if note else text
        if text:
            try:
                self._persist_inflight_task(text, message_id=msg.get("message_id"))
            except Exception as e:
                log.error("task journal failed: %s", e)
                self._turn_active.clear()
                self.tg.send_message(chat_id, "⚠️ Úkol se nepodařilo bezpečně uložit; nebyl spuštěn.")
                return
            self._inject(text)

    def _inject(self, text: str) -> None:
        self._turn_active.set()
        self._last_activity = time.monotonic()   # keep typing lit from the very start
        self._task_started.clear()
        try:
            transcript = getattr(self, "_transcript", None)
            self._turn_transcript_start = transcript.stat().st_size if transcript else 0
        except OSError:
            self._turn_transcript_start = 0
        try:
            self._session.inject(text)
            if self.cfg.agent == "codex" and not self._task_started.wait(2.0):
                log.warning("SUBMIT_RETRY reason=no_task_started action=press_enter")
                self._session.submit()
        except Exception as e:
            log.error("inject failed: %s", e)
            self._turn_active.clear()

    def _handle_command(self, text: str, chat_id: int, message_id: int | None = None) -> bool:
        """Answer a bridge-level slash command. Returns True if handled (don't forward to agent)."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        labels = {"codex": "Codex", "claude-code": "Claude Code"}
        agent = labels.get(self.cfg.agent, self.cfg.agent)
        if cmd in ("start", "help"):
            voice = "on" if self.cfg.elevenlabs_api_key else "off — enable with /setkey"
            self.tg.send_message(chat_id,
                f"👋 You're connected to a live *{agent}* session via Agent2Telegram.\n\n"
                "Just send a message — it goes straight to the agent and you'll see typing, live "
                "progress, what tools it runs, and the reply. You can also send *photos* and "
                "*files*, and react with ❤️ as quick feedback.\n\n"
                f"🎤 Voice transcription: {voice}.\n\n"
                "Commands: /help · /status · /health · /diag · /id · /setkey")
            return True
        if cmd == "id":
            self.tg.send_message(chat_id, f"Your Telegram id: `{chat_id}`")
            return True
        if cmd == "status":
            voice = "✓" if self.cfg.elevenlabs_api_key else "✗"
            self.tg.send_message(chat_id,
                f"✅ Connected — *{agent}* in tmux session `{self.cfg.tmux_session}`.\n"
                f"🎤 Voice (ElevenLabs): {voice}")
            return True
        if cmd == "health":
            session_ok = self._session.alive
            state = "working" if self._turn_active.is_set() else "idle"
            pending = len(self._pending_send)
            icon = "✅" if session_ok and pending == 0 else "⚠️"
            self.tg.send_message(
                chat_id,
                f"{icon} Bridge: running\n"
                f"tmux `{self.cfg.tmux_session}`: {'running' if session_ok else 'missing'}\n"
                f"turn: {state}\n"
                f"outbound queue: {pending}",
            )
            return True
        if cmd == "diag":
            from . import __version__
            session_ok = self._session.alive
            transcript = self._transcript.name if self._transcript else "not detected"
            uptime = int(max(0.0, time.monotonic() - self._bridge_started))
            self.tg.send_message(
                chat_id,
                "Agent2Telegram diagnostics\n"
                f"version: `{__version__}`\n"
                f"pid: `{os.getpid()}`\n"
                f"uptime: `{uptime}s`\n"
                f"agent: `{self.cfg.agent}`\n"
                f"tmux: `{self.cfg.tmux_session}` ({'running' if session_ok else 'missing'})\n"
                f"transcript: `{transcript}`\n"
                f"turn active: `{self._turn_active.is_set()}`\n"
                f"queued replies: `{len(self._pending_send)}`",
            )
            return True
        if cmd == "setkey":
            return self._set_voice_key(arg, chat_id, message_id)
        return False    # unknown command → let the agent handle it

    def _set_voice_key(self, key: str, chat_id: int, message_id: int | None) -> bool:
        """Save an ElevenLabs key to enable voice, then delete the message so the secret isn't
        left in the chat history."""
        if not key:
            self.tg.send_message(chat_id,
                "Usage: `/setkey <your ElevenLabs API key>` — enables voice-message transcription.\n"
                "I'll delete your message right after so the key isn't left in the chat.")
            return True
        self.cfg.elevenlabs_api_key = key
        try:
            from .config import save
            save(self.cfg)                       # persisted 0600 to the active config path
        except Exception as e:
            log.error("setkey: could not persist config: %s", e)
        if message_id is not None:
            self.tg.delete_message(chat_id, message_id)   # don't leave the secret in history
        self.tg.send_message(chat_id,
            "✅ Voice transcription enabled — key saved. I deleted your message so the key "
            "isn't left in the chat history. Send a voice note to try it.")
        return True

    def _typing_loop(self) -> None:
        """Dedicated thread: assert "typing…" every TYPING_INTERVAL while a turn is active.

        It runs independently of the outbound/send loop, so a flood-control sleep in the send path
        (which happens during a burst of messages) can never starve the indicator — that was the
        cause of mid-turn typing gaps. It stops the instant the turn ends (turn_active cleared), so
        no action fires after the final message and typing stops with it (bar Telegram's ~5s decay)."""
        while not self._stop.is_set():
            if self._turn_active.is_set() and self._owner_chat is not None:
                now = time.monotonic()
                gap = now - self._last_typing
                if gap > self._max_gap:
                    self._max_gap = gap
                self.tg.send_chat_action(self._owner_chat, "typing")
                self._last_typing = now
                self._typing_count += 1
            self._stop.wait(TYPING_INTERVAL)

    def _tui_scrape_loop(self) -> None:
        """Codex only: scrape the tmux pane for live tool/web-search lines → status bubbles, so
        Codex (whose rollout logs tools only at completion) shows them live like Claude Code."""
        while not self._stop.is_set():
            if self._turn_active.is_set() and self._turn_from_tg and self._owner_chat is not None:
                # Hold bubbles until the intro text is forwarded (so the bubble doesn't jump ahead
                # of "I'll search the web…"), then release; after a short grace show them anyway.
                ready = self._turn_text_sent or \
                    (time.monotonic() - self._turn_started) >= TUI_BUBBLE_GRACE
                if ready:
                    try:
                        for summary in _extract_tui_tools(self._session._capture()):
                            if summary not in self._tui_seen:
                                self._tui_seen.add(summary)
                                self._status_push(summary)
                    except Exception as e:
                        log.debug("tui scrape: %s", e)
            self._stop.wait(1.0)

    def _consume_turn_end(self) -> None:
        if self._turn_end is not None:
            try:
                self._turn_end.unlink()
            except OSError:
                pass

    def _last_assistant_text(self) -> str | None:
        """Most recent assistant text written during the current injected Telegram turn.

        The byte boundary is captured immediately before injection. This is intentionally scoped:
        a valid Codex turn may finish with no assistant message, and falling back to text from an
        earlier turn would duplicate a stale answer into Telegram.
        """
        if not self._transcript:
            return None
        try:
            size = self._transcript.stat().st_size
            start = min(size, max(0, self._turn_transcript_start))
            with open(self._transcript, "rb") as f:
                f.seek(max(start, size - 2_000_000))
                tail = f.read()
        except OSError:
            return None
        last = None
        for raw in tail.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8", "ignore"))
            except (ValueError, json.JSONDecodeError):
                continue
            try:
                for ev in self._reader.parse(rec):
                    if ev.kind == "text" and ev.text and ev.text.strip():
                        last = ev.text
            except Exception:
                continue
        return last

    def _finish_turn(self) -> None:
        """Drop the technical bubble and stop the typing indicator at the real end of a turn."""
        self._status_clear()
        was_active = self._turn_active.is_set()
        # Backstop: a Telegram-originated turn must NEVER go unanswered. If nothing was forwarded
        # this turn (the [tg] marker was forgotten, the turn was a long heads-down working stretch,
        # or interim forwarding missed it), deliver the final assistant message now. The
        # `_turn_text_sent` guard means this only fires when truly nothing was sent (no double-send),
        # and _send_final sets it True so a second _finish_turn won't re-fire.
        if was_active and self._turn_from_tg and not self._turn_text_sent and self._owner_chat is not None:
            last = self._last_assistant_text()
            out = self._strip_marker(last) if last else ""
            if out:
                self._send_final(out)
                log.info("TURN END backstop → forwarded final answer %r", out[:30])
            else:
                try:
                    pane = self._session._capture()
                except Exception as e:
                    log.warning("EMPTY_TURN_CAPTURE_FAILED error=%s", e)
                    pane = ""
                reason, notice = _empty_turn_notice(pane)
                self._send_final(notice)
                log.warning("TURN END no_agent_answer reason=%s action=notify_owner", reason)
        self._turn_active.clear()
        self._pending_turn_end = False
        self._consume_turn_end()
        if was_active:
            self._clear_inflight_task()
        if was_active:
            log.info("TURN END t=%.2f dur=%.1fs typing_fired=%d max_gap=%.2fs",
                     time.time(), time.monotonic() - self._turn_started,
                     self._typing_count, self._max_gap)

    def _end_turn(self) -> None:
        # Claude Stop-hook path: catch anything written just before the hook fired, then finish.
        self._drain_transcript()
        self._finish_turn()

    # ---- outbound (session → Telegram) ------------------------------------
    def _outbound_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._maybe_reresolve()
                self._flush_pending()         # re-deliver any reply a prior send failed to push
                self._drain_transcript()      # may set _pending_turn_end (Codex task_complete)
                self._drain_signal()
                # End-of-turn detection, in priority order:
                #   * Codex: the reader saw task_complete → end now (no hook needed).
                #   * Claude Code: the Stop hook wrote the end-of-turn marker. Authoritative even
                #     if turn_active is unset (e.g. a restart mid-turn that would orphan a bubble).
                #   * Fallback: force-end if the transcript went quiet too long (hook missing).
                if self._pending_turn_end:
                    self._finish_turn()
                elif self._turn_end is not None and self._turn_end.exists():
                    self._end_turn()
                elif self._turn_active.is_set() and time.monotonic() - self._last_activity > IDLE_DONE:
                    if self._reader.emits_turn_end:
                        if not self._idle_warned:
                            log.warning(
                                "TURN_STALLED reason=transcript_idle idle=%.1fs action=wait_for_authoritative_end",
                                time.monotonic() - self._last_activity,
                            )
                            self._idle_warned = True
                    else:
                        log.warning("TURN_END_FALLBACK reason=idle_timeout idle=%.1fs", IDLE_DONE)
                        self._finish_turn()
                self._beat()                  # reached only on a full, non-blocking forward cycle
            except Exception as e:
                log.error("outbound error: %s", e)
            self._stop.wait(0.4)

    def _beat(self) -> None:
        """Touch the outbound heartbeat — proof the forward loop completed a cycle without blocking.
        A wedged send or a persistent exception never reaches here, so the file goes stale and a
        watchdog can restart the bridge."""
        if self._heartbeat is None:
            return
        try:
            self._heartbeat.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass

    # ---- live tool-call status bubble (shown during the turn, deleted at the end) ------
    def _status_push(self, line: str) -> None:
        # Single line, emoji at the start, rendered in italics. One bubble is edited in place
        # across a run of consecutive tool calls; it's deleted when the next progress message
        # arrives (then re-created below it) and at turn end — so it always trails at the bottom.
        if self._owner_chat is None or not line or line == self._status["shown"]:
            return
        body = f"<i>{html.escape(line)}</i>"
        if self._status["mid"] is None:
            mid = self.tg.send_plain_id(self._owner_chat, body, parse_mode="HTML")
            if mid:
                self._status["mid"] = mid
                self._status["shown"] = line
                self._persist_status(mid)
        else:
            self.tg.edit_plain(self._owner_chat, self._status["mid"], body, parse_mode="HTML")
            self._status["shown"] = line

    def _status_clear(self) -> None:
        if self._status["mid"] is not None and self._owner_chat is not None:
            self.tg.delete_message(self._owner_chat, self._status["mid"])
        self._status = {"mid": None, "shown": ""}
        self._seen_tools.clear()
        self._persist_status(None)

    def _persist_status(self, mid: int | None) -> None:
        if self._status_path is None:
            return
        try:
            if mid is None:
                self._status_path.unlink()
            else:
                self._status_path.parent.mkdir(parents=True, exist_ok=True)
                self._status_path.write_text(str(mid), "utf-8")
        except OSError:
            pass

    def _cleanup_orphan_status(self) -> None:
        """Delete a status bubble left over from a previous run that died mid-turn."""
        if self._status_path is None or self._owner_chat is None:
            return
        try:
            mid = int(self._status_path.read_text("utf-8").strip())
        except (OSError, ValueError):
            return
        self.tg.delete_message(self._owner_chat, mid)
        try:
            self._status_path.unlink()
        except OSError:
            pass

    def _drain_signal(self) -> None:
        if not self._signal or not self._signal.exists():
            return
        try:
            answer = self._signal.read_text("utf-8").strip()
            self._signal.unlink()
        except OSError:
            return
        if answer and self._owner_chat is not None:
            self._status_clear()                         # final message → drop the technical bubble
            self._send_final(answer)                     # reliable: queue + retry on send failure
            self._turn_active.clear()
            self._clear_inflight_task()

    def _drain_transcript(self) -> None:
        if not self._transcript or not self._transcript.exists():
            return
        size = self._transcript.stat().st_size
        if size < self._tpos:          # file rotated/truncated
            self._tpos = 0
        if size == self._tpos:
            return
        with open(self._transcript, "rb") as f:
            f.seek(self._tpos)
            chunk = f.read()
        # Only consume up to the last complete line; keep a partial trailing line for next time.
        nl = chunk.rfind(b"\n")
        if nl == -1:
            return
        self._tpos += nl + 1
        for raw in chunk[:nl].split(b"\n"):
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for ev in self._reader.parse(rec):
                self._handle_event(ev)
        # Any new transcript content during a Telegram turn = the agent is still working;
        # refresh activity so the idle fallback doesn't fire prematurely.
        if self._turn_from_tg:
            self._last_activity = time.monotonic()

    def _strip_marker(self, text: str) -> str:
        """Remove the progress marker (e.g. ``[TG]``) from the start of *any* line. It's a routing
        token, never content — so a stray one mid-message (narration before the marked reply) must
        not leak into the chat. Case-insensitive so ``[tg]`` and ``[TG]`` are both caught."""
        marker = self._marker.lower()
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            s = ln.lstrip()
            if s.lower().startswith(marker):
                lines[i] = s[len(self._marker):].lstrip()
        return "\n".join(lines).strip()

    def _handle_event(self, ev) -> None:
        """Apply one normalized reader event to the Telegram side."""
        if ev.kind == "user":
            # Remember whether this turn came from Telegram (origin prefix) — only those are
            # forwarded; terminal-originated turns stay local.
            self._turn_from_tg = ev.text.lstrip().startswith(self._origins)
            return
        if ev.kind == "turn_start":
            self._task_started.set()
            return                              # inbound already lit typing; nothing else to do
        if ev.kind == "turn_end":
            self._pending_turn_end = True       # outbound loop finishes the turn after this drain
            return
        if not self._turn_from_tg or self._owner_chat is None:
            return
        if ev.kind == "text":
            out = self._strip_marker(ev.text)
            if out and ev.key not in self._sent_keys:
                # A new progress message → delete the current technical bubble so the next tool
                # calls re-create it BELOW this message (the bubble always trails at the bottom).
                self._status_clear()
                # Reliable forward: the dedup ledger is marked only AFTER a confirmed send, and a
                # failed send is queued + retried — so a reply is never silently dropped.
                self._send_final(out, key=ev.key)
        elif ev.kind == "tool":
            if self.cfg.agent == "codex":
                return                            # Codex tools come live from the TUI scraper
            if ev.key and ev.key not in self._seen_tools:
                self._seen_tools.add(ev.key)
                self._status_push(ev.text)

    # ---- media helpers (reuse the same download/STT as one-shot mode) ------
    def _transcribe(self, media: dict, chat_id: int) -> str | None:
        from . import stt
        if not self.cfg.elevenlabs_api_key:
            self.tg.send_message(chat_id,
                "🎤 Voice transcription isn't enabled yet. Add your ElevenLabs key with "
                "`/setkey <your-key>` (I'll delete the message right after) — then resend the voice note.")
            return None
        try:
            fp = self.tg.get_file_path(media["file_id"])
            audio = self.tg.download(fp)
            return stt.transcribe(audio, api_key=self.cfg.elevenlabs_api_key,
                                  filename=Path(fp).name or "voice.ogg")
        except Exception as e:
            log.error("transcription failed: %s", e)
            self.tg.send_message(chat_id, f"⚠️ Couldn't transcribe: {e}")
            return None

    def _download_note(self, msg: dict, chat_id: int) -> str:
        import re
        if msg.get("photo"):
            file_id, default = msg["photo"][-1]["file_id"], "image.jpg"
            size = msg["photo"][-1].get("file_size")
        else:
            doc = msg["document"]
            file_id, default = doc["file_id"], doc.get("file_name") or "file"
            size = doc.get("file_size")
        # Telegram bots can only fetch files up to 20 MB via getFile — fail fast with a clear,
        # actionable message instead of a silent "couldn't download".
        if size and size > 20 * 1024 * 1024:
            self.tg.send_message(chat_id,
                f"⚠️ That file is ~{size // 1024 // 1024} MB. Telegram bots can only receive files up "
                "to 20 MB — please share a link (Drive/Dropbox) or a smaller/zipped version.")
            log.warning("attachment '%s' too big for the Bot API: %s bytes", default, size)
            return ""
        try:
            fp = self.tg.get_file_path(file_id)
            data = self.tg.download(fp)
        except Exception as e:
            log.warning("attachment '%s' download failed: %s", default, e)
            self.tg.send_message(chat_id,
                "⚠️ Couldn't download the attachment (too large, or a transient error — try again).")
            return ""
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(default).name) or "file"
        if "." not in name and (ext := Path(fp).suffix):
            name += ext
        d = Path.home() / ".local/state/agent2telegram/attachments"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / name
        dest.write_bytes(data)
        log.info("saved attachment -> %s (%d bytes)", dest, len(data))
        return f"[The user attached a file saved at: {dest} — open and use it as appropriate.]"
