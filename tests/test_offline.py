"""Tests for the opt-in offline mode (AGENT2TELEGRAM_OFFLINE) - no network required.

Every outbound HTTP entry point must refuse BEFORE a connection is opened: no opener call, no
socket, no DNS lookup. With the mode off, behaviour must be unchanged.
"""
import contextlib
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from agent2telegram import offline, stt, tts
from agent2telegram.bridge import Bridge
from agent2telegram.config import Config
from agent2telegram.offline import OFFLINE_ENV, OfflineError
from agent2telegram.stt import STTError, STTOfflineError
from agent2telegram.telegram import TelegramClient, TelegramError, TelegramOfflineError
from agent2telegram.tts import TTSError, TTSOfflineError


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _CountingOpener:
    """An injected opener that records every open() and answers with an OK reply."""
    def __init__(self, on_open=None):
        self.calls = 0
        self.on_open = on_open          # lets a test stop a loop that wrongly gets through

    def open(self, req, timeout=None):
        self.calls += 1
        if self.on_open:
            self.on_open()
        return _Resp(json.dumps({"ok": True, "result": [], "text": "hi"}).encode())


@contextlib.contextmanager
def network_tripwire():
    """Fail loudly (and count) if anything below the opener tries to reach the network."""
    hits = []

    def trip(name):
        def _raise(*a, **k):
            hits.append(name)
            raise AssertionError(f"network reached via {name}")
        return _raise

    with mock.patch.object(urllib.request.OpenerDirector, "open", trip("OpenerDirector.open")), \
         mock.patch.object(socket, "create_connection", trip("create_connection")), \
         mock.patch.object(socket, "getaddrinfo", trip("getaddrinfo")), \
         mock.patch.object(socket.socket, "connect", trip("socket.connect")), \
         mock.patch.object(http.client.HTTPConnection, "connect", trip("HTTPConnection.connect")):
        yield hits


def _env(value):
    env = {k: v for k, v in os.environ.items() if k != OFFLINE_ENV}
    if value is not None:
        env[OFFLINE_ENV] = value
    return mock.patch.dict(os.environ, env, clear=True)


class OfflineSwitchTests(unittest.TestCase):
    def test_off_by_default(self):
        with _env(None):
            self.assertFalse(offline.is_offline())

    def test_truthy_values_enable(self):
        for value in ("1", "true", "TRUE", "yes", "On", " 1 "):
            with self.subTest(value=value), _env(value):
                self.assertTrue(offline.is_offline())

    def test_other_values_stay_off(self):
        for value in ("", "0", "false", "no", "off", "2", "garbage"):
            with self.subTest(value=value), _env(value):
                self.assertFalse(offline.is_offline())

    def test_read_on_every_call_not_cached(self):
        with _env("1"):
            self.assertTrue(offline.is_offline())
        with _env("0"):
            self.assertFalse(offline.is_offline())

    def test_require_online_noop_when_off(self):
        with _env(None):
            offline.require_online("anything")

    def test_refusal_message_is_clear_and_typed(self):
        with _env("1"):
            with self.assertRaises(OfflineError) as cm:
                offline.require_online("getMe")
        self.assertIn("offline mode", str(cm.exception))
        self.assertIn(OFFLINE_ENV, str(cm.exception))

    def test_typed_errors_keep_existing_handlers_working(self):
        for cls, base in ((TelegramOfflineError, TelegramError), (STTOfflineError, STTError),
                          (TTSOfflineError, TTSError)):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, OfflineError))
                self.assertTrue(issubclass(cls, base))
        # A refusal is not a "transient network outage": it must not be retried as one.
        self.assertFalse(TelegramOfflineError("x").network_error)


