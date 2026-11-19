"""Pilot 4: finishing a turn must deliver the answer THAT TURN wrote, read through the one live read path, never "the last text of the file".

R1: answer 2 already exists when the finishing of turn 1 starts selecting its answer (the outer tick had read only up to turn 1's
    `enqueue`; the rest of the file is appended at the very start of `_finish_turn`, before any read there).
R2: exactly pilot 4: when turn 1's Stop is evaluated the file ends at `enqueue`; answer 1 and everything after it (turn 2's queued human
    record, answer 2, its summary) arrive DURING the finishing, at the first backstop wait.
Both for a DM and for a group (group_interim off: the final text is buffered until the Stop), with the real Stop hook, the real outer
loop, the real send gate and the fake Telegram; idle fallback disabled; no real time (the wait is replaced, never slept).
"""
import time
import unittest

from n1_rig import GROUP, OWNER, RigTestCase
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


if __name__ == "__main__":
    unittest.main()
