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
        # the Stop of the follow-up turn (the hook names the queued human record) ends its route: no idle wait
        r.bridge._idle_limit = lambda: 10 ** 9
        self.stop_marker()
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none", "the real Stop of the follow-up turn ends the route")

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


class QueuedCommandRouteEndsWithItsTurnsStopTests(RigTestCase):
    """A route opened by a queued_command during a terminal-started turn ends with the Stop of THAT turn, not by the idle wait."""

    def open_route_during_a_terminal_turn(self, prompt="typed over SSH"):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9                         # the idle fallback can never help
        r.terminal(prompt)                                             # a terminal-started turn is running
        self.assertTrue(r.tg(OWNER, "from Telegram into the running turn"))
        r.append({"type": "attachment", "uuid": "qc-open", "attachment": {"type": "queued_command", "prompt": r.typed[-1]}})
        r.drain()
        self.assertEqual(r.bridge._route.kind, "tg")

    def test_the_stop_of_the_turn_that_ran_the_queued_command_ends_the_route_and_the_answer_is_delivered(self):
        r = self.rig
        self.open_route_during_a_terminal_turn()
        r.say("[tg] the common answer")
        r.stop()                                                       # the hook names the terminal human prompt
        r.tick()
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none", "ended by the Stop, without any idle wait")
        self.assertEqual(r.sent_to(OWNER), ["the common answer"])
        self.assertIsNone(r.n1.turn())

    def stale_marker(self):
        """A Stop marker that names the OLDER terminal prompt (written by the real hook helper, as an older turn's Stop would)."""
        from agent2telegram import stop_hook
        from agent2telegram.readers import ClaudeCodeReader, latest_user
        r = self.rig
        offset, rec = latest_user(r.transcript, ClaudeCodeReader())
        return {"transcript_path": str(r.transcript.resolve()), "user_offset": offset, "user_uuid": rec.get("uuid")}, stop_hook

    def test_a_stop_naming_an_older_prompt_does_not_end_it(self):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        r.terminal("an older terminal prompt")
        older, stop_hook = self.stale_marker()
        self.open_route_during_a_terminal_turn("a newer terminal prompt")
        stop_hook._mark(str(r.bridge._signal), older)                  # the Stop of the older turn arrives late
        r.say("[tg] the answer")
        r.tick()
        self.assertEqual(r.bridge._route.kind, "tg", "the stale marker of an older turn must not end the newer route")
        self.assertEqual(r.sent_to(OWNER), ["the answer"])
        r.stop()                                                       # now the Stop of the running turn
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none")

    def test_a_restart_in_between_keeps_the_identity(self):
        r = self.rig
        self.open_route_during_a_terminal_turn()
        r.new_bridge()
        r.bridge._idle_limit = lambda: 10 ** 9
        self.assertEqual(r.bridge._route.kind, "tg", "the stored turn carries the route over the restart")
        r.say("[tg] answer after the restart")
        r.stop()
        r.tick()
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none")
        self.assertEqual(r.sent_to(OWNER), ["answer after the restart"])

    def test_a_stale_marker_is_refused_after_a_restart_too(self):
        r = self.rig
        r.terminal("an older terminal prompt")
        older, stop_hook = self.stale_marker()
        self.open_route_during_a_terminal_turn("a newer terminal prompt")
        r.new_bridge()
        r.bridge._idle_limit = lambda: 10 ** 9
        stop_hook._mark(str(r.bridge._signal), older)
        r.tick()                                                       # the stale marker meets the restored route
        self.assertEqual(r.bridge._route.kind, "tg", "identity survives the restart (no injected-turn boundary exists then)")


class AnchorWhenTheStoredTurnWasClearedMidTurnTests(RigTestCase):
    """The stored turn can be None while Claude is still inside a turn; the anchor is the latest boundary record, not the turn."""

    def run_queued_input(self, clear, prompt="typed over SSH"):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        clear(r, prompt)
        self.assertIsNone(r.n1.turn(), "the stored turn is None while the turn runs")
        self.assertTrue(r.tg(OWNER, "from Telegram into the running turn"))
        r.append({"type": "attachment", "uuid": "qc-none", "attachment": {"type": "queued_command", "prompt": r.typed[-1]}})
        r.drain()
        self.assertEqual(r.bridge._route.kind, "tg")
        r.say("[tg] the common answer")
        r.stop()                                                       # the Stop of the running turn names its prompt
        r.tick()
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none", "ended by that Stop, without the idle fallback")
        self.assertEqual(r.sent_to(OWNER), ["the common answer"])

    def test_idle_closed_store_turn_while_the_terminal_turn_runs(self):
        def clear(r, prompt):
            r.terminal(prompt)
            r.n1.close_turn()                                          # e.g. the idle fallback closed the stored turn
        self.run_queued_input(clear)

    def test_a_dead_result_job_ended_the_stored_turn_while_claude_still_runs_it(self):
        from n1_rig import notification
        clock = type("C", (), {"t": 1_000_000.0, "__call__": lambda s: s.t})()

        def clear(r, prompt):
            r.n1._clock = clock
            r.tg_turn(OWNER, "A")
            r.launch("tA")
            r.end_turn()
            r.append(notification("tA", "notif-uuid"))                # Claude starts processing the completion
            r.drain()
            self.assertEqual(r.n1.turn()["kind"], "notif")
            clock.t += 86401 * 2                                       # the job is dead (T2): the stored turn ends
            r.n1.expire()
        self.run_queued_input(clear)

    def test_an_older_stop_is_still_refused_when_the_stored_turn_was_cleared(self):
        from agent2telegram import stop_hook
        from agent2telegram.readers import ClaudeCodeReader, latest_user
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        r.terminal("an older terminal prompt")
        offset, rec = latest_user(r.transcript, ClaudeCodeReader())
        older = {"transcript_path": str(r.transcript.resolve()), "user_offset": offset, "user_uuid": rec.get("uuid")}
        r.terminal("the prompt of the running turn")
        r.n1.close_turn()
        self.assertTrue(r.tg(OWNER, "from Telegram"))
        r.append({"type": "attachment", "uuid": "qc-none", "attachment": {"type": "queued_command", "prompt": r.typed[-1]}})
        r.drain()
        stop_hook._mark(str(r.bridge._signal), older)
        r.tick()
        self.assertEqual(r.bridge._route.kind, "tg", "a Stop naming an older prompt never ends it; there is no any-Stop fallback")

    def test_a_reset_clears_the_boundary_so_nothing_older_can_anchor(self):
        import os
        r = self.rig
        r.terminal("old")
        self.assertIsNotNone(r.n1.snapshot()["boundary_uuid"])
        replacement = r.tmp / "new.jsonl"
        replacement.write_text(json.dumps({"type": "user", "uuid": "z0", "origin": {"kind": "human"},
                                           "message": {"role": "user", "content": "another session"}}) + "\n")
        os.replace(replacement, r.transcript)
        r.drain()
        self.assertIsNone(r.n1.snapshot()["boundary_uuid"])


if __name__ == "__main__":
    unittest.main()
