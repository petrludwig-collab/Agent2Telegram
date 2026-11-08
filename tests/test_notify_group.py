"""The notify CLI requires an explicit, configured group destination."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent2telegram.__main__ import _main
from agent2telegram.config import Config


class NotifyGroupTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], allowed_group_ids=[-500])
        self.client = Mock()

    def run_notify(self, *args):
        self.output = io.StringIO()
        with patch("agent2telegram.config.load", return_value=self.cfg), \
                patch("agent2telegram.telegram.TelegramClient", return_value=self.client), \
                contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return _main(["notify", *args])

    def test_default_keeps_owner_dm(self):
        self.assertEqual(self.run_notify("Notice"), 0)
        self.client.send_message.assert_called_once_with(7, "Notice")

    def test_single_group_without_id_and_content_free_log(self):
        with self.assertLogs("agent2telegram", level="INFO") as logs:
            self.assertEqual(self.run_notify("Synthetic private notice", "--to-group"), 0)
        self.client.send_message.assert_called_once_with(-500, "Synthetic private notice")
        self.assertIn("-500", " ".join(logs.output))
        self.assertNotIn("Synthetic private notice", " ".join(logs.output))

    def test_explicit_group_among_multiple(self):
        self.cfg.allowed_group_ids.append(-600)
        self.assertEqual(self.run_notify("Notice", "--to-group", "-600"), 0)
        self.client.send_message.assert_called_once_with(-600, "Notice")

    def test_missing_ambiguous_and_unauthorized_groups_fail_before_network(self):
        for groups, args in (([], []), ([-500, -600], []), ([-500], ["-999"]),
                             ([-500], ["7"]), ([-500], ["0"])):
            with self.subTest(groups=groups, args=args):
                self.cfg.allowed_group_ids = groups
                self.client.reset_mock()
                self.assertEqual(self.run_notify("Notice", "--to-group", *args), 2)
                self.assertTrue(self.output.getvalue().strip())
                self.client.send_message.assert_not_called()
                self.client.send_file.assert_not_called()

    def test_group_file_only_and_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            attachment = (Path(directory) / "report.txt").resolve()
            attachment.write_text("Report")
            self.cfg.outbox_dirs = [directory]
            self.assertEqual(self.run_notify("--file", str(attachment), "--to-group", "-500"), 0)
            self.client.send_file.assert_called_once_with(-500, attachment)
            self.client.send_message.assert_not_called()
        with patch("sys.stdin", io.StringIO("Piped notice")):
            self.assertEqual(self.run_notify("--to-group", "-500"), 0)
        self.client.send_message.assert_called_once_with(-500, "Piped notice")

    def test_group_file_allowlist_is_still_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "private.txt"
            attachment.write_text("Private")
            self.assertEqual(self.run_notify("--to-group", "-500", "--file", str(attachment)), 1)
            self.client.send_file.assert_not_called()
