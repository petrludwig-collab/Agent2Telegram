"""Configuration loading, validation and persistence.

Config is a small JSON document stored at ``$AGENT2TELEGRAM_CONFIG`` or, by default,
``~/.config/agent2telegram/config.json``. The file holds a bot token, so it is always
written with ``0600`` permissions and never logged.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_ENV = "AGENT2TELEGRAM_CONFIG"
DEFAULT_PATH = Path.home() / ".config" / "agent2telegram" / "config.json"

#: Telegram bot tokens look like ``123456789:AA...`` — used only for a friendly
#: early error, never as real validation (Telegram is the source of truth).
_TOKEN_HINT = ":"


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid (message is user-facing)."""


@dataclass
class Config:
    agent: str                          # adapter name: claude-code | codex | antigravity | generic
    token: str                          # Telegram bot token
    allowed_user_ids: list[int] = field(default_factory=list)   # who may drive the agent
    workdir: str = ""                   # base dir for per-chat working directories
    command: list[str] | None = None    # optional override of the agent's first-turn command
    continue_command: list[str] | None = None   # optional override of the follow-up command
    agent_timeout: int = 600            # seconds before a single agent run is killed
    poll_timeout: int = 50              # long-poll timeout for getUpdates
    elevenlabs_api_key: str = ""        # optional: enables voice-message transcription (STT)
    # ---- persistent "attach" mode (drive an existing live agent session) ----
    mode: str = "oneshot"               # "oneshot" | "attach"
    tmux_session: str = ""              # name of the existing tmux session to drive
    signal_file: str = ""               # where the Stop hook writes the final answer
    transcript_path: str = ""           # agent transcript to tail for interim updates
    origin_prefix: str = "Telegram:"    # injected before each message; hook forwards only these
    claude_session_id: str = ""         # guard: only act on this session's transcript
    progress_marker: str = "[tg]"       # lines starting with this are sent live (interim)

    def path_workdir(self) -> Path:
        base = Path(self.workdir).expanduser() if self.workdir else (_state_dir() / "chats")
        return base

    def validate(self) -> None:
        if not self.agent or not isinstance(self.agent, str):
            raise ConfigError("Missing 'agent' (choose: claude-code, codex, antigravity).")
        if not self.token or _TOKEN_HINT not in self.token:
            raise ConfigError("Missing or malformed Telegram bot token (expected '<id>:<secret>').")
        if not isinstance(self.allowed_user_ids, list) or not all(isinstance(i, int) for i in self.allowed_user_ids):
            raise ConfigError("'allowed_user_ids' must be a list of integers.")
        if self.agent_timeout <= 0:
            raise ConfigError("'agent_timeout' must be positive.")

    def redacted(self) -> dict:
        """A copy safe to print/log: the token is masked."""
        d = asdict(self)
        tok = self.token or ""
        d["token"] = (tok[:6] + "…" + tok[-2:]) if len(tok) > 10 else "…"
        d["elevenlabs_api_key"] = "set" if self.elevenlabs_api_key else ""
        return d


def _state_dir() -> Path:
    return Path(
        os.environ.get("AGENT2TELEGRAM_STATE", Path.home() / ".local" / "state" / "agent2telegram")
    ).expanduser()


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV, DEFAULT_PATH)).expanduser()


def load(path: Path | None = None) -> Config:
    p = path or config_path()
    if not p.exists():
        raise ConfigError(
            f"No config at {p}. Run 'python -m agent2telegram setup' first."
        )
    try:
        raw = json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Config at {p} is not valid JSON: {e}") from e
    # Secrets may also come from the environment, keeping them out of the file entirely.
    if env_token := os.environ.get("TELEGRAM_BOT_TOKEN"):
        raw["token"] = env_token
    if env_key := os.environ.get("ELEVENLABS_API_KEY"):
        raw["elevenlabs_api_key"] = env_key
    known = {f for f in Config.__dataclass_fields__}
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    cfg.validate()
    return cfg


def save(cfg: Config, path: Path | None = None) -> Path:
    cfg.validate()
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Lock down the directory too (best effort) — it holds a secret-bearing file.
    try:
        os.chmod(p.parent, stat.S_IRWXU)   # 0700
    except OSError:
        pass
    # Write atomically, then lock down permissions — the file contains a secret.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), "utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)   # 0600
    os.replace(tmp, p)
    return p
