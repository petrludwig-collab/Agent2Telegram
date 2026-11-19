"""Pilot 4: finishing a turn must deliver the answer THAT TURN wrote, read through the one live read path, never "the last text of the file".

R1: answer 2 already exists when the finishing of turn 1 starts selecting its answer (the outer tick had read only up to turn 1's
    `enqueue`; the rest of the file is appended at the very start of `_finish_turn`, before any read there).
R2: exactly pilot 4: when turn 1's Stop is evaluated the file ends at `enqueue`; answer 1 and everything after it (turn 2's queued human
    record, answer 2, its summary) arrive DURING the finishing, at the first backstop wait.
Both for a DM and for a group (group_interim off: the final text is buffered until the Stop), with the real Stop hook, the real outer
loop, the real send gate and the fake Telegram; idle fallback disabled; no real time (the wait is replaced, never slept).
"""
import json
import os
import time
import unittest

from n1_rig import GROUP, OWNER, RigTestCase, assistant, human
from agent2telegram import attach as attach_mod
from test_shared_session_s3a import (ANSWER_1, ANSWER_2, DURATION_2, ENQUEUE, FOLLOW_ANSWER, HUMAN_1, THINKING, s3a_records)


def text_of(rec):
    return "".join(b.get("text", "") for b in rec["message"]["content"] if isinstance(b, dict) and b.get("type") == "text")


