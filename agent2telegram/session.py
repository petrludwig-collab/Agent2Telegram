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
import shlex
import shutil
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

log = logging.getLogger("agent2telegram.session")

# TUI chrome filters for the idle (screen-scraping) path. capture-pane gives plain text
# (no ANSI), so we only strip recognizable decoration lines and leading bullet markers.
_SEP_RE = re.compile(r"^[\s─━—–_=·.\-]{6,}$")
_STATUS_RE = re.compile(r"^[✻✶✳✢✺✷*]\s")           # spinner/status, e.g. "✻ Worked for 1s"
_BULLET_RE = re.compile(r"^\s*[⏺●○•]\s?")           # assistant output bullet

MAX_TMUX_INJECTION_CHARS = 8000
# Above this size the text goes in as ONE bracketed paste (tmux load-buffer + paste-buffer -p) instead
# of a literal send-keys burst. Measured 2026-09-09 against Claude Code: a 1.7 KB message typed with
# `send-keys -l` reached the agent WITHOUT its first ~1 KB (the routing prefix included) – the TUI's
# paste detection swallowed the first pty read. A bracketed paste arrives whole; so does chunked typing,
# but the paste is one atomic operation and keeps the TUI's own "[Pasted text]" handling.
PASTE_MIN_BYTES = 600
ENTER_VERIFY_INTERVAL = 0.2
ENTER_RETRY_TIMEOUT = 2.0
_INPUT_PROMPT_RE = re.compile(r"^\s*[❯›>] ?(.*)$")
_SHELL_COMMANDS = {
    "ash", "bash", "csh", "dash", "fish", "ksh", "mksh", "pwsh", "sh", "tcsh", "zsh",
}
_AGENT_WRAPPERS = {
    "bun", "deno", "node", "nodejs", "npm", "npx", "pnpm", "python", "python3", "uv",
    "uvx", "yarn",
}


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


def sanitize_for_tmux(text: str) -> str:
    """Drop terminal control bytes that tmux would otherwise pass to the live TUI."""
    out = []
    for ch in text:
        codepoint = ord(ch)
        if (
            ch not in "\n\t"
            and (codepoint < 0x20 or codepoint == 0x7f or 0x80 <= codepoint <= 0x9f)
        ):
            continue
        out.append(ch)
        if len(out) >= MAX_TMUX_INJECTION_CHARS:
            break
    return "".join(out)


def _input_contains_tail(pane: str, text: str) -> bool:
    """Match only the last input prompt, ignoring whitespace and hard wrapping.

    A submitted message can remain visible in the conversation above an empty
    prompt. Searching the entire pane would submit it a second time.
    """
    tail = "".join(text.split())[-24:]
    if not tail:
        return False
    lines = pane.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        match = _INPUT_PROMPT_RE.match(lines[index])
        if match:
            block = [match.group(1)]
            for line in lines[index + 1:]:
                if _SEP_RE.match(line) or line.lstrip().startswith("? for "):
                    break
                if not line.strip():
                    continue
                if (_STATUS_RE.match(line.lstrip()) or _BULLET_RE.match(line)
                        or not line[:1].isspace()):
                    return False
                block.append(line)
            return tail in "".join("".join(block).split())
    return False


def _command_name(command: str) -> str:
    return Path(str(command).strip()).name.lower().lstrip("-")


def _argv0(args: str) -> str:
    if not args:
        return ""
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = str(args).split()
    return parts[0] if parts else ""


