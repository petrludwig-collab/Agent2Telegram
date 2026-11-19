"""Pilot 2, scenario S3a (Claude Code 2.1.328): a follow-up message of the SAME chat during a running answer.

CAPTURED (private raw trace, fixture = the relevant records byte-exact per line except sanitised paths/thinking/hook command):
the follow-up arrives as `queue-operation enqueue`; the answer of turn 1 is written; `queue-operation dequeue`, `system
stop_hook_summary` / `turn_duration` (the Stop hook of turn 1 ran 37 ms and finished BEFORE the next record existed) follow;
then a SEPARATE `user` record origin.kind=human promptSource=queued with the exact envelope (its own turn) and its answer.
The store consumed that nonce as `human`; the answer to it never reached Telegram.

NOT CAPTURED: the bridge log, i.e. in which order the bridge processed the Stop marker of turn 1 and the queued human record.
Because the hook finished before the queued record was written, the marker of turn 1 can only NAME THE PROMPT OF TURN 1 (derived,
not captured). These tests replay the plausible processing orders explicitly through the real reader, route, forwarding, Stop hook
marker and turn end. The envelopes in the fixture are replaced by the ones the bridge really typed (nonces are random);
timestamps are shifted to 'now' (the turn-end identity compares them with the wall clock).
"""
import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from n1_rig import OWNER, RigTestCase

