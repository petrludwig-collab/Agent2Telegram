"""Sending files FROM the agent (`[tg-file] <path>`).

The path comes from the agent's own reply text, so the allowlist is a security
boundary, not a convenience: without it anything able to influence the agent's
output could make the bridge upload `~/.ssh/id_rsa`. These tests pin that down
together with the plumbing (marker extraction, method per file type).
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram.config import Config
from agent2telegram.telegram import TelegramClient


def cfg_with_outbox(outbox: Path, extra: list[str] | None = None) -> Config:
    c = Config(token="1:x", agent="claude-code")
    c.outbox_dirs = [str(outbox)] + (extra or [])
    return c


class FakeBridge:
    """Only the bits `_safe_outbox_path` / `_extract_files` / `_send_files` touch."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tg = mock.Mock()
        self._owner_chat = 42
        self.sent_text: list[str] = []
        self._pending_files: list[str] = []

    def _send_final(self, text, key=None, *, turn_text=True):
        self.sent_text.append(text)


def bind(bridge: FakeBridge):
    from agent2telegram.attach import AttachBridge
    for name in ("_safe_outbox_path", "_extract_files", "_send_files", "_flush_files"):
        setattr(bridge, name, getattr(AttachBridge, name).__get__(bridge, FakeBridge))
    return bridge


class OutgoingFilesTest(unittest.TestCase):
    def test_marker_is_stripped_from_the_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            b = bind(FakeBridge(cfg_with_outbox(Path(tmp))))
            text, files = b._extract_files(
                "Here is the preview.\n[tg-file] /tmp/a.mp4\nAnd the audio too.\n[tg-file] '/tmp/b.mp3'")
            self.assertEqual(text, "Here is the preview.\nAnd the audio too.")
            self.assertEqual(files, ["/tmp/a.mp4", "/tmp/b.mp3"])

    def test_a_file_inside_the_outbox_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            f = out / "clip.mp4"
            f.write_bytes(b"x" * 100)
            b = bind(FakeBridge(cfg_with_outbox(out)))
            resolved, reason = b._safe_outbox_path(str(f))
            self.assertEqual(reason, "ok")
            self.assertEqual(resolved, f.resolve())

    def test_file_outside_outbox_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            secret = Path(tmp) / "id_rsa"
            secret.write_bytes(b"PRIVATE KEY")
            b = bind(FakeBridge(cfg_with_outbox(out)))
            resolved, reason = b._safe_outbox_path(str(secret))
            self.assertIsNone(resolved)
            self.assertIn("outside", reason)

    def test_a_symlink_leading_out_of_the_outbox_is_refused(self) -> None:
        """The nastiest case: a link inside the allowed directory pointing at a secret file.
        That is why the path is resolved BEFORE the check, not after it."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            secret = Path(tmp) / "credentials.json"
            secret.write_bytes(b'{"token":"secret"}')
            link = out / "innocent.json"
            os.symlink(secret, link)
            b = bind(FakeBridge(cfg_with_outbox(out)))
            resolved, reason = b._safe_outbox_path(str(link))
            self.assertIsNone(resolved, "a symlink out of the outbox must be refused")
            self.assertIn("outside", reason)

    def test_a_refusal_is_reported_in_the_chat(self) -> None:
        """A silently dropped attachment would be worse than a visible error."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            b = bind(FakeBridge(cfg_with_outbox(out)))
            b._send_files([str(Path(tmp) / "somewhere-else.mp4")])
            self.assertTrue(b.sent_text, "the refusal must show up in the chat")
            self.assertIn("Couldn't send", b.sent_text[0])
            b.tg.send_file.assert_not_called()

    def test_an_empty_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            f = out / "empty.mp4"
            f.touch()
            b = bind(FakeBridge(cfg_with_outbox(out)))
            resolved, reason = b._safe_outbox_path(str(f))
            self.assertIsNone(resolved)
            self.assertIn("empty", reason)

    def test_method_matches_file_type(self) -> None:
        self.assertEqual(TelegramClient.kind_for("a.mp4"), ("sendVideo", "video"))
        self.assertEqual(TelegramClient.kind_for("a.MOV"), ("sendVideo", "video"))
        self.assertEqual(TelegramClient.kind_for("a.opus"), ("sendAudio", "audio"))
        self.assertEqual(TelegramClient.kind_for("a.png"), ("sendPhoto", "photo"))
        self.assertEqual(TelegramClient.kind_for("a.zip"), ("sendDocument", "document"))

    def test_an_oversized_file_fails_immediately(self) -> None:
        """A clear error beats five minutes of uploading followed by a 413."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "big.mp4"
            f.write_bytes(b"x" * 16)
            c = TelegramClient("1:x", opener=mock.Mock())
            with mock.patch.object(os.path, "getsize", return_value=TelegramClient.MAX_UPLOAD + 1), \
                 self.assertRaises(Exception) as cm:
                c.send_file(1, str(f))
            self.assertIn("at most", str(cm.exception))

    def test_the_default_outbox_is_always_allowed(self) -> None:
        c = Config(token="1:x", agent="claude-code")
        self.assertIn(c.path_outbox(), c.allowed_outbox_dirs())


class NotifyFilesTest(unittest.TestCase):
    """`notify --file` is the path used by background jobs and cron. It must enforce the SAME
    boundary as the in-chat marker — running in the background is not more trustworthy."""

    def test_notify_refuses_a_file_outside_the_outbox(self) -> None:
        from agent2telegram import __main__ as m
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            secret = Path(tmp) / "credentials.json"
            secret.write_text("{}", encoding="utf-8")
            cfg = cfg_with_outbox(out)
            cfg.allowed_user_ids = [1]
            client = mock.Mock()
            args = mock.Mock(message="hi", config=None, file=[str(secret)], to_group=None)
            with mock.patch("agent2telegram.config.load", return_value=cfg), \
                 mock.patch("agent2telegram.telegram.TelegramClient", return_value=client):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = m._cmd_notify(args)
            self.assertEqual(rc, 1, "a refusal must return a non-zero exit code")
            client.send_file.assert_not_called()
            client.send_message.assert_called_once()      # the text goes through, the file does not

    def test_notify_sends_a_file_from_the_outbox(self) -> None:
        from agent2telegram import __main__ as m
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "outbox"
            out.mkdir()
            f = out / "waveform.png"
            f.write_bytes(b"x" * 50)
            cfg = cfg_with_outbox(out)
            cfg.allowed_user_ids = [1]
            client = mock.Mock()
            args = mock.Mock(message=None, config=None, file=[str(f)], to_group=None)
            with mock.patch("agent2telegram.config.load", return_value=cfg), \
                 mock.patch("agent2telegram.telegram.TelegramClient", return_value=client):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = m._cmd_notify(args)
            self.assertEqual(rc, 0)
            client.send_file.assert_called_once()
            client.send_message.assert_not_called()       # with no text, no message is sent


if __name__ == "__main__":
    unittest.main()
