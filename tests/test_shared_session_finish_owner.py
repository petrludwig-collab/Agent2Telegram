"""Pilot 3 (Claude Code 2.1.328): finishing answer 1 must not release the route of the follow-up that Claude runs next.

CAPTURED (bridge log of pilot 3, sanitised): the Stop marker of turn 1 names the prompt of turn 1 and is ACCEPTED; inside the finishing
the bridge reads the rest of the transcript (answer 1, `dequeue`, `stop_hook_summary`, `turn_duration` and the NEW `user` human
promptSource=queued record of turn 2, which opens its tg route for the same chat); then the finishing of turn 1 releases ... the NEW route
(`turn_end` + `_n1_exit`); answer 2 is dropped by the send gate and turn 2's Stop finds route none.

The records are the relevant records of the pilot-2 trace (same shape, fixtures/pilot2_s3a_records.jsonl); the ORDER is the captured one
of pilot 3: the inner drain of the finishing (the backstop wait of `_finish_turn`) is where the new record arrives. The test drives the
real Stop marker, `_end_turn` -> `_hook_ends_current_turn` -> `_finish_turn` -> backstop retry -> inner `_drain_transcript` -> `_n1_turn_ended`
and the real send gate; the new record is NOT read in the outer tick before the Stop check.
"""
import time
import unittest

from n1_rig import GROUP, OWNER, RigTestCase
from test_shared_session_s3a import (ANSWER_1, ANSWER_2, DURATION_2, ENQUEUE, FOLLOW_ANSWER, HUMAN_1, HUMAN_QUEUED, THINKING,
                                     s3a_records)


