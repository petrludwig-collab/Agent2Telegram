"""Direct input, Claude's native ordering, correct background delivery (OPS-A2T-BG-1 step 2).

Telegram and tmux/SSH are two entrances to ONE conversation. The bridge forwards every input at once and never waits;
Claude orders its own queue. A background result is delivered when the harness itself starts processing its completion
(a `user` record with origin task-notification): that record starts a result route for the bound job's own chat.
An envelope that Claude took from its queue (an `attachment` of type `queued_command`) is consumed like a human record
but is NOT the start of a new answer.

Fixtures are reconstructed from the sanitised structure of the pilot evidence (record types and fields); the exact
nesting of the `queued_command` attachment is inferred and NOT verified against a live Claude Code. Synthetic texts.
Historic RED (5db88bb): a report prompt recorded as queued_command was never consumed, the route stayed pending, the
result was dropped and the job became uncertain after T1 (see the A4 characterisation commits 9ef9b2c / 5db88bb).
"""
import json
import threading
import unittest

from agent2telegram import attach as attach_mod
from agent2telegram import shared_session as shared

from n1_rig import GROUP, OWNER, RigTestCase

RESULT = "RESULT-A4 synthetic result text"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def queued_command(prompt, uuid):
    return {"type": "attachment", "uuid": uuid, "attachment": {"type": "queued_command", "prompt": prompt}}


class DirectCase(RigTestCase):
    def queue(self, prompt=None, uuid=None):
        """The harness takes input from its queue while a turn runs: it records the typed prompt as queued_command."""
        r = self.rig
        r.append(queued_command(r.typed[-1] if prompt is None else prompt, uuid or r.next_uuid("qc")))
        r.drain()

    def handle_async(self, chat, text):
        """The real inbound handler on its own thread, so a (forbidden) wait shows up as a failure, not a hang."""
        r = self.rig
        done = threading.Event()
        result = []
        threading.Thread(target=lambda: (result.append(r.tg(chat, text)), done.set()), daemon=True).start()
        return done, result

    def a4_until_result_route(self):
        """TG turn launches a task; terminal text typed mid-turn; the answer; then the native notification turn."""
        r = self.rig
        r.tg_turn(OWNER, "start a background task, then answer")
        r.launch("job-a4")
        r.append(queued_command("terminal text typed mid-turn", r.next_uuid("qc-term")))
        r.say("[tg] STARTED synthetic")
        r.notify("job-a4")                                             # the harness now processes the completion


