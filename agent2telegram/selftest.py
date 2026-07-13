"""End-to-end self-test for attach mode — run the whole Telegram experience against a *real*
agent, with a fake Telegram so it needs no bot and touches nothing live.

For the chosen agent it spins up a throwaway tmux session, launches the agent CLI in a temp dir,
then drives a series of checks through the real bridge:

  1. **text**       — a message is injected (real send-keys) and the reply is forwarded back.
  2. **reaction**   — a ❤️ reaction is delivered to the agent as a feedback line.
  3. **multi-step** — a task that runs shell commands produces tool-call status bubbles + a reply.
  4. **voice**      — a voice message is transcribed (STT stubbed for determinism) and injected.

Run it yourself anytime::

    python3 -m agent2telegram selftest --agent codex
    python3 -m agent2telegram selftest --agent claude-code

It asserts the bridge's outbound behavior (forwarded messages, the live status bubble, the
typing indicator, turn end) without sending anything to a real chat.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import adapters, readers
from .config import Config


# --------------------------------------------------------------------------- fakes
class FakeTelegram:
    """Records every call the bridge would make to Telegram; provides voice file stubs."""

    def __init__(self) -> None:
        self.sends: list[str] = []
        self.bubble_creates: list[str] = []
        self.bubble_edits: list[str] = []
        self.bubble_deletes = 0
        self.typing = 0
        self._mid = 1000

    def get_me(self):
        return {"username": "selftest_bot"}

    def send_message(self, chat_id, text, **k):
        self.sends.append(text)

    def send_plain_id(self, chat_id, text, **k):
        self._mid += 1
        self.bubble_creates.append(text)
        return self._mid

    def edit_plain(self, chat_id, message_id, text, **k):
        self.bubble_edits.append(text)

    def delete_message(self, chat_id, message_id):
        self.bubble_deletes += 1

    def send_chat_action(self, chat_id, action="typing"):
        self.typing += 1

    # voice plumbing
    def get_file_path(self, file_id):
        return "voice/test.ogg"

    def download(self, file_path, **k):
        return b"FAKE_OGG_BYTES"


# --------------------------------------------------------------------------- harness
def _tmux(*args, check=True, timeout=10):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check, timeout=timeout)


def _capture(session: str) -> str:
    return subprocess.run(["tmux", "capture-pane", "-p", "-t", session],
                          capture_output=True, text=True).stdout


def _launch_agent(agent_cls, session: str, workdir: Path) -> bool:
    """Start the agent CLI in a fresh detached tmux session, dismissing first-run prompts."""
    _tmux("kill-session", "-t", session, check=False)
    _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", str(workdir))
    _tmux("send-keys", "-t", session, agent_cls.binary, "Enter")
    # Give the TUI time to boot and clear any "trust this directory?" / onboarding prompt.
    for _ in range(24):
        time.sleep(1)
        pane = _capture(session).lower()
        if "update available!" in pane and "press enter to continue" in pane:
            _tmux("send-keys", "-t", session, "2", "Enter", check=False)
            time.sleep(1)
            continue
        if "trust" in pane or "yes, proceed" in pane:
            _tmux("send-keys", "-t", session, "Enter", check=False)
        if "›" in _capture(session) or "❯" in _capture(session):
            time.sleep(2)
            return True
    return True   # best effort; the first message will surface any problem


def _bridge(agent: str, session: str) -> tuple:
    fake = FakeTelegram()
    cfg = Config(token="1:x", allowed_user_ids=[1], agent=agent, mode="attach",
                 tmux_session=session, signal_file=str(Path(tempfile.mkdtemp()) / "answer.txt"),
                 transcript_path="auto", origin_prefix="[TG] ", progress_marker="[TG]",
                 elevenlabs_api_key="stub")
    from .attach import AttachBridge
    b = AttachBridge(cfg, client=fake)
    b._sent_keys = set()
    b._sent_path = Path(tempfile.mktemp())     # isolated ledger
    b._owner_chat = 1
    if b._transcript and b._transcript.exists():
        b._tpos = b._transcript.stat().st_size
    return b, fake


def _msg(text: str) -> dict:
    return {"update_id": 1, "message": {"message_id": 11, "from": {"id": 1},
                                        "chat": {"id": 1}, "text": text}}


def _reaction() -> dict:
    return {"update_id": 2, "message_reaction": {"user": {"id": 1}, "message_id": 11,
            "new_reaction": [{"type": "emoji", "emoji": "❤️"}]}}


def _voice() -> dict:
    return {"update_id": 3, "message": {"message_id": 12, "from": {"id": 1},
            "chat": {"id": 1}, "voice": {"file_id": "vx", "duration": 2}}}


def _drive(bridge, until, timeout: float) -> bool:
    """Pump the outbound loop until *until()* is true or *timeout* elapses."""
    from .attach import _extract_tui_tools
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        bridge._maybe_reresolve()
        bridge._drain_transcript()
        # Codex tool bubbles come from the live TUI scraper, not the transcript — pump it here
        # so the end-to-end test actually exercises that path (and catches Codex layout drift).
        if bridge.cfg.agent == "codex" and bridge._turn_active.is_set():
            for summary in _extract_tui_tools(bridge._session._capture()):
                if summary not in bridge._tui_seen:
                    bridge._tui_seen.add(summary)
                    bridge._status_push(summary)
        if bridge._pending_turn_end:
            bridge._finish_turn()
        if until():
            return True
        time.sleep(0.5)
    return False


def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# --------------------------------------------------------------------------- checks
def _run_checks(b, fake, agent: str) -> list[bool]:
    results = []

    # 1) text round-trip
    print("\n[1/4] text round-trip", flush=True)
    n = len(fake.sends)
    b._handle(_msg("Reply with exactly the word PONG and nothing else."))
    got = _drive(b, lambda: len(fake.sends) > n, timeout=90)
    results.append(_check("reply forwarded", got, fake.sends[-1][:40] if got else "no reply in 90s"))
    results.append(_check("typing indicator fired", fake.typing > 0, f"{fake.typing} actions"))

    # 2) reaction → feedback line injected into the session
    print("\n[2/4] reaction (❤️)", flush=True)
    injected = []
    orig = b._session.inject
    b._session.inject = lambda t: (injected.append(t), orig(t))[1]
    try:
        b._handle(_reaction())
    finally:
        b._session.inject = orig
    results.append(_check("reaction delivered to agent", any("react" in t for t in injected),
                          injected[-1][:40] if injected else "nothing injected"))

    # 3) multi-step task → tool-call bubble(s) + reply
    print("\n[3/4] multi-step (shell tool calls)", flush=True)
    nb, ns = len(fake.bubble_creates), len(fake.sends)
    b._handle(_msg("Run the commands `echo one` and `echo two`, then tell me what they printed."))
    got = _drive(b, lambda: len(fake.bubble_creates) > nb and len(fake.sends) > ns, timeout=120)
    results.append(_check("tool-call bubble shown", len(fake.bubble_creates) > nb,
                          f"{len(fake.bubble_creates) - nb} bubble(s)"))
    results.append(_check("final reply forwarded", len(fake.sends) > ns,
                          fake.sends[-1][:40] if len(fake.sends) > ns else "no reply"))

    # 4) voice transcription (STT stubbed) → injected
    print("\n[4/4] voice transcription", flush=True)
    from . import stt
    real_stt = stt.transcribe
    stt.transcribe = lambda *a, **k: "TRANSCRIBED_OK list the current directory"
    vinj = []
    orig2 = b._session.inject
    b._session.inject = lambda t: (vinj.append(t), orig2(t))[1]
    try:
        b._handle(_voice())
    finally:
        b._session.inject = orig2
        stt.transcribe = real_stt
    results.append(_check("voice transcribed & injected",
                          any("TRANSCRIBED_OK" in t for t in vinj),
                          vinj[-1][:40] if vinj else "nothing injected"))
    return results


def run(agent: str = "codex", keep: bool = False) -> int:
    cls = adapters.REGISTRY.get(agent)
    if cls is None or agent not in ("codex", "claude-code"):
        print(f"✗ selftest supports 'codex' and 'claude-code' (got {agent!r}).", file=sys.stderr)
        return 2
    if not cls.detect():
        print(f"✗ '{cls.binary}' not found on PATH — install {cls.label} first.", file=sys.stderr)
        return 2
    if readers.for_agent(agent).name != agent:
        print(f"✗ no transcript reader for {agent}.", file=sys.stderr)
        return 2

    session = f"a2t-selftest-{uuid.uuid4().hex[:6]}"
    workdir = Path(tempfile.mkdtemp(prefix="a2t-selftest-"))
    print(f"=== Agent2Telegram self-test · {cls.label} ===")
    print(f"session={session}  workdir={workdir}")
    print("launching agent…", flush=True)
    try:
        _launch_agent(cls, session, workdir)
        b, fake = _bridge(agent, session)
        if not b._session.alive:
            print("✗ agent session didn't start.", file=sys.stderr)
            return 1
        results = _run_checks(b, fake, agent)
    finally:
        if not keep:
            _tmux("kill-session", "-t", session, check=False)
            subprocess.run(["rm", "-rf", str(workdir)], check=False)

    passed, total = sum(results), len(results)
    print(f"\n=== {passed}/{total} checks passed for {cls.label} ===")
    return 0 if passed == total else 1


if __name__ == "__main__":   # pragma: no cover
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "codex"))
