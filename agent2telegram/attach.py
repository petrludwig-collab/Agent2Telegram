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

from collections import deque
import glob
import html
import json
import os
import logging
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import adapters
from . import readers
from .background import SubagentWatcher, _epoch as _record_wall_time
from . import tts
from .compat import AlreadyRunning, single_instance_lock
import re

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
from .config import Config, _state_dir
from .durable import DurableInbox, DurableOutbox
from .inbound import sender_label
from .notices import busy_text, restart_notice
from .redaction import message_metadata, update_metadata
from .session import SessionError, TmuxSession
from .telegram import TelegramClient, TelegramError, is_network_error, split_message

log = logging.getLogger("agent2telegram.attach")

#: Fallback only: how long the transcript may be quiet before we force-end a turn, in case
#: the Stop-hook turn-end marker never arrives. The marker is the primary, precise signal —
#: this just stops "typing…" from hanging forever if the hook is missing/misconfigured.
IDLE_DONE = 90.0
# With a Stop hook wired (Claude Code), the hook is the authoritative end of turn and silence is
# only a safety net. 90 s of transcript silence is NORMAL for a long-thinking model: on
# 2026-09-02 22:23 the bridge closed a live turn after 90 s of thinking, the answer landed two
# minutes later with no turn open and was dropped. Ten minutes leaves room to think.
IDLE_DONE_HOOKED = 600.0
# A write to tmux can fail transiently (busy pane, full buffer), so it is retried.
# A short pause on purpose: a user's message must not wait longer than the user's patience.
INJECT_ATTEMPTS = 3
INJECT_RETRY_WAIT = 1.5
# How many times a stored inbound message is retried before it lands in dead-letter.
# Better to shelve it there with a reason than delete it — at least it stays traceable.
INBOUND_MAX_ATTEMPTS = 5
# How many times an outgoing attachment is retried before giving up. Without a cap one bad
# attachment would hold the head of the FIFO queue forever and block every later reply.
OUTBOX_MAX_ATTEMPTS = 3
# How often a message left in durable storage is retried WHILE RUNNING. Without this it waited
# for a restart — for a service running for weeks, effectively forever (review finding #1).
INBOUND_RETRY_INTERVAL = 30.0
#: A turn finishing faster than this is almost certainly a stale end-of-turn signal, not real work.
SUSPICIOUS_TURN_SECONDS = 3.0
# How often the outbound loop checks whether there is anything to send. Was 0.4 s; shortened to
# 0.15 s, because over long work with dozens of progress messages it added up to a noticeable
# delay. The cost is reading the queue more often — so it was measured, not guessed.
OUTBOUND_TICK = 0.15
# How long after a receipt ACK no further ACK is sent. Five messages in a row should trigger
# one "got it", not five.
QUEUE_ACK_COOLDOWN = 30.0
# How many characters of a quoted message are passed to the agent. Enough to recognise, few
# enough not to flood the prompt.
REPLY_QUOTE_CHARS = 300
# How long a message from another chat waits for the running turn before switching recipient.
CHAT_SWITCH_MAX_WAIT = 1800.0
CHAT_SWITCH_POLL = 0.5

#: Deduplicate repeated deliveries of the same reaction on the same message.
#: Keep the window short so a user can remove and intentionally add a reaction again.
REACTION_DEDUP_S = 60.0
#: How often we re-assert the "typing…" chat action (Telegram shows it for ~5s). Kept well
#: under that window so a turn never shows a gap, even right after a sent message clears it.
TYPING_INTERVAL = 1.5

# Turn-end backstop: transcript writes can lag the turn-end signal by a fraction of a second
# (especially after a reset/reconnect), so give the final assistant text a short chance to land.
BACKSTOP_RETRY_ATTEMPTS = 5
BACKSTOP_RETRY_DELAY = 0.35

#: Background progress (opt-in, Claude Code): how often the sub-agent watcher polls, and how old a
#: transcript record may be to still count as live work. Records older than this — or written
#: before the bridge started — are history being replayed, never shown as a status bubble.
BACKGROUND_TICK = 2.0
BACKGROUND_LIVE_WINDOW = 120.0

#: Codex only: hold the first scraped tool bubble until the turn's intro text has been forwarded
#: (agents usually say what they'll do, THEN call the tool). The scraper is live but the Codex
#: transcript text lags, so without this the bubble jumps ahead of the intro line. After this
#: grace we show bubbles anyway, since some turns call a tool with no intro text.
TUI_BUBBLE_GRACE = 3.0

# Short disk ledger for Telegram update_ids already accepted by attach mode. Offset persistence is
# the primary protection; this catches stale replays if a crash lands in the small ACK window.
PROCESSED_UPDATE_LEDGER_LIMIT = 500
# Keep slow media downloads/STT from becoming a resource sink. Telegram voice notes are normally
# tiny; 25 MB still leaves room for long clips while bounding download + STT work.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
# Final prompt guard after text, captions, downloaded-file notes, or STT results are combined.
MAX_INBOUND_PROMPT_CHARS = 12_000

#: Registered with Telegram (setMyCommands) so typing "/" shows the command autocomplete menu.
BOT_COMMANDS = [
    {"command": "start", "description": "Intro and what you can send"},
    {"command": "help", "description": "Intro and what you can send"},
    {"command": "status", "description": "Connection and voice status"},
    {"command": "setkey", "description": "Enable voice (your ElevenLabs API key)"},
    {"command": "voice", "description": "Toggle spoken (voice-note) replies"},
    {"command": "id", "description": "Show your Telegram id"},
]

#: Injected ahead of the user's message while voice mode is ON, so the AGENT writes a reply meant
#: to be HEARD rather than read. This marker — not a regex — is the heart of voice mode: the model
#: phrases speakable text (short, numbers as words, no paths) far better than any post-processing.
#: Same idea as the "[voice transcript …]" marker on the inbound side.
VOICE_MODE_HINT = (
    "[voice mode ON — your reply will be SPOKEN, not read. Default: keep it short (a few "
    "sentences, ~20 seconds), say the one thing that matters and offer details only if asked. "
    "Exception: when the user explicitly asks for a long narration (a full summary, a whole list, "
    "a briefing to listen to while driving), speak it in full — up to about five minutes. "
    "Conversational tone, numbers as words, no markdown, tables, code, file paths or URLs.]"
)
#: Above this length a reply is sent as TEXT even in voice mode — reading a two-page analysis
#: aloud is worse than scannable text (and a >~45 s voice note is unwieldy). The agent is asked
#: to keep spoken replies short; this is the backstop when it doesn't.
# Ceiling for reading aloud: roughly five minutes of Czech speech.
# Still a BACKSTOP: the hint asks the agent to stay short unless a long narration was requested.
VOICE_MAX_CHARS = 6000



