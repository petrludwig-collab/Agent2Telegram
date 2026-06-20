"""Behavioural tests for the bridge — no network, no real agent."""
import tempfile
import unittest

from agent2telegram.bridge import Bridge
from agent2telegram.config import Config


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.actions = []

    def get_me(self):
        return {"username": "fakebot"}

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    def run(self, prompt, *, chat_dir, is_continuation):
        chat_dir.mkdir(parents=True, exist_ok=True)   # mimic a real adapter
        self.calls.append({"prompt": prompt, "is_continuation": is_continuation})
        return f"echo: {prompt}"


class BridgeContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(agent="claude-code", token="1:2", allowed_user_ids=[7],
                     workdir=self.tmp.name)
        self.bridge = Bridge(cfg, client=_FakeClient())
        self.adapter = _FakeAdapter()
        self.bridge.adapter = self.adapter

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_turn_is_not_continuation_second_is(self):
        self.bridge.process(100, "hello")
        self.bridge.process(100, "again")
        self.assertEqual(self.adapter.calls[0]["is_continuation"], False)
        self.assertEqual(self.adapter.calls[1]["is_continuation"], True)

    def test_reply_is_sent(self):
        self.bridge.process(100, "hello")
        self.assertEqual(self.bridge.tg.sent[-1], (100, "echo: hello"))

    def test_reset_makes_next_turn_fresh(self):
        self.bridge.process(100, "hello")
        self.bridge._reset_chat(100)
        self.bridge.process(100, "after reset")
        self.assertEqual(self.adapter.calls[-1]["is_continuation"], False)

    def test_separate_chats_are_independent(self):
        self.bridge.process(1, "a")
        self.bridge.process(2, "b")
        self.assertFalse(self.adapter.calls[0]["is_continuation"])
        self.assertFalse(self.adapter.calls[1]["is_continuation"])  # different chat → fresh

    def test_non_text_message_gets_friendly_notice(self):
        # Authorized user sends a photo (no 'text') → friendly notice, no agent call.
        self.bridge._dispatch({"update_id": 1, "message": {
            "chat": {"id": 100}, "from": {"id": 7}, "photo": [{"file_id": "x"}]}})
        self.assertEqual(self.adapter.calls, [])
        self.assertTrue(any("text" in t.lower() for _, t in self.bridge.tg.sent))

    def test_unauthorized_user_is_refused(self):
        self.bridge._dispatch({"update_id": 1, "message": {
            "chat": {"id": 100}, "from": {"id": 999}, "text": "do something"}})
        # No agent call; a refusal was sent.
        self.assertEqual(self.adapter.calls, [])
        self.assertTrue(any("not authorized" in t.lower() for _, t in self.bridge.tg.sent))


if __name__ == "__main__":
    unittest.main()
