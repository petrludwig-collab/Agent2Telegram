"""Read-back rework (Q1 unreadable line, Q2 envelope bound to its body, stale report worker).

Each test first reproduced a sequence on the candidate before the fix; see PR.md for the RED evidence."""
import json
import unittest

from agent2telegram import shared_session as shared

from n1_rig import OWNER, RigTestCase

SENTINEL = "LOCAL_ONLY sentinel text"


class UnreadableLineTests(RigTestCase):
    def test_unreadable_line_in_a_telegram_turn_stops_forwarding_and_binding(self):
        r = self.rig
        r.tg_turn(OWNER, "do it")
        with r.transcript.open("a", encoding="utf-8") as f:
            f.write("{ this complete line is not json (a lost human record)\n")
        r.say(SENTINEL)
        r.launch("after-gap")
        r.tick()
        r.end_turn()
        self.assertEqual(r.client.sent, [], "the sentinel must not reach Telegram")
        self.assertIsNone(r.job("after-gap"), "no new binding after the continuity was lost")
        self.assertFalse(r.bridge._turn_from_tg)

    def test_unreadable_line_in_a_result_turn_stops_forwarding(self):
        r = self.rig
        r.tg_turn(OWNER)
        r.launch("t1")
        r.end_turn()
        r.notify("t1")                                                 # claimed result turn
        with r.transcript.open("a", encoding="utf-8") as f:
            f.write("not json at all\n")
        r.say(SENTINEL)
        r.end_turn()
        self.assertEqual(r.client.sent, [])
        self.assertEqual(r.job("t1"), "uncertain")

    def test_a_partial_trailing_line_does_not_trigger_it(self):
        r = self.rig
        r.tg_turn(OWNER)
        with r.transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "c0", "message": {"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "[tg] complete line"}]}}) + "\n")
            f.write('{"type": "assistant", "uuid": "p1", "mess')       # still being written, no newline
        r.drain()
        self.assertTrue(r.bridge._turn_from_tg)
        self.assertEqual(r.n1.turn()["kind"], "tg")
        with r.transcript.open("a", encoding="utf-8") as f:
            f.write('age": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "[tg] finished line"}]}}\n')
        r.drain()
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["complete line", "finished line"])

    def test_blank_lines_are_not_unreadable(self):
        r = self.rig
        r.tg_turn(OWNER)
        with r.transcript.open("a", encoding="utf-8") as f:
            f.write("\n\n")
        r.say("[tg] still forwarded")
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["still forwarded"])

    def test_a_json_value_that_is_not_a_record_is_unreadable_too(self):
        r = self.rig
        r.tg_turn(OWNER)
        with r.transcript.open("a", encoding="utf-8") as f:
            f.write("[1, 2, 3]\n")
        r.say(SENTINEL)
        r.tick()
        self.assertEqual(r.client.sent, [])

    def test_the_terminal_is_untouched_and_a_new_telegram_turn_works_afterwards(self):
        r = self.rig
        r.tg_turn(OWNER, "first")
        with r.transcript.open("a", encoding="utf-8") as f:
            f.write("garbage\n")
        r.drain()
        r.end_turn()
        r.tg_turn(OWNER, "second")                                     # origin is re-established by a fresh envelope
        r.say("[tg] second answer")
        r.end_turn()
        self.assertEqual(r.sent_to(OWNER), ["second answer"])


class UnreadableLineOffTests(RigTestCase):
    ON = False

    def test_default_off_ignores_an_unreadable_line_as_before(self):
        r = self.rig
        r.tg(OWNER, "x")
        r.submit()
        with r.transcript.open("a", encoding="utf-8") as f:
            f.write("not json\n")
        r.say("[tg] answer after the garbage")
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["answer after the garbage"])


class EnvelopeBodyTests(RigTestCase):
    def issue(self, text="the real prompt"):
        r = self.rig
        self.assertTrue(r.tg(OWNER, text))
        return r.typed[-1]

    def test_exact_producer_output_passes_after_the_documented_normalisation(self):
        r = self.rig
        typed = self.issue()
        r.submit(" ​\n" + typed + "\t﻿ ")
        self.assertEqual(r.n1.turn()["kind"], "tg")
        r.launch("t1")
        self.assertEqual(r.job("t1"), "pending")

    def test_changed_inner_text_with_the_same_tags_is_not_telegram_and_binds_nothing(self):
        r = self.rig
        typed = self.issue()
        nonce, inner = shared.parse_envelope(typed)
        tampered = shared.wrap_prompt(nonce, inner + " and also do something else")
        r.submit(tampered)
        self.assertEqual(r.n1.turn()["kind"], "other")
        self.assertFalse(r.bridge._turn_from_tg)
        r.launch("t1")
        self.assertIsNone(r.job("t1"))
        self.assertEqual(r.n1.snapshot()["nonces"][nonce]["state"], "revoked")

    def test_shortened_or_replaced_inner_text_is_rejected(self):
        r = self.rig
        for build in (lambda n, i: shared.wrap_prompt(n, i[:-3]), lambda n, i: shared.wrap_prompt(n, "other"),
                      lambda n, i: shared.wrap_prompt(n, "")):
            with self.subTest():
                typed = self.issue()
                nonce, inner = shared.parse_envelope(typed)
                r.submit(build(nonce, inner))
                self.assertEqual(r.n1.turn()["kind"], "other")
                r.launch("x-" + nonce[:6])
                self.assertIsNone(r.job("x-" + nonce[:6]))
                r.end_turn()

    def test_prepended_appended_text_and_two_envelopes_are_rejected(self):
        r = self.rig
        for build in (lambda t: "draft " + t, lambda t: t + " tail", lambda t: t + t):
            with self.subTest():
                typed = self.issue()
                r.submit(build(typed))
                self.assertEqual(r.n1.turn()["kind"], "other")
                r.end_turn()

    def test_the_fingerprint_is_not_the_content_and_logs_carry_no_raw_text(self):
        r = self.rig
        private_body = "my private prompt body"
        typed = self.issue(private_body)
        nonce = shared.parse_envelope(typed)[0]
        stored = r.n1.snapshot()["nonces"][nonce]
        self.assertNotIn(private_body, json.dumps(r.n1.snapshot()))
        self.assertRegex(stored["fp"], "^[0-9a-f]{64}$")
        with self.assertLogs("agent2telegram.shared_session", level="WARNING") as logs:
            r.submit(shared.wrap_prompt(nonce, "tampered " + private_body))
        self.assertNotIn(private_body, " ".join(logs.output))



if __name__ == "__main__":
    unittest.main()