class SingleReadTests(RigTestCase):
    def begin(self, chat):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        self.assertTrue(r.tg(chat, "Pilot S3b. Without any tools write 250 lines"))
        first = r.typed[-1]
        self.assertTrue(r.tg(chat, "Pilot S3b follow-up"))
        self.recs = s3a_records(first, r.typed[-1], time.time())
        r.append(self.recs[HUMAN_1])
        r.drain()
        r.append(*self.recs[ENQUEUE:ENQUEUE + 1])                      # the outer tick has read only up to turn 1's `enqueue`
        r.drain()
        self.assertEqual((r.bridge._route.kind, r.bridge._route.chat), ("tg", chat))
        self.selected = []
        real = r.bridge._last_assistant_text

        def spy():
            out = real()
            self.selected.append(out)
            return out
        r.bridge._last_assistant_text = spy

    def rest(self):
        self.rig.append(*self.recs[THINKING:DURATION_2 + 1])           # thinking, answer 1, ..., queued human of turn 2, answer 2, summary

    def run_finish(self, where):
        """`where`: 'start' = the rest is appended at the start of _finish_turn (R1); 'wait' = at the first backstop wait (R2)."""
        r = self.rig
        r.stop()                                                       # the real hook: names turn 1's prompt
        real_finish, real_wait = r.bridge._finish_turn, r.bridge._wait_backstop_retry
        done = []

        def once():
            if not done:
                done.append(1)
                self.rest()

        def finish(*a, **k):
            if where == "start":
                once()
            return real_finish(*a, **k)

        def wait():
            if where == "wait":
                once()                                                 # no sleeping: the wait IS the arrival of the records
        r.bridge._finish_turn, r.bridge._wait_backstop_retry = finish, wait
        r.tick()
        r.bridge._finish_turn, r.bridge._wait_backstop_retry = real_finish, real_wait
        self.assertEqual(done, [1], "the rest of the transcript arrived at the intended seam")
        r.drain()
        r.tick()
        r.stop()                                                       # turn 2's own Stop (names its queued human record)
        r.tick()
        r.tick()

    def check(self, chat):
        r = self.rig
        sent = r.sent_to(chat)
        expected1, expected2 = text_of(self.recs[ANSWER_1]), text_of(self.recs[ANSWER_2])
        self.assertEqual(expected2, FOLLOW_ANSWER)
        got1 = [t for t in sent if t == expected1]
        got2 = [t for t in sent if t == expected2]
        context = f"sent={[t[:20] for t in sent]} tail-scan-returned={[(t or '')[:20] for t in self.selected]}"
        self.assertEqual((len(got1), len(got2)), (1, 1), "full answer 1 and answer 2, each exactly once: " + context)
        self.assertEqual(sent.index(expected1) < sent.index(expected2), True, "in order 1 -> 2: " + context)
        self.assertEqual(len(sent), 2, "nothing else was sent: " + context)
        self.assertEqual(r.bridge._route.kind, "none", "turn 2 ended with ITS OWN Stop (idle disabled)")
        self.assertFalse(r.bridge._turn_end.exists())
        self.assertEqual(sorted(p.name for p in r.bridge._turn_end.parent.glob(".turn_end-*")), [])
        self.assertEqual(self.selected, [], "shared mode has no second reader: the tail scan is never used")

    def test_r1_dm_answer_two_already_exists_when_turn_one_is_finished(self):
        self.begin(OWNER)
        self.run_finish("start")
        self.check(OWNER)

    def test_r2_dm_pilot4_answer_one_arrives_during_the_finishing(self):
        self.begin(OWNER)
        self.run_finish("wait")
        self.check(OWNER)

    def test_r1_group_answer_two_already_exists_when_turn_one_is_finished(self):
        self.begin(GROUP)
        self.run_finish("start")
        self.check(GROUP)

    def test_r2_group_pilot4_answer_one_arrives_during_the_finishing(self):
        self.begin(GROUP)
        self.run_finish("wait")
        self.check(GROUP)

    def test_a_text_that_was_already_sent_does_not_end_the_read_of_the_own_turn(self):
        """An interim/first text sent before the Stop is no proof that the final answer was read: the loop ends on the turn's end."""
        r = self.rig
        self.begin(OWNER)
        r.say("[tg] first part, sent at once")
        self.assertTrue(r.bridge._turn_text_sent)
        self.run_finish("wait")
        sent = r.sent_to(OWNER)
        self.assertEqual(sent[0], "first part, sent at once")
        self.assertEqual([t for t in sent if t == text_of(self.recs[ANSWER_1])], [text_of(self.recs[ANSWER_1])], "final answer 1 read and sent once")
        self.assertEqual(sent.count(FOLLOW_ANSWER), 1)
        self.assertEqual(r.bridge._route.kind, "none")

    def test_group_attachment_withheld_until_the_stop_is_sent_after_the_own_turn_was_read(self):
        from n1_rig import assistant
        r = self.rig
        outbox_dir = r.bridge.cfg.path_outbox()
        outbox_dir.mkdir(parents=True, exist_ok=True)
        report = outbox_dir / "chart.txt"
        report.write_text("synthetic attachment")
        files = []
        r.client.send_file = lambda chat, path, *a, **k: files.append((chat, str(path)))
        self.begin(GROUP)
        self.recs[ANSWER_1] = dict(assistant(f"Answer one\n[tg-file] {report}", "a-1"))
        self.run_finish("wait")
        self.assertEqual([c for c, _ in files], [GROUP], "the attachment of turn 1 went to ITS chat, once")
        self.assertIn("Answer one", r.sent_to(GROUP))
        self.assertEqual(r.sent_to(GROUP).count(FOLLOW_ANSWER), 1)
        self.assertEqual(r.bridge._route.kind, "none")


