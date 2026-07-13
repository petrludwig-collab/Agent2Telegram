import unittest

from agent2telegram.attach import _blocking_session_prompt


class SessionReadinessTests(unittest.TestCase):
    def test_update_prompt_blocks_bridge(self):
        pane = "Update available! 0.144.0 -> 0.144.1\nPress enter to continue"
        self.assertEqual(_blocking_session_prompt(pane), "codex_update_prompt")

    def test_normal_codex_prompt_is_ready(self):
        pane = "OpenAI Codex (v0.144.0)\n› Explain this codebase"
        self.assertIsNone(_blocking_session_prompt(pane))


if __name__ == "__main__":
    unittest.main()
