"""Persistent agent sessions over **tmux** — the same approach as a hand-rolled bridge that
drives a live TUI, generalized into a product.

Why tmux: it keeps the agent's *interactive* session alive, so full context, loaded tools
and working state persist across messages — exactly like talking to it in a terminal. It
works for any agent that has an interactive CLI (Claude Code, Codex, …).

Inbound (proven send-keys sequence): clear the prompt line (``C-u``), type the message
literally (``send-keys -l --``), then submit (``Enter``). Newlines are collapsed so a single
Enter submits the whole message.

Completion + response — two strategies:
  * **Hook** (robust, used for Claude Code): the agent runs a Stop hook at end of turn that
    writes the final answer to a per-session signal file; the bridge waits for it. This is
    authoritative (reads the transcript, not the screen). Set up by the installer.
  * **Idle** (universal fallback): poll ``capture-pane`` and treat the turn as done once the
    output has been stable for ``idle`` seconds. Good enough for agents without a hook.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

log = logging.getLogger("agent2telegram.session")

# TUI chrome filters for the idle (screen-scraping) path. capture-pane gives plain text
# (no ANSI), so we only strip recognizable decoration lines and leading bullet markers.
_SEP_RE = re.compile(r"^[\s─━—–_=·.\-]{6,}$")
_STATUS_RE = re.compile(r"^[✻✶✳✢✺✷*]\s")           # spinner/status, e.g. "✻ Worked for 1s"
_BULLET_RE = re.compile(r"^\s*[⏺●○•]\s?")           # assistant output bullet


def _clean_tui(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if _SEP_RE.match(s):
            continue
        if s.lstrip().startswith("❯") or s.lstrip().startswith(">"):   # prompt / input echo
            continue
        if _STATUS_RE.match(s.lstrip()):
            continue
        out.append(_BULLET_RE.sub("", s))
    return "\n".join(out).strip()


class SessionError(Exception):
    pass


def _tmux(*args: str, check: bool = True, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check, timeout=timeout)


class TmuxSession:
    """A live agent running in a detached tmux session, fed via send-keys."""

    def __init__(self, agent_argv: list[str], *, cwd: Path, name: str | None = None,
                 timeout: int = 600, idle: float = 1.5, settle: float = 0.4,
                 origin_prefix: str = "", signal_file: Path | None = None,
                 boot_wait: float = 2.0) -> None:
        if shutil.which("tmux") is None:
            raise SessionError("tmux is not installed. Install tmux (e.g. `brew install tmux` "
                               "or `apt install tmux`) — the persistent session needs it.")
        self.name = name or ("a2t_" + uuid.uuid4().hex[:10])
        self._timeout = timeout
        self._idle = idle
        self._settle = settle
        self._origin = origin_prefix
        self._signal = signal_file
        cwd.mkdir(parents=True, exist_ok=True)
        if not self._exists():
            if not agent_argv:
                # Attach mode: we only drive an existing session, never create one.
                raise SessionError(f"tmux session '{self.name}' does not exist (attach mode).")
            _tmux("new-session", "-d", "-s", self.name, "-x", "220", "-y", "50", *agent_argv,
                  timeout=15)
            time.sleep(boot_wait)   # let the TUI come up before the first message

    # ---- lifecycle ---------------------------------------------------------
    def _exists(self) -> bool:
        return subprocess.run(["tmux", "has-session", "-t", self.name],
                              capture_output=True).returncode == 0

    @property
    def alive(self) -> bool:
        return self._exists()

    def close(self) -> None:
        _tmux("kill-session", "-t", self.name, check=False)

    # ---- messaging ---------------------------------------------------------
    def _send_keys(self, text: str) -> None:
        text = " ".join(text.splitlines())                 # one Enter submits everything
        if self._origin:
            text = f"{self._origin}{text}"
        _tmux("send-keys", "-t", self.name, "C-u"); time.sleep(0.05)
        _tmux("send-keys", "-t", self.name, "-l", "--", text); time.sleep(0.15)
        _tmux("send-keys", "-t", self.name, "Enter")

    def _capture(self) -> str:
        return _tmux("capture-pane", "-p", "-t", self.name, check=False).stdout

    def inject(self, text: str) -> None:
        """Fire-and-forget: type the message into the session, don't wait for a reply.
        Used by the async attach bridge (outbound is handled separately)."""
        if not self.alive:
            raise SessionError(f"agent session '{self.name}' is gone")
        self._send_keys(text)

    def send(self, text: str) -> str:
        if not self.alive:
            raise SessionError(f"agent session '{self.name}' is gone")
        if self._signal is not None:
            return self._send_with_hook(text)
        return self._send_with_idle(text)

    def _send_with_hook(self, text: str) -> str:
        """Authoritative completion: a Stop hook writes the final answer to the signal file."""
        try:
            self._signal.unlink()                          # clear any stale answer
        except FileNotFoundError:
            pass
        self._send_keys(text)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if self._signal.exists():
                answer = self._signal.read_text("utf-8")
                try:
                    self._signal.unlink()
                except OSError:
                    pass
                return answer.strip()
            time.sleep(0.3)
        raise SessionError(f"agent timed out after {self._timeout}s")

    def _send_with_idle(self, text: str) -> str:
        before = self._capture()
        self._send_keys(text)
        deadline = time.monotonic() + self._timeout
        last, stable_since = "", 0.0
        while time.monotonic() < deadline:
            time.sleep(self._settle)
            cur = self._capture()
            if cur != last:
                last, stable_since = cur, time.monotonic()
                continue
            if stable_since and (time.monotonic() - stable_since) >= self._idle and cur != before:
                return self._delta(before, cur)
        raise SessionError(f"agent timed out after {self._timeout}s")

    @staticmethod
    def _delta(before: str, after: str) -> str:
        b, a = before.splitlines(), after.splitlines()
        i = 0
        while i < len(b) and i < len(a) and b[i] == a[i]:
            i += 1
        return _clean_tui("\n".join(a[i:]))
