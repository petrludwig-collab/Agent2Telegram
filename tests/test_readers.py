"""Regression tests for Codex rollout transcript parsing."""
import unittest

from agent2telegram.readers import CodexReader


class CodexReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader = CodexReader()

    def test_current_codex_assistant_message_is_forwarded(self):
        """Codex 0.147 stores reply text in response_item/message content blocks."""
        record = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_123",
                "role": "assistant",
                "phase": "final_answer",
                "content": [
                    {"type": "output_text", "text": "Ahoj! Jak ti můžu pomoct?"},
                ],
            },
        }

        self.assertEqual(
            list(self.reader.parse(record)),
            [
                self._event("Ahoj! Jak ti můžu pomoct?", "msg_123", final=True),
            ],
        )

    def test_current_codex_user_message_marks_telegram_turn(self):
        record = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "[TG] reply with PONG"},
                ],
            },
        }

        self.assertEqual(self.reader.user_text(record), "[TG] reply with PONG")
        self.assertEqual(
            list(self.reader.parse(record)),
            [self._user_event("[TG] reply with PONG")],
        )

    def test_current_codex_only_forwards_assistant_output(self):
        """Instructions and user inputs in a rollout must never reach Telegram."""
        record = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "output_text", "text": "internal instruction"}],
            },
        }

        self.assertEqual(list(self.reader.parse(record)), [])

    @staticmethod
    def _event(text, key, *, final):
        from agent2telegram.readers import Ev
        return Ev("text", text=text, key=key, final=final)

    @staticmethod
    def _user_event(text):
        from agent2telegram.readers import Ev
        return Ev("user", text=text)


if __name__ == "__main__":
    unittest.main()