FIXTURE = Path(__file__).parent / "fixtures" / "pilot2_s3a_records.jsonl"
FOLLOW_ANSWER = "S3A-EXTRA-SAME-CHAT"
# fixture line numbers
HUMAN_1, ENQUEUE, THINKING, ANSWER_1, DEQUEUE, STOP_SUMMARY_1, DURATION_1, SNAPSHOT, HUMAN_QUEUED, ANSWER_2, \
    STOP_SUMMARY_2, DURATION_2 = range(12)


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def s3a_records(first_prompt, follow_prompt, now):
    records = [json.loads(line) for line in FIXTURE.read_text("utf-8").splitlines()]
    first = datetime.strptime(records[HUMAN_1]["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    for rec in records:
        if "timestamp" in rec:
            old = datetime.strptime(rec["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
            rec["timestamp"] = _iso(now + (old - first))
    records[HUMAN_1]["message"]["content"] = first_prompt
    records[ENQUEUE]["content"] = follow_prompt
    records[HUMAN_QUEUED]["message"]["content"] = follow_prompt
    return records


class S3aReplayTests(RigTestCase):
    def begin(self):
        """Message 1 typed and recorded (a Telegram route), the follow-up of the same chat typed at once (type-ahead)."""
        r = self.rig
        self.assertTrue(r.tg(OWNER, "Pilot S3a. Without any tools write 250 lines"))
        first = r.typed[-1]
        self.assertTrue(r.tg(OWNER, "Pilot S3a follow-up from the same chat"))   # injected immediately, no waiting
        self.recs = s3a_records(first, r.typed[-1], time.time())
        r.append(self.recs[HUMAN_1])
        r.drain()
        self.assertEqual(r.bridge._route.kind, "tg")

    def feed(self, first, last):
        self.rig.append(*self.recs[first:last + 1])

    def finish_and_check(self):
        r = self.rig
        r.tick()
        r.tick()
        sent = r.sent_to(OWNER)
        self.assertEqual(sent.count(FOLLOW_ANSWER), 1, f"the answer to the follow-up must be delivered once: {sent}")
        self.assertEqual(len([t for t in sent if t.startswith("1 SAMEA")]), 1, "and so must the first answer")

    def stop_marker(self):
        self.rig.stop()                       # the real Stop hook entrypoint, reading the transcript as it is NOW

    # ---- the plausible processing orders ---------------------------------------------------------------------
    def test_order_marker_of_turn_1_is_processed_before_the_queued_human_record(self):
        r = self.rig
        self.begin()
        self.feed(ENQUEUE, ANSWER_1)
        self.stop_marker()                    # hook of turn 1: it can only name the prompt of turn 1
        r.tick()                              # bridge: marker first -> turn 1 ends
        self.feed(DEQUEUE, DURATION_2)
        self.finish_and_check()

    def test_order_queued_human_record_is_read_before_the_marker_but_its_answer_after_it(self):
        r = self.rig
        self.begin()
        self.feed(ENQUEUE, ANSWER_1)
        self.stop_marker()
        self.feed(DEQUEUE, HUMAN_QUEUED)      # the bridge reads the queued human record first ...
        r.tick()                              # ... then handles the (old) marker of turn 1
        self.feed(ANSWER_2, DURATION_2)       # ... and only then the answer
        self.finish_and_check()

    def test_order_queued_human_record_and_its_answer_are_read_before_the_marker(self):
        r = self.rig
        self.begin()
        self.feed(ENQUEUE, ANSWER_1)
        self.stop_marker()
        self.feed(DEQUEUE, ANSWER_2)
        r.tick()
        self.feed(STOP_SUMMARY_2, DURATION_2)
        self.finish_and_check()

    def test_order_everything_is_written_before_the_bridge_looks_at_the_marker(self):
        self.begin()
        self.feed(ENQUEUE, ANSWER_1)
        self.stop_marker()
        self.feed(DEQUEUE, DURATION_2)
        self.finish_and_check()

    def test_the_stop_hook_of_the_follow_up_turn_ends_its_route_afterwards(self):
        r = self.rig
        self.begin()
        self.feed(ENQUEUE, ANSWER_1)
        self.stop_marker()
        self.feed(DEQUEUE, HUMAN_QUEUED)
        r.tick()
        self.feed(ANSWER_2, DURATION_2)
        r.tick()
        r.bridge._idle_limit = lambda: 10 ** 9
        self.stop_marker()                    # the hook of turn 2 names the queued human record
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none")
        self.assertEqual(r.sent_to(OWNER).count(FOLLOW_ANSWER), 1)


class S3aQueueReceiptAndModesTests(RigTestCase):
    def test_a_queue_receipt_is_neither_an_answer_nor_a_switch_of_the_recipient(self):
        from n1_rig import GROUP
        r = self.rig
        r.tg_turn(OWNER, "route of chat A")
        self.assertTrue(r.tg(GROUP, "chat B, waiting in Claude's queue"))
        receipt = {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-11-19T05:00:26.204Z",
                   "sessionId": "s", "content": r.typed[-1]}
        r.append(receipt)
        r.drain()
        self.assertEqual((r.bridge._route.kind, r.bridge._route.chat, r.bridge._route.cut), ("tg", OWNER, False))
        r.say("[tg] still the answer of chat A")
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["still the answer of chat A"])
        self.assertEqual(r.sent_to(GROUP), [])

    def test_both_modes_deliver_the_follow_up_answer_queued_command_inside_a_turn_and_a_separate_queued_turn(self):
        from n1_rig import human
        r = self.rig
        # mode 1 (pilot 1, 2.1.326): an attachment queued_command inside the running turn; the old Stop then names the opener
        r.tg_turn(OWNER, "first, with tool calls")
        self.assertTrue(r.tg(OWNER, "follow-up one"))
        r.append({"type": "attachment", "uuid": "qc-1", "attachment": {"type": "queued_command", "prompt": r.typed[-1]}})
        r.drain()
        r.say("[tg] answer to both, inside one turn")
        r.stop()
        r.tick()
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none")
        # mode 2 (pilot 2, 2.1.328): a separate human promptSource=queued turn after the Stop of the previous one
        r.tg_turn(OWNER, "second")
        self.assertTrue(r.tg(OWNER, "follow-up two"))
        queued_prompt = r.typed[-1]
        r.stop()                                                       # hook of the running turn names the opener
        r.append(human(queued_prompt, "queued-human-2"))
        r.bridge._drain_transcript()
        r.tick()                                                       # the OLD marker meets the new route
        self.assertEqual(r.bridge._route.kind, "tg", "an old turn end must not cancel the new answer")
        r.say("[tg] answer to the queued follow-up")
        r.tick()
        self.assertEqual(r.sent_to(OWNER).count("answer to the queued follow-up"), 1)


if __name__ == "__main__":
    unittest.main()
