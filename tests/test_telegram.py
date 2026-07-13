"""Tests for the Telegram client and message helpers — no network required."""
import io
import json
import unittest
import urllib.error

from agent2telegram.telegram import TelegramClient, split_message, MAX_MESSAGE_LEN


class SplitMessageTests(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(split_message("hello"), ["hello"])

    def test_empty_text_no_chunks(self):
        self.assertEqual(split_message(""), [])

    def test_long_text_is_chunked_within_limit(self):
        text = "\n".join(f"line {i}" for i in range(5000))
        chunks = split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= MAX_MESSAGE_LEN for c in chunks))

    def test_reassembles_to_original_content(self):
        text = "word " * 4000
        chunks = split_message(text)
        joined = " ".join(c.strip() for c in chunks)
        self.assertEqual(joined.split(), text.split())

    def test_hard_split_when_no_boundary(self):
        text = "x" * (MAX_MESSAGE_LEN * 2 + 5)
        chunks = split_message(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c) <= MAX_MESSAGE_LEN for c in chunks))

    def test_emoji_are_counted_as_two_utf16_units(self):
        text = "🚀" * (MAX_MESSAGE_LEN + 1)
        chunks = split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= MAX_MESSAGE_LEN for c in chunks))


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _FakeOpener:
    """Replays a scripted sequence of responses/exceptions for `.open()`."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(json.dumps(item).encode())


def _ok(result):
    return {"ok": True, "result": result}


class TelegramClientTests(unittest.TestCase):
    def test_get_me(self):
        op = _FakeOpener([_ok({"username": "mybot"})])
        client = TelegramClient("123:abc", opener=op)
        self.assertEqual(client.get_me()["username"], "mybot")

    def test_api_error_raises(self):
        op = _FakeOpener([{"ok": False, "description": "Unauthorized"}])
        client = TelegramClient("123:abc", opener=op)
        with self.assertRaises(Exception):
            client.get_me()

    def test_flood_control_retry(self):
        # First call: HTTP 429 with retry_after; then success. Should transparently retry.
        flood = urllib.error.HTTPError(
            "url", 429, "Too Many Requests", {},
            io.BytesIO(json.dumps({"parameters": {"retry_after": 0}}).encode()),
        )
        op = _FakeOpener([flood, _ok({"username": "mybot"})])
        client = TelegramClient("123:abc", opener=op)
        self.assertEqual(client.get_me()["username"], "mybot")
        self.assertEqual(len(op.calls), 2)

    def test_send_message_splits_long_text(self):
        long_text = "a\n" * 5000
        op = _FakeOpener([_ok({"message_id": i}) for i in range(1, 11)])
        client = TelegramClient("123:abc", opener=op)
        ids = client.send_message(42, long_text)
        self.assertGreater(len(op.calls), 1)
        self.assertEqual(ids, list(range(1, len(op.calls) + 1)))

    def test_send_message_falls_back_to_plain_on_markdown_error(self):
        md_fail = {"ok": False, "description": "can't parse entities"}
        op = _FakeOpener([md_fail, _ok({"message_id": 7})])
        client = TelegramClient("123:abc", opener=op)
        client.send_message(42, "*broken", parse_mode="Markdown")
        # Second call must omit parse_mode.
        self.assertEqual(len(op.calls), 2)
        self.assertNotIn(b"parse_mode", op.calls[1].data)

    def test_send_message_requires_confirmation_id(self):
        client = TelegramClient("123:abc", opener=_FakeOpener([_ok({})]))
        with self.assertRaises(Exception):
            client.send_message(42, "hello")


if __name__ == "__main__":
    unittest.main()
