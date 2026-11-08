"""Tests for config load/save/validation."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent2telegram.config import Config, ConfigError, load, mark_secret_from_file, save


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"
        self._env = {
            "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
            "ELEVENLABS_API_KEY": os.environ.get("ELEVENLABS_API_KEY"),
        }
        for name in self._env:
            os.environ.pop(name, None)

    def tearDown(self):
        self.dir.cleanup()
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_roundtrip(self):
        cfg = Config(agent="codex", token="123:secret", allowed_user_ids=[7])
        save(cfg, self.path)
        loaded = load(self.path)
        self.assertEqual(loaded.agent, "codex")
        self.assertEqual(loaded.allowed_user_ids, [7])

    def test_saved_file_is_0600(self):
        save(Config(agent="codex", token="1:2", allowed_user_ids=[1]), self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_missing_token_invalid(self):
        with self.assertRaises(ConfigError):
            Config(agent="codex", token="", allowed_user_ids=[]).validate()

    def test_malformed_token_invalid(self):
        with self.assertRaises(ConfigError):
            Config(agent="codex", token="no-colon", allowed_user_ids=[]).validate()

    def test_allowed_user_ids_must_be_real_integers_not_bools(self):
        with self.assertRaises(ConfigError):
            Config(agent="codex", token="1:2", allowed_user_ids=[True]).validate()
        with self.assertRaises(ConfigError):
            Config(agent="codex", token="1:2", allowed_user_ids=["7"]).validate()

    def test_timeouts_must_be_positive_integers(self):
        bad = [
            Config(agent="codex", token="1:2", allowed_user_ids=[1], agent_timeout=0),
            Config(agent="codex", token="1:2", allowed_user_ids=[1], agent_timeout=True),
            Config(agent="codex", token="1:2", allowed_user_ids=[1], poll_timeout=0),
            Config(agent="codex", token="1:2", allowed_user_ids=[1], poll_timeout=True),
        ]
        for cfg in bad:
            with self.subTest(cfg=cfg):
                with self.assertRaises(ConfigError):
                    cfg.validate()

    def test_unknown_mode_is_invalid(self):
        with self.assertRaises(ConfigError):
            Config(agent="codex", token="1:2", allowed_user_ids=[1], mode="webhook").validate()

    def test_enter_verify_timeout_defaults_and_roundtrips(self):
        cfg = Config(agent="codex", token="1:2", allowed_user_ids=[1])
        self.assertEqual(cfg.enter_verify_timeout, 8.0)
        cfg.enter_verify_timeout = 0.25
        save(cfg, self.path)
        self.assertEqual(load(self.path).enter_verify_timeout, 0.25)

    def test_enter_verify_timeout_rejects_invalid_or_unbounded_values(self):
        for value in (True, False, 0, -1, "8", None, float("nan"), float("inf")):
            with self.subTest(value=value):
                cfg = Config(agent="codex", token="1:2", enter_verify_timeout=value)
                with self.assertRaises(ConfigError):
                    cfg.validate()

    def test_redacted_masks_token(self):
        cfg = Config(agent="codex", token="123456:supersecret", allowed_user_ids=[1])
        self.assertNotIn("supersecret", str(cfg.redacted()))

    def test_env_token_override(self):
        save(Config(agent="codex", token="111:fromfile", allowed_user_ids=[1]), self.path)
        os.environ["TELEGRAM_BOT_TOKEN"] = "999:fromenv"
        self.assertEqual(load(self.path).token, "999:fromenv")

    def test_env_secrets_are_not_persisted_on_save(self):
        self.path.write_text(
            json.dumps({"agent": "codex", "allowed_user_ids": [1]}),
            "utf-8",
        )
        os.environ["TELEGRAM_BOT_TOKEN"] = "999:fromenv"
        os.environ["ELEVENLABS_API_KEY"] = "env-elevenlabs-key"

        cfg = load(self.path)
        cfg.bot_username = "agent2telegram_bot"
        save(cfg, self.path)

        text = self.path.read_text("utf-8")
        data = json.loads(text)
        self.assertNotIn("token", data)
        self.assertNotIn("elevenlabs_api_key", data)
        self.assertNotIn("999:fromenv", text)
        self.assertNotIn("env-elevenlabs-key", text)

    def test_file_secrets_are_preserved_when_env_overrides_runtime(self):
        save(
            Config(
                agent="codex",
                token="111:fromfile",
                allowed_user_ids=[1],
                elevenlabs_api_key="file-elevenlabs-key",
            ),
            self.path,
        )
        os.environ["TELEGRAM_BOT_TOKEN"] = "999:fromenv"
        os.environ["ELEVENLABS_API_KEY"] = "env-elevenlabs-key"

        cfg = load(self.path)
        self.assertEqual(cfg.token, "999:fromenv")
        self.assertEqual(cfg.elevenlabs_api_key, "env-elevenlabs-key")
        cfg.bot_username = "agent2telegram_bot"
        save(cfg, self.path)

        data = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(data["token"], "111:fromfile")
        self.assertEqual(data["elevenlabs_api_key"], "file-elevenlabs-key")

    def test_mark_secret_from_file_persists_user_entered_secret(self):
        cfg = Config(agent="codex", token="111:fromfile", allowed_user_ids=[1])
        cfg.elevenlabs_api_key = "new-file-backed-key"
        mark_secret_from_file(cfg, "elevenlabs_api_key")

        save(cfg, self.path)

        data = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(data["elevenlabs_api_key"], "new-file-backed-key")

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load(Path(self.dir.name) / "nope.json")


if __name__ == "__main__":
    unittest.main()
