import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent2telegram.attach import AttachBridge, _empty_turn_notice
from agent2telegram.readers import CodexReader
from agent2telegram.session import TmuxSession


class TurnBackstopTests(unittest.TestCase):
    def test_usage_limit_without_agent_message_is_reported(self):
        reason, notice = _empty_turn_notice(
            "› [TG] Odpověz.\n\n■ You've hit your usage limit. Please try again at 12:31 PM."
        )

        self.assertEqual(reason, "usage_limit")
        self.assertIn("limit používání", notice)
        self.assertIn("12:31 PM", notice)

    def test_unknown_empty_turn_is_never_silent(self):
        reason, notice = _empty_turn_notice("› [TG] Odpověz.\n\n■ Turn ended")

        self.assertEqual(reason, "empty_response")
        self.assertIn("bez odpovědi", notice)

    def test_finish_turn_forwards_unsent_final_answer(self):
        bridge = object.__new__(AttachBridge)
        bridge._turn_active = threading.Event()
        bridge._turn_active.set()
        bridge._turn_from_tg = True
        bridge._turn_text_sent = False
        bridge._owner_chat = 42
        bridge._pending_turn_end = False
        bridge._turn_end = None
        bridge._task_path = None
        bridge._inflight_task = None
        bridge._turn_started = time.monotonic()
        bridge._typing_count = 1
        bridge._max_gap = 0.0
        bridge._status_clear = lambda: None
        bridge._last_assistant_text = lambda: "[TG] RECOVERED"
        bridge._strip_marker = lambda text: text.removeprefix("[TG] ")
        sent = []
        bridge._send_final = lambda text, key=None: sent.append(text)

        bridge._finish_turn()

        self.assertEqual(sent, ["RECOVERED"])
        self.assertFalse(bridge._turn_active.is_set())

    def test_backstop_never_reuses_answer_from_previous_turn(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "rollout.jsonl"
            old = (
                '{"timestamp":"old","type":"event_msg","payload":'
                '{"type":"agent_message","message":"STALE ANSWER"}}\n'
            )
            transcript.write_text(old, "utf-8")

            bridge = object.__new__(AttachBridge)
            bridge._transcript = transcript
            bridge._reader = CodexReader()
            bridge._turn_transcript_start = transcript.stat().st_size
            with transcript.open("a", encoding="utf-8") as f:
                f.write('{"type":"event_msg","payload":{"type":"task_complete",'
                        '"last_agent_message":null}}\n')

            self.assertIsNone(bridge._last_assistant_text())

    def test_codex_reader_requires_authoritative_turn_end(self):
        from agent2telegram.readers import CodexReader, ClaudeCodeReader

        self.assertTrue(CodexReader.emits_turn_end)
        self.assertFalse(ClaudeCodeReader.emits_turn_end)


class LongMessageInjectionTests(unittest.TestCase):
    def test_long_message_waits_for_tui_before_pressing_enter(self):
        session = object.__new__(TmuxSession)
        session.name = "test-session"
        session._origin = "[TG] "
        message = "x" * 4000

        with patch("agent2telegram.session._tmux") as tmux, \
             patch("agent2telegram.session.time.sleep") as sleep:
            session._send_keys(message)

        self.assertEqual(tmux.call_args_list[-1].args[-1], "Enter")
        self.assertTrue(any(call.args[0] == "load-buffer" for call in tmux.call_args_list))
        self.assertTrue(any(call.args[0] == "paste-buffer" for call in tmux.call_args_list))
        self.assertGreaterEqual(
            sleep.call_args_list[-1].args[0],
            0.5,
            "Long input needs a length-aware settling delay before Enter",
        )

    def test_missing_task_start_retries_only_enter(self):
        bridge = object.__new__(AttachBridge)
        bridge.cfg = type("Cfg", (), {"agent": "codex"})()
        bridge._turn_active = threading.Event()
        bridge._task_started = type("Ack", (), {
            "clear": lambda self: None,
            "wait": lambda self, timeout: False,
        })()
        bridge._last_activity = 0.0
        bridge._session = type("Session", (), {
            "inject": lambda self, text: None,
            "submit": lambda self: setattr(self, "retried", True),
            "retried": False,
        })()

        bridge._inject("long message")

        self.assertTrue(bridge._session.retried)


class RebootContinuityTests(unittest.TestCase):
    def _bridge(self, root: Path):
        bridge = object.__new__(AttachBridge)
        bridge._task_path = root / "inflight.json"
        bridge._inflight_task = None
        bridge._turn_active = threading.Event()
        bridge._turn_from_tg = False
        bridge._last_activity = 0.0
        bridge._owner_chat = 42
        bridge.tg = type("Telegram", (), {
            "sent": [],
            "send_message": lambda self, chat, text: self.sent.append((chat, text)),
        })()
        bridge.injected = []
        bridge._inject = lambda text: bridge.injected.append(text)
        return bridge

    def test_task_is_atomically_persisted_and_cleared(self):
        with tempfile.TemporaryDirectory() as td, patch.object(AttachBridge, "_boot_id", return_value="boot-a"):
            bridge = self._bridge(Path(td))
            bridge._persist_inflight_task("finish setup", message_id=17)
            saved = bridge._load_inflight_task()

            self.assertEqual(saved["text"], "finish setup")
            self.assertEqual(saved["message_id"], 17)
            self.assertEqual(saved["accepted_boot_id"], "boot-a")
            bridge._clear_inflight_task()
            self.assertFalse(bridge._task_path.exists())

    def test_same_boot_follows_existing_turn_without_duplicate_injection(self):
        with tempfile.TemporaryDirectory() as td, patch.object(AttachBridge, "_boot_id", return_value="boot-a"):
            bridge = self._bridge(Path(td))
            bridge._persist_inflight_task("keep working")
            bridge._restore_inflight_task()

            self.assertEqual(bridge.injected, [])
            self.assertTrue(bridge._turn_active.is_set())
            self.assertTrue(bridge._turn_from_tg)

    def test_new_boot_resumes_exactly_once(self):
        with tempfile.TemporaryDirectory() as td:
            bridge = self._bridge(Path(td))
            with patch.object(AttachBridge, "_boot_id", return_value="boot-a"):
                bridge._persist_inflight_task("verify all agents")
            with patch.object(AttachBridge, "_boot_id", return_value="boot-b"):
                bridge._restore_inflight_task()
                bridge._restore_inflight_task()

            self.assertEqual(len(bridge.injected), 1)
            self.assertIn("PŮVODNÍ ÚKOL:\nverify all agents", bridge.injected[0])
            self.assertEqual(len(bridge.tg.sent), 1)
            self.assertEqual(bridge._load_inflight_task()["resumed_boot_id"], "boot-b")


if __name__ == "__main__":
    unittest.main()
