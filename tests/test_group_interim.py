"""Group turns expose only their final answer unless explicitly opted into progress."""
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from agent2telegram.config import Config, ConfigError
from agent2telegram.readers import ClaudeCodeReader, CodexReader, Ev
from test_attach_backstop import _bridge


class GroupInterimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = _bridge(self.tmp.name)
        self.b.cfg.reply_to_origin_chat = True
        self.b.cfg.allowed_group_ids = [-500]
        self.b._reader = ClaudeCodeReader()
        self.b._origins = ("Telegram:",)
        self.b._tpos = 0
        self.b._transcript.write_text("")
        self.b._turn_active.clear()
        self.b.tg = Mock()
        self.b.tg.send_plain_id.return_value = 55
        self.b._handle({"message": {"from": {"id": 7},
                                   "chat": {"id": -500, "type": "supergroup"},
                                   "text": "Please check", "message_id": 1}})

    def append(self, *records):
        with self.b._transcript.open("a") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
        self.b._drain_transcript()

    def text(self, text, key, **message):
        return {"type": "assistant", "uuid": key,
                "message": {"content": [{"type": "text", "text": text}], **message}}

    def assert_no_progress(self):
        self.b.tg.send_message.assert_not_called()
        self.b.tg.send_plain_id.assert_not_called()
        self.b.tg.edit_plain.assert_not_called()
        self.b.tg.send_chat_action.assert_not_called()

    def test_group_turn_holds_narration_and_delivers_only_final_with_file(self):
        self.b.cfg.outbox_dirs = [str(self.b._signal.parent)]
        attachment = (self.b._signal.parent / "report.txt").resolve()
        attachment.write_text("Synthetic report")
        self.b._use_durable_outbox = True
        self.b._queue_path = self.b._signal.parent / "pending.jsonl"
        self.append(self.text("[tg] Private work", "intro"),
                    {"type": "assistant", "message": {"content": [
                        {"type": "tool_use", "id": "t1", "name": "Read",
                         "input": {"file_path": "/private/notes"}}]}})
        self.b._status_push("Private tool detail", background=True)
        self.assert_no_progress()
        self.append(self.text(f"[tg] Final answer\n[tg-file] {attachment}", "final"))
        self.assert_no_progress()
        self.b._end_turn()
        self.b._flush_pending()
        self.b.tg.send_message.assert_called_once_with(-500, "Final answer")
        self.b.tg.send_file.assert_called_once_with(-500, attachment)

    def test_dm_and_explicit_group_opt_in_keep_live_progress(self):
        for chat, enabled in ((7, False), (-500, True)):
            with self.subTest(chat=chat, enabled=enabled):
                self.b._owner_chat = chat
                self.b.cfg.group_interim = enabled
                self.b.tg.reset_mock()
                self.b._handle_event(Ev("text", text="Working", key=str(chat)))
                self.b._status_push("Tool detail")
                self.b.tg.send_message.assert_called_once_with(chat, "Working")
                self.b.tg.send_plain_id.assert_called_once()

    def test_typing_loop_and_queue_ack_are_silent_in_group(self):
        self.assertEqual(self.b._typing_count, 0)
        self.b._stop = Mock()
        self.b._stop.is_set.side_effect = [False, True]
        self.b._typing_loop()
        self.b._maybe_ack_queued({"message": {"from": {"id": 7},
                                             "chat": {"id": -500}, "text": "Next"}})
        self.assert_no_progress()

    def test_tui_scraper_does_not_read_private_tool_lines_for_group(self):
        self.b._session = Mock()
        self.b._stop = Mock()
        self.b._stop.is_set.side_effect = [False, True]
        self.b._tui_scrape_loop()
        self.b._session._capture.assert_not_called()
        self.assert_no_progress()

    def test_waiting_for_another_chat_keeps_group_silent_and_destination_bound(self):
        self.b._owner_chat = 7
        with patch("agent2telegram.attach.CHAT_SWITCH_MAX_WAIT", 0), \
                patch("agent2telegram.attach.CHAT_SWITCH_POLL", .01):
            thread = threading.Thread(target=self.b._wait_for_turn_before_chat_switch, args=(-500,))
            thread.start()
            try:
                time.sleep(.05)
                self.assertTrue(thread.is_alive())
                self.assertEqual(self.b._owner_chat, 7)
                self.assert_no_progress()
            finally:
                self.b._turn_active.clear()
                thread.join(2)

    def test_outbound_loop_observes_hook_and_delivers_unclassified_final(self):
        self.append(self.text("Final answer", "final"))
        self.b._turn_end = self.b._signal.parent / "turn_end"
        self.b._turn_end.write_text("1")
        self.b._maybe_reresolve = Mock()
        self.b._replay_pending_inbound = Mock()
        self.b._heartbeat = None
        self.b._stop = Mock()
        self.b._stop.is_set.side_effect = [False, True]
        self.b._outbound_loop()
        self.b.tg.send_message.assert_called_once_with(-500, "Final answer")
        self.assertFalse(self.b._turn_active.is_set())

    def test_codex_final_survives_reader_dedup_and_is_delivered_once(self):
        self.b.cfg.agent = "codex"
        self.b._reader = CodexReader()
        def record(text, phase):
            return {"type": "response_item", "timestamp": text,
                    "payload": {"type": "message", "role": "assistant", "phase": phase,
                                "content": [{"type": "output_text", "text": text}]}}
        self.append(record("Private commentary", "commentary"), record("Final answer", "final_answer"))
        self.assert_no_progress()
        self.append({"type": "event_msg", "payload": {"type": "task_complete"}})
        self.b._finish_turn()
        self.b._finish_turn()
        self.b.tg.send_message.assert_called_once_with(-500, "Final answer")

    def test_codex_commentary_never_becomes_final_at_turn_end(self):
        self.b.cfg.agent = "codex"
        self.b._reader = CodexReader()
        self.append({"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"type": "output_text", "text": "Private commentary"}]}})
        with patch("agent2telegram.attach.BACKSTOP_RETRY_DELAY", 0):
            self.b._finish_turn()
        self.assert_no_progress()

    def test_backstop_recovers_final_without_live_candidate(self):
        self.append(self.text("Private intro", "intro"), self.text("Final answer", "final"))
        self.b._group_reply = None  # exercise the independent transcript recovery path
        self.b._end_turn()
        self.b.tg.send_message.assert_called_once_with(-500, "Final answer")

    def test_backstop_does_not_duplicate_file_markers_already_withheld(self):
        attachment = (self.b._signal.parent / "report.txt").resolve()
        attachment.write_text("Report")
        self.b.cfg.outbox_dirs = [str(attachment.parent)]
        self.b._use_durable_outbox = True
        self.b._queue_path = attachment.parent / "pending.jsonl"
        self.append(self.text(f"Final answer\n[tg-file] {attachment}", "final"))
        self.b._group_reply = None
        self.b._end_turn()
        self.assertIsNotNone(self.b._ensure_outbox().head())
        self.assertEqual(self.b._ensure_outbox().head().files, (str(attachment),))
        self.b._flush_pending()
        self.b.tg.send_message.assert_called_once_with(-500, "Final answer")
        self.b.tg.send_file.assert_called_once_with(-500, attachment)

    def test_inactivity_does_not_promote_unclassified_text(self):
        self.append(self.text("Possibly still working", "intro"))
        with patch("agent2telegram.attach.BACKSTOP_RETRY_DELAY", 0):
            self.b._end_turn(confirmed=False)
        self.assert_no_progress()

    def test_native_file_is_held_until_turn_end(self):
        attachment = (self.b._signal.parent / "report.txt").resolve()
        attachment.write_text("Report")
        self.b.cfg.outbox_dirs = [str(attachment.parent)]
        self.b._use_durable_outbox = True
        self.b._queue_path = attachment.parent / "pending.jsonl"
        self.b._handle_event(Ev("files", key="f1", files=(str(attachment),)))
        self.b.tg.send_file.assert_not_called()
        self.append(self.text("Final answer", "final"))
        self.b._end_turn()
        self.b._flush_pending()
        self.b.tg.send_message.assert_called_once_with(-500, "Final answer")
        self.b.tg.send_file.assert_called_once_with(-500, attachment)

    def test_interim_file_marker_keeps_attachment_without_narration(self):
        attachment = (self.b._signal.parent / "report.txt").resolve()
        attachment.write_text("Report")
        self.b.cfg.outbox_dirs = [str(attachment.parent)]
        self.b._use_durable_outbox = True
        self.b._queue_path = attachment.parent / "pending.jsonl"
        self.append(self.text(f"Private commentary\n[tg-file] {attachment}", "intro", stop_reason="tool_use"),
                    {"type": "assistant", "message": {"content": [
                        {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
                    self.text("Final answer", "final"))
        self.assert_no_progress()
        self.b.tg.send_file.assert_not_called()
        self.b._end_turn()
        self.b._flush_pending()
        self.b.tg.send_message.assert_called_once_with(-500, "Final answer")
        self.b.tg.send_file.assert_called_once_with(-500, attachment)

    def test_backstop_does_not_promote_narration_before_a_tool(self):
        self.append(self.text("Private commentary", "intro"),
                    {"type": "assistant", "message": {"content": [
                        {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}})
        with patch("agent2telegram.attach.BACKSTOP_RETRY_DELAY", 0):
            self.b._end_turn()
        self.assert_no_progress()

    def test_background_marker_and_signal_cannot_reuse_last_group(self):
        self.b._turn_active.clear()
        self.b._turn_from_tg = False
        self.b._handle_event(Ev("text", text="[tg] Background narration", key="bg"))
        self.b._signal.write_text("Background signal")
        self.b._drain_signal()
        self.assert_no_progress()

    def test_group_marker_from_dm_waits_for_final_even_after_dm_progress(self):
        self.b._owner_chat = 7
        self.b.cfg.group_marker = "[team]"
        self.b._handle_event(Ev("text", text="Working privately", key="dm"))
        self.b.tg.reset_mock()
        self.append(self.text("[team] Private commentary", "intro"),
                    self.text("[team] Shared final", "final", stop_reason="end_turn"))
        self.b.tg.send_message.assert_not_called()
        self.b._end_turn()
        self.b.tg.send_message.assert_called_once_with(-500, "Shared final")

    def test_background_watcher_never_ticks_for_suppressed_group_destination(self):
        self.b._bg_watcher = Mock(chat_id=-500)
        self.b._stop = Mock()
        self.b._stop.is_set.side_effect = [False, True]
        self.b._background_loop()
        self.b._bg_watcher.tick.assert_not_called()

    def test_config_defaults_off_and_rejects_non_booleans(self):
        config = Config(agent="generic", token="1:2")
        self.assertIs(config.group_interim, False)
        for invalid in (None, 0, 1, "false", []):
            config.group_interim = invalid
            with self.subTest(value=invalid), self.assertRaises(ConfigError):
                config.validate()

    def test_disabled_file_marker_keeps_final_text(self):
        self.b.cfg.file_marker = ""
        self.append(self.text("Final answer", "final"))
        self.b._end_turn()
        self.b.tg.send_message.assert_called_once_with(-500, "Final answer")
