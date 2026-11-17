"""explicit_only_outbound: every passive outbound path is blocked, service replies and the
explicit notify CLI keep working, default behaviour is unchanged, a pending queue is never
touched. Real entrypoints, synthetic fixtures, a network tripwire; no bot, tmux or real state.

Test names carry the row of the path inventory in DESIGN.md (row1 ... row14).
"""
import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent2telegram import attach as attach_mod
from agent2telegram import outbound_policy as op
from agent2telegram.attach import AttachBridge, BOT_COMMANDS
from agent2telegram.config import Config, ConfigError, load
from agent2telegram.session import SessionError

CONTENT = {"send_message", "send_file", "send_voice", "send_plain_id", "edit_plain",
           "send_chat_action", "delete_message"}


class Recorder:
    """Stands in for TelegramClient. Records every call; never touches the network."""

    def __init__(self):
        self.calls = []
        self.fail_file_path = False

    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))

    def get_me(self): self._rec("get_me"); return {"username": "bot"}

    def set_my_commands(self, commands): self._rec("set_my_commands", commands)

    def get_updates(self, *a, **k): self._rec("get_updates"); return []

    def get_file_path(self, file_id):
        self._rec("get_file_path", file_id)
        if self.fail_file_path:
            raise RuntimeError("SECRET-EXCEPTION-TEXT")
        return "voice/x.ogg"

    def download(self, *a, **k): self._rec("download"); return b"x"

    def _call(self, method, params=None, **kw): self._rec("_call", method, params); return []

    def send_message(self, chat_id, text, parse_mode="auto"): self._rec("send_message", chat_id, text)

    def send_file(self, chat_id, path, **k): self._rec("send_file", chat_id, str(path))

    def send_voice(self, chat_id, path, **k): self._rec("send_voice", chat_id)

    def send_plain_id(self, chat_id, text, **k): self._rec("send_plain_id", chat_id, text); return 99

    def edit_plain(self, *a, **k): self._rec("edit_plain")

    def send_chat_action(self, chat_id, action="typing"): self._rec("send_chat_action", chat_id, action)

    def delete_message(self, chat_id, message_id): self._rec("delete_message", chat_id, message_id)

    def content(self):
        return [c for c in self.calls if c[0] in CONTENT or (c[0] == "_call" and c[1][0] != "getUpdates")]

    def texts(self):
        return [c[1][1] for c in self.calls if c[0] == "send_message"]


class FakeSession:
    alive = True

    def __init__(self):
        self.injected = []
        self.error = None

    def inject(self, text):
        if self.error is not None:
            raise self.error
        self.injected.append(text)

    def _capture(self): return ""

    def _pane_ok(self): return True, ""


def snapshot(root):
    out = {}
    for p in sorted(Path(root).rglob("*")):
        st = p.lstat()
        out[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size, st.st_ino, p.is_dir())
    return out


