"""Pilot case A4 (evidence 12-pilot, scenario 3b), reproduced offline through the REAL candidate code.

Observed with Claude Code 2.1.326: a Telegram turn launches a background task; terminal text typed meanwhile is
recorded as an `attachment` (type `queued_command`); the completion arrives as a task-notification; the Stop hook ends
the first turn; the bridge then types the REPORT prompt while Claude is already processing the notification, and Claude
records that prompt as an `attachment` (`queued_command`) too, not as a `human` user record. The envelope is never
consumed, the route stays `pending`, the send gate drops the answer, T1 passes and the job becomes `uncertain`.

Records are shaped like transcript-structure.jsonl (types and fields); texts are synthetic. The record layout
`{"type": "attachment", "attachment": {"type": "queued_command", "prompt": ...}}` is inferred from the flattened
`attachment_type` / `prompt_head` keys of that evidence (see the uncertainty note in the report).
"""
import unittest
from unittest.mock import patch

from agent2telegram import attach as attach_mod
from agent2telegram import shared_session as shared

from n1_rig import OWNER, GROUP, RigTestCase

RESULT = "RESULT-A4 synthetic result text"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def queued_command(prompt, uuid):
    return {"type": "attachment", "uuid": uuid, "attachment": {"type": "queued_command", "prompt": prompt}}


class A4Mixin:
    """The observed sequence up to the moment the report prompt is recorded."""

    def observed_sequence(self):
        r = self.rig
        self.clock = Clock()
        r.n1._clock = self.clock
        r.tg_turn(OWNER, "start a background task, then answer")          # TG turn (human, envelope)
        r.launch("job-a4")                                                  # bash background launch
        r.append(queued_command("terminal text typed mid-turn", "qc-terminal"))   # queued_command, no envelope
        r.say("STARTED synthetic")                                           # assistant text (answers both)
        r.notify("job-a4")                                                  # notification record, mid-turn
        r.end_turn()                                                        # Stop hook of the first turn
        self.assertEqual(r.bridge._route.kind, "pending", "the report was claimed and typed")
        self.report_prompt = r.typed[-1]
        self.assertIsNotNone(shared.parse_envelope(self.report_prompt))
        self.nonce = shared.parse_envelope(self.report_prompt)[0]
        # Claude is already processing the notification: it queues the typed report as an attachment.
        r.append(queued_command(self.report_prompt, "qc-report"))
        r.say(RESULT)                                                       # assistant text RESULT
        r.tick()


class A4BaselineTests(A4Mixin, RigTestCase):
    def test_BASELINE_result_is_not_delivered_nonce_stays_issued_job_uncertain_after_t1(self):
        r = self.rig
        self.observed_sequence()
        sent = r.sent_to(OWNER)
        self.assertNotIn(RESULT, sent, "today: the result never reaches Telegram")
        self.assertEqual(r.n1.nonce_state(self.nonce), "issued", "the envelope was never consumed")
        self.assertEqual(r.bridge._route.kind, "pending", "route stays pending: the send gate is closed")
        self.assertEqual(r.job("job-a4"), "claimed")
        self.clock.t += 601                                                 # T1
        r.tick()
        self.assertEqual(r.job("job-a4"), "uncertain")
        self.assertNotIn(RESULT, r.sent_to(OWNER))
        self.assertEqual(r.bridge._route.kind, "none")

    def test_BASELINE_stop_hook_of_the_notification_turn_is_ignored_so_only_idle_ends_it(self):
        r = self.rig
        self.observed_sequence()
        r.stop()                                                            # Stop of the notification turn
        with self.assertLogs(attach_mod.log, level="INFO") as logs:
            r.tick()
        self.assertTrue(any("turn end ignored" in line for line in logs.output))
        self.assertTrue(r.bridge._turn_active.is_set(), "the bridge still holds the turn")
        r.bridge._last_activity -= 10_000                                   # silence longer than the idle limit
        with self.assertLogs(attach_mod.log, level="WARNING") as logs:
            r.tick()
        self.assertTrue(any("turn ended after inactivity" in line and "no hook signal" in line
                            for line in logs.output))


class A4DesiredTests(A4Mixin, RigTestCase):
    @unittest.expectedFailure
    def test_DESIRED_a_queued_command_with_the_report_envelope_consumes_it_and_the_result_is_delivered(self):
        r = self.rig
        self.observed_sequence()
        self.assertEqual(r.n1.nonce_state(self.nonce), "consumed")
        self.assertEqual(r.sent_to(OWNER).count(RESULT), 1, "delivered once, to the job's chat only")
        self.assertEqual(r.sent_to(GROUP), [])
        r.bridge._last_activity -= 10_000                                   # the turn ends (idle fallback)
        r.tick()
        self.assertEqual(r.job("job-a4"), "delivered")

    @unittest.expectedFailure
    def test_DESIRED_report_goes_only_to_the_jobs_chat_even_if_another_chat_is_last(self):
        r = self.rig
        r.n1._clock = Clock()
        r.tg_turn(OWNER, "task of chat A")
        r.launch("job-a4")
        r.say("[tg] started")
        r.notify("job-a4")
        r.end_turn()
        report = r.typed[-1]
        r.append(queued_command(report, "qc-r"))
        r.say(RESULT)
        r.tick()
        self.assertEqual(r.sent_to(OWNER).count(RESULT), 1)
        self.assertEqual(r.sent_to(GROUP), [])


class A4QueuedCommandWithoutEnvelopeTests(RigTestCase):
    """Today's behaviour that any fix must keep: a plain queued_command is not an input record (mixing is allowed)."""

    def test_plain_queued_command_is_ignored_and_does_not_close_the_window(self):
        r = self.rig
        r.tg_turn(OWNER, "q")
        r.append(queued_command("terminal text typed mid-turn", "qc-1"))
        r.drain()
        self.assertEqual(r.n1.turn()["kind"], "tg")
        self.assertTrue(r.bridge._turn_from_tg)
        with patch.object(r.bridge, "_n1_exit") as leave:
            r.say("[tg] still forwarded")
        leave.assert_not_called()
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["still forwarded"])


if __name__ == "__main__":
    unittest.main()
