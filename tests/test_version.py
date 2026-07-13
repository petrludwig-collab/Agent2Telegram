import re
import unittest
from pathlib import Path

import agent2telegram


class VersionTests(unittest.TestCase):
    def test_runtime_matches_project_metadata(self):
        pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(agent2telegram.__version__, match.group(1))


if __name__ == "__main__":
    unittest.main()
