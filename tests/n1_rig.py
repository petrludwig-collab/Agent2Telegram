"""Shared fixtures for the shared-session (N1) tests: real AttachBridge, real TmuxSession text assembly,
fake Telegram client, synthetic transcript records, a real Stop-hook entrypoint. No network, no tmux,
no real config: HOME and the state directory are temporary, `_tmux` is a recorder."""
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent2telegram import attach as attach_mod
from agent2telegram import session as session_mod
from agent2telegram import stop_hook
from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config
from agent2telegram.session import TmuxSession

OWNER = 7
GROUP = -500


class FakeClient:
    def __init__(self):
        self.sent = []
        self.actions = []

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def delete_message(self, chat_id, message_id):
        pass

    def send_plain_id(self, chat_id, text, parse_mode=None):
        return 1

    def edit_plain(self, chat_id, message_id, text, parse_mode=None):
        pass


class TypedSession(TmuxSession):
    """The REAL _send_keys/inject text assembly with the tmux calls recorded instead of executed."""
    alive = True

    def __init__(self, origin, typed):
        self.name = "a2t"
        self._origin = origin
        self._typed = typed
        self._enter_verify_timeout = 0.1

    def _pane_ok(self):
        return True, ""

    def _verify_submission(self, text):
        pass


def human(text, uuid, *, kind="human"):
    return {"type": "user", "uuid": uuid, "origin": {"kind": kind}, "promptSource": "typed",
            "message": {"role": "user", "content": text}}


def launch(task, uuid, *, agent=False, is_async=True):
    result = {"agentId": task, "isAsync": is_async} if agent else {"backgroundTaskId": task}
    return {"type": "user", "uuid": uuid, "toolUseResult": result,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}


def notification(task, uuid, *, kind="task-notification", content=None):
    text = content if content is not None else (
        f"<task-notification>\n<task-id>{task}</task-id>\n<status>completed</status>\n</task-notification>")
    return {"type": "user", "uuid": uuid, "origin": {"kind": kind}, "promptSource": "system",
            "message": {"role": "user", "content": text}}


def assistant(text, uuid):
    return {"type": "assistant", "uuid": uuid,
            "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}}


def message(chat, text, *, user=OWNER, mid=1):
    kind = "supergroup" if chat < 0 else "private"
    return {"message": {"message_id": mid, "from": {"id": user}, "chat": {"id": chat, "type": kind},
                        "text": text}}


class Rig:
    """One bridge over temporary state. ``new_bridge()`` simulates a restart over the same state."""

    def __init__(self, tmp: Path, *, on=True, **cfg_overrides):
        self.tmp = tmp
        self.home = tmp / "home"
        self.state = tmp / "state"
        self.home.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        self.transcript = tmp / "session-1.jsonl"
        self.transcript.write_text(json.dumps(human("history", "h0")) + "\n", encoding="utf-8")
        self.typed = []
        self.uuid = 0
        self.cfg_args = dict(
            agent="claude-code", token="1:2", allowed_user_ids=[OWNER], tmux_session="a2t", mode="attach",
            signal_file=str(self.state / "answer.txt"), transcript_path=str(self.transcript),
            reply_to_origin_chat=True, allowed_group_ids=[GROUP], origin_prefix="Telegram:",
            progress_marker="[tg]", shared_session_delivery=on)
        self.cfg_args.update(cfg_overrides)
        self.client = FakeClient()
        self.bridge = None
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "AGENT2TELEGRAM_STATE": str(self.state)})
        self.env.start()
        self.tmux = patch.object(session_mod, "_tmux", side_effect=self._tmux)
        self.tmux.start()
        self.new_bridge()

    def close(self):
        self.tmux.stop()
        self.env.stop()

    def _tmux(self, *args, **kwargs):
        if args[:1] == ("send-keys",) and "-l" in args:
            self.typed.append(args[-1])
        return type("R", (), {"stdout": "", "returncode": 0})()

    def new_bridge(self, **cfg_overrides):
        args = dict(self.cfg_args, **cfg_overrides)
        cfg = Config(**args)
        cfg.validate()
        client = self.client
        typed = self.typed
        with patch.object(attach_mod, "TmuxSession",
                          side_effect=lambda *a, **k: TypedSession(k.get("origin_prefix", ""), typed)):
            self.bridge = AttachBridge(cfg, client=client)
        self.bridge._n1_run = lambda fn: fn()
        for name in ("_maybe_reresolve", "_replay_pending_inbound"):
            setattr(self.bridge, name, lambda *a, **k: None)
        if self.bridge._n1 is not None:
            self.bridge._n1_startup()
        else:
            self.bridge._tpos = self.transcript.stat().st_size
        return self.bridge

    # ---- transcript -------------------------------------------------------------
    def next_uuid(self, prefix="u"):
        self.uuid += 1
        return f"{prefix}{self.uuid}"

    def append(self, *records):
        with self.transcript.open("a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def drain(self):
        self.bridge._drain_transcript()

    def submit(self, text=None):
        """The harness records what was typed (default: the last injected prompt) as a human record."""
        self.append(human(self.typed[-1] if text is None else text, self.next_uuid("h")))
        self.drain()

    def terminal(self, text="please look at the logs"):
        self.append(human(text, self.next_uuid("term")))
        self.drain()

    def tg(self, chat, text="do the thing", **kw):
        return self.bridge._handle(message(chat, text, **kw))

    def tg_turn(self, chat, text="do the thing"):
        """A Telegram message, typed into the session, recorded by the harness, drained."""
        assert self.tg(chat, text) is True
        self.submit()

    def launch(self, task, **kw):
        self.append(launch(task, self.next_uuid("l"), **kw))
        self.drain()

    def notify(self, task, **kw):
        self.append(notification(task, self.next_uuid("n"), **kw))
        self.drain()

    def say(self, text="[tg] working on it"):
        self.append(assistant(text, self.next_uuid("a")))
        self.drain()

    def stop(self):
        """Claude Code's Stop hook, through the real hook entrypoint."""
        payload = json.dumps({"transcript_path": str(self.transcript)})
        cfg = {"agent": "claude-code", "signal_file": str(self.bridge._signal)}
        with patch.object(stop_hook, "_all_cfgs", return_value=[cfg]), \
                patch.object(stop_hook, "_own_tmux_session", return_value=""), \
                patch("sys.stdin", io.StringIO(payload)):
            stop_hook.main()

    def tick(self):
        """One real outbound-loop iteration (drain, flush, turn end, claims)."""
        b = self.bridge
        b._stop.clear()
        with patch.object(b._stop, "wait", side_effect=lambda _: b._stop.set()):
            b._outbound_loop()

    def end_turn(self):
        self.stop()
        self.tick()
        self.tick()                      # a final enqueued while the turn ended is flushed by the next cycle

    def sent_to(self, chat):
        return [t for c, t in self.client.sent if c == chat]

    @property
    def n1(self):
        return self.bridge._n1

    def job(self, task):
        return self.n1.snapshot()["jobs"].get(task, {}).get("state")


class RigTestCase(unittest.TestCase):
    ON = True

    def setUp(self):
        self.tmpdir = TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.rig = Rig(Path(self.tmpdir.name), on=self.ON)
        self.addCleanup(self.rig.close)
        self.b = self.rig.bridge
        self.client = self.rig.client
        # the real Stop hook reads configs: keep it away from the real config directory
        self.assertFalse(os.environ["HOME"].startswith("/home/"))