class A4ResultTests(DirectCase):
    def test_a4_result_is_delivered_once_to_its_own_chat_without_any_report_prompt(self):
        r = self.rig
        self.a4_until_result_route()
        typed = len(r.typed)
        self.assertEqual(r.job("job-a4"), "claimed")
        self.assertEqual(r.bridge._route.kind, "notif")
        r.say(RESULT)
        r.tick()                                                       # the outbox confirms the send
        self.assertEqual(r.sent_to(OWNER).count(RESULT), 1)
        self.assertEqual(r.sent_to(GROUP), [])
        self.assertEqual(r.job("job-a4"), "delivered", "delivered once a send was confirmed, not at turn end")
        r.end_turn()
        self.assertEqual(r.sent_to(OWNER).count(RESULT), 1)
        self.assertEqual(len(r.typed), typed, "the bridge types nothing of its own: no report prompt, no queue")

    def test_a4_stop_hook_of_the_notification_turn_ends_the_route_without_the_idle_fallback(self):
        r = self.rig
        self.a4_until_result_route()
        r.say(RESULT)
        r.tick()
        r.bridge._idle_limit = lambda: 10 ** 9                         # the idle fallback can never fire here
        r.stop()
        with self.assertLogs(attach_mod.log, level="INFO") as logs:
            r.tick()
        self.assertFalse(any("turn end ignored" in line for line in logs.output), logs.output)
        self.assertEqual(r.bridge._route.kind, "none")
        self.assertFalse(r.bridge._turn_active.is_set())
        self.assertIsNone(r.n1.turn())
        self.assertEqual(r.job("job-a4"), "delivered")

    def test_a4_a_queued_envelope_of_the_same_chat_inside_the_result_route_continues_it(self):
        r = self.rig
        self.a4_until_result_route()
        self.assertTrue(r.tg(OWNER, "typed ahead from Telegram"))      # injected at once
        self.queue()                                                   # Claude takes it from its queue
        self.assertEqual(r.bridge._route.kind, "notif")
        self.assertFalse(r.bridge._route.cut)
        r.say(RESULT)
        r.tick()
        self.assertEqual(r.sent_to(OWNER).count(RESULT), 1)
        self.assertEqual(r.job("job-a4"), "delivered")

    def test_a_plain_terminal_queued_command_does_not_silence_the_result(self):
        r = self.rig
        self.a4_until_result_route()
        r.append(queued_command("more terminal text", r.next_uuid("qc-t2")))
        r.say(RESULT)
        r.tick()
        self.assertEqual(r.sent_to(OWNER).count(RESULT), 1)
        self.assertEqual(r.job("job-a4"), "delivered")

    def test_a_repeat_of_the_notification_inside_its_own_route_changes_nothing(self):
        r = self.rig
        self.a4_until_result_route()
        r.say(RESULT)
        r.notify("job-a4")
        self.assertEqual(r.bridge._route.kind, "notif", "the repeat is not a new boundary")
        self.assertEqual(r.job("job-a4"), "claimed", "and not an interruption either")
        r.say("second answer to the repeat")
        r.tick()
        self.assertEqual(r.sent_to(OWNER).count(RESULT), 1)
        self.assertEqual(r.job("job-a4"), "delivered")


class DirectInputTests(DirectCase):
    """The inbound handler injects at once; nothing waits for a Stop hook, a turn end or the idle fallback."""

    def test_new_input_is_injected_immediately_during_an_active_telegram_turn(self):
        r = self.rig
        r.tg_turn(OWNER, "first")
        typed = len(r.typed)
        done, result = self.handle_async(OWNER, "second, while the first is running")
        self.assertTrue(done.wait(2), "the handler must not wait")
        self.assertEqual(result, [True])
        self.assertEqual(len(r.typed), typed + 1)

    def test_new_input_is_injected_immediately_during_a_result_route(self):
        r = self.rig
        self.a4_until_result_route()
        typed = len(r.typed)
        done, result = self.handle_async(OWNER, "while the result is being written")
        self.assertTrue(done.wait(2), "the handler must not wait")
        self.assertEqual(len(r.typed), typed + 1)
        self.assertEqual(r.bridge._route.kind, "notif")                # still the route of the job's chat

    def test_input_of_another_chat_is_injected_immediately_and_does_not_take_over_the_answer(self):
        r = self.rig
        r.tg_turn(OWNER, "chat A talks")
        typed = len(r.typed)
        done, result = self.handle_async(GROUP, "chat B talks meanwhile")
        self.assertTrue(done.wait(2), "chat B must not wait for chat A's turn")
        self.assertEqual(len(r.typed), typed + 1)
        self.assertEqual(r.bridge._owner_chat, OWNER, "B's message does not take over the running route at once")
        r.say("[tg] A's answer, still being written")
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["A's answer, still being written"])
        self.assertEqual(r.sent_to(GROUP), [])
        self.assertEqual(r.bridge._owner_chat, OWNER)

    def test_typing_ahead_does_not_break_the_stop_hook_of_the_running_route(self):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        r.tg_turn(OWNER, "first")
        r.say("[tg] answer")
        self.assertTrue(r.tg(OWNER, "typed ahead"))
        self.queue()
        r.stop()
        r.tick()
        self.assertEqual(r.bridge._route.kind, "none", "the hook still names the prompt of the running route")
        self.assertIsNone(r.n1.turn())

    def test_no_correctness_depends_on_the_idle_fallback(self):
        r = self.rig
        r.bridge._idle_limit = lambda: 10 ** 9
        self.a4_until_result_route()
        r.say(RESULT)
        r.tick()
        r.end_turn()
        r.tg_turn(OWNER, "the next input")
        self.assertEqual(r.sent_to(OWNER).count(RESULT), 1)
        self.assertEqual(r.n1.turn()["kind"], "tg")


