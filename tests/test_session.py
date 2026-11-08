"""Tests for tmux session input handling."""
import os
import subprocess
import unittest
from unittest.mock import patch

from agent2telegram import session
from agent2telegram.session import (MAX_TMUX_INJECTION_CHARS, PASTE_MIN_BYTES, SessionError, TmuxSession,
                                    sanitize_for_tmux)


class SanitizeForTmuxTests(unittest.TestCase):
    def test_regular_text_is_unchanged(self):
        text = "Hello, world 123. Regular punctuation: []{};:,./?\n\tDone."
        self.assertEqual(sanitize_for_tmux(text), text)

    def test_empty_text_stays_empty(self):
        self.assertEqual(sanitize_for_tmux(""), "")

    def test_unicode_is_preserved(self):
        text = "Grüße ✅ Привет こんにちは 🚀"
        self.assertEqual(sanitize_for_tmux(text), text)

    def test_control_characters_are_stripped_except_newline_and_tab(self):
        control_chars = "".join(chr(i) for i in [*range(0x20), 0x7f, *range(0x80, 0xa0)])
        self.assertEqual(sanitize_for_tmux(f"a{control_chars}b"), "a\t\nb")

    def test_text_is_truncated(self):
        text = "x" * (MAX_TMUX_INJECTION_CHARS + 1)
        self.assertEqual(sanitize_for_tmux(text), "x" * MAX_TMUX_INJECTION_CHARS)

    def test_truncation_counts_only_surviving_characters(self):
        text = ("\x00" * 100) + ("x" * (MAX_TMUX_INJECTION_CHARS + 50))
        self.assertEqual(sanitize_for_tmux(text), "x" * MAX_TMUX_INJECTION_CHARS)