class TelegramRefusalTests(unittest.TestCase):
    """Every Telegram entry point refuses before any connection, with an injected opener and
    with the default (real) opener."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.photo = Path(self.tmp.name) / "a.png"
        self.photo.write_bytes(b"\x89PNG")
        self.voice = Path(self.tmp.name) / "v.ogg"
        self.voice.write_bytes(b"OggS")

    def calls(self, client):
        return {
            "getMe": lambda: client.get_me(),
            "getUpdates": lambda: client.get_updates(0, timeout=1),
            "getFile": lambda: client.get_file_path("f"),
            "download": lambda: client.download("p/x.bin"),
            "sendMessage auto": lambda: client.send_message(1, "hi"),
            "sendMessage plain": lambda: client.send_message(1, "hi", parse_mode=""),
            "sendMessage html": lambda: client.send_message(1, "hi", parse_mode="HTML"),
            "send_file": lambda: client.send_file(1, self.photo),
            "send_voice": lambda: client.send_voice(1, self.voice),
            "_call": lambda: client._call("sendMessage", {"chat_id": 1}),
            "_call_multipart": lambda: client._call_multipart("sendDocument", {}, "document", "a", b"x"),
        }

    def test_every_entry_point_refuses_with_injected_opener(self):
        op = _CountingOpener()
        client = TelegramClient("1:abc", opener=op)
        with _env("1"):
            for name, call in self.calls(client).items():
                with self.subTest(entry=name):
                    with self.assertRaises(TelegramOfflineError):
                        call()
        self.assertEqual(op.calls, 0)

    def test_every_entry_point_refuses_before_any_socket_with_default_opener(self):
        client = TelegramClient("1:abc")
        with _env("1"), network_tripwire() as hits:
            for name, call in self.calls(client).items():
                with self.subTest(entry=name):
                    with self.assertRaises(TelegramOfflineError):
                        call()
        self.assertEqual(hits, [])

    def test_refusal_never_sleeps_or_retries(self):
        client = TelegramClient("1:abc", opener=_CountingOpener())
        with _env("1"), mock.patch("time.sleep") as sleep:
            for call in self.calls(client).values():
                with self.assertRaises(TelegramOfflineError):
                    call()
        sleep.assert_not_called()

    def test_send_message_and_send_file_do_not_log_fallbacks(self):
        client = TelegramClient("1:abc", opener=_CountingOpener())
        with _env("1"), self.assertLogs("agent2telegram.telegram", level="DEBUG") as cm:
            import logging
            logging.getLogger("agent2telegram.telegram").debug("marker")
            for call in (lambda: client.send_message(1, "hi"), lambda: client.send_file(1, self.photo)):
                with self.assertRaises(TelegramOfflineError):
                    call()
        text = " ".join(r.getMessage() for r in cm.records)
        self.assertNotIn("falling back", text)
        self.assertNotIn("retrying as a document", text)

    def test_best_effort_helpers_degrade_without_touching_the_network(self):
        op = _CountingOpener()
        client = TelegramClient("1:abc", opener=op)
        with _env("1"):
            # Existing contract: these swallow any TelegramError (the refusal included) and
            # return None; they are cosmetic by design, so the refusal is not logged there.
            self.assertIsNone(client.send_plain_id(1, "x"))
            self.assertIsNone(client.edit_plain(1, 2, "x"))
            self.assertIsNone(client.delete_message(1, 2))
            self.assertIsNone(client.send_chat_action(1))
            self.assertIsNone(client.set_my_commands([{"command": "a", "description": "b"}]))
        self.assertEqual(op.calls, 0)

    def test_switch_is_live_not_latched_at_construction(self):
        op = _CountingOpener()
        with _env(None):
            client = TelegramClient("1:abc", opener=op)
            client.get_me()
        self.assertEqual(op.calls, 1)
        with _env("1"):
            with self.assertRaises(TelegramOfflineError):
                client.get_me()
        self.assertEqual(op.calls, 1)
        with _env("0"):
            client.get_me()
        self.assertEqual(op.calls, 2)


class SwitchedOnDuringBackoffTests(unittest.TestCase):
    """The switch is checked before EVERY connection attempt, not only the first one."""

    class _FailingOpener:
        def __init__(self):
            self.calls = 0

        def open(self, req, timeout=None):
            self.calls += 1
            raise urllib.error.URLError("boom")

    @contextlib.contextmanager
    def flip_on_sleep(self):
        """Offline mode is off until the first back-off sleep, then on."""
        def sleeper(*a, **k):
            os.environ[OFFLINE_ENV] = "1"
        with _env(None), mock.patch("time.sleep", side_effect=sleeper) as sleep:
            yield sleep

    def check(self, op, call, error):
        with self.flip_on_sleep():
            with self.assertRaises(error):
                call()
        self.assertEqual(op.calls, 1)           # the first attempt only; no open() after the flip

    def test_telegram_call(self):
        op = self._FailingOpener()
        self.check(op, TelegramClient("1:abc", opener=op).get_me, TelegramOfflineError)

    def test_telegram_download(self):
        op = self._FailingOpener()
        self.check(op, lambda: TelegramClient("1:abc", opener=op).download("p"), TelegramOfflineError)

    def test_telegram_multipart(self):
        op = self._FailingOpener()
        client = TelegramClient("1:abc", opener=op)
        self.check(op, lambda: client._call_multipart("sendDocument", {}, "document", "a", b"x"),
                   TelegramOfflineError)

    def test_stt(self):
        op = self._FailingOpener()
        with self.flip_on_sleep():
            with self.assertRaises(STTOfflineError):
                stt.transcribe_elevenlabs(b"a", api_key="k", opener=op,
                                          sleeper=lambda s: os.environ.__setitem__(OFFLINE_ENV, "1"))
        self.assertEqual(op.calls, 1)

    def test_tts(self):
        op = self._FailingOpener()
        with _env(None):
            with self.assertRaises(TTSOfflineError):
                tts.synthesize("hello", api_key="k", voice_id="v", opener=op,
                               sleeper=lambda s: os.environ.__setitem__(OFFLINE_ENV, "1"))
        self.assertEqual(op.calls, 1)


class GuardPlacementTests(unittest.TestCase):
    def test_local_validation_runs_before_the_guard(self):
        """Cheap local checks keep their own errors even when offline."""
        client = TelegramClient("1:abc", opener=_CountingOpener())
        with tempfile.TemporaryDirectory() as tmp, _env("1"):
            big = Path(tmp) / "big.bin"
            big.write_bytes(b"xx")
            with mock.patch.object(TelegramClient, "MAX_UPLOAD", 1):
                with self.assertRaises(TelegramError) as cm:
                    client.send_file(1, big)
            self.assertNotIsInstance(cm.exception, OfflineError)
            with self.assertRaises(OSError):
                client.send_file(1, Path(tmp) / "missing.png")


class DefaultUnchangedTests(unittest.TestCase):
    """With the mode off, requests go through exactly as before."""

    def test_telegram_requests_reach_the_opener(self):
        op = _CountingOpener()
        client = TelegramClient("1:abc", opener=op)
        with _env(None):
            client.get_me()
            client.send_message(1, "hi")
            client.get_updates(0, timeout=1)
            client.download("p/x.bin")
        self.assertEqual(op.calls, 4)

    def test_default_opener_is_used_when_off(self):
        client = TelegramClient("1:abc")
        with _env(None), mock.patch.object(
                urllib.request.OpenerDirector, "open",
                return_value=_Resp(json.dumps({"ok": True, "result": {"id": 1}}).encode())) as op:
            self.assertEqual(client.get_me(), {"id": 1})
        self.assertEqual(op.call_count, 1)

    def test_stt_and_tts_reach_the_opener(self):
        op = _CountingOpener()
        with _env(None):
            self.assertEqual(stt.transcribe_elevenlabs(b"a", api_key="k", opener=op), "hi")

        class _Audio(_CountingOpener):
            def open(self, req, timeout=None):
                self.calls += 1
                return _Resp(b"MP3")
        aop = _Audio()
        with _env(None):
            self.assertEqual(tts.synthesize("hello", api_key="k", voice_id="v", opener=aop), b"MP3")
        self.assertEqual((op.calls, aop.calls), (1, 1))

    def test_validation_errors_unchanged(self):
        with _env("1"):
            with self.assertRaises(STTError) as cm:
                stt.transcribe_elevenlabs(b"a", api_key="")
            self.assertNotIsInstance(cm.exception, OfflineError)
            with self.assertRaises(TTSError) as cm:
                tts.synthesize("  ", api_key="k", voice_id="v")
            self.assertNotIsInstance(cm.exception, OfflineError)


class ElevenLabsRefusalTests(unittest.TestCase):
    def test_stt_refuses_before_connect(self):
        op = _CountingOpener()
        with _env("1"), network_tripwire() as hits, mock.patch("time.sleep") as sleep:
            with self.assertRaises(STTOfflineError):
                stt.transcribe_elevenlabs(b"a", api_key="k", opener=op)
            with self.assertRaises(STTOfflineError):
                stt.transcribe_elevenlabs(b"a", api_key="k")           # default opener
            with self.assertRaises(STTOfflineError):
                stt.transcribe(b"a", api_key="k")
        self.assertEqual((op.calls, hits), (0, []))
        sleep.assert_not_called()

    def test_tts_refuses_before_connect(self):
        op = _CountingOpener()
        with _env("1"), network_tripwire() as hits, mock.patch("time.sleep") as sleep:
            with self.assertRaises(TTSOfflineError):
                tts.synthesize("hello", api_key="k", voice_id="v", opener=op)
            with self.assertRaises(TTSOfflineError):
                tts.synthesize("hello", api_key="k", voice_id="v")     # default opener
            with self.assertRaises(TTSOfflineError):
                tts.synthesize_long("hello. world.", api_key="k", voice_id="v")
        self.assertEqual((op.calls, hits), (0, []))
        sleep.assert_not_called()


class PollingOfflineTests(unittest.TestCase):
    def _bridge(self, tmp, client):
        cfg = Config(agent="claude-code", token="1:abc", allowed_user_ids=[7], workdir=tmp)
        bridge = Bridge(cfg, client=client)
        bridge._offset_file = Path(tmp) / "offset"
        return bridge

    def test_bridge_start_fails_fast_when_offline(self):
        """Startup must not retry forever like an unreachable network: it raises at once."""
        op = _CountingOpener()
        with tempfile.TemporaryDirectory() as tmp, _env("1"), \
                mock.patch("time.sleep") as sleep:
            bridge = self._bridge(tmp, TelegramClient("1:abc", opener=op))
            with mock.patch.object(bridge._stop, "wait", side_effect=bridge._stop.set) as wait:
                with self.assertRaises(OfflineError):
                    bridge.run()
            wait.assert_not_called()
        self.assertEqual(op.calls, 0)
        sleep.assert_not_called()

    def test_poll_loop_fails_fast_if_switched_on_mid_run(self):
        """Offline mode appearing while the poll loop runs ends the loop with OfflineError: no
        request, no ERROR-log-and-retry cycle, no hot spin."""
        op = _CountingOpener()
        client = TelegramClient("1:abc", opener=op)
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self._bridge(tmp, client)
            op.on_open = bridge._stop.set       # a regression ends the loop instead of hanging
            client.get_me = lambda: {"username": "bot"}     # startup succeeded while online
            with _env("1"), network_tripwire() as hits, \
                    mock.patch.object(bridge._stop, "wait", side_effect=bridge._stop.set) as wait:
                with self.assertRaises(OfflineError):
                    bridge.run()
        self.assertEqual((op.calls, hits), (0, []))
        wait.assert_not_called()

    def test_attach_poll_loop_fails_fast_if_switched_on_mid_run(self):
        from agent2telegram.attach import AttachBridge
        op = _CountingOpener()
        bridge = AttachBridge.__new__(AttachBridge)
        bridge.cfg = Config(agent="claude-code", token="1:abc", allowed_user_ids=[7])
        bridge.tg = TelegramClient("1:abc", opener=op)
        bridge._stop = threading.Event()
        op.on_open = bridge._stop.set
        with mock.patch.object(AttachBridge, "_start_inbound_worker"), \
                mock.patch.object(AttachBridge, "_replay_pending_inbound"), \
                mock.patch.object(AttachBridge, "_load_offset", return_value=0), \
                mock.patch.object(bridge._stop, "wait", side_effect=bridge._stop.set) as wait, \
                _env("1"), network_tripwire() as hits:
            with self.assertRaises(OfflineError):
                bridge._inbound_loop()
        self.assertEqual((op.calls, hits), (0, []))
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
