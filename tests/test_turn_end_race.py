"""A delayed end of an older turn must leave the Telegram turn running."""
import io
import json
import time
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from agent2telegram import attach, stop_hook
from agent2telegram.readers import CodexReader
from test_late_answer import _assistant, _bridge


class TurnEndRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = _bridge(self.tmp.name)
        self.b._origins = ("Telegram:",)
        self.b._turn_from_tg = False
        self.b._turn_end = self.b._signal.parent / "turn_end"
        self.b._session = Mock()
        for name in ("_maybe_reresolve", "_replay_pending_inbound", "_beat"):
            setattr(self.b, name, Mock())
        self.retry = patch.object(attach, "BACKSTOP_RETRY_DELAY", 0)
        self.retry.start()
        self.addCleanup(self.retry.stop)

    def append(self, *records):
        with self.b._transcript.open("a", encoding="utf-8") as stream:
            for rec in records:
                stream.write(json.dumps(rec) + "\n")

    def prompt(self, uuid, text):
        return {"type": "user", "uuid": uuid, "message": {"content": text}}

    def start_telegram_turn(self):
        self.append(self.prompt("heartbeat", "Background heartbeat"),
                    json.loads(_assistant("background", "Background done")))
        self.b._drain_transcript()
        self.b._transcribe = Mock(return_value="Please check")
        self.b._handle({"message": {"from": {"id": 7}, "chat": {"id": 7},
                                    "voice": {"file_id": "voice-fixture"}}})
        self.b._session.inject.assert_called_once()
        self.assertTrue(self.b._turn_active.is_set())

    def tick(self):
        self.b._stop.clear()
        with patch.object(self.b._stop, "wait", side_effect=lambda _: self.b._stop.set()):
            self.b._outbound_loop()

    def hook(self):
        payload = json.dumps({"transcript_path": str(self.b._transcript)})
        cfg = {"agent": "claude-code", "signal_file": str(self.b._signal)}
        with patch.object(stop_hook, "_all_cfgs", return_value=[cfg]), \
                patch.object(stop_hook, "_own_tmux_session", return_value=""), \
                patch("sys.stdin", io.StringIO(payload)):
            stop_hook.main()

    def test_delayed_heartbeat_stop_keeps_typing_and_delivers_real_answer_once(self):
        self.start_telegram_turn()
        # The old Stop arrives AFTER successful injection, even after the 3 s heuristic.
        self.b._turn_started -= 30
        self.b._turn_begun_at -= 30
        self.b._status = {"mid": 55, "shown": "Working"}
        self.b.tg.delete_message = Mock()
        self.b._turn_end.write_text(str(time.time()), encoding="utf-8")
        with self.assertLogs(attach.log, level="INFO") as logs:
            self.tick()
        self.assertTrue(self.b._turn_active.is_set(), "old Stop closed the new Telegram turn")
        self.assertFalse(any("without an answer" in line for line in logs.output))
        self.b.tg.delete_message.assert_not_called()
        self.assertFalse(self.b._turn_end.exists(), "stale marker must be consumed")
        self.append(self.prompt("telegram", "Telegram: Please check"),
                    json.loads(_assistant("answer", "[tg] The real answer")))
        self.hook()
        self.tick()
        self.assertEqual(self.b.tg.sent, [(7, "The real answer")])
        self.assertFalse(self.b._turn_active.is_set())

    def test_hook_keeps_original_prompt_when_marker_write_is_delayed(self):
        self.start_telegram_turn()

        def configs():
            # Hook processing is delayed while the next prompt reaches the transcript.
            self.append(self.prompt("telegram", "Telegram: Please check"))
            return [{"agent": "claude-code", "signal_file": str(self.b._signal)}]

        with patch.object(stop_hook, "_all_cfgs", side_effect=configs):
            # Keep the other patches from hook(), but retain this delayed config lookup.
            payload = json.dumps({"transcript_path": str(self.b._transcript)})
            with patch.object(stop_hook, "_own_tmux_session", return_value=""), \
                    patch("sys.stdin", io.StringIO(payload)):
                stop_hook.main()
        self.tick()
        self.assertTrue(self.b._turn_active.is_set(), "late hook lost its original turn identity")

    def test_current_unanswered_turn_still_runs_backstop(self):
        self.start_telegram_turn()
        self.append(self.prompt("telegram", "Telegram: Please check"))
        self.hook()
        with self.assertLogs(attach.log, level="ERROR") as logs:
            self.tick()
        self.assertTrue(any("Telegram turn ended without an answer" in line for line in logs.output))
        self.assertFalse(self.b._turn_active.is_set())
        self.assertEqual(self.b.tg.sent, [])

    def test_late_codex_task_complete_does_not_close_new_turn(self):
        self.b.cfg.agent = "codex"
        self.b._reader = CodexReader()
        self.append({"type": "event_msg", "payload": {"type": "user_message", "message": "Heartbeat"}})
        self.b._drain_transcript()
        self.b._begin_turn()
        self.append({"type": "event_msg", "payload": {"type": "task_complete"}})
        self.tick()
        self.assertTrue(self.b._turn_active.is_set(), "old task_complete closed the new turn")
        self.append({"type": "event_msg", "payload": {"type": "user_message", "message": "Telegram: Check"}},
                    {"type": "event_msg", "payload": {"type": "task_complete"}})
        with self.assertLogs(attach.log, level="ERROR"):
            self.tick()
        self.assertFalse(self.b._turn_active.is_set())

    def test_codex_turn_id_rejects_old_end_after_the_new_prompt(self):
        self.b.cfg.agent = "codex"
        self.b._reader = CodexReader()
        self.b._begin_turn()
        self.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "new"}},
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "Telegram: Check"}},
                    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old"}})
        self.tick()
        self.assertTrue(self.b._turn_active.is_set())
        self.append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "new"}})
        with self.assertLogs(attach.log, level="ERROR"):
            self.tick()
        self.assertFalse(self.b._turn_active.is_set())

    def test_unread_old_telegram_prompt_is_before_actual_delivery_boundary(self):
        self.append(self.prompt("old", "Telegram: Previous question"))
        self.assertEqual(self.b._tpos, 0)  # the tailer has not caught up with the file
        self.b._begin_turn()
        self.hook()
        self.tick()
        self.assertTrue(self.b._turn_active.is_set())

    def test_hook_finds_current_prompt_before_large_tool_and_harness_records(self):
        self.start_telegram_turn()
        self.append(self.prompt("telegram", "Telegram: Please check"),
                    {"type": "user", "toolUseResult": {}, "message": {"content": [
                        {"type": "tool_result", "content": "x" * 150000}]}},
                    {"type": "user", "isCompactSummary": True,
                     "message": {"content": "Compaction summary"}})
        self.hook()
        marker = json.loads(self.b._turn_end.read_text("utf-8"))
        self.assertEqual(marker["user_uuid"], "telegram")
        with self.assertLogs(attach.log, level="ERROR"):
            self.tick()
        self.assertFalse(self.b._turn_active.is_set(), "genuinely unanswered turn lost its backstop")

    def test_late_flushed_old_prompt_is_rejected_by_timestamp(self):
        self.start_telegram_turn()
        prompt = self.prompt("old-telegram", "Telegram: Earlier question")
        prompt["timestamp"] = "2020-01-01T00:00:00Z"
        self.append(prompt)
        self.hook()
        self.tick()
        self.assertTrue(self.b._turn_active.is_set())

    def test_new_transcript_accepts_current_prompt_by_timestamp(self):
        self.start_telegram_turn()
        self.b._transcript = self.b._transcript.with_name("new-session.jsonl")
        self.b._tpos = 0
        prompt = self.prompt("new-telegram", "Telegram: Please check")
        prompt["timestamp"] = datetime.now(timezone.utc).isoformat()
        self.append(prompt)
        self.hook()
        with self.assertLogs(attach.log, level="ERROR"):
            self.tick()
        self.assertFalse(self.b._turn_active.is_set())

    def test_idle_fallback_still_reports_a_truly_unanswered_turn(self):
        self.start_telegram_turn()
        self.b._last_activity = time.monotonic() - attach.IDLE_DONE_HOOKED - 1
        with self.assertLogs(attach.log, level="ERROR"):
            self.tick()
        self.assertFalse(self.b._turn_active.is_set())

    def test_new_input_between_hook_detection_and_handling_keeps_turn_open(self):
        self.start_telegram_turn()
        self.append(self.prompt("telegram", "Telegram: Please check"))
        self.hook()
        end_turn = self.b._end_turn

        def new_input_before_handling(**kwargs):
            self.b._begin_turn()  # consumes the old marker after the loop observed it
            end_turn(**kwargs)

        self.b._end_turn = new_input_before_handling
        self.tick()
        self.assertTrue(self.b._turn_active.is_set())

    def test_new_input_during_final_transcript_drain_keeps_turn_open(self):
        self.start_telegram_turn()
        self.append(self.prompt("telegram", "Telegram: Please check"))
        self.hook()
        drain = self.b._drain_transcript
        calls = 0

        def new_input_during_drain():
            nonlocal calls
            calls += 1
            drain()
            if calls == 2:  # the extra drain inside _end_turn
                self.b._begin_turn()

        self.b._drain_transcript = new_input_during_drain
        self.tick()
        self.assertTrue(self.b._turn_active.is_set())

    def test_repeated_codex_prompt_still_binds_its_own_turn_end(self):
        self.b.cfg.agent = "codex"
        self.b._reader = CodexReader()
        prompt = {"type": "event_msg", "payload": {"type": "user_message", "message": "Telegram: Check"}}
        self.append(prompt)
        self.b._drain_transcript()  # reader now deduplicates the same text
        self.b._begin_turn()
        self.append(prompt, {"type": "event_msg", "payload": {"type": "task_complete"}})
        with self.assertLogs(attach.log, level="ERROR"):
            self.tick()
        self.assertFalse(self.b._turn_active.is_set())

    def test_truncated_transcript_accepts_current_prompt_by_timestamp(self):
        self.start_telegram_turn()
        self.b._transcript.write_text("", encoding="utf-8")
        prompt = self.prompt("telegram", "Telegram: Please check")
        prompt["timestamp"] = datetime.now(timezone.utc).isoformat()
        self.append(prompt)
        self.hook()
        with self.assertLogs(attach.log, level="ERROR"):
            self.tick()
        self.assertFalse(self.b._turn_active.is_set())


if __name__ == "__main__":
    unittest.main()