class TmuxSessionSendKeysTests(unittest.TestCase):
    def _bare(self, expected=("codex",)):
        s = object.__new__(TmuxSession)
        s.name = "a2t-test"
        s._origin = ""
        s._expected_agent_commands = tuple(expected)
        return s

    @staticmethod
    def _cp(args, *, stdout="", returncode=0):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    def test_send_keys_passes_sanitized_literal_argument_to_tmux(self):
        with patch.object(session, "_tmux") as tmux, patch.object(session.time, "sleep"):
            tmux.return_value = self._cp(())
            s = object.__new__(TmuxSession)
            s.name = "a2t-test"
            s._origin = "from tg: "
            s._expected_agent_commands = ()
            s._send_keys("hello\x0bthere\x03\x1b[31m\x7fworld\x85!")

        calls = [call.args for call in tmux.call_args_list]
        literal_calls = [c for c in calls if c[:5] == ("send-keys", "-t", "a2t-test", "-l", "--")]
        self.assertEqual(len(literal_calls), 1)
        self.assertEqual(literal_calls[0][-1], "from tg: hellothere[31mworld!")

    def test_send_keys_collapses_newlines_to_one_literal_submit(self):
        with patch.object(session, "_tmux") as tmux, patch.object(session.time, "sleep"):
            tmux.return_value = self._cp(())
            s = self._bare(())
            s._send_keys("first line\nsecond line\r\nthird\tline")

        calls = [call.args for call in tmux.call_args_list]
        literal = [c[-1] for c in calls if c[:5] == ("send-keys", "-t", "a2t-test", "-l", "--")]
        submits = [c for c in calls if c == ("send-keys", "-t", "a2t-test", "Enter")]
        self.assertEqual(literal, ["first line second line third\tline"])
        self.assertEqual(len(submits), 1)

    def test_allowed_agent_pane_allows_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                return self._cp(args, stdout="codex\n")
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_exists", return_value=True):
            self._bare(("codex",)).inject("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_node_wrapper_for_expected_agent_allows_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                if args[-1] == "#{pane_current_command}":
                    return self._cp(args, stdout="node\n")
                if args[-1] == "#{pane_pid}":
                    return self._cp(args, stdout="100\n")
            return self._cp(args)

        ps_out = "\n".join([
            "100 1 /bin/zsh zsh",
            "101 100 /usr/local/bin/node node /opt/tools/codex/bin/codex.js",
        ])
        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.subprocess, "run", return_value=self._cp(["ps"], stdout=ps_out)), \
             patch.object(session.time, "sleep"):
            self._bare(("codex",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_full_path_expected_agent_allows_basename_match(self):
        with patch.object(session, "_pane_value", return_value="/opt/homebrew/bin/codex"):
            ok, detail = session._agent_alive("a2t-test", ("/Users/me/.local/bin/codex",))

        self.assertTrue(ok)
        self.assertIn("codex", detail)

    def test_python_wrapper_that_mentions_expected_agent_allows_injection(self):
        calls = []
        processes = [
            (100, 1, "/bin/zsh", "zsh"),
            (101, 100, "/usr/bin/python3", "python3 /opt/wrappers/run_agent.py --agent codex"),
        ]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="python3"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            self._bare(("codex",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_pane_without_agent_child_blocks_injection_even_when_not_shell(self):
        calls = []
        processes = [(100, 1, "/usr/bin/vim", "vim README.md")]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="vim"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            with self.assertRaises(SessionError):
                self._bare(("codex",))._send_keys("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])

    def test_shell_leader_with_full_path_claude_child_allows_injection(self):
        calls = []
        processes = [
            (100, 1, "-sh", "-sh"),
            (
                101,
                100,
                "/Users/you/.local/bin/claude",
                "/Users/you/.local/bin/claude --resume abc123 --dangerously-skip-permissions",
            ),
        ]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="2.1.185"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            self._bare(("claude",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_shell_leader_without_agent_child_blocks_injection(self):
        calls = []
        processes = [(100, 1, "-sh", "-sh")]

        def tmux(*args, **kwargs):
            calls.append(args)
            return self._cp(args)

        with patch.object(session, "_pane_value", return_value="2.1.185"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            with self.assertRaises(SessionError):
                self._bare(("claude",))._send_keys("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])

    def test_shell_prompt_blocks_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                return self._cp(args, stdout="zsh\n")
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_exists", return_value=True):
            with self.assertRaises(SessionError):
                self._bare(("codex",)).inject("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])


if __name__ == "__main__":
    unittest.main()


class LongMessagePasteTests(unittest.TestCase):
    """A long message typed with `send-keys -l` loses its head in Claude Code's TUI (seen 2026-09-09:
    a 1.7 KB Telegram message arrived without its first ~1 KB, routing prefix included). Long text
    must therefore go in as ONE bracketed paste through a tmux buffer; short text keeps the proven
    literal path."""

    def _bare(self):
        s = object.__new__(TmuxSession)
        s.name = "a2t-test"
        s._origin = "[TG] "
        s._expected_agent_commands = ()
        return s

    def _run(self, text):
        loaded = {}

        def tmux(*args, **kwargs):
            if args and args[0] == "load-buffer":
                with open(args[-1], encoding="utf-8") as fh:
                    loaded["content"] = fh.read()
                loaded["path"] = args[-1]
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with patch.object(session, "_tmux", side_effect=tmux) as mocked, \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_pane_ok", return_value=(True, "")):
            self._bare()._send_keys(text)
        return [c.args for c in mocked.call_args_list], loaded

    def test_long_text_goes_in_as_one_bracketed_paste_with_prefix_intact(self):
        body = "Text with Unicode λ, commas and full stops. " * 40          # ≈ 1.7 KB, like the real case
        calls, loaded = self._run(body + "\nfinal line")
        names = [c[0] for c in calls]
        self.assertEqual(names, ["send-keys", "load-buffer", "paste-buffer", "send-keys", "capture-pane"])
        self.assertEqual(calls[0][1:], ("-t", "a2t-test", "C-u"))
        self.assertEqual(calls[-2][1:], ("-t", "a2t-test", "Enter"))
        self.assertIn("-p", calls[2])                                     # bracketed paste
        self.assertIn("-d", calls[2])                                     # buffer deleted after use
        self.assertEqual(calls[1][2], calls[2][calls[2].index("-b") + 1])  # same buffer name
        self.assertTrue(loaded["content"].startswith("[TG] Text with Unicode λ"))
        self.assertEqual(loaded["content"], "[TG] " + body + " final line")   # whole text, one line
        self.assertFalse(os.path.exists(loaded["path"]))                   # temp file cleaned up

    def test_short_text_keeps_the_literal_send_keys_path(self):
        calls, loaded = self._run("✅ short message")
        names = [c[0] for c in calls]
        self.assertEqual(names, ["send-keys", "send-keys", "send-keys", "capture-pane"])
        self.assertEqual(calls[1][1:], ("-t", "a2t-test", "-l", "--", "[TG] ✅ short message"))
        self.assertEqual(loaded, {})

    def test_threshold_is_measured_in_bytes_not_characters(self):
        text = "λ" * (PASTE_MIN_BYTES // 2 + 10)      # few characters, but > PASTE_MIN_BYTES bytes
        calls, _ = self._run(text)
        self.assertIn("paste-buffer", [c[0] for c in calls])
