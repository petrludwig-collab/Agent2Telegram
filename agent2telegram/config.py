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
_SECRET_ENVS = {
    "token": "TELEGRAM_BOT_TOKEN",
    "elevenlabs_api_key": "ELEVENLABS_API_KEY",
    "elevenlabs_language": "ELEVENLABS_LANGUAGE",
}
_SOURCE_ENV = "env"
_SOURCE_FILE = "file"


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid (message is user-facing)."""


@dataclass
class Config:
    agent: str                          # adapter name: claude-code | codex | generic
    token: str                          # Telegram bot token
    allowed_user_ids: list[int] = field(default_factory=list)   # who may drive the agent
    workdir: str = ""                   # base dir for per-chat working directories
    state_dir: str = ""                 # explicit opt-in; legacy default/offset stays unchanged
    command: list[str] | None = None    # optional override of the agent's first-turn command
    continue_command: list[str] | None = None   # optional override of the follow-up command
    agent_timeout: int = 600            # seconds before a single agent run is killed
    poll_timeout: int = 50              # long-poll timeout for getUpdates
    elevenlabs_api_key: str = ""        # optional: enables voice-message transcription (STT)
    elevenlabs_language: str = ""       # ISO code (cs, de…); blank = let Scribe auto-detect
    tts_voice_id: str = "XB0fDUnXU5powFXDhCwa"   # ElevenLabs voice for /voice replies (Charlotte)
    tts_model_id: str = "eleven_turbo_v2_5"  # 2026-09-05: v3 whispered halfway through long notes; turbo v2.5 measured level
    # ---- persistent "attach" mode (drive an existing live agent session) ----
    mode: str = "oneshot"               # "oneshot" | "attach"
    tmux_session: str = ""              # name of the existing tmux session to drive
    signal_file: str = ""               # where the Stop hook writes the final answer
    transcript_path: str = ""           # agent transcript to tail for interim updates
    origin_prefix: str = "Telegram:"    # injected before each message; hook forwards only these
    inbound_sender_label: bool = False  # attach: display sender/chat after origin_prefix
    claude_session_id: str = ""         # guard: only act on this session's transcript
    progress_marker: str = "[tg]"       # lines starting with this are sent live (interim)
    bot_username: str = ""              # the bot's @username (non-secret; for t.me/ deep links)
    require_mention: bool = False        # groups: only @bot mentions / replies to this bot
    reply_to_origin_chat: bool = False   # attach: answer in the chat the last authorized message came from
    group_interim: bool = False          # attach: opt into narration, tool/status bubbles and typing in groups
    group_only_user_ids: list[int] = field(default_factory=list)   # allowed users accepted ONLY in groups
    allowed_group_ids: list[int] = field(default_factory=list)     # if set, groups other than these are ignored
    group_marker: str = ""              # reply starting with this goes to allowed_group_ids[0] (marker stripped)
    language: str = "en"                # operational notices: en | cs (independent of STT)
    restart_notice: bool = False         # one startup notice, persisted 30-second cooldown
    restart_notice_text: str = ""        # optional short literal text, no formatting placeholders
    # Directories the agent may send files FROM (see `file_marker`). Deliberately narrow:
    # the path comes from the agent's own reply text, so a wide allowlist would turn a
    # prompt-injection into file exfiltration. Empty list = the default outbox only.
    outbox_dirs: list[str] = field(default_factory=list)
    file_marker: str = "[tg-file]"       # lines like `[tg-file] /path/clip.mp4` upload that file
    # Attach mode, Claude Code: show work that runs outside a Telegram turn — running sub-agents
    # (one message edited in place) and tool calls of turns not started from Telegram (e.g. a
    # background-task notification). Off by default: it adds messages to the owner's chat.
    background_progress: bool = False
    background_edit_interval: float = 10.0   # min seconds between edits of the progress message
    background_stale_after: float = 300.0    # silent this long → progress is dropped/deleted

    def path_outbox(self) -> Path:
        """Default place to put files meant for Telegram."""
        return _state_dir(self) / "outbox"

    def allowed_outbox_dirs(self) -> list[Path]:
        """Resolved allowlist. The default outbox is always in, extra dirs come from config."""
        dirs = [self.path_outbox()]
        for d in self.outbox_dirs or []:
            try:
                dirs.append(Path(d).expanduser().resolve())
            except OSError:
                continue
        return dirs

    def path_workdir(self) -> Path:
        base = Path(self.workdir).expanduser() if self.workdir else (_state_dir(self) / "chats")
        return base

    def validate(self) -> None:
        if not isinstance(self.state_dir, str) or (self.state_dir and not Path(self.state_dir).expanduser().is_absolute()):
            raise ConfigError("'state_dir' must be an absolute path or empty.")
        if type(self.inbound_sender_label) is not bool:
            raise ConfigError("'inbound_sender_label' must be a boolean.")
        if not isinstance(self.require_mention, bool):
            raise ConfigError("'require_mention' must be a boolean.")
        if type(self.reply_to_origin_chat) is not bool:
            raise ConfigError("'reply_to_origin_chat' must be a boolean.")
        if type(self.group_interim) is not bool:
            raise ConfigError("'group_interim' must be a boolean.")
        owner = self.allowed_user_ids[0] if self.allowed_user_ids else None
        if (not isinstance(self.group_only_user_ids, list)
                or any(type(i) is not int or i not in self.allowed_user_ids or i == owner
                       for i in self.group_only_user_ids)):
            raise ConfigError("'group_only_user_ids' must list allowed users other than the owner.")
        if (not isinstance(self.allowed_group_ids, list)
                or any(type(i) is not int or i >= 0 for i in self.allowed_group_ids)):
            raise ConfigError("'allowed_group_ids' must be a list of negative chat ids.")
        if type(self.restart_notice) is not bool:
            raise ConfigError("'restart_notice' must be a boolean.")
        if type(self.background_progress) is not bool:
            raise ConfigError("'background_progress' must be a boolean.")
        for name in ("background_edit_interval", "background_stale_after"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                raise ConfigError(f"'{name}' must be a positive number.")
        if self.language not in ('en', 'cs'):
            raise ConfigError("'language' must be 'en' or 'cs'.")
        if (not isinstance(self.restart_notice_text, str) or len(self.restart_notice_text) > 200
                or any(ord(c) < 32 for c in self.restart_notice_text)):
            raise ConfigError("'restart_notice_text' must be a single line of at most 200 characters.")
        if not self.agent or not isinstance(self.agent, str):
            raise ConfigError("Missing 'agent' (choose: claude-code, codex, generic).")
        if not self.token or _TOKEN_HINT not in self.token:
            raise ConfigError("Missing or malformed Telegram bot token (expected '<id>:<secret>').")
        if (
            not isinstance(self.allowed_user_ids, list)
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in self.allowed_user_ids)
        ):
            raise ConfigError("'allowed_user_ids' must be a list of integers.")
        if (
            not isinstance(self.agent_timeout, int)
            or isinstance(self.agent_timeout, bool)
            or self.agent_timeout <= 0
        ):
            raise ConfigError("'agent_timeout' must be positive.")
        if (
            not isinstance(self.poll_timeout, int)
            or isinstance(self.poll_timeout, bool)
            or self.poll_timeout <= 0
        ):
            raise ConfigError("'poll_timeout' must be positive.")
        if self.mode not in ("oneshot", "attach"):
            raise ConfigError("'mode' must be one of: oneshot, attach.")

    def redacted(self) -> dict:
        """A copy safe to print/log: the token is masked."""
        d = asdict(self)
        tok = self.token or ""
        d["token"] = (tok[:6] + "…" + tok[-2:]) if len(tok) > 10 else "…"
        d["elevenlabs_api_key"] = "set" if self.elevenlabs_api_key else ""
        return d


def _state_dir(cfg=None) -> Path:
    """Bridge state directory. The default path is DELIBERATELY shared.

    Two instances over one directory are guarded by the single-instance lock
    (`compat.single_instance_lock`, see `AttachBridge._instance_lock`): the second exits with a
    clear message pointing at `AGENT2TELEGRAM_STATE` instead of silently sharing the offset.
    An explicit `AGENT2TELEGRAM_STATE` takes precedence — a keepalive sets it per bridge.

    Why NOT a per-bot default path (a rejected change): it would help only someone running
    without the env var AND operating two DIFFERENT bots — nobody does that (a fleet sets the
    env, a single install runs one bot) — while breaking upgrades of a running install: a fresh
    empty per-bot directory means the offset resets → the whole Telegram backlog replays and the
    durable queue in the old path is orphaned. A safe old→new migration is impossible from the
    data (you can't tell which bot a shared directory belonged to). New wizard configs carry
    an explicit state_dir, while older configs keep their exact previous path and offset.
    """
    env = os.environ.get("AGENT2TELEGRAM_STATE")
    if env:
        return Path(env).expanduser()
    if cfg is not None and getattr(cfg, 'state_dir', ''):
        return Path(cfg.state_dir).expanduser()
    return Path.home() / ".local" / "state" / "agent2telegram"


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV, DEFAULT_PATH)).expanduser()


def load(path: Path | None = None) -> Config:
    p = path or config_path()
    if not p.exists():
        raise ConfigError(
            f"No config at {p}. Run 'agent2telegram setup' first."
        )
    try:
        raw = json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Config at {p} is not valid JSON: {e}") from e
    secret_sources = {}
    file_secret_values = {}
    for name, env_name in _SECRET_ENVS.items():
        if name in raw:
            secret_sources[name] = _SOURCE_FILE
            file_secret_values[name] = raw[name]
        if env_value := os.environ.get(env_name):
            # Runtime may use env-provided secrets, but save() must not spill them to disk.
            secret_sources[name] = _SOURCE_ENV
            raw[name] = env_value
    known = {f for f in Config.__dataclass_fields__}
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    _set_secret_metadata(cfg, secret_sources, file_secret_values)
    cfg.validate()
    return cfg


def _set_secret_metadata(cfg: Config, sources: dict, file_values: dict) -> None:
    cfg._secret_sources = dict(sources)
    cfg._secret_file_values = dict(file_values)


def mark_secret_from_file(cfg: Config, name: str) -> None:
    """Mark a user-entered secret as file-backed so save() may persist it."""
    if name not in _SECRET_ENVS:
        raise ValueError(f"unknown secret field: {name}")
    sources = dict(getattr(cfg, "_secret_sources", {}))
    file_values = dict(getattr(cfg, "_secret_file_values", {}))
    sources[name] = _SOURCE_FILE
    file_values[name] = getattr(cfg, name)
    _set_secret_metadata(cfg, sources, file_values)


def _as_persisted_dict(cfg: Config) -> dict:
    data = asdict(cfg)
    sources = getattr(cfg, "_secret_sources", {})
    file_values = getattr(cfg, "_secret_file_values", {})
    for name in _SECRET_ENVS:
        if sources.get(name) != _SOURCE_ENV:
            continue
        if name in file_values:
            data[name] = file_values[name]
        else:
            data.pop(name, None)
    return data


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
    tmp.write_text(json.dumps(_as_persisted_dict(cfg), indent=2, ensure_ascii=False), "utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)   # 0600
    os.replace(tmp, p)
    return p