class FinishOwnershipTests(RigTestCase):
    def begin(self, follow_chat=OWNER):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9                         # no idle fallback in any of these tests
        self.assertTrue(r.tg(OWNER, "Pilot S3a. Without any tools write 250 lines"))
        first = r.typed[-1]
        self.assertTrue(r.tg(follow_chat, "Pilot S3a follow-up"))
        self.recs = s3a_records(first, r.typed[-1], time.time())
        r.append(self.recs[HUMAN_1])
        r.drain()
        self.assertEqual(r.bridge._route.kind, "tg")
        r.append(*self.recs[ENQUEUE:THINKING + 1])                     # the follow-up was queued; no answer text yet
        r.drain()

    def feed(self, first, last):
        self.rig.append(*self.recs[first:last + 1])

    def finish_turn_one_while_the_rest_of_the_transcript_arrives(self):
        """The correct Stop of turn 1 is accepted; during its finishing (the backstop wait) the rest of the transcript arrives:
        answer 1, dequeue, the stop summary and the NEW queued human record of turn 2."""
        r = self.rig
        r.stop()                                                       # the real hook: it names the prompt of turn 1
        waits = []
        real_wait = r.bridge._wait_backstop_retry

        def arrive_during_the_wait():
            if not waits:
                waits.append(1)
                self.feed(ANSWER_1, HUMAN_QUEUED)                      # read by the INNER drain of the finishing
            real_wait()
        r.bridge._wait_backstop_retry = arrive_during_the_wait
        r.tick()                                                       # outer tick: drain, marker, _end_turn -> _finish_turn
        r.bridge._wait_backstop_retry = real_wait
        self.assertEqual(waits, [1], "the finishing really waited for the transcript (the captured order)")
        r.drain()                                                      # the next normal read: the new turn starts here

    def check_the_new_route_survived(self, chat):
        r = self.rig
        self.assertEqual((r.bridge._route.kind, r.bridge._route.chat), ("tg", chat), "the new route must survive the finishing")
        self.assertEqual(r.n1.turn()["kind"], "tg")
        self.assertTrue(r.bridge._turn_active.is_set())
        self.assertTrue(r.bridge._turn_from_tg)

    def test_same_chat_finishing_turn_one_keeps_the_route_of_turn_two_and_answer_two_is_delivered(self):
        r = self.rig
        self.begin()
        self.finish_turn_one_while_the_rest_of_the_transcript_arrives()
        self.check_the_new_route_survived(OWNER)
        self.feed(ANSWER_2, DURATION_2)                                # answer 2 arrives later
        r.tick()
        r.tick()
        sent = r.sent_to(OWNER)
        self.assertEqual(sent.count(FOLLOW_ANSWER), 1, "answer 2 delivered exactly once")
        self.assertEqual(len([t for t in sent if t.startswith("1 SAMEA")]), 1, "answer 1 delivered exactly once")
        r.stop()                                                       # the Stop of turn 2 (names the queued human record)
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none", "turn 2 ends with ITS Stop, no idle fallback")
        self.assertIsNone(r.n1.turn())

    def test_the_stop_marker_of_a_short_next_turn_written_during_the_finishing_survives_and_ends_that_turn(self):
        r = self.rig
        self.begin()
        r.stop()                                                       # the marker of turn 1
        arrived = []
        real_wait = r.bridge._wait_backstop_retry

        def turn_two_runs_and_stops_meanwhile():
            if not arrived:
                arrived.append(1)
                self.feed(ANSWER_1, DURATION_2)                        # turn 2 is short: its answer is already there ...
                r.stop()                                               # ... and its Stop marker (naming the queued record)
            real_wait()
        r.bridge._wait_backstop_retry = turn_two_runs_and_stops_meanwhile
        r.tick()
        r.bridge._wait_backstop_retry = real_wait
        self.assertTrue(r.bridge._turn_end.exists(), "finishing turn 1 must not delete the marker of turn 2")
        r.tick()
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none", "turn 2 ends with ITS OWN marker, not by idle")
        self.assertEqual(r.sent_to(OWNER).count(FOLLOW_ANSWER), 1)
        self.assertEqual(len([x for x in r.sent_to(OWNER) if x.startswith("1 SAMEA")]), 1)
        self.assertFalse(r.bridge._turn_end.exists(), "and the evaluated marker is consumed")

    def test_a_new_inbound_message_does_not_drop_a_stop_marker_in_shared_session_mode(self):
        r = self.rig
        r.stop()                                                       # a marker is on disk (e.g. of a running turn)
        self.assertTrue(r.bridge._turn_end.exists())
        self.assertTrue(r.tg(OWNER, "a new message"))                  # _begin_turn runs
        self.assertTrue(r.bridge._turn_end.exists(), "the marker is dropped only by the evaluation that read it")

    def test_answer_two_read_in_the_same_inner_drain_is_covered_too(self):
        r = self.rig
        self.begin()
        r.stop()
        done = []
        real_wait = r.bridge._wait_backstop_retry

        def everything_arrives():
            if not done:
                done.append(1)
                self.feed(ANSWER_1, DURATION_2)                        # answer 1 .. the new record .. answer 2 .. its summary
            real_wait()
        r.bridge._wait_backstop_retry = everything_arrives
        r.tick()
        r.bridge._wait_backstop_retry = real_wait
        r.tick()
        r.tick()
        sent = r.sent_to(OWNER)
        self.assertEqual(sent.count(FOLLOW_ANSWER), 1)
        self.assertEqual(len([t for t in sent if t.startswith("1 SAMEA")]), 1)

    def test_different_chat_the_old_final_text_never_goes_to_the_new_recipient(self):
        r = self.rig
        self.begin(follow_chat=GROUP)
        self.finish_turn_one_while_the_rest_of_the_transcript_arrives()
        self.assertEqual((r.bridge._route.kind, r.bridge._route.chat), ("tg", GROUP), "the new turn belongs to the other chat")
        self.feed(ANSWER_2, DURATION_2)
        r.tick()
        r.stop()                                                       # a group's final answer is sent at the Stop of its turn
        r.tick()
        r.tick()
        self.assertEqual(len([t for t in r.sent_to(OWNER) if t.startswith("1 SAMEA")]), 1, "answer 1 went to ITS chat")
        self.assertEqual([t for t in r.sent_to(GROUP) if t.startswith("1 SAMEA")], [], "and never to the new recipient")
        self.assertEqual(r.sent_to(GROUP).count(FOLLOW_ANSWER), 1)
        self.assertEqual(r.sent_to(OWNER).count(FOLLOW_ANSWER), 0, "answer 2 never to the old recipient")
        self.assertEqual(r.bridge._route.kind, "none")