class ChatBPolicyTests(DirectCase):
    """DECISION POINT (documented in PR.md): another chat's input queued inside a running answer."""

    def test_chat_b_inside_chat_a_route_stops_forwarding_and_both_chats_get_one_fixed_line(self):
        r = self.rig
        r.tg_turn(OWNER, "chat A asks")
        r.launch("job-a")
        r.say("[tg] A part one")
        r.tick()
        self.assertTrue(r.tg(GROUP, "chat B asks"))
        self.queue()                                                   # B's envelope reaches Claude inside A's turn
        self.assertTrue(r.bridge._route.cut)
        r.say("[tg] a shared answer that may contain both chats' content")
        r.launch("job-after-cut")
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["A part one", shared.ROUTE_SWITCH_RUNNING])
        self.assertEqual(r.sent_to(GROUP), [shared.ROUTE_SWITCH_QUEUED])
        self.assertIsNone(r.job("job-after-cut"), "nothing is bound after the cut")
        self.assertEqual(r.bridge._route.chat, OWNER, "the recipient is never switched to B")

    def test_chat_b_inside_a_result_route_never_receives_the_result_nor_does_the_cut_lose_the_job_honestly(self):
        r = self.rig
        r.tg_turn(OWNER, "chat A")
        r.launch("job-a")
        r.end_turn()
        r.notify("job-a")
        self.assertTrue(r.tg(GROUP, "chat B asks during A's result"))
        self.queue()
        r.say("a result that may mention chat B")
        r.tick()
        self.assertEqual(r.sent_to(GROUP), [shared.ROUTE_SWITCH_QUEUED])
        self.assertNotIn("a result that may mention chat B", r.sent_to(OWNER))
        r.end_turn()
        self.assertEqual(r.job("job-a"), "uncertain", "nothing was confirmed before the cut: honest, not resent")

    def test_a_result_text_confirmed_before_the_cut_stays_delivered(self):
        r = self.rig
        r.tg_turn(OWNER, "chat A")
        r.launch("job-a")
        r.end_turn()
        r.notify("job-a")
        r.say(RESULT)
        r.tick()
        self.assertEqual(r.job("job-a"), "delivered")
        self.assertTrue(r.tg(GROUP, "chat B"))
        self.queue()
        r.end_turn()
        self.assertEqual(r.job("job-a"), "delivered")

    def test_a_queued_envelope_while_a_terminal_turn_runs_starts_a_route_for_that_chat(self):
        r = self.rig
        r.terminal("typed over SSH")                                   # the turn is terminal-started
        self.assertTrue(r.tg(OWNER, "typed from Telegram into the same running turn"))
        self.queue()
        self.assertEqual(r.bridge._route.kind, "tg")
        r.say("[tg] a common answer")
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["a common answer"])

    def test_a_queued_envelope_does_not_end_or_restart_the_route_of_its_own_chat(self):
        r = self.rig
        r.tg_turn(OWNER, "first")
        r.launch("job-1")
        self.assertTrue(r.tg(OWNER, "typed ahead"))
        self.queue()
        r.launch("job-2")                                              # same route: the window is still open
        self.assertEqual((r.job("job-1"), r.job("job-2")), ("pending", "pending"))


