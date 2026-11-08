"""A pre-delivery wait must not exhaust the durable inbox's failure budget."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent2telegram import attach
from agent2telegram.durable import DurableInbox


# Keep the behavioral reproduction runnable against the unpatched bridge too.
Deferred = getattr(attach, "InboundDeferred", type("InboundDeferred", (Exception,), {}))


class InboundDeferralTests(unittest.TestCase):
    def bridge(self, root):
        bridge = object.__new__(attach.AttachBridge)
        bridge._init_inbound_worker_state()
        bridge._inbound_worker_started = True  # Drive the worker synchronously.
        bridge._inbox = DurableInbox(root)
        bridge._stop = Mock(wraps=threading.Event())
        bridge._stop.wait.return_value = False
        failed = bridge._inbound_failed

        def fail_and_stop(record, reason):
            failed(record, reason)
            bridge._stop.set()

        bridge._inbound_failed = fail_and_stop
        return bridge

    def enqueue(self, bridge, identity):
        update = {"update_id": identity, "message": {"text": "private fixture"}}
        record = bridge._inbox.reserve(update)
        bridge._submit_inbound_update(update, record)
        return record

    def test_many_deferrals_preserve_fifo_inflight_and_failure_budget(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = self.bridge(Path(root))
            first, second = self.enqueue(bridge, 1), self.enqueue(bridge, 2)
            for _ in range(4):
                bridge._inbox.fail(first, "earlier failure")
            calls = []

            def handle(update):
                calls.append(update["update_id"])
                if len(calls) <= 8:
                    self.assertEqual(bridge._replay_pending_inbound(), 0)
                    raise Deferred("session_busy")
                if update["update_id"] == 2:
                    bridge._stop.set()
                return True

            bridge._handle = handle
            with self.assertLogs(attach.log, level="INFO") as logs:
                bridge._inbound_worker_loop()
            self.assertEqual(calls, [1] * 9 + [2])
            self.assertEqual([call.args[0] for call in bridge._stop.wait.call_args_list],
                             [1, 2, 4, 8, 16, 30, 30, 30])
            self.assertEqual(bridge._inbox.pending(), [])
            self.assertEqual(bridge._inbox.dead_letters(), [])
            self.assertEqual(bridge._inbound_inflight(), set())
            self.assertEqual(bridge._inbound_queue.unfinished_tasks, 0)
            self.assertNotIn("private fixture", "\n".join(logs.output))
            self.assertNotIn(second, bridge._inbound_inflight())

    def test_shutdown_during_deferral_preserves_both_records_for_restart(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = self.bridge(Path(root))
            first, second = self.enqueue(bridge, 1), self.enqueue(bridge, 2)
            bridge._handle = Mock(side_effect=Deferred("lifecycle_busy"))
            bridge._stop.wait.side_effect = lambda _: bridge._stop.set() or True
            bridge._inbound_worker_loop()
            self.assertEqual(bridge._handle.call_count, 1)
            self.assertEqual([row.attempts for row in bridge._inbox.pending()], [0, 0])
            self.assertEqual(bridge._inbound_inflight(), set())
            self.assertEqual(bridge._inbound_queue.unfinished_tasks, 0)

            restarted = self.bridge(Path(root))
            self.assertEqual(restarted._replay_pending_inbound(), 2)
            delivered = []

            def handle(update):
                delivered.append(update["update_id"])
                if len(delivered) == 2:
                    restarted._stop.set()
                return True

            restarted._handle = handle
            restarted._inbound_worker_loop()
            self.assertEqual(delivered, [1, 2])
            self.assertEqual(restarted._inbox.pending(), [])
            self.assertEqual(restarted._inbox.dead_letters(), [])
            self.assertNotIn(first, restarted._inbound_inflight())
            self.assertNotIn(second, restarted._inbound_inflight())

    def test_shutdown_before_processing_never_calls_handler_or_counts_failure(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = self.bridge(Path(root))
            self.enqueue(bridge, 1)
            bridge._stop.set()
            bridge._handle = Mock(return_value=False)
            bridge._inbound_worker_loop()
            bridge._handle.assert_not_called()
            self.assertEqual(bridge._inbox.pending()[0].attempts, 0)
            self.assertEqual(bridge._inbound_inflight(), set())

    def test_waiting_update_survives_long_wait_and_restart_without_age_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = self.bridge(Path(root))
            bridge._offset_file = Path(root) / "offset"
            bridge._inbox = None
            inbox = bridge._ensure_inbox()
            record = inbox.reserve({"update_id": 1})
            inbox.save_offset(2)
            old = time.time() - 60 * 24 * 60 * 60
            os.utime(inbox.pending_directory / (record + ".json"), (old, old))
            bridge._inbox = None
            restarted = bridge._ensure_inbox()
            self.assertEqual([row.id for row in restarted.pending()], [record])
            self.assertEqual(restarted.pending()[0].attempts, 0)
            self.assertEqual(restarted.dead_letters(), [])
            self.assertEqual(restarted.load_offset(), 2)

    def test_real_failures_still_reach_dead_letter(self):
        for outcome in (False, RuntimeError("fixture failure")):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as root:
                bridge = self.bridge(Path(root))
                record = self.enqueue(bridge, 1)
                for _ in range(4):
                    bridge._inbox.fail(record, "earlier failure")

                def handle(_):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                bridge._handle = handle
                bridge._inbound_worker_loop()
                self.assertEqual(bridge._inbox.pending(), [])
                self.assertEqual(bridge._inbox.dead_letters()[0].attempts, 5)