class GroupBufferOwnershipTests(RigTestCase):
    def new_turn(self, chat, text):
        from n1_rig import human
        r = self.rig
        self.assertTrue(r.tg(chat, text))
        r.append(human(r.typed[-1], r.next_uuid("h2")))                # the harness runs it as its own (queued) turn
        r.drain()

    def test_a_buffered_group_final_goes_to_its_own_group_when_another_chats_turn_starts(self):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        r.tg_turn(GROUP, "group question")
        r.say("[tg] the group's final answer")
        self.assertIsNotNone(r.bridge._group_reply, "withheld until the turn end (group_interim off)")
        self.new_turn(OWNER, "a DM follow-up")                         # the boundary of turn 2 is read BEFORE turn 1's Stop
        r.tick()
        self.assertEqual(r.sent_to(GROUP), ["the group's final answer"], "delivered to ITS chat at the boundary")
        self.assertEqual(r.sent_to(OWNER), [])
        r.stop()                                                       # the Stop of turn 2
        r.tick()
        r.tick()
        self.assertEqual(r.sent_to(OWNER), [], "the old final text never reaches the new recipient")
        self.assertEqual(r.sent_to(GROUP), ["the group's final answer"], "and is not sent twice")

    def test_a_revoked_group_gets_nothing_and_the_new_recipient_does_not_get_it_either(self):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        r.tg_turn(GROUP, "group question")
        r.say("[tg] the group's final answer")
        r.bridge.cfg.allowed_group_ids = []                            # the group is no longer authorised
        self.new_turn(OWNER, "a DM follow-up")
        r.tick()
        r.stop()
        r.tick()
        r.tick()
        self.assertEqual(r.sent_to(GROUP), [])
        self.assertEqual(r.sent_to(OWNER), [])

    def test_same_group_two_turns_each_final_answer_is_sent_once(self):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        r.tg_turn(GROUP, "first")
        r.say("[tg] answer one")
        self.new_turn(GROUP, "second")
        r.say("[tg] answer two")
        r.stop()
        r.tick()
        r.tick()
        self.assertEqual(r.sent_to(GROUP), ["answer one", "answer two"])


class FinishOwnershipForResultRoutesTests(RigTestCase):
    def test_a_result_route_that_starts_while_the_previous_turn_is_being_finished_survives(self):
        from n1_rig import assistant, notification
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        r.tg_turn(OWNER, "start a background task")
        r.launch("tA")
        r.stop()                                                       # the correct Stop of the first turn
        arrived = []
        real_wait = r.bridge._wait_backstop_retry

        def arrive():
            if not arrived:
                arrived.append(1)
                r.append(assistant("[tg] STARTED", "a-1"), notification("tA", "n-1"))   # the harness starts the completion
            real_wait()
        r.bridge._wait_backstop_retry = arrive
        r.tick()
        r.bridge._wait_backstop_retry = real_wait
        self.assertEqual(arrived, [1])
        r.drain()
        self.assertEqual((r.bridge._route.kind, r.n1.job_state("tA")), ("notif", "claimed"))
        r.say("[tg] RESULT of the task")
        r.tick()
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["STARTED", "RESULT of the task"])
        self.assertEqual(r.job("tA"), "delivered")
        r.stop()
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none")


if __name__ == "__main__":
    unittest.main()