class OwnTurnReadEndTests(RigTestCase):
    """What ends the read of the own turn (evidence), and that it is bounded by the existing retry count."""
    def finish(self, arrivals, third=None):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        calls, waits = [], []
        real = r.bridge._last_assistant_text
        r.bridge._last_assistant_text = lambda: (calls.append(1), real())[1]

        def wait():
            waits.append(1)
            if len(waits) == 1 and arrivals:
                r.append(*arrivals)
            if len(waits) == 3 and third:
                r.append(*third)
        r.bridge._wait_backstop_retry = wait
        r.stop()
        r.tick()
        r.tick()
        return calls, waits

    def start(self, text="question"):
        self.rig.tg_turn(OWNER, text)

    def test_nothing_arrives_the_read_is_bounded_by_the_existing_retry_count_and_no_tail_scan_runs(self):
        self.start()
        calls, waits = self.finish([])
        self.assertEqual(len(waits), attach_mod.BACKSTOP_RETRY_ATTEMPTS - 1)
        self.assertEqual(calls, [], "no second reader in shared mode")
        self.assertEqual(self.rig.sent_to(OWNER), [])
        self.assertEqual(self.rig.bridge._route.kind, "none")

    def test_the_turn_summary_ends_the_read(self):
        self.start()
        calls, waits = self.finish([assistant("the answer", "a-1"), {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-1", "parentUuid": "a-1"}])
        self.assertEqual(len(waits), 1, "ended as soon as the own turn's summary was read")
        self.assertEqual(self.rig.sent_to(OWNER), ["the answer"])

    def test_the_next_turns_record_ends_the_read(self):
        r = self.rig
        self.start()
        self.assertTrue(r.tg(OWNER, "next"))
        calls, waits = self.finish([assistant("the answer", "a-1"), human(r.typed[-1], "h-2")])
        self.assertEqual(len(waits), 1, "ended at the boundary of the next turn")
        self.assertEqual(r.sent_to(OWNER), ["the answer"])

    def test_a_summary_of_an_earlier_turn_is_not_evidence_for_the_next_one(self):
        r = self.rig
        self.start("first")
        r.append(assistant("answer A", "a-0"), {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-0", "parentUuid": "a-0"})
        r.drain()
        self.finish([])
        r.client.sent.clear()
        self.start("second")
        calls, waits = self.finish([assistant("answer B", "a-1"), {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-1", "parentUuid": "a-1"}])
        self.assertEqual(len(waits), 1)
        self.assertEqual(r.sent_to(OWNER), ["answer B"], "the stale flag did not cut the read of turn 2 short")

    def prompt_uuid(self):
        lines = [json.loads(x) for x in self.rig.transcript.read_text("utf-8").splitlines()]
        return [x for x in lines if x.get("type") == "user"][-1]["uuid"]

    def test_a_summary_that_hangs_on_a_foreign_record_does_not_end_the_read(self):
        self.start()
        calls, waits = self.finish([assistant("the answer", "a-1"),
                                    {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-9", "parentUuid": "an-earlier-turn"}])
        self.assertEqual(len(waits), attach_mod.BACKSTOP_RETRY_ATTEMPTS - 1, "a summary not chained to this route's output is no evidence")
        self.assertEqual(self.rig.sent_to(OWNER), ["the answer"])

    def test_a_malformed_parent_uuid_is_no_evidence_and_never_loses_the_answer_in_the_same_chunk(self):
        for bad in ([], {}, 7, None):
            with self.subTest(parent=bad):
                r = self.rig
                r.client.sent.clear()
                self.start(f"question {bad!r}")
                answer = assistant("the answer", f"a-{id(bad)}")
                answer["parentUuid"] = bad
                summary = {"type": "system", "subtype": "stop_hook_summary", "uuid": f"s-{id(bad)}", "parentUuid": bad}
                weird = {"type": "attachment", "uuid": ["not", "a", "str"], "parentUuid": {}}
                duration = {"type": "system", "subtype": "turn_duration", "uuid": f"d-{id(bad)}", "parentUuid": bad}
                tip = {"type": "attachment", "uuid": f"t-{id(bad)}", "parentUuid": None}      # a valid output record: the tip is set
                calls, waits = self.finish([tip, weird, summary, duration, answer])       # one chunk: the answer follows the malformed records
                self.assertEqual(r.sent_to(OWNER), ["the answer"], "delivered fully, exactly once")
                self.assertEqual(len(waits), attach_mod.BACKSTOP_RETRY_ATTEMPTS - 1, "no evidence: the bounded retry ran")
                self.assertEqual(r.bridge._route.kind, "none")

    def test_an_output_record_without_a_usable_uuid_makes_the_next_summary_no_evidence(self):
        self.start()
        calls, waits = self.finish([assistant("first part", "a"), {"type": "attachment", "uuid": [], "parentUuid": "a"},
                                    {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-1", "parentUuid": "a"}],
                                   third=[assistant("the rest of the answer", "a-2"),
                                          {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-2", "parentUuid": "a-2"}])
        self.assertEqual(len(waits), 3, "the summary hung on 'a', not on the last output read: no evidence, read on")
        self.assertEqual(self.rig.sent_to(OWNER), ["first part", "the rest of the answer"], "delivered fully, once each")

    def test_a_summary_written_before_the_own_answer_does_not_end_the_read_and_the_answer_is_delivered_once(self):
        self.start()
        early = {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-early", "parentUuid": self.prompt_uuid()}
        calls, waits = self.finish([early], third=[assistant("the full answer", "a-1"),
                                                   {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-1", "parentUuid": "a-1"}])
        self.assertEqual(len(waits), 3, "read on until the answer's own summary")
        self.assertEqual(self.rig.sent_to(OWNER), ["the full answer"])

    def test_the_evidence_of_a_route_does_not_carry_over_to_the_next_turn_that_replaces_it(self):
        r = self.rig
        self.start("one")
        r.append(assistant("answer one", "a-0"), {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-0", "parentUuid": "a-0"})
        r.drain()
        self.assertTrue(r.bridge._own_turn_read)
        self.start("two")                                               # turn 2 opens its own route (no exit between)
        self.assertFalse(r.bridge._own_turn_read, "turn 1's summary is not evidence about turn 2")
        calls, waits = self.finish([assistant("answer two", "a-1"),
                                    {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-1", "parentUuid": "a-1"}])
        self.assertEqual(len(waits), 1)
        self.assertIn("answer two", r.sent_to(OWNER))

    def test_a_transcript_reset_clears_the_evidence(self):
        r = self.rig
        self.start()
        r.append(assistant("the answer", "a-1"), {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-1", "parentUuid": "a-1"})
        r.drain()
        self.assertTrue(r.bridge._own_turn_read)
        replacement = r.tmp / "new.jsonl"
        replacement.write_text(json.dumps(human("another session", "z0")) + "\n")
        os.replace(replacement, r.transcript)
        r.drain()
        self.assertEqual(r.bridge._route.kind, "none")
        self.assertEqual((r.bridge._own_turn_read, r.bridge._own_tip), (False, None))

    def test_a_group_turn_with_only_files_delivers_them_once_to_its_chat(self):
        r = self.rig
        outbox_dir = r.bridge.cfg.path_outbox()
        outbox_dir.mkdir(parents=True, exist_ok=True)
        report = outbox_dir / "chart.txt"
        report.write_text("synthetic attachment")
        files = []
        r.client.send_file = lambda chat, path, *a, **k: files.append((chat, str(path)))
        r.tg_turn(GROUP, "send me the chart")
        calls, waits = self.finish([assistant(f"[tg] [tg-file] {report}", "a-1"),
                                    {"type": "system", "subtype": "stop_hook_summary", "uuid": "s-1", "parentUuid": "a-1"}])
        self.assertEqual([c for c, _ in files], [GROUP], "files only, no text: delivered once, to its chat")
        self.assertEqual(r.sent_to(GROUP), [])
        self.assertEqual(r.bridge._route.kind, "none")

    def test_an_unreadable_complete_line_under_hold_ends_the_read_and_is_left_for_the_normal_drain(self):
        r = self.rig
        self.start()
        line_start = r.transcript.stat().st_size
        seen = []
        real_finish = r.bridge._finish_turn

        def finish(*a, **k):
            seen.append(r.bridge._tpos)
            return real_finish(*a, **k)
        r.bridge._finish_turn = finish

        def garbage():
            with r.transcript.open("a", encoding="utf-8") as f:
                f.write("this is not json\n")
        r.bridge._idle_limit = lambda: 10 ** 9
        waits = []
        r.bridge._wait_backstop_retry = lambda: (waits.append(1), garbage() if len(waits) == 1 else None)
        real_read = r.bridge._n1_read_own_turn

        def read_and_look():
            real_read()
            seen.append(r.bridge._tpos)
        r.bridge._n1_read_own_turn = read_and_look
        r.stop()
        r.tick()
        self.assertEqual(len(waits), 1, "the unreadable line is a boundary: the read ends")
        self.assertEqual(seen[-1], line_start, "and the line is left unread for the normal drain")
        r.tick()
        self.assertEqual(r.bridge._route.kind, "other", "the normal drain then reads it: fail-closed, the Telegram window is closed")


if __name__ == "__main__":
    unittest.main()