class Base(unittest.TestCase):
    explicit = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "AGENT2TELEGRAM_STATE": str(self.state),
            "TMPDIR": str(self.root), "XDG_CONFIG_HOME": str(self.root / "xdg")})
        self._env.start()
        (self.root / "home").mkdir()
        self.net = []
        def trip(*a, **k):
            self.net.append(a)
            raise AssertionError("network access in a test")
        self._patches = [
            mock.patch.object(attach_mod, "TmuxSession", lambda *a, **k: FakeSession()),
            mock.patch.object(attach_mod, "_expected_agent_commands", lambda cfg: []),
            mock.patch.object(socket.socket, "connect", trip),
            mock.patch.object(socket, "create_connection", trip),
            mock.patch.object(urllib.request.OpenerDirector, "open", trip),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._env.stop()
        self._tmp.cleanup()
        self.assertEqual(self.net, [])

    def cfg(self, **kw):
        base = dict(agent="claude-code", token="1:abc", allowed_user_ids=[7], tmux_session="t",
                    mode="attach", signal_file=str(self.state / "answer.txt"),
                    state_dir=str(self.state), explicit_only_outbound=self.explicit)
        base.update(kw)
        c = Config(**base)
        c.validate()
        return c

    def bridge(self, **kw):
        self.rec = Recorder()
        b = AttachBridge(self.cfg(**kw), client=self.rec)
        b._session = FakeSession()
        b._transcript = self.root / "transcript.jsonl"
        b._transcript.write_text("")
        b._tpos = 0
        b._sent_path = self.root / "sent_keys"
        return b

    def line(self, rec):
        with open(self.root / "transcript.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def user(self, text="[TG] question"):
        self.line({"type": "user", "message": {"content": text}})

    def assistant(self, text, uuid="u1", stop="end_turn", extra=()):
        blocks = [{"type": "text", "text": text}] if text else []
        self.line({"type": "assistant", "uuid": uuid,
                   "message": {"content": blocks + list(extra), "stop_reason": stop}})

    def one_outbound_pass(self, b):
        with mock.patch.object(b._stop, "wait", side_effect=lambda *_: b._stop.set()):
            b._outbound_loop()

    def msg(self, text=None, uid=7, chat=7, **extra):
        m = {"chat": {"id": chat, "type": "private"}, "from": {"id": uid}, "message_id": 10}
        if text is not None:
            m["text"] = text
        m.update(extra)
        return {"update_id": 1, "message": m}


class DefaultParity(Base):
    """Flag off: identical call sequence to the base commit (this class also runs unchanged
    against a checkout of the base SHA, see PR.md)."""
    explicit = False

    def cfg(self, **kw):
        kw.pop("explicit_only_outbound", None)
        base = dict(agent="claude-code", token="1:abc", allowed_user_ids=[7], tmux_session="t",
                    mode="attach", signal_file=str(self.state / "answer.txt"),
                    state_dir=str(self.state))
        base.update(kw)
        c = Config(**base)
        c.validate()
        return c

    def test_default_client_is_not_wrapped_and_forwards_as_before(self):
        b = self.bridge()
        self.assertIs(b.tg, self.rec)
        b._begin_turn()
        self.user()
        self.assistant("hello")
        b._drain_transcript()
        b._flush_pending()
        self.assertEqual([(c[0], c[1]) for c in self.rec.content()],
                         [("send_chat_action", (7, "typing")), ("send_message", (7, "hello"))])

    def test_default_service_texts_unchanged(self):
        b = self.bridge()
        b._handle(self.msg("/id"))
        b._handle(self.msg("hi", uid=99))
        self.assertEqual(self.rec.texts(), ["Your Telegram id: `7`", "⛔ Not authorized."])


class Config_(Base):
    def write(self, **raw):
        data = {"agent": "claude-code", "token": "1:abc", "allowed_user_ids": [7],
                "tmux_session": "t", "mode": "attach"}
        data.update(raw)
        p = self.root / "cfg.json"
        p.write_text(json.dumps(data))
        return p

    def test_default_off_and_unknown_keys_still_tolerated(self):
        self.assertFalse(load(self.write(future_key=1)).explicit_only_outbound)

    def test_invalid_values_rejected_before_any_effect(self):
        for raw in ({"explicit_only_outbound": "true"}, {"explicit_only_outbound": 1},
                    {"explicit_only_outbound": True, "mode": "oneshot"},
                    {"explicit_only_outbound": True, "background_progress": True},
                    {"explicit_only_outbound": True, "group_marker": "[g]"},
                    {"explicit_only_outbound": True, "restart_notice_text": "hi"},
                    {"explicit_only_outbound": True, "explicit_only_outbond": True}):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                load(self.write(**raw))

    def test_row9_group_marker_routing_cannot_be_combined(self):
        with self.assertRaises(ConfigError):
            self.cfg(group_marker="[g]", allowed_group_ids=[-5])

    def test_doctor_reports_capability(self):
        out = io.StringIO()
        from agent2telegram.__main__ import _cmd_doctor
        client = mock.Mock()
        client.get_me.return_value = {"username": "bot"}
        with mock.patch("agent2telegram.config.load", return_value=self.cfg()), \
                mock.patch("agent2telegram.telegram.TelegramClient", return_value=client), \
                contextlib.redirect_stdout(out):
            _cmd_doctor(SimpleNamespace(config=None))
        self.assertIn("explicit-only supported", out.getvalue())
        self.assertIn("explicit_only_outbound=True", out.getvalue())


class BlockedPassivePaths(Base):
    def assertSilent(self, b):
        self.assertEqual(self.rec.content(), [])
        self.assertEqual(self.rec.texts(), [])
        self.assertFalse((self.state / "queue").exists(), "no durable outbox may be created")
        self.assertFalse((self.state / "outbound_queue_t.jsonl").exists())

    def test_injected_client_is_wrapped_at_the_boundary(self):
        b = self.bridge()
        self.assertIsInstance(b.tg, op.OutboundGuard)

    def test_row1_transcript_text(self):
        b = self.bridge()
        b._begin_turn()
        self.user()
        self.assistant("progress", uuid="a", stop="tool_use")
        self.assistant("final answer", uuid="b")
        b._drain_transcript()
        self.one_outbound_pass(b)
        self.assertSilent(b)

    def test_row2_turn_end_backstop_hook_and_idle(self):
        b = self.bridge()
        b._begin_turn()
        self.user()
        self.assistant("the answer")
        b._finish_turn()                                     # backstop path
        self.assertSilent(b)
        b._begin_turn()
        b._turn_end.write_text("{}")                         # Stop-hook marker path
        self.one_outbound_pass(b)
        self.assertSilent(b)
        b._begin_turn()
        b._last_activity = time.monotonic() - 10_000         # idle fallback
        self.one_outbound_pass(b)
        self.assertSilent(b)

    def test_row3_signal_file_untouched(self):
        b = self.bridge()
        b._begin_turn()
        (self.state / "answer.txt").write_text("agent wrote this")
        before = snapshot(self.state)
        self.one_outbound_pass(b)
        b._finish_turn()
        self.assertSilent(b)
        self.assertEqual(snapshot(self.state)["answer.txt"], before["answer.txt"])

    def test_row4_file_marker_and_native_file_event(self):
        b = self.bridge()
        outbox = Path(b.cfg.path_outbox())
        outbox.mkdir(parents=True, exist_ok=True)
        f = outbox / "report.txt"
        f.write_text("data")
        b._begin_turn()
        self.user()
        self.assistant(f"[tg-file] {f}", uuid="m")
        self.assistant("", uuid="t", extra=[{"type": "tool_use", "id": "t1", "name": "SendUserFile",
                                             "input": {"files": [str(f)]}}])
        b._drain_transcript()
        b._send_files([str(f), str(self.root / "elsewhere.txt")])   # also the refusal report
        b._finish_turn()
        self.assertSilent(b)
        self.assertEqual(b._pending_files if hasattr(b, "_pending_files") else [], [])

    def test_row5_voice_reply(self):
        b = self.bridge(elevenlabs_api_key="sk_" + "a" * 40)
        (self.state / "voice_mode").write_text("on")
        b._voice_on = True
        self.assertFalse(b._voice_reply_on())
        with mock.patch.object(attach_mod.tts, "synthesize_long", side_effect=AssertionError("TTS")):
            b._begin_turn()
            self.user()
            self.assistant("speak this")
            b._drain_transcript()
            b._finish_turn()
        self.assertSilent(b)

    def test_row6_typing_and_tui_scrape(self):
        b = self.bridge()
        b._begin_turn()
        with mock.patch.object(b._stop, "wait", side_effect=lambda *_: b._stop.set()):
            b._typing_loop()                                   # even if someone starts it
        self.assertSilent(b)
        self.assertTrue(any("send_chat_action" in d for d in b.tg.denied))

    def test_row7_status_bubble(self):
        b = self.bridge()
        b._begin_turn()
        b._status_push("🛠️ working")
        self.user()
        self.line({"type": "assistant", "uuid": "x", "message": {"content": [
            {"type": "tool_use", "id": "tt", "name": "Bash", "input": {"command": "ls"}}]}})
        b._drain_transcript()
        self.assertIsNone(b.tg.send_plain_id(7, "x"))          # the sink itself, not just the source
        self.assertSilent(b)

    def test_row8_background_watcher(self):
        from agent2telegram.background import SubagentWatcher
        sub = self.root / "sess" / "subagents"
        sub.mkdir(parents=True)
        main = self.root / "sess.jsonl"
        main.write_text("")
        now = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".000Z"
        (sub / "agent-a1.meta.json").write_text(json.dumps({"description": "Job"}))
        (sub / "agent-a1.jsonl").write_text("\n".join(json.dumps(r) for r in (
            {"type": "user", "timestamp": iso, "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "timestamp": iso, "uuid": "ux", "message": {"stop_reason": "tool_use",
             "content": [{"type": "tool_use", "id": "k1", "name": "Read", "input": {"file_path": "/x"}}]}})) + "\n")
        # liveness of the fixture: through a raw client the watcher DOES send
        raw = Recorder()
        SubagentWatcher(raw, 7, transcript=lambda: main, state_path=self.root / "s0").tick()
        self.assertEqual([c[0] for c in raw.calls], ["send_plain_id"])
        # through the bridge's guard it cannot
        b = self.bridge()
        orphan = self.root / "bgstate"
        orphan.write_text("123")
        w = SubagentWatcher(b.tg, 7, transcript=lambda: main, state_path=orphan)
        w.cleanup_orphan()
        w.tick()
        self.assertSilent(b)
        self.assertTrue(any("send_plain_id" in d for d in b.tg.denied))
        self.assertTrue(any("delete_message" in d for d in b.tg.denied))

    def test_row10_resume_rewind_and_retry(self):
        b = self.bridge()
        self.user()
        self.assistant("reply written while we were down")
        b._sent_keys.add("older")
        b._stop.set()
        b._run_locked()                                        # start path incl. _resume_position
        self.assertSilent(b)
        self.assertEqual(self.rec.calls[0][0], "get_me")
        self.assertIn(("set_my_commands", (BOT_COMMANDS,), {}), self.rec.calls)

    def test_row13_file_refusal_report(self):
        b = self.bridge()
        b._send_files([str(self.root / "nope")])
        self.assertSilent(b)

    def test_guard_denies_and_handles_everything_else(self):
        b = self.bridge()
        g = b.tg
        for name, args in (("send_message", (7, "x")), ("send_file", (7, "p")), ("send_voice", (7, "p")),
                           ("send_plain_id", (7, "x")), ("edit_plain", (7, 1, "x")),
                           ("send_chat_action", (7, "typing")), ("delete_message", (7, 1))):
            self.assertIsNone(getattr(g, name)(*args))
        self.assertEqual(g._call("sendMessage", {"text": "x"}), [])
        self.assertEqual(g._call("sendPhoto", {}), [])
        g.set_my_commands([{"command": "x", "description": "transcript text"}])
        with self.assertRaises(AttributeError):
            g._call_multipart
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(len(g.denied), 10)


class AllowedPaths(Base):
    def test_row12_service_replies_use_fixed_templates(self):
        b = self.bridge(restart_notice=True)
        for text in ("/help", "/start", "/id", "/status", "/setkey sk_" + "a" * 40, "/voice"):
            b._handle(self.msg(text))
        b._handle(self.msg("x", uid=99))
        b._handle(self.msg(voice={"file_id": "v"}))                  # STT not configured
        b._handle(self.msg("y" * (attach_mod.MAX_INBOUND_PROMPT_CHARS + 1)))
        b._handle(self.msg(document={"file_id": "d", "file_size": 30 * 1024 * 1024}))
        self.rec.fail_file_path = True
        b._handle(self.msg(document={"file_id": "d", "file_size": 10}))
        b._handle(self.msg(voice={"file_id": "v", "file_size": 10}))   # download error, not STT key
        attach_mod.restart_notice(b, 7)
        texts = self.rec.texts()
        self.assertIn("Your Telegram id: `7`", texts)
        self.assertIn("⛔ Not authorized.", texts)
        self.assertEqual(texts.count("Not available in explicit-only mode."), 2)
        self.assertTrue(all("SECRET-EXCEPTION-TEXT" not in t for t in texts))
        self.assertIn("🔄 Bridge started and connected.", texts)
        self.assertEqual([c for c in self.rec.calls if c[0] == "delete_message"], [],
                         "/setkey must not run: the secret message could not be deleted")
        self.assertEqual(self.rec.content().__len__(), len(texts))   # only send_message
        self.assertEqual(b.cfg.elevenlabs_api_key, "", "/setkey must not save a key")

    def test_service_failures_inject_and_unsafe_pane_are_fixed_text(self):
        b = self.bridge()
        b._session.error = RuntimeError("PRIVATE-REASON")
        with mock.patch.object(attach_mod, "INJECT_RETRY_WAIT", 0):
            b._handle(self.msg("hello"))
            b._session.error = SessionError("refusing to inject PRIVATE-REASON")
            b._handle(self.msg("hello again"))
        texts = self.rec.texts()
        self.assertEqual(len(texts), 2)
        self.assertTrue(all("PRIVATE" not in t and "hello" not in t for t in texts))

    def test_inbound_still_reaches_the_agent_incl_reactions(self):
        b = self.bridge()
        b._handle(self.msg("do the thing"))
        b._handle({"update_id": 2, "message_reaction": {"user": {"id": 7}, "chat": {"id": 7},
                   "message_id": 3, "new_reaction": [{"type": "emoji", "emoji": "❤"}]}})
        self.assertEqual(len(b._session.injected), 2)
        self.assertEqual(self.rec.content(), [])

    def test_state_revealing_replies_blocked(self):
        b = self.bridge()
        b._begin_turn()
        b._last_activity = time.monotonic()
        b._maybe_ack_queued(self.msg("second"))
        b._owner_chat = 7
        with mock.patch.object(attach_mod, "CHAT_SWITCH_MAX_WAIT", 0), \
                mock.patch.object(attach_mod, "CHAT_SWITCH_POLL", 0.01):
            t = threading.Timer(0.2, b._turn_active.clear)
            t.start()
            b._wait_for_turn_before_chat_switch(8)
            t.join()
        self.assertEqual(self.rec.content(), [])

    def test_typed_door_only(self):
        b = self.bridge()
        g = b.tg
        g.send_service(7, op.ChatId(7))
        self.assertEqual(self.rec.texts(), ["Your Telegram id: `7`"])
        n = len(g.denied)
        g.send_service(7, "free text")                 # no free-text API
        g.send_service(7, "[TG] looks like a reply")
        self.assertEqual(len(g.denied), n + 2)
        with self.assertRaises(TypeError):
            op.ChatId("7")
        with self.assertRaises(TypeError):
            op.AudioTooLarge("5 MB; transcript")
        self.assertEqual(self.rec.texts(), ["Your Telegram id: `7`"])

    def test_row14_control_calls_reactions_callbacks(self):
        b = self.bridge()
        b.tg.set_my_commands(BOT_COMMANDS)
        b.tg.get_me()
        b.tg._call("getUpdates", {})
        b.tg.send_service(7, op.ReactionAck(7, 3))
        b.tg.send_service(7, op.CallbackAck("12345"))
        names = [(c[0], c[1][0] if c[0] == "_call" else None) for c in self.rec.calls]
        self.assertEqual(names, [("set_my_commands", None), ("get_me", None), ("_call", "getUpdates"),
                                 ("_call", "setMessageReaction"), ("_call", "answerCallbackQuery")])
        react = self.rec.calls[3][1][1]
        self.assertIn("👍", react["reaction"])
        self.assertNotIn("text", self.rec.calls[4][1][1])
        self.assertEqual(self.rec.calls[0][1][0], BOT_COMMANDS)
        with self.assertRaises(TypeError):
            op.ReactionAck(7, "3")

    def test_row14_poll_loop_survives_denied_calls(self):
        b = self.bridge()
        polls = []
        def fake(method, params=None, **kw):
            polls.append(method)
            b._stop.set()
            return [self.msg("hi")]
        self.rec._call = fake
        b._handle = lambda upd: (b.tg.send_message(7, "denied"), True)[1]
        b._inbound_loop()
        self.assertEqual(polls, ["getUpdates"])

    def test_row11_notify_cli_still_delivers(self):
        from agent2telegram.__main__ import _main
        cfgfile = self.root / "cfg.json"
        cfgfile.write_text(json.dumps({"agent": "claude-code", "token": "1:abc", "allowed_user_ids": [7],
                                       "tmux_session": "t", "mode": "attach",
                                       "explicit_only_outbound": True}))
        client = mock.Mock()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT2TELEGRAM_CONFIG": str(cfgfile)}), \
                mock.patch("agent2telegram.telegram.TelegramClient", return_value=client), \
                contextlib.redirect_stdout(out):
            self.assertEqual(_main(["notify", "build finished"]), 0)
        client.send_message.assert_called_once_with(7, "build finished")

    def test_concurrent_passive_and_service(self):
        b = self.bridge()
        stop = threading.Event()
        def passive():
            while not stop.is_set():
                b._send_final("passive text", key="k")
                b.tg.send_message(7, "passive")
                b._status_push("x")
        threads = [threading.Thread(target=passive) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            for i in range(25):
                b.tg.send_service(7, op.ChatId(i))
        finally:
            stop.set()
            for t in threads:
                t.join()
        self.assertEqual(self.rec.texts(), [f"Your Telegram id: `{i}`" for i in range(25)])


class PendingQueueRestart(Base):
    def seed(self, kind):
        out = self.state / "queue" / "outbox"
        out.mkdir(parents=True)
        if kind == "durable":
            rec = {"version": 1, "kind": "outbox", "id": "out-1", "created_ns": 1, "updated_ns": 1,
                   "status": "pending", "attempts": 0, "last_error": None, "chunks": ["old"],
                   "files": [], "key": None, "chat": 7, "sent_chunks": [], "sent_files": []}
            p = out / "out-1.json"
            p.write_text(json.dumps(rec))
        elif kind == "corrupt":
            (out / "bad.json").write_text("{not json")
        elif kind == "legacy":
            (self.state / "outbound_queue_t.jsonl").write_text(json.dumps({"text": "old"}) + "\n")
        elif kind == "old":
            rec = {"version": 1, "kind": "outbox", "id": "out-2", "created_ns": 1, "updated_ns": 1,
                   "status": "pending", "chunks": ["ancient"], "files": [], "key": None}
            p = out / "out-2.json"
            p.write_text(json.dumps(rec))
            old = time.time() - 40 * 86400            # beyond the 30-day retention _maintain enforces
            os.utime(p, (old, old))

    def test_row10_restart_with_pending_queue_refuses_and_changes_nothing(self):
        for kind in ("durable", "corrupt", "legacy", "old"):
            with self.subTest(kind=kind):
                for p in list(self.state.iterdir()):
                    if p.is_dir():
                        import shutil
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                self.seed(kind)
                b = self.bridge()
                before = snapshot(self.state)
                b._stop.set()                  # an un-guarded start must end, not hang
                with self.assertRaises(op.OutboundQueuePresent):
                    b._run_locked()
                self.assertEqual(snapshot(self.state), before, "mtime/location/inode changed")
                self.assertEqual(self.rec.calls, [], "nothing may be sent or even polled")

    def test_run_exits_with_distinct_code_and_doctor_still_works(self):
        from agent2telegram.__main__ import _cmd_run
        self.seed("durable")
        cfgfile = self.root / "cfg.json"
        cfgfile.write_text(json.dumps({"agent": "claude-code", "token": "1:abc", "allowed_user_ids": [7],
                                       "tmux_session": "t", "mode": "attach", "bot_username": "bot",
                                       "signal_file": str(self.state / "answer.txt"),
                                       "state_dir": str(self.state), "explicit_only_outbound": True}))
        err = io.StringIO()
        before = snapshot(self.state)
        with mock.patch.dict(os.environ, {"AGENT2TELEGRAM_CONFIG": str(cfgfile)}), \
                mock.patch("agent2telegram.claude_hooks.register"), \
                contextlib.redirect_stderr(err):
            rc = _cmd_run(SimpleNamespace(config=None))
        self.assertEqual(rc, op.EXIT_QUEUE_PRESENT)
        self.assertNotEqual(rc, 2)
        self.assertIn("refuses to start", err.getvalue())
        self.assertEqual(snapshot(self.state)["queue/outbox/out-1.json"][:3],
                         before["queue/outbox/out-1.json"][:3])

    def test_empty_state_starts_and_creates_no_queue(self):
        b = self.bridge()
        b._stop.set()
        b._run_locked()
        self.assertFalse((self.state / "queue").exists())


class StaticAudit(unittest.TestCase):
    def test_no_free_text_entry_point_and_known_callers(self):
        import inspect
        sig = inspect.signature(op.OutboundGuard.send_service)
        self.assertEqual(list(sig.parameters), ["self", "chat_id", "reply"])
        src = Path(attach_mod.__file__).read_text()
        self.assertEqual(src.count("send_service("), src.count("send_service(chat_id, typed)")
                         + src.count("send_service(chat_id, op.") + src.count("send_service(self._owner_chat, op."))


if __name__ == "__main__":
    unittest.main()
