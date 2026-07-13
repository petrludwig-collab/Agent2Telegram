import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent2telegram.attach import AttachBridge


class _Telegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))


class DiagnosticCommandTests(unittest.TestCase):
    def setUp(self):
        self.bridge = object.__new__(AttachBridge)
        self.bridge.cfg = SimpleNamespace(
            agent="codex", tmux_session="moneypenny", elevenlabs_api_key=""
        )
        self.bridge.tg = _Telegram()
        self.bridge._session = SimpleNamespace(alive=True)
        self.bridge._turn_active = threading.Event()
        self.bridge._pending_send = []
        self.bridge._transcript = Path("rollout-safe.jsonl")
        self.bridge._bridge_started = time.monotonic() - 10

    def test_health_reports_runtime_state(self):
        self.assertTrue(self.bridge._handle_command("/health", 7))
        text = self.bridge.tg.sent[-1][1]
        self.assertIn("Bridge: running", text)
        self.assertIn("moneypenny", text)
        self.assertIn("outbound queue: 0", text)

    def test_diag_contains_no_config_or_token(self):
        self.assertTrue(self.bridge._handle_command("/diag", 7))
        text = self.bridge.tg.sent[-1][1]
        self.assertIn("Agent2Telegram diagnostics", text)
        self.assertIn("rollout-safe.jsonl", text)
        self.assertNotIn("token", text.lower())


if __name__ == "__main__":
    unittest.main()
