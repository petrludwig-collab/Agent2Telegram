"""Offline regressions for a TUI swallowing the final Enter key."""
import os
import subprocess
import threading
import unittest
from unittest.mock import patch

from agent2telegram import session
from agent2telegram.session import TmuxSession


class EnterVerificationTests(unittest.TestCase):
    text = "Please verify the submitted message ends here."

    def run_injection(self, captures, *, text=None, pane_checks=None, capture_latency=0.0):
        target = object.__new__(TmuxSession)
        target.name = "a2t-enter-test"
        target._origin = "[TG] "
        target._enter_verify_timeout = 0.7
        clock = [0.0]
        capture_index = [0]
        loaded = {}

        def sleep(seconds):
            clock[0] += seconds

        def tmux(*args, **kwargs):
            output = ""
            if args[0] == "capture-pane":
                if kwargs["timeout"] < capture_latency:
                    raise subprocess.TimeoutExpired("tmux", kwargs["timeout"])
                sleep(capture_latency)
                index = min(capture_index[0], len(captures) - 1)
                capture_index[0] += 1
                output = captures[index]
                if isinstance(output, Exception):
                    raise output
            elif args[0] == "load-buffer":
                with open(args[-1], encoding="utf-8") as stream:
                    loaded["text"] = stream.read()
                loaded["path"] = args[-1]
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with patch.object(session, "_tmux", side_effect=tmux) as mocked, \
                patch.object(session.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(session.time, "sleep", side_effect=sleep), \
                patch.object(TmuxSession, "_pane_ok", side_effect=pane_checks,
                             return_value=(True, "agent running")), \
                patch.object(session, "log") as logs:
            target._send_keys(self.text if text is None else text)
        calls = [call.args for call in mocked.call_args_list]
        enters = [call for call in calls if call == (
            "send-keys", "-t", target.name, "Enter")]
        return calls, enters, logs, loaded, capture_index[0]

    def test_submitted_normally_checks_input_without_resending_history(self):
        for prompt in ("❯", "›", ">"):
            with self.subTest(prompt=prompt):
                pane = f"{prompt} [TG] {self.text}\nAnswer in progress\n{prompt} \n? for help\n"
                calls, enters, logs, _, count = self.run_injection([pane])
                self.assertEqual(len(enters), 1)
                self.assertEqual(count, 1)
                self.assertIn(("capture-pane", "-p", "-t", "a2t-enter-test"), calls)
                logs.warning.assert_not_called()
                logs.error.assert_not_called()

    def test_stuck_input_gets_exactly_one_resend_after_deadline(self):
        stuck = "❯ [TG] " + self.text
        _, enters, logs, _, count = self.run_injection([stuck] * 3 + ["❯ "])
        self.assertEqual(len(enters), 2)
        self.assertGreaterEqual(count, 4)
        logs.warning.assert_called_once_with("enter_resend session=%s", "a2t-enter-test")
        logs.error.assert_not_called()

    def test_input_still_stuck_after_resend_logs_error_without_third_enter(self):
        _, enters, logs, _, _ = self.run_injection(["› [TG] " + self.text])
        self.assertEqual(len(enters), 2)
        logs.warning.assert_called_once_with("enter_resend session=%s", "a2t-enter-test")
        logs.error.assert_called_once_with("enter_not_submitted session=%s", "a2t-enter-test")

    def test_slow_submission_before_deadline_does_not_resend(self):
        stuck = "> [TG] " + self.text
        _, enters, logs, _, count = self.run_injection([stuck, stuck, "> "])
        self.assertEqual(count, 3)
        self.assertEqual(len(enters), 1)
        logs.warning.assert_not_called()
        logs.error.assert_not_called()

    def test_capture_latency_does_not_suppress_resend_at_deadline(self):
        _, enters, logs, _, _ = self.run_injection(
            ["❯ [TG] " + self.text], capture_latency=0.03,
        )
        self.assertEqual(len(enters), 2)
        logs.warning.assert_called_once_with("enter_resend session=%s", "a2t-enter-test")
        logs.error.assert_called_once_with("enter_not_submitted session=%s", "a2t-enter-test")

    def test_wrapped_tail_and_whitespace_are_matched_in_input(self):
        pane = "❯ [TG] Please verify the subm\n  itted   message\n  ends\there.\n────────────\n? for help"
        _, enters, logs, _, _ = self.run_injection([pane])
        self.assertEqual(len(enters), 2)
        logs.error.assert_called_once()

    def test_bracketed_paste_is_verified_without_reinjecting_text(self):
        text = "Long Unicode λ message. " * 40 + "\n" + self.text
        pane = "› [TG] " + text.replace("\n", " ")
        calls, enters, logs, loaded, count = self.run_injection([pane], text=text)
        self.assertGreater(count, 1)
        self.assertEqual(len(enters), 2)
        self.assertEqual(sum(call[0] == "paste-buffer" for call in calls), 1)
        self.assertFalse(any("-l" in call for call in calls))
        self.assertEqual(loaded["text"], "[TG] " + text.replace("\n", " "))
        self.assertFalse(os.path.exists(loaded["path"]))
        logs.warning.assert_called_once()
        logs.error.assert_called_once()

    def test_shell_guard_is_rechecked_before_resend(self):
        _, enters, logs, _, _ = self.run_injection(
            ["❯ [TG] " + self.text],
            pane_checks=[(True, "agent running"), (False, "shell prompt")],
        )
        self.assertEqual(len(enters), 1)
        logs.warning.assert_not_called()
        logs.error.assert_called_once()

    def test_capture_failure_is_logged_without_raising_or_resending(self):
        _, enters, logs, _, count = self.run_injection([subprocess.TimeoutExpired("tmux", 1)])
        self.assertEqual(count, 1)
        self.assertEqual(len(enters), 1)
        logs.warning.assert_not_called()
        logs.error.assert_called_once()

    def test_resend_guard_failure_does_not_escape_into_injection_retries(self):
        _, enters, logs, _, _ = self.run_injection(
            ["❯ [TG] " + self.text],
            pane_checks=[(True, "agent running"), OSError("pane unavailable")],
        )
        self.assertEqual(len(enters), 1)
        logs.error.assert_called_once()

    def test_submitted_echo_followed_by_agent_output_does_not_resend(self):
        for output in ("✻ Thinking...", "  ● Working...", "Answer in progress"):
            with self.subTest(output=output):
                pane = "❯ [TG] " + self.text + "\n\n" + output
                _, enters, logs, _, _ = self.run_injection([pane])
                self.assertEqual(len(enters), 1)
                logs.warning.assert_not_called()
                logs.error.assert_not_called()

    def test_failed_verification_keeps_bridge_turn_active_without_reinjecting(self):
        from agent2telegram.attach import AttachBridge

        target = object.__new__(TmuxSession)
        target.name = "a2t-enter-test"
        target._origin = ""
        bridge = object.__new__(AttachBridge)
        bridge._session = target
        bridge._turn_active = threading.Event()
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(session, "_tmux", return_value=result) as tmux, \
                patch.object(session.time, "sleep"), \
                patch.object(TmuxSession, "_exists", return_value=True), \
                patch.object(TmuxSession, "_pane_ok", return_value=(True, "agent running")), \
                patch.object(TmuxSession, "_wait_for_submission", return_value=False), \
                self.assertLogs(session.log, level="WARNING") as logs:
            self.assertTrue(bridge._inject(self.text))
        self.assertTrue(bridge._turn_active.is_set())
        calls = [call.args for call in tmux.call_args_list]
        self.assertEqual(sum(call[-1] == "Enter" for call in calls), 2)
        self.assertEqual(sum("-l" in call for call in calls), 1)
        self.assertTrue(any("enter_not_submitted" in message for message in logs.output))


if __name__ == "__main__":
    unittest.main()