def _ogg_duration(path: str) -> int | None:
    """Length of an audio file in whole seconds via ffprobe; None when unavailable. Passed to
    sendVoice so Telegram shows the true length of long notes."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return int(round(float(r.stdout.strip()))) if r.returncode == 0 and r.stdout.strip() else None
    except Exception:  # noqa: BLE001 – best effort only
        return None

import re as _re  # noqa: E402
from .readers import _short  # noqa: E402

#: Codex renders tool activity live in its TUI but only writes it to the rollout at completion.
#: For Codex (attach), we scrape the tmux pane for these lines so tool bubbles appear LIVE —
#: matching Claude Code (which logs tool_use to its transcript immediately). Claude needs no scrape.
_TUI_VERBS = {"Read": "📄", "List": "📂", "Search": "🔎", "Ran": "🛠️",
              "Edit": "✏️", "Wrote": "✏️", "Added": "✏️", "Updated": "✏️",
              "Deleted": "🗑️", "Removed": "🗑️"}


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


def _expected_agent_commands(cfg: Config) -> list[str]:
    """Commands that are allowed to own the tmux pane before we send keys into it."""
    commands: list[str] = []
    cls = adapters.REGISTRY.get(cfg.agent)
    if cls is not None:
        commands.extend(cls.tui_launch()[:1])
        if cls.binary:
            commands.append(cls.binary)
    if cfg.command:
        commands.append(cfg.command[0])
    if cfg.continue_command:
        commands.append(cfg.continue_command[0])

    seen = set()
    out = []
    for command in commands:
        name = Path(str(command)).name
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    if not out:
        raise ValueError(
            "attach mode cannot verify the tmux pane: configure an agent command to allow"
        )
    return out



def _in_test_run() -> bool:
    """True when this process is a test run, whatever runner started it.

    Checking only ``PYTEST_CURRENT_TEST`` was not enough: the project's own CI runs
    ``python -m unittest discover``, where that variable does not exist — so the guard this
    backs (see ``_ensure_outbox``) silently did not apply, in exactly the environment where an
    unfamiliar contributor runs the suite for the first time. The production bridge imports
    neither runner, so their presence in ``sys.modules`` is a reliable signal.
    """
    return ("PYTEST_CURRENT_TEST" in os.environ
            or "pytest" in sys.modules
            or "unittest" in sys.modules)


class AttachBridge:
    _turn_end_backstop_enabled = True
    _group_reply = None          # last group-bound text, held until the turn ends
    _group_files = ()            # native file-tool attachments held for the same turn
    _group_end_confirmed = False
    # Background progress state. Class-level defaults so objects built without __init__
    # (focused tests) behave as "feature off, nothing shown".
    _bg_status = False          # the status bubble belongs to a turn NOT started from Telegram
    _bg_status_last = 0.0       # monotonic ts of that turn's last tool call
    _bg_arm = None              # (transcript, size at start/switch): only records after it are live
    _rec_live = False           # the transcript record being handled is live background work
    _bg_watcher = None          # SubagentWatcher (sub-agent progress message)
    _bg_state_path = None
    #: Durable outbox with per-part confirmation. Off by default for objects built without
    #: __init__ (focused tests), which have no queue path to write to.
    _use_durable_outbox = True

    def __init__(self, cfg: Config, *, client: TelegramClient | None = None) -> None:
        if not cfg.tmux_session:
            raise ValueError("attach mode requires 'tmux_session' in config")
        self.cfg = cfg
        self.tg = client or TelegramClient(cfg.token)
        self._reaction_seen: dict = {}   # (chat, msg, emoji) -> first reaction timestamp
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
        # Registration alone does not prove that the running Claude session loaded its hook.
        self._claude_hook_ready = cfg.agent != 'claude-code'
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
        expected_agent_commands = _expected_agent_commands(cfg)
        self._session = TmuxSession([], name=cfg.tmux_session, cwd=Path.home(),
                                    origin_prefix=cfg.origin_prefix, boot_wait=0,
                                    expected_agent_commands=expected_agent_commands)
        self._stop = threading.Event()
        self._init_inbound_worker_state()
        self._init_update_state()
        # Voice-reply mode is persisted in the state dir (survives restart), not just memory.
        self._voice_state_path = _state_dir(self.cfg) / "voice_mode"
        self._voice_on = self._load_voice_state()
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
        self._tpos = 0
        self._turn_active = threading.Event()
        self._turn_from_tg = False           # is the current transcript turn Telegram-originated?
        self._last_activity = 0.0            # monotonic ts of last transcript activity (for typing)
        self._status = {"mid": None, "shown": ""}   # live one-line tool-call status bubble
        self._last_typing = 0.0                      # monotonic ts of last "typing…" chat action
        self._typing_count = 0                       # diagnostics: typing actions in current turn
        self._turn_started = 0.0                     # diagnostics: monotonic ts of turn start
        self._max_gap = 0.0                          # diagnostics: largest gap between typing actions
        self._last_pane_warning = 0.0                # throttle unsafe-pane owner alerts
        # Persist the bubble's message_id so a restart/crash mid-turn can delete the orphan it
        # would otherwise leave behind in the chat.
        self._status_path = (self._signal.parent / "status_bubble") if self._signal else None
        # Same for the background progress message (per bridge, like the heartbeat).
        self._bg_state_path = (self._signal.parent / f"background_progress_{_slug}") if self._signal else None
        self._seen_tools: set = set()
        self._tui_seen: set = set()          # Codex TUI scrape: tool lines already shown this turn
        self._turn_text_sent = False         # has any text been forwarded this turn (bubble gate)
        self._turn_is_reaction = False       # turn opened by a reaction → exempt from the backstop

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
            # Claude Code writes subagent transcripts under "<conversation>/subagents/". A running
            # subagent writes far more often than the main conversation, so on mtime it always
            # wins the "newest" race — the bridge then tails the subagent and the summary the
            # agent writes to the user is never forwarded. Reported by a user running an
            # architect/tester/reviewer setup, where 17 of 21 transcripts were subagents.
            files = [f for f in files if f"{os.sep}subagents{os.sep}" not in f]
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

    def _session_uuid(self) -> str | None:
        """The Codex session id the driven tmux session was launched with (``codex resume <id>``
        on its command line), or None for a fresh session that has no id yet.

        The cwd is not enough to tell sessions apart: every agent on a box tends to run from the
        same home directory, so when a *sibling* agent restarted, its rollout became the newest
        one "for our cwd" and the bridge tailed the wrong session, then jumped back and
        replayed old messages as the answer to a new request."""
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-t", self.cfg.tmux_session, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5)
            pids = {int(x) for x in out.stdout.split() if x.isdigit()}
            if not pids:
                return None
            ps = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                                capture_output=True, text=True, timeout=5)
        except (subprocess.SubprocessError, OSError, ValueError):
            return None
        rows = []
        for ln in ps.stdout.splitlines():
            parts = ln.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
        # Walk the pane's process tree (shell → codex) looking for a resume id on argv.
        family = set(pids)
        grew = True
        while grew:
            grew = False
            for pid, ppid, _ in rows:
                if ppid in family and pid not in family:
                    family.add(pid); grew = True
        for pid, _, cmd in rows:
            if pid in family and "codex" in cmd:
                m = readers.UUID_RE.search(cmd) if hasattr(readers, "UUID_RE") else _UUID_RE.search(cmd)
                if m:
                    return m.group(0).lower()
        return None

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
        sid = self._session_uuid()
        if sid:
            own = [f for f in files if sid in os.path.basename(f).lower()]
            if own:
                return Path(own[0])               # the session's own rollout, whatever its mtime
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
        self._seek_to_turn()
        self._arm_background()

    def _seek_to_turn(self) -> None:
        """Position the cursor in a freshly adopted transcript so that only the CURRENT turn is
        read from it. Everything already in the file is history — a resumed session replays its
        whole past, and starting at byte 0 forwarded 22 old messages as one answer (2026-09-04).

        Outside a turn: start at the end. Inside a turn: start at the first record stamped at or
        after the turn began (records carry a wall-clock ``timestamp``); if nothing in the file is
        that new yet, start at the end and let the drain pick the answer up as it is written."""
        try:
            size = self._transcript.stat().st_size
        except OSError:
            size = 0
        pos = size
        if self._turn_active.is_set():
            t0 = float(getattr(self, "_turn_started_wall", 0.0) or 0.0) - 2.0
            start = max(0, size - 5_000_000)
            try:
                with open(self._transcript, "rb") as f:
                    f.seek(start)
                    tail = f.read()
            except OSError:
                tail = b""
            cur = start
            for raw in tail.split(b"\n"):
                line_end = cur + len(raw) + 1
                ts = self._record_epoch(raw)
                if ts is not None and ts >= t0:
                    pos = cur
                    break
                cur = line_end
        self._tpos = pos
        self._turn_tpos = pos

    @staticmethod
    def _record_epoch(raw: bytes) -> float | None:
        """Wall-clock time of a transcript record (Codex and Claude Code both write an ISO
        ``timestamp``), or None when the line has none."""
        try:
            rec = json.loads(raw.decode("utf-8", "ignore"))
        except (ValueError, json.JSONDecodeError):
            return None
        ts = rec.get("timestamp") if isinstance(rec, dict) else None
        if not isinstance(ts, str) or not ts:
            return None
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    # ---- lifecycle ---------------------------------------------------------
    def run(self) -> None:
        # Lock on the state dir. Two instances over one bot fight over getUpdates (409), both
        # can inject the same update and both drain the same queue — messages then vanish and
        # duplicate. The lock was written and tested in compat.py but never called (cross-review
        # findings #3 / F3), so that protection was only on paper.
        with self._instance_lock():
            self._run_locked()

    @contextmanager
    def _instance_lock(self):
        offset_path = getattr(self, "_offset_file", None)
        if offset_path is None:
            yield
            return
        try:
            with single_instance_lock(Path(offset_path).parent / "bridge.lock"):
                yield
        except AlreadyRunning:
            raise RuntimeError(
                f"Another bridge instance is already running over the state at {Path(offset_path).parent}. "
                "Two at once fight over messages — stop the other one, or give each its "
                "own AGENT2TELEGRAM_STATE."
            ) from None

    def _run_locked(self) -> None:
        me = self.tg.get_me()
        log.info("Attach bridge live as @%s → tmux '%s', owner=%s",
                 me.get("username"), self.cfg.tmux_session, self._owner_chat)
        self.tg.set_my_commands(BOT_COMMANDS)    # enable the "/" command menu in Telegram
        if not self._session.alive:
            raise RuntimeError(f"tmux session '{self.cfg.tmux_session}' not found")
        # Start tailing at EOF. If we've run before (the ledger has entries), rewind to the start
        # of the current turn so a reply written while we were restarting still gets forwarded —
        # the ledger dedups, so nothing already delivered is re-sent. On the very first run we do
        # NOT rewind, so attaching to an already-busy session never re-posts its prior turn.
        if self._transcript and self._transcript.exists():
            self._tpos = self._transcript.stat().st_size
            if self._sent_keys:
                self._resume_position()
        self._arm_background()              # everything already in the transcript is history
        self._cleanup_orphan_status()       # remove a bubble orphaned by a prior crash/restart
        self._start_background()
        if self.cfg.restart_notice and self._session._pane_ok()[0]:
            restart_notice(self, self._owner_chat)
        # Typing runs in its own thread so a flood-control sleep in the send path never starves it.
        threading.Thread(target=self._outbound_loop, daemon=True).start()
        threading.Thread(target=self._typing_loop, daemon=True).start()
        if self.cfg.agent == "codex":
            # Codex logs tools to the rollout only at completion → scrape the TUI for LIVE bubbles.
            threading.Thread(target=self._tui_scrape_loop, daemon=True).start()
        self._inbound_loop()

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

    def _mark_sent(self, uuid: str) -> bool:
        """Record a delivered message uuid in memory and on disk. Returns whether the disk write
        succeeded.

        The return value matters: an `OSError` used to be swallowed silently, so the key stayed
        only in memory. The caller then removed the record from the queue and after a restart the
        SAME reply was sent again — a duplicate at exactly the moment the disk is full. A review
        called the earlier write-order fix "only skin-deep"; it was right.
        """
        if not uuid:
            return True
        if uuid in self._sent_keys:
            return True
        self._sent_keys.add(uuid)
        try:
            self._sent_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._sent_path, "a", encoding="utf-8") as f:
                f.write(uuid + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError as e:
            log.error("could not write the delivered-message ledger (%s) — keeping the record queued "
                      "so the reply is not sent twice", e)
            return False

    # ---- inbound update persistence -----------------------------------------
    def _init_update_state(self) -> None:
        self._offset_file = _state_dir(self.cfg) / "offset"
        self._processed_updates_file = _state_dir(self.cfg) / "processed_updates"
        self._processed_update_ids, self._processed_update_order = self._read_processed_updates()

    def _ensure_update_state(self) -> None:
        # Focused tests may build the object without calling our __init__.
        if not hasattr(self, "_offset_file"):
            self._offset_file = _state_dir(self.cfg) / "offset"
        if not hasattr(self, "_processed_updates_file"):
            self._processed_updates_file = _state_dir(self.cfg) / "processed_updates"
        if not hasattr(self, "_processed_update_ids") or not hasattr(self, "_processed_update_order"):
            self._processed_update_ids, self._processed_update_order = self._read_processed_updates()
        self._ensure_inbox()

    def _ensure_inbox(self) -> DurableInbox | None:
        """Durable inbox over the SAME directory the offset lives in — so state stays together
        in tests and in production regardless of who assembled the object."""
        inbox = getattr(self, "_inbox", None)
        if inbox is None:
            try:
                inbox = DurableInbox(self._offset_file.parent)
            except Exception as e:                     # must not break message intake
                log.error("could not open the durable inbox: %s", e)
                inbox = None
            self._inbox = inbox
        return inbox

    def _load_offset(self) -> int:
        self._ensure_update_state()
        try:
            return int(json.loads(self._offset_file.read_text("utf-8"))["offset"])
        except Exception:
            return 0

    def _save_offset(self, offset: int) -> None:
        self._ensure_update_state()
        try:
            self._offset_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._offset_file.parent / (self._offset_file.name + ".tmp")
            tmp.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
            tmp.replace(self._offset_file)
        except OSError as e:
            log.warning("could not persist attach offset: %s", e)

    def _read_processed_updates(self) -> tuple[set[int], deque[int]]:
        ids: set[int] = set()
        order: deque[int] = deque()
        try:
            raw = json.loads(self._processed_updates_file.read_text("utf-8")).get("update_ids", [])
        except Exception:
            raw = []
        for value in raw[-PROCESSED_UPDATE_LEDGER_LIMIT:]:
            try:
                update_id = int(value)
            except (TypeError, ValueError):
                continue
            if update_id not in ids:
                ids.add(update_id)
                order.append(update_id)
        return ids, order

    def _persist_processed_updates(self) -> None:
        self._ensure_update_state()
        try:
            self._processed_updates_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._processed_updates_file.parent / (self._processed_updates_file.name + ".tmp")
            data = {"update_ids": list(self._processed_update_order)}
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._processed_updates_file)
        except OSError as e:
            log.warning("could not persist processed updates: %s", e)

    def _mark_update_processed(self, update_id: int | None) -> None:
        if update_id is None:
            return
        self._ensure_update_state()
        try:
            update_id = int(update_id)
        except (TypeError, ValueError):
            return
        if update_id in self._processed_update_ids:
            return
        self._processed_update_ids.add(update_id)
        self._processed_update_order.append(update_id)
        while len(self._processed_update_order) > PROCESSED_UPDATE_LEDGER_LIMIT:
            old = self._processed_update_order.popleft()
            self._processed_update_ids.discard(old)
        self._persist_processed_updates()

    def _update_was_processed(self, update_id: int | None) -> bool:
        if update_id is None:
            return False
        self._ensure_update_state()
        try:
            return int(update_id) in self._processed_update_ids
        except (TypeError, ValueError):
            return False

    def _handle_update_once(self, upd: dict, offset: int) -> int:
        raw_id = upd.get("update_id")
        try:
            update_id = int(raw_id)
        except (TypeError, ValueError):
            log.warning("skipping malformed Telegram update without a valid update_id: %s",
                        update_metadata(upd))
            return offset
        next_offset = max(offset, update_id + 1) if update_id is not None else offset
        if update_id is not None and update_id < offset:
            return offset
        if self._update_was_processed(update_id):
            self._save_offset(next_offset)
            return next_offset

        # ORDER IS CRITICAL. Telegram treats an update as handled the moment we ask for a higher
        # offset — it never resends it. The offset used to advance before the message reached
        # anywhere durable (it only sat in the in-memory queue), so a crash in that window erased
        # it. A monitor was killing the bridge several times a day, so this was not theoretical.
        #
        # Now: write to disk first, ACK only after. If the write fails the offset does NOT
        # advance and Telegram resends the message — better twice than never.
        inbox = self._ensure_inbox()
        if inbox is None:
            # Without durable storage the offset must NOT advance. This case used to fall through
            # to the old lossy path — exactly when the disk is full or corrupt and durability is
            # needed most (review finding #2).
            log.error("update %s: durable storage unavailable, not advancing the offset", update_id)
            return offset
        try:
            record_id = inbox.reserve(upd)
        except Exception as e:
            log.error("could not store update %s, not advancing the offset: %s", update_id, e)
            return offset
        self._save_offset(next_offset)
        # The POLLER must send the receipt ACK, not the worker: the worker handles messages one
        # at a time, so a second message only gets its turn after the first finishes — the ACK
        # would arrive late and useless. Confirmed live: in the original form it never arrived.
        self._maybe_ack_queued(upd)
        # Log EVERY accepted incoming message. Without this, "I wrote and nothing happened"
        # cannot be answered from the log: there was no record that the message ever arrived
        # (gap found 2026-08-02). IDs and a full-text fingerprint preserve correlation without
        # storing any message/quoted-message content or attachment bytes in the operational log.
        log.info("IN  id=%s update=%s %s", record_id, update_id, update_metadata(upd))
        self._submit_inbound_update(upd, record_id)
        self._mark_update_processed(update_id)
        return next_offset

    # ---- durable outbound delivery (never drop a reply) --------------------
    def _interim_allowed(self, chat: int | None) -> bool:
        """Telegram group/supergroup ids are negative. Apply policy to the actual destination."""
        return chat is None or chat >= 0 or getattr(self.cfg, "group_interim", False) is True

    def _load_queue(self) -> list:
        if self._queue_path is None or not self._queue_path.exists():
            return []
        out = []
        try:
            for line in self._queue_path.read_text("utf-8").splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except (OSError, ValueError):
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
        except OSError:
            pass

    def _enqueue(self, text: str, key: str | None, *, turn_text: bool = True,
                 chat: "int | None" = None) -> None:
        item = {"text": text, "key": key}
        if chat is not None:
            item["chat"] = chat
        if not turn_text:
            item["turn_text"] = False
        self._pending_send.append(item)
        self._persist_queue()
        if turn_text:
            self._turn_text_sent = True

    def _route_group_marker(self, text: str) -> tuple[str, "int | None"]:
        """Return (text, recipient) for one reply.

        A reply that starts with `group_marker` goes to the first allowed group (marker stripped)
        – but ONLY while the current turn belongs to the owner's own private chat. Model text must
        never pick a recipient on its own: from a group turn, or from any other user's DM, the
        marker is ignored and the reply stays where the turn came from (security audit
        2026-10-10, finding bridge-reply-routing). The recipient is returned for THIS reply only;
        `_owner_chat` is not changed, so later parts of the turn cannot drift into the group.
        """
        target = self._owner_chat
        marker = getattr(self.cfg, "group_marker", "") or ""
        groups = getattr(self.cfg, "allowed_group_ids", []) or []
        if not marker or not groups or not text:
            return text, target
        stripped = text.lstrip()
        if not stripped.lower().startswith(marker.lower()):
            return text, target
        stripped = stripped[len(marker):].lstrip()
        owner = self.cfg.allowed_user_ids[0] if self.cfg.allowed_user_ids else None
        if owner is None or self._owner_chat != owner:
            log.warning("group marker ignored: the turn is not from the owner's private chat")
            return stripped, target
        log.info("group marker → reply routed to the configured group")
        return stripped, groups[0]

    def _send_final(self, text: str, key: str | None = None, *, turn_text: bool = True) -> None:
        """Forward one reply RELIABLY. Marks the dedup ledger only AFTER a confirmed send; on any
        send failure the reply is queued to disk and the outbound loop keeps retrying until Telegram
        confirms. Order is preserved: if anything is already queued, this appends behind it."""
        text, target = self._route_group_marker(text)
        if target is None or (not text and not getattr(self, "_pending_files", None)):
            return
        if key and key in self._sent_keys:
            return
        # Voice-reply mode: speak the reply as a Telegram voice note. Best-effort ENHANCEMENT —
        # a too-long reply or ANY failure falls straight through to the durable TEXT path below,
        # so voice mode can NEVER make a reply not arrive (that is the whole point of v2).
        if (turn_text and text and self._voice_reply_on() and target == self._owner_chat
                and not (self.cfg.file_marker and self.cfg.file_marker.lower() in text.lower())):
            if len(text) > VOICE_MAX_CHARS:
                text = f"{text}\n\n🗣️→📝 (too long to read aloud — sent as text)"
            elif self._try_send_voice(text):
                if key:
                    self._mark_sent(key)
                self._turn_text_sent = True
                return
            else:
                text = f"🗣️→📝 (voice unavailable — sent as text)\n{text}"
        # `[tg-file] <path>` lines are attachments, not content — pull them out, send the
        # text first, then upload. Without this the agent had to bypass the bridge with a
        # raw curl, which is exactly what we do not want (markdown broke, [tg] leaked).
        if self.cfg.file_marker and self.cfg.file_marker.lower() in text.lower():
            text, files = self._extract_files(text)
            if files:
                self._pending_files = getattr(self, "_pending_files", []) + files
                if not self._interim_allowed(target):
                    # Recovery can re-read markers already collected while withholding narration.
                    self._pending_files = list(dict.fromkeys(self._pending_files))
            if not text and self._ensure_outbox() is None:
                # Only without the durable queue do we take the old path. EVERY attachment-only
                # reply used to slip through here: _flush_files clears the path from memory before
                # the upload, so a network failure lost the attachment (review finding #6).
                self._flush_files()
                return
        # The durable outbox tracks EACH PART separately. Previously a failure re-queued the
        # WHOLE text, so already-delivered parts arrived a second time — the user saw duplicated
        # opening paragraphs of long replies (finding D; found independently in two reviews).
        # Attachments weren't queued at all and vanished on restart (finding F).
        outbox = self._ensure_outbox()
        if outbox is not None:
            chunks = split_message(text) if text else []
            files = list(getattr(self, "_pending_files", []) or [])
            try:
                outbox.enqueue(chunks, files, key, chat=target)
            except Exception as e:
                log.error("could not persist the outgoing reply, falling back to the old path: %s", e)
            else:
                self._pending_files = []
                if turn_text:
                    # For the backstop the reply is handled once it is durably stored.
                    # Waiting for Telegram would, during an outage, create a second identical record.
                    self._turn_text_sent = True
                return

        # Subclasses (sandbox-agent RoutedDelivery) override _enqueue with the original
        # signature and bind the recipient themselves – pass `chat` only when it differs.
        extra = {"chat": target} if target != self._owner_chat else {}
        if self._pending_send:                       # something already waiting → keep FIFO order
            self._enqueue(text, key, turn_text=turn_text, **extra)
            return
        try:
            self.tg.send_message(target, text)
        except Exception as e:                        # network reset, 5xx after retries, etc.
            log.warning("forward failed → queued for re-delivery: error=%s %s",
                        type(e).__name__, message_metadata(text))
            self._enqueue(text, key, turn_text=turn_text, **extra)
            return
        if key:
            self._mark_sent(key)
        if turn_text:
            self._turn_text_sent = True
        log.info("FWD (send, legacy path) id=%s %s", key,
                 message_metadata(text, attachments=len(getattr(self, "_pending_files", []) or [])))
        self._flush_files()

    def _ensure_outbox(self) -> "DurableOutbox | None":
        # Attach mode only. StreamBridge shares this code but has its own queue alongside the
        # configs and a different lifecycle — switching it without a spec change would be an
        # unrequested behaviour change (and its test correctly caught that).
        if not getattr(self, "_use_durable_outbox", False):
            return None
        outbox = getattr(self, "_outbox", None)
        if outbox is None:
            queue_path = getattr(self, "_queue_path", None)
            if queue_path is None and _in_test_run() and not os.environ.get("AGENT2TELEGRAM_STATE"):
                # HARD STOP. A test that forgets to isolate its queue would otherwise enqueue into
                # the SHARED state directory, and the live bridge — a different process watching
                # that same queue — delivers it to the real chat. That is not hypothetical: on
                # 2026-08-23 a test run sent eleven fixture strings ("the real answer", "thanks!")
                # straight to a real chat. A test must never be able to do that, so the outbox
                # refuses the default path instead of trusting every test to opt out.
                log.error("durable outbox refused: test run without an isolated queue "
                          "(set _queue_path or AGENT2TELEGRAM_STATE)")
                return None
            root = Path(queue_path).parent if queue_path is not None else _state_dir(self.cfg)
            # OWN subdirectory, not the state root. DurableOutbox creates an "outbox" folder
            # under the root it is given — but `<state>/outbox` is ALSO the folder user
            # attachments are sent from (Config.path_outbox). Real files were sitting there;
            # without this separation the queue would count them against its quota, move the
            # foreign .json files to dead-letter and delete them after 90 days. The single
            # destructive finding of the review round, caught just before the switch.
            try:
                outbox = DurableOutbox(root / "queue")
            except Exception as e:                     # delivery must not stop
                log.error("could not open the durable outbox: %s", e)
                outbox = None
            self._outbox = outbox
        return outbox

    def _flush_pending(self) -> None:
        """Deliver the queue FIFO, stopping at the first failure to preserve order. Called every
        outbound cycle, so a network hiccup self-heals within one tick."""
        outbox = self._ensure_outbox()
        while outbox is not None and self._owner_chat is not None:
            rec = outbox.head()
            if rec is None:
                break
            # The recipient was bound when the reply was queued. Using the CURRENT chat here sent a
            # private reply into a group once a group message arrived in between (audit
            # 2026-10-10). Legacy records without a stored chat keep the old behaviour.
            chat = rec.chat if rec.chat is not None else self._legacy_chat()
            # Parts already confirmed by Telegram are NOT resent — only the remaining ones.
            for index, chunk in rec.pending_chunks:
                try:
                    self.tg.send_message(chat, chunk)
                except Exception as e:
                    log.warning("delivery of part %d still failing: error=%s %s",
                                index, type(e).__name__, message_metadata(chunk))
                    return
                outbox.mark_chunk_sent(rec.record_id, index)
            gave_up = False
            for file_path in rec.pending_files:
                try:
                    self._send_one_file(file_path, chat)
                except Exception as e:
                    # Bounded retries: without them one bad attachment (over the size limit,
                    # a deleted file, HTTP 400) would hold the head of the FIFO queue forever
                    # and NO further reply would reach the user (finding F2).
                    attempts = outbox.fail(rec.record_id, f"{file_path}: {e}")
                    log.warning("delivery of attachment %s failed (%d/%d): %s",
                                file_path, attempts, OUTBOX_MAX_ATTEMPTS, e)
                    if attempts >= OUTBOX_MAX_ATTEMPTS:
                        # mark_file_sent must NOT be used: it would record that the attachment
                        # arrived when it did not — a silent loss, exactly what this project
                        # removes. An earlier form of this fix did that; caught in the third
                        # review round. The whole record goes to dead-letter: it leaves the queue
                        # (so it does not clog it) but stays traceable, including which parts
                        # did get delivered.
                        self._report_permanent_refusal(Path(file_path).name, str(e), chat)
                        outbox.give_up(rec.record_id, f"attachment {file_path}: {e}")
                        log.error("attachment %s gave up after %d attempts → dead-letter",
                                  file_path, attempts)
                        gave_up = True
                        break
                    return
                outbox.mark_file_sent(rec.record_id, file_path)
            if gave_up:
                continue          # the record is in dead-letter, the queue moves on
            if rec.key and not self._mark_sent(rec.key):
                # The ledger write failed → the record STAYS in the queue. Delivered parts are
                # marked, so they are not resent; the next cycle just tries to finish it.
                return
            outbox.done(rec.record_id)
            # The record id and a content-free fingerprint are logged ON PURPOSE: the user
            # received one reply twice and the log could not tell whether it was the same
            # message delivered twice or two different ones. Without that, any fix is a guess.
            log.info("FWD (delivered) id=%s %d parts, %s", rec.record_id, len(rec.chunks),
                     message_metadata("".join(rec.chunks), kind="text" if rec.chunks else "file",
                                      attachments=len(rec.files)))

        while self._pending_send and self._owner_chat is not None:
            item = self._pending_send[0]
            chat = item.get("chat", self._legacy_chat())
            try:
                self.tg.send_message(chat, item.get("text", ""))
            except Exception as e:
                log.warning("re-delivery still failing (%d queued): error=%s %s",
                            len(self._pending_send), type(e).__name__,
                            message_metadata(str(item.get("text", ""))))
                return
            self._pending_send.pop(0)
            self._persist_queue()
            if item.get("key"):
                self._mark_sent(item["key"])
            log.info("FWD (re-delivered) id=%s %s", item.get("key"),
                     message_metadata(str(item.get("text", ""))))

    # ---- inbound (Telegram → session) -------------------------------------
    def _init_inbound_worker_state(self) -> None:
        self._inbound_queue = queue.Queue()
        self._inbound_worker_started = False
        self._inbound_worker_lock = threading.RLock()

    def _ensure_inbound_worker_state(self) -> None:
        # Focused tests may construct the object without AttachBridge.__init__.
        if not hasattr(self, "_inbound_queue"):
            self._inbound_queue = queue.Queue()
        if not hasattr(self, "_inbound_worker_started"):
            self._inbound_worker_started = False
        if not hasattr(self, "_inbound_worker_lock"):
            self._inbound_worker_lock = threading.RLock()

    def _start_inbound_worker(self) -> None:
        self._ensure_inbound_worker_state()
        with self._inbound_worker_lock:
            if self._inbound_worker_started:
                return
            threading.Thread(target=self._inbound_worker_loop, daemon=True).start()
            self._inbound_worker_started = True

    def _submit_inbound_update(self, upd: dict, record_id: str | None = None) -> None:
        self._start_inbound_worker()
        self._ensure_inbound_worker_state()
        with self._inbound_worker_lock:
            if record_id:
                if record_id in self._inbound_inflight():
                    return
                # Recovery can claim and even finish this record after reserve() but
                # before the poller reaches submit (a receipt HTTP call is in between).
                # Recheck disk under the same lock as completion, not a stale snapshot.
                inbox = getattr(self, '_inbox', None)
                if inbox is not None and not any(r.record_id == record_id for r in inbox.pending()):
                    return
                self._inbound_inflight().add(record_id)
            self._inbound_queue.put((upd, record_id))

    def _inbound_inflight(self) -> set:
        """Records currently in flight in the in-memory queue. Without this bookkeeping a
        retry would enqueue the same message again and the user would receive it twice."""
        if not hasattr(self, "_inflight_ids"):
            self._inflight_ids = set()
        return self._inflight_ids

    def _replay_pending_inbound(self) -> int:
        """After startup, deliver messages left in durable storage.

        Without this, durability was only half done: a message was stored on disk but nobody ever
        took it from there — Telegram won't resend it, so it would sit until retention expired.
        Found independently in two cross-reviews.
        """
        inbox = self._ensure_inbox()
        if inbox is None:
            return 0
        try:
            waiting = inbox.pending()
        except Exception as e:
            log.error("could not read the leftover inbound messages: %s", e)
            return 0
        in_flight = self._inbound_inflight()
        waiting = [r for r in waiting if r.record_id not in in_flight]
        for rec in waiting:
            self._submit_inbound_update(rec.update, rec.record_id)
        if waiting:
            log.info("delivering %d message(s) left over from before restart", len(waiting))
        return len(waiting)

    def _inbound_worker_loop(self) -> None:
        """Process accepted Telegram updates FIFO outside the long-poll thread.

        Media download and STT retries can take minutes; keeping them here prevents one slow voice
        note from blocking getUpdates while preserving message order for the owner.
        """
        while not self._stop.is_set() or not self._inbound_queue.empty():
            try:
                item = self._inbound_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            upd, record_id = item if isinstance(item, tuple) else (item, None)
            try:
                delivered = self._handle(upd)
            except Exception as e:
                log.error("inbound worker error: error=%s id=%s %s",
                          type(e).__name__, record_id, update_metadata(upd))
                self._inbound_failed(record_id, str(e))
            else:
                # `_handle` returns False when the message never reached the session. Both cases
                # used to just call task_done and the message vanished — that is audit finding B.
                if delivered is not False:
                    log.info("IN  delivered to session id=%s", record_id)
                if delivered is False:
                    self._inbound_failed(record_id, "not delivered to the session")
                else:
                    self._inbound_done(record_id)
            finally:
                self._inbound_queue.task_done()

    def _inbound_done(self, record_id: str | None) -> None:
        self._ensure_inbound_worker_state()
        with self._inbound_worker_lock:
            self._complete_inbound_record(record_id)

    def _complete_inbound_record(self, record_id: str | None) -> None:
        # ORDER MATTERS. Removing the record from the in-flight set BEFORE deleting it from the
        # inbox leaves a window where it is "not in flight" and still "pending on disk" — and a
        # concurrent _replay_pending_inbound() lands exactly there and submits the message a
        # SECOND time. The user gets the same message twice. Caught by CI, where the timing
        # differs enough for the window to be hit.
        #
        # Deleting from the inbox first closes it: while the delete runs the record still counts
        # as in flight (so replay skips it), and once it is gone replay cannot see it at all.
        inbox = getattr(self, "_inbox", None)
        try:
            if record_id and inbox is not None:
                inbox.done(record_id)
        except Exception as e:
            log.warning("cleanup of delivered record %s failed: %s", record_id, e)
        finally:
            # Always release the id, even if the delete failed — otherwise it stays marked in
            # flight forever and the message can never be retried.
            self._inbound_inflight().discard(record_id)

    def _inbound_failed(self, record_id: str | None, reason: str) -> None:
        """Not delivered → the record stays and is retried. After the attempts are exhausted it
        goes to dead-letter, so the message never vanishes without a trace even in a hopeless case."""
        self._inbound_inflight().discard(record_id)
        inbox = getattr(self, "_inbox", None)
        if not record_id or inbox is None:
            return
        try:
            attempts = inbox.fail(record_id, reason)
            if attempts >= INBOUND_MAX_ATTEMPTS:
                inbox.give_up(record_id, f"{reason} (after {attempts} attempts)")
                log.error("update %s could not be delivered even on attempt %d → dead-letter",
                          record_id, attempts)
        except Exception as e:
            log.warning("marking undelivered record %s failed: %s", record_id, e)

    def _inbound_loop(self) -> None:
        self._start_inbound_worker()
        self._replay_pending_inbound()   # clear the backlog first, then new messages
        offset = self._load_offset()
        allowed_updates = json.dumps(["message", "edited_message", "message_reaction"])
        transient_fails = 0
        outage_alerted = False
        while not self._stop.is_set():
            try:
                updates = self.tg._call(
                    "getUpdates",
                    {"offset": offset, "timeout": self.cfg.poll_timeout,
                     "allowed_updates": allowed_updates},
                    timeout=self.cfg.poll_timeout + 15,
                )
            except (TelegramError, OSError) as e:
                if is_network_error(e):
                    # Transient network/DNS problem (Errno 8 "nodename nor servname", connection
                    # reset, timeout) — the bridge recovers on its own. WARNING + backoff; ERROR
                    # only ONCE during a longer outage, so monitoring doesn't alert on a
                    # self-healing VPN/DNS blip.
                    transient_fails += 1
                    if transient_fails >= 10 and not outage_alerted:
                        log.error("getUpdates network outage (%d in a row) is lasting: %s",
                                  transient_fails, e)
                        outage_alerted = True
                    else:
                        log.warning("getUpdates transient network error (%d): %s", transient_fails, e)
                    self._stop.wait(min(3 * transient_fails, 30))
                    continue
                log.error("getUpdates failed: %s", e)   # a real (non-network) error
                self._stop.wait(3)
                continue
            if outage_alerted:
                log.info("getUpdates network recovered after %d errors", transient_fails)
            transient_fails = 0
            outage_alerted = False
            for upd in updates:
                try:
                    offset = self._handle_update_once(upd, offset)
                except Exception as e:
                    log.error("inbound error: error=%s %s", type(e).__name__, update_metadata(upd))

    def _handle(self, upd: dict) -> bool:
        """Handle one update. Returns False ONLY when the message was not delivered to the session.

        The return value drives the durable inbox: True = handled, the record may go; False = not
        delivered, the record stays and is retried. So every "nothing to do" branch returns True —
        retrying them is pointless. False comes solely from a failed write to tmux, exactly what
        retrying cures.
        """
        # Reactions (e.g. ❤️) → quick-feedback line.
        mr = upd.get("message_reaction")
        if mr:
            if mr.get("user", {}).get("id") not in self._allowed:
                return True
            emojis = "".join(r.get("emoji", "") for r in mr.get("new_reaction", [])
                             if r.get("type") == "emoji")
            if emojis:
                # Telegram can redeliver the SAME reaction as multiple updates. Without
                # deduplication each update would ask the agent for another reply.
                # The reaction itself is identified by chat + message +
                # emoji, so a repeat of that triple inside a short window is a duplicate, never
                # a second opinion. This is identity matching, not guessing from content.
                key = (mr.get("chat", {}).get("id"), mr.get("message_id"), emojis)
                now = time.time()
                # Legacy callers and tests can construct instances without __init__.
                # Initialize the reaction cache lazily for those callers.
                if getattr(self, "_reaction_seen", None) is None:
                    self._reaction_seen = {}
                seen = self._reaction_seen.get(key)
                if seen is not None and now - seen < REACTION_DEDUP_S:
                    log.info("reaction %s on #%s ignored as a duplicate (%.1fs after the first)",
                             emojis, mr.get("message_id"), now - seen)
                    return True
                self._reaction_seen[key] = now
                if len(self._reaction_seen) > 512:      # bound cache growth
                    for k in sorted(self._reaction_seen, key=self._reaction_seen.get)[:256]:
                        self._reaction_seen.pop(k, None)

                # A reaction DOES deserve an answer, but a one-liner — being left on read feels
                # like the bridge swallowed it. The prompt therefore asks for a very short reply.
                #
                # The backstop exemption below is the safety net for when the agent still stays
                # silent. Without it the backstop ("a Telegram turn must never go unanswered")
                # grabs the last assistant text and can forward an INTERNAL note — that is how
                # "No response requested." reached a user on a live bridge (2026-08-02). Silence
                # is the lesser evil; the log line in _finish_turn keeps it visible.
                # Only when no turn was running: a reaction landing mid-turn must not disarm the
                # backstop for the real question underneath it.
                turn_running = self._turn_active.is_set()
                self._begin_turn()
                if not turn_running:
                    self._turn_is_reaction = True
                text = (
                    f"{emojis} reacted {emojis} to your message #{mr.get('message_id')} "
                    f"— quick feedback. Always answer, but with ONE very short line "
                    f"(a few words or an emoji), nothing more.")
                if self.cfg.inbound_sender_label:
                    text = f"{sender_label({'from': mr.get('user'), 'chat': mr.get('chat')}, self.cfg.language)} {text}"
                return self._inject(text)
            return True

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return True
        user_id = msg.get("from", {}).get("id")
        chat_id = msg["chat"]["id"]
        if user_id not in self._allowed:
            self.tg.send_message(chat_id, "⛔ Not authorized.")
            return True
        chat_type = (msg.get("chat") or {}).get("type")
        in_group = chat_type in ("group", "supergroup")
        groups = getattr(self.cfg, "allowed_group_ids", []) or []
        if in_group:
            log.info("IN  group chat=%s from=%s", chat_id, user_id)
            if groups and chat_id not in groups:
                return True                      # foreign group: silent, never routed
        elif user_id in (getattr(self.cfg, "group_only_user_ids", []) or []):
            self.tg.send_message(chat_id, "⛔ Not authorized here – please use the shared group.")
            return True

        from .mentions import accepts_message
        if not accepts_message(self.cfg, msg):
            return True
        if self.cfg.reply_to_origin_chat:
            # Replies follow the chat the last AUTHORIZED message came from (DM or group). The
            # session has ONE conversation, so a message from another chat must not switch the
            # recipient while a turn is still answering the previous chat – the rest of that
            # answer would land in the wrong conversation (audit 2026-10-10). Wait for the turn
            # to end first; the inbound worker is FIFO, so order is preserved.
            self._wait_for_turn_before_chat_switch(chat_id)
            self._owner_chat = chat_id


        # Bridge-level slash commands (e.g. /start, /help) are answered here instead of being
        # forwarded to the agent — so the first contact is a friendly intro, not the agent
        # puzzling over "/start". Only plain-text commands, never media captions.
        text0 = (msg.get("text") or "").strip()
        if text0.startswith("/") and not (msg.get("voice") or msg.get("audio")
                                           or msg.get("photo") or msg.get("document")):
            if self._handle_command(text0, chat_id, msg.get("message_id")):
                return True

        text = (msg.get("text") or msg.get("caption") or "").strip()
        if msg.get("voice") or msg.get("audio"):
            # Transcription and download do their own retries and report errors to the user
            # themselves, so we don't loop here — an external retry would just pay for STT again.
            text = self._transcribe(msg.get("voice") or msg.get("audio"), chat_id) or text
            if not text:
                return True
            # The agent MUST know it is reading a machine transcript, not written text. Without
            # this it treats the transcript as verbatim and, on an error, sees nonsense instead
            # of a mis-recognition: a voice note once transcribed into the wrong language entirely
            # and the agent insisted it had arrived exactly as sent. With this marker it can guess
            # from context instead.
            text = f"[voice transcript – may contain recognition errors]\n{text}"
        elif msg.get("photo") or msg.get("document"):
            note = self._download_note(msg, chat_id)
            if not note:
                return True
            text = f"{text}\n{note}".strip()
        if text:
            text = self._prepend_reply_context(msg, text)
            if self._voice_reply_on():
                # Tell the agent to write for the EAR, not the eye. This is the core of voice
                # mode — the model phrases speakable text far better than any post-processing.
                text = f"{VOICE_MODE_HINT}\n{text}"
            if self.cfg.inbound_sender_label:
                text = f"{sender_label(msg, self.cfg.language)} {text}"
            text = self._limit_inbound_prompt(text, chat_id)
            if not text:
                return True
            self._begin_turn()
            return self._inject(text)
        return True

    def _legacy_chat(self) -> "int | None":
        """Recipient of a queued record from before recipients were stored. Never the CURRENT
        chat (that may be a group that wrote later): only the owner's DM, which is where every
        reply went before group routing existed. Without reply_to_origin_chat nothing changes."""
        if getattr(self.cfg, "reply_to_origin_chat", False) and self.cfg.allowed_user_ids:
            return self.cfg.allowed_user_ids[0]
        return self._owner_chat

    def _wait_for_turn_before_chat_switch(self, chat_id: int) -> None:
        active = getattr(self, "_turn_active", None)
        if self._owner_chat == chat_id or active is None or not active.is_set():
            return
        log.info("IN  chat %s waits: a turn for another chat is still running", chat_id)
        stop = getattr(self, "_stop", None)
        deadline = time.monotonic() + CHAT_SWITCH_MAX_WAIT
        warned = False
        # FAIL-CLOSED: the recipient never switches while the old turn is still active, however
        # long it runs – a timeout that switched anyway re-opened the leak (audit 2026-10-10,
        # second run). A long wait is reported loudly instead, once, to the waiting chat.
        while active.is_set() and not (stop is not None and stop.is_set()):
            if not warned and time.monotonic() >= deadline:
                warned = True
                log.error("IN  chat %s still waiting after %.0fs: the previous turn has not ended",
                          chat_id, CHAT_SWITCH_MAX_WAIT)
                try:
                    from .notices import waiting_for_turn_end_text
                    if self._interim_allowed(chat_id):
                        self.tg.send_message(chat_id, busy_text(self.cfg) if self._busy_for_notice()
                                             else waiting_for_turn_end_text(self.cfg))
                except Exception as e:
                    log.warning("could not report the long wait: %s", e)
            time.sleep(CHAT_SWITCH_POLL)

    def _prepend_reply_context(self, msg: dict, text: str) -> str:
        """When the user replies to a specific message, tell the agent which one.

        Without this the agent gets only the bare text and has to guess from context. For a
        terse "fix this" under an automated notice it is impossible to guess. The marker is in
        English because the tool writes it, not the agent.
        """
        replied_to = msg.get("reply_to_message") or {}
        quote = (replied_to.get("text") or replied_to.get("caption") or "").strip()
        if not quote:
            return text
        quote = " ".join(quote.split())
        if len(quote) > REPLY_QUOTE_CHARS:
            quote = quote[:REPLY_QUOTE_CHARS] + "…"
        return f"[replying to: {quote}]\n{text}"

    def _maybe_ack_queued(self, upd: dict) -> None:
        """Acknowledge a message that arrived while work was already in progress.

        The bridge writes this operational notice in cfg.language, independently of STT
        and of the model's own responses. The default preserves existing English behavior.
        """
        if not self._busy_for_notice():
            log.debug("ACK not sent: no recent evidence of work in progress")
            return
        msg = upd.get("message") or upd.get("edited_message") or {}
        if msg.get("from", {}).get("id") not in self._allowed:
            return
        from .mentions import accepts_message
        if not accepts_message(self.cfg, msg):
            return
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            return
        if not self._interim_allowed(chat_id):
            return
        now = time.monotonic()
        # MIND the default: `time.monotonic()` counts from SYSTEM start, so on a long-running
        # macOS it is a large number, but on a freshly booted Linux (container, server restart)
        # nearly zero. With a 0.0 default no ACK would be sent for the first 30 seconds there.
        # So None = "never yet", not zero.
        last_ack = getattr(self, "_last_queue_ack", None)
        if last_ack is not None and now - last_ack < QUEUE_ACK_COOLDOWN:
            return
        self._last_queue_ack = now
        try:
            self.tg.send_message(chat_id, busy_text(self.cfg))
            # Must log: without a record you can't tell after an incident whether the ACK went
            # out. It was once impossible to decide whether the feature even worked.
            log.info("receipt ACK sent (another turn is running)")
        except Exception as e:
            log.warning("could not send the receipt ACK: %s", e)

    def _busy_for_notice(self) -> bool:
        """Display-only confidence: never close a turn or release its destination on silence.

        Without an observed Claude Stop hook, 90 seconds of silence is ambiguous. Suppress
        busy/typing then, but retain the long delivery window and cross-chat ownership lock.
        New transcript activity restores the indicator; an actual hook confirms the lifecycle.
        """
        if not self._turn_active.is_set():
            return False
        if getattr(self, '_claude_hook_ready', True):
            return True
        return time.monotonic() - max(self._last_activity, self._turn_started) < IDLE_DONE

    def _begin_turn(self) -> None:
        # Light "typing…" from the actual injection point.
        self._consume_turn_end()                 # drop any stale end-marker from a prior turn
        now = time.monotonic()
        self._turn_active.set()
        self._turn_from_tg = True
        self._last_activity = now
        self._turn_started = now
        self._turn_started_wall = time.time()   # records are stamped with wall-clock time
        self._typing_count = 0
        self._max_gap = 0.0
        self._last_typing = now
        self._turn_text_sent = False             # gate TUI bubbles until intro text lands
        self._group_reply = None
        self._group_files = ()
        self._group_end_confirmed = False
        self._turn_is_reaction = False           # set by the reaction branch right after this
        # Where the transcript stood when this turn began. The backstop scans only from here:
        # anything before it belongs to an earlier turn and must never be re-sent as this turn's
        # answer (2026-09-02 22:19: the backstop "forwarded" the 21:39 reply, the dedup ledger
        # dropped it, and the log still claimed the turn was answered).
        self._turn_tpos = getattr(self, "_tpos", 0)
        # Clear any end-of-turn signal left over from the PREVIOUS turn. Codex writes
        # task_complete to the rollout with a delay, so a late one could land after the next
        # turn had already started and end it within ~1 s — the reply was then never sent and
        # the backstop never fired (2026-08-02: a voice note answered with silence).
        self._pending_turn_end = False
        self._turn_begun_at = now
        # Seed the TUI dedup with tool lines ALREADY on screen from previous turns, so the
        # scraper only emits calls that appear DURING this turn — otherwise stale lines still
        # visible in the pane get re-sent as bubbles under the new turn.
        if self.cfg.agent == "codex" and hasattr(self, "_session"):
            try:
                self._tui_seen = set(_extract_tui_tools(self._session._capture()))
            except Exception:
                self._tui_seen = set()
        else:
            self._tui_seen = set()
        if self._interim_allowed(self._owner_chat):
            self.tg.send_chat_action(self._owner_chat, "typing")   # instant, don't wait for the loop
            self._typing_count = 1
        log.info("TURN START t=%.2f", time.time())

    def _inject(self, text: str) -> bool:
        """Deliver a message to the session. A failure must NEVER be silent.

        Previously a failed write to tmux was only logged and the message vanished — the user
        learned nothing. Seen in production:
            19:04:41 ERROR inject failed: '[TG] are you there?' timed out after 10 seconds
        The user sent that message and never got a reply; no trace was left of it.

        A write to tmux can fail transiently (busy pane, full buffer), so it is retried. When
        even the retries don't help, the user has to be told.
        """
        self._turn_active.set()
        self._last_activity = time.monotonic()   # keep typing lit from the very start
        last_error: Exception | None = None
        for attempt in range(1, INJECT_ATTEMPTS + 1):
            try:
                self._session.inject(text)
                if attempt > 1:
                    log.info("inject succeeded on attempt %d", attempt)
                return True
            except SessionError as e:
                last_error = e
                if "refusing to inject" in str(e):
                    # The pane isn't running the expected agent — retrying won't help, only delay.
                    log.error("inject failed: error=%s %s", type(e).__name__, message_metadata(text))
                    self._turn_active.clear()
                    self._notify_unsafe_pane(str(e))
                    return False
            except Exception as e:
                last_error = e
            if attempt < INJECT_ATTEMPTS:
                self._stop.wait(INJECT_RETRY_WAIT)
        # TimeoutExpired/CalledProcessError can include the entire send-keys argv (the message).
        log.error("inject failed after %d attempts: error=%s %s", INJECT_ATTEMPTS,
                  type(last_error).__name__, message_metadata(text))
        self._turn_active.clear()
        self._notify_inject_failed(text, str(last_error))
        return False

    def _notify_inject_failed(self, text: str, reason: str) -> None:
        """Tell the owner their message did not get through — better twice than never."""
        if not self._owner_chat:
            return
        preview = text.strip().replace("\n", " ")
        if len(preview) > 80:
            preview = preview[:80] + "…"
        try:
            self.tg.send_message(
                self._owner_chat,
                "⚠️ Couldn't deliver your message to the agent session — the write to tmux "
                f"failed after {INJECT_ATTEMPTS} attempts.\n\n"
                f"Message: {preview}\n{reason}\n\n"
                "The agent may be frozen. Check the tmux pane, then send it again.",
            )
            # Log it: without this the daily report sees "N injects failed" but cannot tell
            # whether the user was ever told. That is the exact blind spot delivery logging
            # was added to close (2026-08-04).
            log.info("inject failure reported to the owner %s", message_metadata(text))
        except Exception as e:
            log.warning("inject-failure notification failed: error=%s %s",
                        type(e).__name__, message_metadata(text))

    def _notify_unsafe_pane(self, reason: str) -> None:
        if not self._owner_chat:
            return
        now = time.monotonic()
        if now - self._last_pane_warning < 60:
            return
        self._last_pane_warning = now
        try:
            self.tg.send_message(
                self._owner_chat,
                "⚠️ Agent2Telegram blocked a Telegram message because the configured tmux "
                f"session no longer appears to be running the expected agent.\n\n{reason}\n\n"
                "Restart the agent in that tmux pane, then resend the message.",
            )
        except Exception as e:
            log.warning("unsafe-pane owner notification failed: error=%s", type(e).__name__)

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
                "Commands: /help · /status · /id · /setkey · /voice")
            return True
        if cmd == "id":
            self.tg.send_message(chat_id, f"Your Telegram id: `{chat_id}`")
            return True
        if cmd == "status":
            voice = "✓" if self.cfg.elevenlabs_api_key else "✗"
            replies = "🔊 on" if self._voice_reply_on() else "🔇 off"
            self.tg.send_message(chat_id,
                f"✅ Connected — *{agent}* in tmux session `{self.cfg.tmux_session}`.\n"
                f"🎤 Voice transcription (ElevenLabs): {voice}\n"
                f"🗣️ Voice replies (/voice): {replies}")
            return True
        if cmd == "setkey":
            return self._set_voice_key(arg, chat_id, message_id)
        if cmd == "voice":
            return self._toggle_voice(chat_id)
        return False    # unknown command → let the agent handle it

    # ---- voice-reply mode ----------------------------------------------------
    def _load_voice_state(self) -> bool:
        try:
            return self._voice_state_path.read_text("utf-8").strip() == "on"
        except OSError:
            return False

    def _voice_reply_on(self) -> bool:
        """Voice replies only when the switch is on AND a key exists to actually synthesize."""
        return bool(getattr(self, "_voice_on", False) and self.cfg.elevenlabs_api_key)

    def _set_voice_state(self, on: bool) -> None:
        self._voice_on = on
        try:
            self._voice_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._voice_state_path.parent / (self._voice_state_path.name + ".tmp")
            tmp.write_text("on" if on else "off", encoding="utf-8")
            tmp.replace(self._voice_state_path)          # atomic persist
        except OSError as e:
            log.warning("could not persist voice-mode state: %s", e)

    def _try_send_voice(self, text: str) -> bool:
        """Render *text* to a Telegram voice note (ElevenLabs → mp3 → OGG/OPUS via ffmpeg →
        sendVoice). Returns True on success; on ANY failure returns False so the caller falls
        back to durable text. Never raises — voice must not break delivery."""
        key = self.cfg.elevenlabs_api_key
        if not key:
            return False
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            log.warning("voice reply skipped: ffmpeg not on PATH")
            return False
        try:
            spoken = tts.sanitize_for_speech(text)            # rough safety net only
            # Long narrations go in sentence-sized pieces (see tts.synthesize_long); short
            # replies are a single piece, so nothing changes for them.
            segments = tts.synthesize_long(spoken, api_key=key, voice_id=self.cfg.tts_voice_id,
                                           model_id=self.cfg.tts_model_id)
        except Exception as e:
            log.warning("voice reply TTS failed: error=%s %s", type(e).__name__,
                        message_metadata(text, kind="voice"))
            return False
        tmpdir = tempfile.mkdtemp(prefix="a2t_voice_")
        try:
            ogg_path = os.path.join(tmpdir, "reply.ogg")
            list_path = os.path.join(tmpdir, "parts.txt")
            with open(list_path, "w", encoding="utf-8") as lst:
                for i, seg in enumerate(segments):
                    part = os.path.join(tmpdir, f"part{i:03d}.mp3")
                    with open(part, "wb") as fh:
                        fh.write(seg)
                    lst.write(f"file '{part}'\n")
            # OGG/OPUS is what Telegram wants for a real voice bubble; other formats become a
            # plain audio attachment. The pieces are glued with the concat demuxer and 1.5 s of
            # silence is padded at the end to protect the last words from clipping. No shell.
            # 48 kHz mono is what Telegram voice notes use natively; 0.7 s of lead-in and 1.5 s
            # of tail silence protect the first and last words from client-side clipping; loudnorm
            # (EBU R128) keeps the level even across a long note (2026-09-05: whispering half).
            r = subprocess.run(
                [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                 "-af", "adelay=700,apad=pad_dur=1.5,loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000", "-ac", "1",
                 "-c:a", "libopus", "-b:a", "48k", "-application", "voip", ogg_path],
                capture_output=True, timeout=max(60, 30 * len(segments)),
            )
            if r.returncode != 0 or not os.path.exists(ogg_path) or os.path.getsize(ogg_path) == 0:
                log.warning("voice reply ffmpeg conversion failed (rc=%s)", r.returncode)
                return False
            self.tg.send_voice(self._owner_chat, ogg_path, duration=_ogg_duration(ogg_path))
            log.info("FWD (voice) %s", message_metadata(text, kind="voice", attachments=1))
            return True
        except Exception as e:
            log.warning("voice reply send failed: error=%s %s", type(e).__name__,
                        message_metadata(text, kind="voice", attachments=1))
            return False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _toggle_voice(self, chat_id: int) -> bool:
        if not self.cfg.elevenlabs_api_key:
            self.tg.send_message(chat_id,
                "🔊 Voice replies need an ElevenLabs key first. Add one with /setkey, then /voice.")
            return True
        new_state = not bool(getattr(self, "_voice_on", False))
        self._set_voice_state(new_state)
        self.tg.send_message(chat_id,
            "🔊 Voice replies ON — I'll answer with voice notes. Long or table-heavy replies still "
            "come as text. /voice again to turn off."
            if new_state else
            "🔇 Voice replies OFF — back to text.")
        return True

    def _set_voice_key(self, key: str, chat_id: int, message_id: int | None) -> bool:
        """Save an ElevenLabs key to enable voice, then delete the message so the secret isn't
        left in the chat history."""
        if not key:
            self.tg.send_message(chat_id,
                "Usage: `/setkey <your ElevenLabs API key>` — enables voice-message transcription.\n"
                "I'll delete your message right after so the key isn't left in the chat.")
            return True
        from . import stt
        if not stt.looks_like_api_key(key):
            # Caught here, the user retypes once. Caught at the first voice message, it
            # looks like the bridge is broken (2026-08-07: a legacy key sat in every
            # config and every bot answered "HTTP 400" without saying why).
            if message_id is not None:
                self.tg.delete_message(chat_id, message_id)
            self.tg.send_message(chat_id,
                "That doesn't look like an ElevenLabs key — they start with `sk_`. "
                "Grab a fresh one at elevenlabs.io → Profile → API Keys and send "
                "`/setkey sk_…` again. (Your message is deleted either way.)")
            return True
        self.cfg.elevenlabs_api_key = key
        try:
            from .config import mark_secret_from_file, save
            mark_secret_from_file(self.cfg, "elevenlabs_api_key")
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
        """Dedicated thread: assert "typing…" every TYPING_INTERVAL during Telegram turns.

        It runs independently of the outbound/send loop, so a flood-control sleep in the send path
        (which happens during a burst of messages) can never starve the indicator — that was the
        cause of mid-turn typing gaps. It stops the instant the turn ends (turn_active cleared), so
        no action fires after the final message and typing stops with it (bar Telegram's ~5s decay)."""
        while not self._stop.is_set():
            if (self._busy_for_notice() and self._turn_from_tg and self._owner_chat is not None
                    and self._interim_allowed(self._owner_chat)):
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
            if (self._turn_active.is_set() and self._turn_from_tg and self._owner_chat is not None
                    and self._interim_allowed(self._owner_chat)):
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
        """The most recent assistant text in the transcript (the turn's final answer). Read-only
        tail scan — used purely by the turn-end backstop, doesn't touch the live _tpos cursor."""
        if not self._transcript:
            return None
        try:
            size = self._transcript.stat().st_size
            with open(self._transcript, "rb") as f:
                start = int(getattr(self, "_turn_tpos", 0) or 0)
                if start > size:
                    # The offset is a position in ONE file. A new (or truncated) transcript makes
                    # it meaningless: seeking past EOF reads nothing and the answer is dropped —
                    # the very failure this backstop exists to prevent.
                    start = 0
                f.seek(max(0, size - 2_000_000, start))
                tail = f.read()
        except OSError:
            return None
        last = None
        last_key = None                              # transcript with no assistant text at all
        sent = getattr(self, "_sent_keys", set())
        group = not self._interim_allowed(self._owner_chat)
        last_group = group
        # A fresh reader must see texts withheld by the live reader, including Codex's
        # duplicate-record window. Never alter the live reader's state during a tail scan.
        reader = (readers.for_agent(self.cfg.agent) if group or getattr(self.cfg, "group_marker", "")
                  else getattr(self, "_reader", None) or readers.for_agent(self.cfg.agent))
        for raw in tail.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8", "ignore"))
            except (ValueError, json.JSONDecodeError):
                continue
            try:
                for ev in reader.parse(rec):
                    if last_group and ev.kind in ("tool", "files", "user", "turn_start"):
                        last, last_key = None, None
                    if ev.kind == "text" and ev.text and ev.text.strip():
                        _, target = self._route_group_marker(self._strip_marker(ev.text))
                        last_group = not self._interim_allowed(target)
                        if last_group:
                            last, last_key = None, None
                            if ev.interim or not (ev.final or self._group_end_confirmed):
                                continue
                        if ev.key and ev.key in sent:
                            continue                 # already delivered → not this turn's answer
                        last = ev.text
                        last_key = ev.key            # dedup id of that very message
            except Exception:
                continue
        self._last_backstop_key = last_key
        return last

    def _wait_backstop_retry(self) -> None:
        stop = getattr(self, "_stop", None)
        if stop is not None:
            stop.wait(BACKSTOP_RETRY_DELAY)
        else:
            time.sleep(BACKSTOP_RETRY_DELAY)

    def _retry_last_assistant_text(self) -> str | None:
        last = self._last_assistant_text()
        if last and last.strip():
            return last
        for attempt in range(BACKSTOP_RETRY_ATTEMPTS):
            if self._turn_text_sent:
                return None
            try:
                self._drain_transcript()
            except Exception as e:
                log.debug("turn-end backstop transcript drain failed: %s", e)
            if self._turn_text_sent:
                return None
            last = self._last_assistant_text()
            if last and last.strip():
                return last
            if attempt != BACKSTOP_RETRY_ATTEMPTS - 1:
                self._wait_backstop_retry()
        return None

    def _consume_signal_text(self) -> str | None:
        signal = getattr(self, "_signal", None)
        if not signal or not signal.exists():
            return None
        try:
            answer = signal.read_text("utf-8").strip()
            signal.unlink()
        except OSError:
            return None
        return answer or None

    def _has_turn_end_backstop_source(self) -> bool:
        return (getattr(self, "_transcript", None) is not None
                or getattr(self, "_signal", None) is not None)

    def _finish_turn(self, *, confirmed: bool = True) -> None:
        """Drop the technical bubble and stop the typing indicator at the real end of a turn."""
        self._status_clear()
        was_active = self._turn_active.is_set()
        from_tg_at_end = self._turn_from_tg
        text_sent_at_end = self._turn_text_sent
        reaction_at_end = getattr(self, "_turn_is_reaction", False)
        self._group_end_confirmed = confirmed
        reply = self._group_reply
        self._group_reply = None
        if was_active and self._turn_from_tg and self._group_files:
            self._pending_files = list(getattr(self, "_pending_files", []) or []) + list(self._group_files)
        self._group_files = ()
        if was_active and self._turn_from_tg and reply is not None:
            text, key, final = reply
            if final or confirmed:
                self._send_final(text, key=key)
        # Backstop: a Telegram-originated turn must NEVER go unanswered. If nothing was forwarded
        # this turn (the [tg] marker was forgotten, the turn was a long heads-down working stretch,
        # or interim forwarding missed it), deliver the final assistant message now. The
        # `_turn_text_sent` guard means this only fires when truly nothing was sent (no double-send),
        # and _send_final sets it True so a second _finish_turn won't re-fire.
        if (was_active and self._turn_from_tg and not self._turn_text_sent
                and not getattr(self, "_turn_is_reaction", False)
                and self._owner_chat is not None
                and getattr(self, "_turn_end_backstop_enabled", True)):
            source = "transcript"
            out = ""
            if self._has_turn_end_backstop_source():
                last = self._retry_last_assistant_text()
                out = self._strip_marker(last) if last else ""
                if not out and not self._turn_text_sent:
                    source = "signal"
                    answer = self._consume_signal_text()
                    out = self._strip_marker(answer) if answer else ""
            if (out or getattr(self, "_pending_files", None)) and not self._turn_text_sent:
                # Send WITH the message's dedup key. Without it the backstop and the normal
                # transcript path both queue the same text under different ids and the user
                # gets it twice — proven from the log on 2026-08-02 (ids 433fa145 / 52df94c3,
                # same text, same second). The key makes the queue recognise the duplicate.
                self._send_final(out, key=getattr(self, "_last_backstop_key", "") or None)
                log.info("TURN END backstop → forwarded final answer from %s %s",
                         source, message_metadata(out))
            elif not self._turn_text_sent:
                dur = time.monotonic() - self._turn_started
                log.error("TURN END backstop: Telegram turn ended without an answer; "
                          "dur=%.1fs typing_count=%d transcript_configured=%s "
                          "signal_configured=%s",
                          dur, self._typing_count, getattr(self, "_transcript", None) is not None,
                          getattr(self, "_signal", None) is not None)
        elif (was_active and self._turn_from_tg and not self._turn_text_sent
                and self._owner_chat is not None
                and not getattr(self, "_turn_is_reaction", False)):
            # Should be unreachable — the branch above covers it. Kept because on 2026-08-23 a
            # Telegram turn ended with no forward and NO log line at all, which made the cause
            # impossible to determine after the fact. Whatever silently skips the backstop must
            # leave a trace.
            log.error("TURN END: Telegram turn ended unanswered and the backstop did not run "
                      "(backstop_enabled=%s)", getattr(self, "_turn_end_backstop_enabled", True))
        elif (was_active and getattr(self, "_turn_is_reaction", False)
                and not self._turn_text_sent):
            # Intended silence, but it must still be visible in the log — otherwise a genuinely
            # lost reply would look exactly the same in the daily traffic analysis.
            log.info("TURN END reaction turn without a reply (backstop deliberately skipped)")
        self._turn_active.clear()
        self._group_end_confirmed = False
        self._pending_turn_end = False
        self._consume_turn_end()
        if was_active:
            # from_tg/text_sent are in the line on purpose: when a reply goes missing, these two
            # flags decide whether anything was even attempted, and reconstructing them
            # afterwards from the log was impossible (2026-08-23).
            log.info("TURN END t=%.2f dur=%.1fs typing_fired=%d max_gap=%.2fs "
                     "from_tg=%s text_sent=%s reaction=%s",
                     time.time(), time.monotonic() - self._turn_started,
                     self._typing_count, self._max_gap, from_tg_at_end, text_sent_at_end,
                     reaction_at_end)

    def _end_turn(self, *, confirmed: bool = True) -> None:
        # Claude Stop-hook path: catch anything written just before the hook fired, then finish.
        self._drain_transcript()
        if confirmed:
            self._finish_turn()
        else:
            self._finish_turn(confirmed=False)


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
                    started = getattr(self, "_turn_begun_at", 0.0)
                    if started and time.monotonic() - started < SUSPICIOUS_TURN_SECONDS:
                        log.warning("turn ending after %.1fs — suspiciously fast, a stale "
                                    "end-of-turn signal is the usual cause",
                                    time.monotonic() - started)
                    self._finish_turn()
                elif self._turn_end is not None and self._turn_end.exists():
                    self._claude_hook_ready = True
                    self._end_turn()
                elif self._turn_active.is_set() and time.monotonic() - self._last_activity > self._idle_limit():
                    # This branch used to just close the turn: the backstop didn't run, so the
                    # reply wasn't sent and not even a log line was left. It was one of three ways
                    # messages vanished without a trace (audit finding C). Turn end now goes
                    # through one shared path regardless of what triggered it; the warning
                    # distinguishes "ended in silence" from "the hook reported it".
                    log.warning("turn ended after inactivity (%.0f s without activity); no hook signal",
                                self._idle_limit())
                    if self.cfg.agent == 'claude-code':
                        self._claude_hook_ready = False
                    if self._interim_allowed(getattr(self, "_owner_chat", None)):
                        self._end_turn()
                    else:
                        self._end_turn(confirmed=False)
                self._expire_background_status()
                # Live retry: messages that failed to deliver are retried while running too.
                # Replay only at startup meant, for a long-running service, waiting forever.
                if time.monotonic() - getattr(self, "_last_inbound_retry", 0.0) > INBOUND_RETRY_INTERVAL:
                    self._last_inbound_retry = time.monotonic()
                    self._replay_pending_inbound()
                self._beat()                  # reached only on a full, non-blocking forward cycle
            except Exception as e:
                log.error("outbound error: %s", e)
            self._stop.wait(OUTBOUND_TICK)

    def _idle_limit(self) -> float:
        """Seconds of transcript silence after which a turn is force-ended. Long when a Stop hook
        marker is configured (the hook ends turns; silence is only the fallback), short otherwise."""
        return IDLE_DONE_HOOKED if getattr(self, "_turn_end", None) is not None else IDLE_DONE

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

    # ---- background progress (opt-in, Claude Code) ------------------------------------
    def _background_enabled(self) -> bool:
        cfg = getattr(self, "cfg", None)
        return (getattr(cfg, "background_progress", False) is True
                and getattr(cfg, "agent", "") == "claude-code")

    def _arm_background(self) -> None:
        """Remember where the transcript ends NOW. Records up to here are history (a startup
        rewind or a freshly adopted transcript replays them) and never produce a bubble."""
        t = getattr(self, "_transcript", None)
        try:
            size = t.stat().st_size if t is not None else 0
        except OSError:
            size = 0
        self._bg_arm = (t, size)

    def _record_is_live(self, offset: int, rec: dict) -> bool:
        """Is this record live work worth a background bubble? Appended after the bridge armed
        (not a replay) AND stamped recently (not an old record in a re-read file)."""
        if not self._background_enabled() or self._bg_arm is None:
            return False
        path, size = self._bg_arm
        if path != getattr(self, "_transcript", None) or offset < size:
            return False
        ts = _record_wall_time(rec)
        return ts is not None and abs(time.time() - ts) <= BACKGROUND_LIVE_WINDOW

    def _expire_background_status(self) -> None:
        """Safety net for a background bubble whose turn end was never signalled."""
        if not self._bg_status or self._turn_active.is_set():
            return
        limit = float(getattr(self.cfg, "background_stale_after", 300.0) or 300.0)
        if time.monotonic() - self._bg_status_last > limit:
            log.info("background status bubble silent for %.0f s → removed", limit)
            self._status_clear()

    def _start_background(self) -> None:
        """Remove an orphaned sub-agent progress message and, if enabled, start the watcher."""
        watcher = SubagentWatcher(
            self.tg, self._owner_chat,
            transcript=lambda: self._transcript,
            state_path=self._bg_state_path,
            language=getattr(self.cfg, "language", "en"),
            edit_interval=getattr(self.cfg, "background_edit_interval", 10.0),
            stale_after=getattr(self.cfg, "background_stale_after", 300.0))
        watcher.cleanup_orphan()             # even when disabled: a leftover from an earlier run
        if not self._background_enabled():
            return
        self._bg_watcher = watcher
        threading.Thread(target=self._background_loop, daemon=True).start()
        log.info("background progress on (sub-agents + non-Telegram turns)")

    def _background_loop(self) -> None:
        """Own thread: a flood-control wait on an edit must never stall reply forwarding."""
        while not self._stop.is_set():
            try:
                if self._interim_allowed(self._bg_watcher.chat_id):
                    self._bg_watcher.tick()
            except Exception as e:
                log.error("background progress error: %s", e)
            self._stop.wait(BACKGROUND_TICK)

    # ---- live tool-call status bubble (shown during the turn, deleted at the end) ------
    def _status_push(self, line: str, *, background: bool = False) -> None:
        if not self._interim_allowed(self._owner_chat):
            return
        # Single line, emoji at the start, rendered in italics. One bubble is edited in place
        # across a run of consecutive tool calls; it's deleted when the next progress message
        # arrives (then re-created below it) and at turn end — so it always trails at the bottom.
        if background:
            # A turn not started from Telegram (background-task notification, terminal). Only
            # live records get here (see _record_is_live), and such a bubble has two ways out:
            # the Stop-hook turn end, or _expire_background_status after a silence.
            self._bg_status = True
            self._bg_status_last = time.monotonic()
        if self._owner_chat is None or not line or line == self._status["shown"]:
            return
        # No live turn → no bubble. The bubble is technical progress and is deleted at turn end;
        # one created outside a turn has nothing to delete it and hangs in the chat until the NEXT
        # turn happens to finish. Seen in practice: "Editing MEMORY.md" stuck for eight
        # minutes after a bridge restart drained the transcript with no turn running (2026-08-02).
        # The background path is the one exception, and it is guarded against exactly that replay.
        if not self._turn_active.is_set() and not background:
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
            try:
                self.tg.delete_message(self._owner_chat, self._status["mid"])
            except Exception as e:
                log.warning("status bubble cleanup failed: %s", e)
        self._status = {"mid": None, "shown": ""}
        self._seen_tools.clear()
        self._bg_status = False
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
        answer = self._consume_signal_text()
        if not answer:
            return
        _, target = self._route_group_marker(answer)
        if (not self._interim_allowed(target)
                and not (self._turn_active.is_set() and self._turn_from_tg)):
            log.info("signal ignored outside a Telegram group turn chat=%s", self._owner_chat)
            return
        if answer and self._owner_chat is not None:
            self._group_reply = None
            self._pending_files = list(getattr(self, "_pending_files", []) or []) + list(self._group_files)
            self._group_files = ()
            self._status_clear()                         # final message → drop the technical bubble
            self._send_final(answer)                     # reliable: queue + retry on send failure
            self._turn_active.clear()

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
        off = self._tpos
        self._tpos += nl + 1
        try:
            for raw in chunk[:nl].split(b"\n"):
                rec_off = off
                off += len(raw) + 1
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._rec_live = self._record_is_live(rec_off, rec)
                for ev in self._reader.parse(rec):
                    self._handle_event(ev)
        finally:
            self._rec_live = False
        # Any new transcript content during a Telegram turn = the agent is still working; refresh
        # activity so the idle fallback doesn't fire prematurely.
        if self._turn_from_tg:
            self._last_activity = time.monotonic()

    # ---- outgoing files ------------------------------------------------------
    def _safe_outbox_path(self, raw: str):
        """Validate a path the AGENT asked to send. Returns the resolved path or a reason.

        The path comes from the agent's own reply text, so this is the security boundary:
        without it, anything able to influence the agent's output could make the bridge
        upload `~/.ssh/id_rsa`. Rules: must resolve inside an allowed directory (symlinks
        are followed BEFORE the check), must be a regular file, must be non-empty.
        """
        try:
            path = Path(raw).expanduser().resolve()
        except OSError as e:
            return None, f"cannot resolve ({e.__class__.__name__})"
        allowed = self.cfg.allowed_outbox_dirs()
        if not any(path == d or d in path.parents for d in allowed):
            return None, ("outside the allowed folders — put it in "
                          f"{self.cfg.path_outbox()} or add the folder to 'outbox_dirs'")
        if not path.is_file():
            return None, "not a regular file"
        if path.stat().st_size == 0:
            return None, "file is empty"
        return path, "ok"

    def _extract_files(self, text: str):
        """Pull `[tg-file] <path>` lines out of a reply. Returns (text without them, paths)."""
        marker = self.cfg.file_marker.lower()
        keep, wanted = [], []
        for ln in text.splitlines():
            s = ln.strip()
            if s.lower().startswith(marker):
                arg = s[len(self.cfg.file_marker):].strip().strip('"').strip("'")
                if arg:
                    wanted.append(arg)
                continue
            keep.append(ln)
        return "\n".join(keep).strip(), wanted

    def _send_files(self, paths: list[str]) -> None:
        """Upload the requested files. A rejection is always reported back to the chat —
        silently dropping an attachment would be worse than a visible error."""
        for raw in paths:
            resolved, reason = self._safe_outbox_path(raw)
            if resolved is None:
                log.warning("refusing to send %s: %s",
                            message_metadata(raw, kind="file", attachments=1), reason)
                self._send_final(f"⚠️ Couldn't send {Path(raw).name}: {reason}", turn_text=False)
                continue
            try:
                self.tg.send_file(self._owner_chat, resolved)
            except Exception as e:
                log.warning("file send failed (%s): %s", resolved.name, e)
                self._send_final(f"⚠️ Couldn't send {resolved.name}: {e}", turn_text=False)

    def _send_one_file(self, raw: str, chat: "int | None" = None) -> None:
        """Send one attachment from the durable outbox.

        Distinguishes two kinds of failure because they are handled oppositely:
        a path outside the allowed folders is a PERMANENT rejection — reported and treated as
        done, else it would clog the queue forever. A send error is TRANSIENT and is let out,
        so the outbox can retry it.
        """
        if chat is None:
            chat = self._owner_chat
        resolved, reason = self._safe_outbox_path(raw)
        if resolved is None:
            self._report_permanent_refusal(Path(raw).name, reason, chat)
            return
        try:
            self.tg.send_file(chat, resolved)
        except OSError as e:
            # The file vanished or can't be read — retrying won't fix it.
            self._report_permanent_refusal(resolved.name, str(e), chat)
        # Other errors (including TelegramError) are let out as transient. Classifying them
        # binarily proved a trap: an unclear error would either be dropped (message loss) or
        # retried forever (a clogged FIFO queue blocking every later reply — finding F2).
        # Instead of guessing, it is retried a BOUNDED number of times and then given up; the
        # outbox keeps the counter.

    def _report_permanent_refusal(self, name: str, reason: str,
                                  chat: "int | None" = None) -> None:
        """Report an attachment not worth retrying and treat it as done. A silent drop would be
        worse than a visible error — and a clogged queue worse still."""
        log.warning("refusing to send attachment %s: %s", name, reason)
        try:
            self.tg.send_message(chat if chat is not None else self._owner_chat, f"⚠️ Couldn't send {name}: {reason}")
        except Exception as e:
            log.warning("could not report the rejected attachment: %s", e)

    def _flush_files(self) -> None:
        """Upload whatever the last reply asked for. Kept separate from the text path so a
        failed upload can never block or duplicate the message itself."""
        files = getattr(self, "_pending_files", [])
        if not files:
            return
        self._pending_files = []
        self._send_files(files)

    def _has_marker(self, text: str) -> bool:
        """True when the FIRST non-blank line starts with the progress marker (case-insensitive).

        An empty marker means "no routing marker configured" everywhere else in this file (see the
        ``file_marker`` guards), so it must mean the same here. Without this guard every text would
        start with it, and EVERY answer of a terminal turn would be pushed to the user's phone."""
        marker = (self._marker or "").lower()
        if not marker:
            return False
        for ln in text.splitlines():
            s = ln.strip()
            if s:
                return s.lower().startswith(marker)
        return False

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
            #
            # A record WITHOUT the prefix may never downgrade a turn that is already marked as
            # Telegram-originated. The reader now filters tool results out, but anything else the
            # transcript files as a "user" record would otherwise silently reclassify a live turn
            # as local — and then the answer is never forwarded, the backstop never runs, and
            # nothing at all appears in the log. That happened on 2026-08-23. A new turn resets
            # the flag in _begin_turn; that is the only place it may go back to False.
            if ev.text.lstrip().startswith(self._origins):
                self._turn_from_tg = True
            elif not self._turn_active.is_set():
                self._turn_from_tg = False
            return
        if ev.kind == "turn_start":
            if not self._turn_active.is_set():
                self._turn_from_tg = False
            return                              # local TUI turns do not make Telegram inbound busy
        if ev.kind == "turn_end":
            self._pending_turn_end = True       # outbound loop finishes the turn after this drain
            return
        if self._owner_chat is None:
            return
        if ev.kind in ("tool", "files", "text"):
            self._group_reply = None  # a later tool invalidates a narration-only candidate
        if ev.kind == "tool" and not self._turn_from_tg and self._rec_live:
            # Live tool call of a turn NOT started from Telegram (opt-in background progress).
            if ev.key and ev.key not in self._seen_tools:
                self._seen_tools.add(ev.key)
                self._status_push(ev.text, background=True)
            return
        if not self._turn_from_tg:
            # A text that carries the routing marker is addressed to Telegram no matter what the
            # turn bookkeeping says: the flag is reconstructed from transcript records and has
            # been wrong before (a harness record with no prefix reset it on 2026-09-02 while the
            # answer to "?" was still being written; the answer then vanished silently).
            if ev.kind == "text" and self._has_marker(ev.text):
                log.info("text carries %s outside a Telegram turn → forwarded anyway", self._marker)
            else:
                if ev.kind == "text" and ev.text.strip() and ev.key not in self._sent_keys:
                    log.info("text NOT forwarded (turn not from Telegram) id=%s %s",
                             ev.key, message_metadata(ev.text))
                return
        if ev.kind == "text":
            out = self._strip_marker(ev.text)
            if out and ev.key not in self._sent_keys:
                _, target = self._route_group_marker(out)
                if not self._interim_allowed(target):
                    if self._turn_active.is_set() and self._turn_from_tg:
                        if (target == self._owner_chat and self.cfg.file_marker
                                and self.cfg.file_marker.lower() in out.lower()):
                            out, files = self._extract_files(out)
                            self._group_files = tuple(dict.fromkeys(self._group_files + tuple(files)))
                        if not ev.interim:
                            self._group_reply = (out, ev.key, ev.final)
                    return
                # A new progress message → delete the current technical bubble so the next tool
                # calls re-create it BELOW this message (the bubble always trails at the bottom).
                self._status_clear()
                # Reliable forward: the dedup ledger is marked only AFTER a confirmed send, and a
                # failed send is queued + retried — so a reply is never silently dropped.
                self._send_final(out, key=ev.key)
        elif ev.kind == "files":
            # The agent used its own file-sending tool. The harness runs that tool itself, so the
            # bridge never sees a call — only this record in the transcript. Deliver the files the
            # same way as a [tg-file] line, INCLUDING the allowlist check: the path comes from
            # agent output either way, so the security boundary must not differ.
            if ev.key in self._seen_tools:
                return                            # already handled (transcript re-read on resume)
            self._seen_tools.add(ev.key)
            if not self._interim_allowed(self._owner_chat):
                if self._turn_active.is_set() and self._turn_from_tg:
                    self._group_files = tuple(dict.fromkeys(self._group_files + tuple(ev.files)))
            else:
                self._send_files(list(ev.files))
        elif ev.kind == "tool":
            if self.cfg.agent == "codex":
                return                            # Codex tools come live from the TUI scraper
            if ev.key and ev.key not in self._seen_tools:
                self._seen_tools.add(ev.key)
                self._status_push(ev.text)

    # ---- media helpers (reuse the same download/STT as one-shot mode) ------
    def _reject_audio_too_large(self, chat_id: int, size: int | None) -> None:
        mb = (int(size or 0) + 1024 * 1024 - 1) // (1024 * 1024)
        self.tg.send_message(
            chat_id,
            f"🎤 That audio is ~{mb} MB. Voice/audio limit is 25 MB — "
            "please send a shorter clip or write it as text.",
        )

    def _limit_inbound_prompt(self, text: str, chat_id: int) -> str | None:
        if len(text) <= MAX_INBOUND_PROMPT_CHARS:
            return text
        self.tg.send_message(
            chat_id,
            f"⚠️ That message is too long after processing ({len(text)} characters). "
            f"Please shorten it to {MAX_INBOUND_PROMPT_CHARS} characters or less.",
        )
        log.warning("inbound prompt too long: %d characters", len(text))
        return None

    def _transcribe(self, media: dict, chat_id: int) -> str | None:
        from . import stt
        if not self.cfg.elevenlabs_api_key:
            self.tg.send_message(chat_id,
                "🎤 Voice transcription isn't enabled yet. Add your ElevenLabs key with "
                "`/setkey <your-key>` (I'll delete the message right after) — then resend the voice note.")
            return None
        try:
            size = int(media.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MAX_AUDIO_BYTES:
            self._reject_audio_too_large(chat_id, size)
            log.warning("voice/audio too large for STT: %s bytes", size)
            return None
        try:
            fp = self.tg.get_file_path(media["file_id"])
            audio = self.tg.download(fp)
            if len(audio) > MAX_AUDIO_BYTES:
                self._reject_audio_too_large(chat_id, len(audio))
                log.warning("downloaded voice/audio too large for STT: %s bytes", len(audio))
                return None
            return stt.transcribe(audio, api_key=self.cfg.elevenlabs_api_key,
                                  language=getattr(self.cfg, "elevenlabs_language", "") or None,
                                  filename=Path(fp).name or "voice.ogg")
        except stt.EmptyTranscript:
            # Silent or accidentally sent note: the user's side, not a bridge fault.
            log.warning("voice note had no speech")
            self._send_final(
                "🎤 I couldn't hear any speech in that voice note. Please record it again.",
                turn_text=False,
            )
            return None
        except Exception as e:
            log.error("transcription failed: %s", e)
            # This is an inbound voice-note failure, not agent output for the current turn.
            self._send_final(
                f"🎤 Voice transcription failed ({e}). "
                "Please resend it or type your message instead.",
                turn_text=False,
            )
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
        if dest.exists():
            i = 1
            while (cand := d / f"{dest.stem}-{i}{dest.suffix}").exists():
                i += 1
            dest = cand
        dest.write_bytes(data)
        log.info("saved attachment -> %s (%d bytes)", dest, len(data))
        return f"[The user attached a file saved at: {dest} — open and use it as appropriate.]"