def _command_names(commands: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen = set()
    for command in commands:
        name = _command_name(command)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _mentions_expected_agent(args: str, expected: tuple[str, ...]) -> bool:
    low = (args or "").lower()
    for name in expected:
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", low):
            return True
    return False


def _process_command_names(command: str, args: str) -> tuple[str, ...]:
    out: list[str] = []
    seen = set()
    for value in (command, _argv0(args)):
        name = _command_name(value)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _process_matches_agent(command: str, args: str, expected: tuple[str, ...]) -> bool:
    names = _process_command_names(command, args)
    if any(name in expected for name in names):
        return True
    return any(name in _AGENT_WRAPPERS for name in names) and _mentions_expected_agent(args, expected)


def _process_shell_name(command: str, args: str) -> str:
    for name in _process_command_names(command, args):
        if name in _SHELL_COMMANDS:
            return name
    return ""


def _pane_value(target: str, fmt: str) -> str:
    res = _tmux("display-message", "-p", "-t", target, fmt, check=False, timeout=3)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def _pane_processes(target: str) -> list[tuple[int, int, str, str]]:
    pid_s = _pane_value(target, "#{pane_pid}")
    if not pid_s.isdigit():
        return []
    root_pid = int(pid_s)
    try:
        proc = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm=,args="],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    rows: dict[int, tuple[int, int, str, str]] = {}
    children: dict[int, list[int]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        command = parts[2]
        args = parts[3] if len(parts) > 3 else command
        rows[pid] = (pid, ppid, command, args)
        children.setdefault(ppid, []).append(pid)

    found: list[tuple[int, int, str, str]] = []
    stack = [root_pid]
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        row = rows.get(pid)
        if row:
            found.append(row)
        stack.extend(children.get(pid, ()))
    return found


def _agent_alive(target: str, expected_commands: list[str] | tuple[str, ...] | set[str]) -> tuple[bool, str]:
    expected = _command_names(expected_commands)
    if not expected:
        return True, "no expected agent command configured"

    current = _pane_value(target, "#{pane_current_command}")
    current_name = _command_name(current)
    expected_s = ", ".join(expected)
    if current_name in expected:
        return True, f"tmux pane command is {current_name}"

    # Security guard: send-keys must only target a live agent TUI. tmux may report a
    # stale title or a login shell as the pane command, so first search the pane's full
    # process subtree for the configured agent binary by basename.
    processes = _pane_processes(target)
    for _pid, _ppid, command, args in processes:
        if _process_matches_agent(command, args, expected):
            return True, f"tmux pane process is {_command_name(command)}"

    shell_name = current_name if current_name in _SHELL_COMMANDS else ""
    if not shell_name and len(processes) == 1:
        _pid, _ppid, command, args = processes[0]
        shell_name = _process_shell_name(command, args)
    if shell_name:
        return False, f"tmux pane is at a shell prompt ({shell_name}); expected {expected_s}"

    detail = current_name or "unknown"
    return False, f"tmux pane command is {detail}; expected {expected_s}"


class SessionError(Exception):
    pass


def _tmux(*args: str, check: bool = True, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check, timeout=timeout)


class TmuxSession:
    """A live agent running in a detached tmux session, fed via send-keys."""

    def __init__(self, agent_argv: list[str], *, cwd: Path, name: str | None = None,
                 timeout: int = 600, idle: float = 1.5, settle: float = 0.4,
                 origin_prefix: str = "", signal_file: Path | None = None,
                 boot_wait: float = 2.0,
                 enter_verify_timeout: float = 8.0,
                 expected_agent_commands: list[str] | tuple[str, ...] | None = None) -> None:
        if shutil.which("tmux") is None:
            raise SessionError("tmux is not installed. Install it first: `sudo apt install tmux` (Debian/Ubuntu) or `brew install tmux` (macOS) — "
                               "or `apt install tmux`) — the persistent session needs it.")
        self.name = name or ("a2t_" + uuid.uuid4().hex[:10])
        self._timeout = timeout
        self._idle = idle
        self._settle = settle
        self._origin = origin_prefix
        self._signal = signal_file
        self._enter_verify_timeout = enter_verify_timeout
        self._expected_agent_commands = tuple(expected_agent_commands or (agent_argv[:1] if agent_argv else ()))
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
    def _pane_ok(self) -> tuple[bool, str]:
        return _agent_alive(self.name, getattr(self, "_expected_agent_commands", ()))

    def _send_keys(self, text: str) -> None:
        ok, detail = self._pane_ok()
        if not ok:
            # Fail closed: if the agent has crashed back to a shell, Enter would execute the
            # Telegram message as a shell command instead of submitting it to the agent TUI.
            log.warning("refusing tmux injection into '%s': %s", self.name, detail)
            raise SessionError(f"refusing to inject into tmux session '{self.name}': {detail}")
        text = sanitize_for_tmux(text)
        text = " ".join(text.splitlines())                 # one Enter submits everything
        if self._origin:
            text = f"{self._origin}{text}"
        text = sanitize_for_tmux(text)
        _tmux("send-keys", "-t", self.name, "C-u"); time.sleep(0.05)
        if len(text.encode("utf-8")) > PASTE_MIN_BYTES:
            self._paste(text); time.sleep(0.3)
        else:
            _tmux("send-keys", "-t", self.name, "-l", "--", text); time.sleep(0.15)
        _tmux("send-keys", "-t", self.name, "Enter")
        self._verify_submission(text)

    def _wait_for_submission(self, text: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        # Let the TUI render before the first capture. This runs in the same
        # inbound worker as send-keys, leaving polling and outbound work free.
        time.sleep(min(ENTER_VERIFY_INTERVAL, timeout / 2))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            pane = _tmux("capture-pane", "-p", "-t", self.name,
                         timeout=min(1.0, remaining)).stdout
            if not _input_contains_tail(pane, text):
                return True
            # Do not start a new capture after the deadline: an artificial tiny
            # subprocess timeout there would hide the stuck input instead of retrying.
            time.sleep(min(ENTER_VERIFY_INTERVAL, max(0.0, deadline - time.monotonic())))

    def _verify_submission(self, text: str) -> None:
        try:
            if self._wait_for_submission(text, getattr(self, "_enter_verify_timeout", 8.0)):
                return
            # The agent may have exited during the wait. Never send Enter to a shell.
            ok, _detail = self._pane_ok()
            if not ok:
                log.error("enter_not_submitted session=%s reason=unsafe_pane", self.name)
                return
            _tmux("send-keys", "-t", self.name, "Enter")
            log.warning("enter_resend session=%s", self.name)
            if not self._wait_for_submission(text, ENTER_RETRY_TIMEOUT):
                log.error("enter_not_submitted session=%s", self.name)
        except Exception as error:
            # Verification must not trigger a full reinjection or reset turn tracking.
            # Exception text can contain message content, so log only its type.
            log.error("enter_not_submitted session=%s error=%s", self.name, type(error).__name__)

    def _paste(self, text: str) -> None:
        """Deliver *text* as one bracketed paste (ESC[200~ … ESC[201~) through a private tmux buffer.
        The TUI then treats it as a single paste and keeps it whole (see PASTE_MIN_BYTES)."""
        buf = f"a2t-{os.getpid()}-{time.monotonic_ns()}"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".a2t", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            _tmux("load-buffer", "-b", buf, path)
            _tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", self.name)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

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