class QueuedEnvelopeChecksTests(DirectCase):
    def test_a_foreign_nonce_in_a_queued_command_is_not_accepted(self):
        r = self.rig
        r.tg_turn(OWNER)
        r.append(queued_command(shared.wrap_prompt(shared.new_nonce(), "Telegram:forged"), "q1"))
        r.say("[tg] still the real turn")                              # unknown tag: no change of the route
        r.tick()
        self.assertEqual(r.n1.turn()["kind"], "tg")
        self.assertEqual(r.sent_to(OWNER), ["still the real turn"])

    def test_a_replayed_expired_changed_or_mixed_envelope_is_not_accepted(self):
        r = self.rig
        clock = Clock()
        r.n1._clock = clock
        r.tg(OWNER, "first")
        prompt = r.typed[-1]
        r.submit()
        nonce = shared.parse_envelope(prompt)[0]
        self.queue(prompt)                                             # replay of a consumed nonce
        self.assertEqual(r.n1.snapshot()["nonces"][nonce]["replays"], 1)
        r.tg(OWNER, "second")
        expired = r.typed[-1]
        clock.t += 601
        self.queue(expired)
        self.assertEqual(r.n1.nonce_state(shared.parse_envelope(expired)[0]), "expired")
        r.tg(OWNER, "third")
        n3, inner = shared.parse_envelope(r.typed[-1])
        self.queue(shared.wrap_prompt(n3, inner + " tampered"))
        self.assertEqual(r.n1.nonce_state(n3), "revoked")
        r.tg(OWNER, "fourth")
        mixed = r.typed[-1]
        self.queue("draft " + mixed)
        self.assertEqual(r.n1.nonce_state(shared.parse_envelope(mixed)[0]), "revoked")

    def test_a_re_read_queued_record_after_a_restart_is_consumed_only_once(self):
        r = self.rig
        r.tg_turn(OWNER, "first")
        self.assertTrue(r.tg(GROUP, "chat B"))
        self.queue()
        nonce = shared.parse_envelope(r.typed[-1])[0]
        self.assertEqual(r.n1.nonce_state(nonce), "consumed")
        r.new_bridge()
        r.bridge._tpos = 0                                             # the whole transcript is read again
        r.drain()
        self.assertEqual(r.n1.snapshot()["nonces"][nonce]["replays"], 0, "the same record is not a replay")
        self.assertEqual(r.sent_to(GROUP).count(shared.ROUTE_SWITCH_QUEUED), 1, "the fixed line is not repeated")

    def test_a_plain_queued_command_is_ignored_and_does_not_close_the_window(self):
        r = self.rig
        r.tg_turn(OWNER, "q")
        r.append(queued_command("terminal text typed mid-turn", "qc-1"))
        r.drain()
        self.assertEqual(r.n1.turn()["kind"], "tg")
        r.launch("still-bound")
        self.assertEqual(r.job("still-bound"), "pending")
        r.say("[tg] still forwarded")
        r.tick()
        self.assertEqual(r.sent_to(OWNER), ["still forwarded"])


