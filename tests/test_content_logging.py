"""Operational logs must not become a second message transcript (no network)."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.cases import cases, expand_cases

from agent2telegram import attach, bridge
from agent2telegram.readers import Ev
from agent2telegram.redaction import update_metadata
from agent2telegram.session import SessionError
from tests.test_v2_durability import _bridge, _msg
from tests.test_attach_backstop import _bridge as backstop_bridge


SAMPLES = [
    "Aq7vF2xMp9Jc4Zs6" * 3 + "#SyntheticOAuthState",
    "123456789:AAExampleTokenForTestsOnlyNeverUseThisValue",
    "Private medical appointment details never belong in operational logs.",
]


@expand_cases
class ContentLoggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = io.StringIO()
        handler = logging.StreamHandler(self.log)
        self.addCleanup(handler.close)
        logger = logging.getLogger("agent2telegram")
        self.patch(logger, "handlers", [handler])
        self.patch(logger, "level", logging.INFO)
        self.patch(logger, "propagate", False)

    def patch(self, target, name, value):
        replacement = patch.object(target, name, value)
        replacement.start()
        self.addCleanup(replacement.stop)

    def assert_private(self, text):
        # Including fragments catches truncation-before-redaction, not only full-string leaks.
        assert text[:12] not in self.log.getvalue()
        assert text[12:24] not in self.log.getvalue()
        assert "len=" in self.log.getvalue()
        assert "sha256=" in self.log.getvalue()


    @cases(text=SAMPLES, route=["in", "caption", "quote", "legacy", "durable", "retry",
                                "not_forwarded", "inject_notice", "backstop", "voice"])
    def test_message_routes_log_metadata_only(self, text, route):
        b = _bridge(self.root)
        if route in {"in", "caption", "quote"}:
            upd = _msg(99, text)
            if route == "caption":
                upd["message"].pop("text")
                upd["message"].update(caption=text, document={"file_id": "fixture"})
            if route == "quote":
                upd["message"].update(text="safe reply", reply_to_message={"message_id": 88, "text": text})
            b._submit_inbound_update = Mock()
            assert b._handle_update_once(upd, 0) == 100
            b._submit_inbound_update.assert_called_once()
            assert "update=99" in self.log.getvalue()
            if route == "quote":
                assert "reply_to_id=88" in self.log.getvalue()
        elif route in {"legacy", "durable"}:
            b._use_durable_outbox = route == "durable"
            b._send_final(text, key="fixture-reply")
            b._flush_pending()
            assert b.tg.sent == [text]
        elif route == "retry":
            b._pending_send = [{"text": text, "key": "fixture-reply"}]
            b._flush_pending()
            assert b.tg.sent == [text]
        elif route == "not_forwarded":
            b._handle_event(Ev("text", text=text, key="fixture-reply"))
            assert not b.tg.sent
        elif route == "inject_notice":
            b._notify_inject_failed(text, "fixture failure")
            assert text in b.tg.sent[0]  # Owner's notice is allowed to retain their own message.
        elif route == "backstop":
            b = backstop_bridge(self.root)
            b._last_assistant_text = lambda: text
            b._finish_turn()
            assert b.tg.sent == [(7, text)]
        elif route == "voice":
            b.cfg.elevenlabs_api_key = "fixture-only"
            b.tg.send_voice = Mock()
            self.patch(attach.shutil, "which", lambda _: "fixture-ffmpeg")
            self.patch(attach.tts, "synthesize_long", lambda *a, **kw: [b"audio"])
            self.patch(attach.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
            self.patch(attach.os.path, "exists", lambda _: True)
            self.patch(attach.os.path, "getsize", lambda _: 100)
            self.patch(attach, "_ogg_duration", lambda _: 1)
            assert b._try_send_voice(text)
            b.tg.send_voice.assert_called_once()
        self.assert_private(text)
        if route != "quote":
            assert "sha256=" + hashlib.sha256(text.encode()).hexdigest()[:8] in self.log.getvalue()


    @cases(text=SAMPLES, unsafe_pane=[False, True])
    def test_injection_exception_does_not_echo_message(self, text, unsafe_pane):
        b = _bridge(self.root)
        error = (SessionError("refusing to inject " + text) if unsafe_pane
                 else subprocess.TimeoutExpired(["tmux", "send-keys", text], timeout=10))
        b._session.inject = Mock(side_effect=error)
        self.patch(attach, "INJECT_RETRY_WAIT", 0)
        assert not b._inject(text)
        self.assert_private(text)


    @cases(text=SAMPLES, mode=["attach", "bridge"])
    def test_malformed_update_never_dumps_payload(self, text, mode):
        upd = _msg(99, text)
        upd["update_id"] = text
        if mode == "attach":
            b = _bridge(self.root)
            assert b._handle_update_once(upd, 0) == 0
        else:
            b = object.__new__(bridge.Bridge)
            b.cfg = SimpleNamespace(allowed_user_ids=[], agent="generic", poll_timeout=1)
            b._allowed = set()
            b._connect = lambda: {"username": "fixture"}
            b._install_signal_handlers = b._save_offset = b._shutdown = Mock()
            b._load_offset = lambda: 0
            b._stop = Mock()
            b._stop.is_set.side_effect = [False, True]
            b.tg = Mock()
            b.tg.get_updates.return_value = [upd]
            b.run()
        self.assert_private(text)


    def test_durable_files_only_and_photo_variants_count_as_one_attachment(self):
        b = _bridge(self.root)
        b._use_durable_outbox = True
        b._send_one_file = Mock()
        b._pending_files = ["fixture.pdf"]
        b._send_final("", key="fixture-files")
        b._flush_pending()
        assert "kind=file" in self.log.getvalue() and "attachments=1" in self.log.getvalue()
        assert "len=0" in self.log.getvalue()
        self.log.seek(0)
        self.log.truncate(0)
        upd = _msg(100, "")
        upd["message"]["photo"] = [{"file_id": "small"}, {"file_id": "large"}]
        b._submit_inbound_update = Mock()
        b._handle_update_once(upd, 0)
        assert "attachments=1" in self.log.getvalue()  # Resolutions of a single photo are not separate files.


    @cases(route=["legacy", "durable", "retry", "voice", "inject_notice"])
    def test_provider_failure_cannot_echo_message_to_log(self, route):
        text = SAMPLES[2]
        b = _bridge(self.root)
        b.tg.send_message = Mock(side_effect=RuntimeError("provider echoed: " + text))
        if route == "inject_notice":
            b._notify_inject_failed(text, "fixture")
        elif route == "voice":
            b.cfg.elevenlabs_api_key = "fixture-only"
            self.patch(attach.shutil, "which", lambda _: "fixture-ffmpeg")
            self.patch(attach.tts, "synthesize_long", Mock(side_effect=RuntimeError(text)))
            assert not b._try_send_voice(text)
        elif route == "retry":
            b._pending_send = [{"text": text, "key": "fixture"}]
            b._flush_pending()
            assert len(b._pending_send) == 1
        else:
            b._use_durable_outbox = route == "durable"
            b._send_final(text, key="fixture")
            b._flush_pending()
        assert text[:12] not in self.log.getvalue()
        assert "RuntimeError" in self.log.getvalue()


    def test_fingerprint_includes_suffix_after_old_preview_boundary(self):
        b = _bridge(self.root)
        for suffix in ("first", "second"):
            text = "x" * 80 + suffix
            b._send_final(text)
            b._flush_pending()
            assert hashlib.sha256(text.encode()).hexdigest()[:8] in self.log.getvalue()
        assert "x" * 12 not in self.log.getvalue()


    @cases(route=["inbound_worker", "oneshot_agent"])
    def test_worker_exception_never_logs_prompt(self, route):
        text = SAMPLES[2]
        if route == "inbound_worker":
            b = _bridge(self.root)
            b._ensure_inbound_worker_state()
            b._inbound_queue.put((_msg(99, text), None))
            def fail(_):
                b._stop.set()
                raise RuntimeError(text)

            b._handle = Mock(side_effect=fail)
            b._inbound_worker_loop()
        else:
            b = bridge.Bridge(attach.Config(agent="generic", token="1:fixture", workdir=str(self.root),
                                           command=["fixture-never-executed"]),
                              client=Mock())
            b.adapter = Mock()
            b.adapter.run.side_effect = RuntimeError(text)
            b.process(7, bridge.Task(text=text))
        assert text[:12] not in self.log.getvalue()
        assert "RuntimeError" in self.log.getvalue()


    @cases(update=[None, [], SAMPLES[2], {"message": SAMPLES[2]},
        {"message": {"message_id": SAMPLES[2], "reply_to_message": {"message_id": SAMPLES[2]}}}])
    def test_malformed_fields_are_not_logged_or_allowed_to_break_diagnostics(self, update):
        metadata = update_metadata(update)
        assert SAMPLES[2][:12] not in metadata
        assert "sha256=" in metadata


if __name__ == "__main__":
    unittest.main()