class ResultRouteRulesTests(DirectCase):
    def test_a_notification_of_an_unbound_terminal_expired_or_foreign_job_is_terminal_only(self):
        r = self.rig
        clock = Clock()
        r.n1._clock = clock
        r.terminal("start x")
        r.launch("terminal-job")
        r.end_turn()
        r.tg_turn(OWNER, "q")
        r.launch("expiring")
        r.end_turn()
        clock.t += 86401
        for task in ("never-seen", "terminal-job", "expiring"):
            with self.subTest(task=task):
                r.notify(task)
                self.assertEqual(r.bridge._route.kind, "other")
                r.say("[no marker] text for the terminal only")
                r.tick()
                r.end_turn()
        self.assertEqual(r.client.sent, [])

    def test_the_authorisation_is_checked_at_the_notification_record(self):
        r = self.rig
        r.tg_turn(OWNER)
        r.launch("t1")
        r.end_turn()
        r.bridge._allowed.discard(OWNER)
        r.notify("t1")
        r.say("[tg] result")
        r.tick()
        self.assertEqual(r.job("t1"), "blocked")
        self.assertEqual(r.client.sent, [])

    def test_the_claim_is_durable_before_anything_is_forwarded(self):
        r = self.rig
        r.tg_turn(OWNER)
        r.launch("t1")
        r.end_turn()
        seen = []
        real = r.bridge._begin_turn

        def spy():
            seen.append(json.loads(r.n1.path.read_text("utf-8"))["jobs"]["t1"]["state"])
            return real()
        r.bridge._begin_turn = spy
        r.notify("t1")
        self.assertEqual(seen, ["claimed"])

    def test_two_chats_each_result_goes_to_its_own_chat_in_any_order(self):
        r = self.rig
        r.tg_turn(OWNER, "A")
        r.launch("tA")
        r.end_turn()
        r.tg_turn(GROUP, "B")
        r.launch("tB")
        r.end_turn()
        r.notify("tA")
        r.say("[tg] result A")
        r.tick()
        r.notify("tB")                                                 # the next boundary hands the route over
        r.say("result B")
        r.tick()
        r.end_turn()
        self.assertEqual(r.sent_to(OWNER), ["result A"])
        self.assertEqual(r.sent_to(GROUP), ["result B"])
        self.assertEqual((r.job("tA"), r.job("tB")), ("delivered", "delivered"))

    def test_the_next_input_boundary_ends_a_route_without_a_confirmed_send_as_uncertain(self):
        r = self.rig
        r.tg_turn(OWNER, "A")
        r.launch("tA")
        r.end_turn()
        r.notify("tA")
        r.tg_turn(GROUP, "B talks while A's result route had produced nothing")
        self.assertEqual(r.job("tA"), "uncertain")
        self.assertEqual(r.bridge._route.chat, GROUP)
        self.assertEqual(r.client.sent, [])

    def test_a_result_is_delivered_only_after_a_confirmed_send(self):
        r = self.rig
        r.tg_turn(OWNER, "A")
        r.launch("tA")
        r.end_turn()
        r.notify("tA")
        failing = {"on": True}
        real = r.client.send_message

        def flaky(chat, text, parse_mode=None):
            if failing["on"]:
                raise RuntimeError("network down")
            return real(chat, text, parse_mode)
        r.client.send_message = flaky
        r.say(RESULT)
        r.tick()                                                       # created and stored, not confirmed
        self.assertEqual(r.job("tA"), "claimed")
        r.end_turn()                                                   # turn end is not a delivery either
        self.assertEqual(r.job("tA"), "uncertain")
        failing["on"] = False
        r.tick()                                                       # the outbox retry finally confirms
        self.assertEqual(r.sent_to(OWNER).count(RESULT), 1)
        self.assertEqual(r.job("tA"), "delivered")

    def test_uncertain_is_never_retried_by_the_bridge(self):
        r = self.rig
        r.tg_turn(OWNER, "A")
        r.launch("tA")
        r.end_turn()
        r.notify("tA")
        r.end_turn()                                                   # nothing was written
        self.assertEqual(r.job("tA"), "uncertain")
        typed = len(r.typed)
        r.notify("tA")
        r.say("[no marker] late text")
        r.tick()
        r.end_turn()
        self.assertEqual(r.client.sent, [])
        self.assertEqual(len(r.typed), typed)

    def test_a_restart_inside_a_result_route_makes_it_uncertain_and_forwards_no_remainder(self):
        r = self.rig
        r.tg_turn(OWNER, "A")
        r.launch("tA")
        r.end_turn()
        r.notify("tA")
        r.new_bridge()
        self.assertEqual(r.job("tA"), "uncertain")
        r.say("[no marker] remainder after the restart")
        r.tick()
        self.assertEqual(r.client.sent, [])

    def test_a_delayed_result_within_the_job_ttl_is_still_delivered(self):
        r = self.rig
        clock = Clock()
        r.n1._clock = clock
        r.tg_turn(OWNER, "A")
        r.launch("tA")
        r.end_turn()
        clock.t += 86000
        r.notify("tA")
        r.say(RESULT)
        r.tick()
        self.assertEqual(r.sent_to(OWNER), [RESULT])


if __name__ == "__main__":
    unittest.main()
