"""Adapter registry. New agents drop a module here and register their class."""
from __future__ import annotations

from .base import Adapter, AdapterError
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .generic import GenericAdapter

#: Order matters: this is the order shown in the setup wizard.
REGISTRY: dict[str, type[Adapter]] = {
    a.name: a for a in (ClaudeCodeAdapter, CodexAdapter, GenericAdapter)
}


def available() -> list[type[Adapter]]:
    return list(REGISTRY.values())


def build(cfg) -> Adapter:
    """Instantiate the adapter named in *cfg*, applying any command overrides."""
    cls = REGISTRY.get(cfg.agent)
    if cls is None:
        raise AdapterError(
            f"Unknown agent '{cfg.agent}'. Choose one of: {', '.join(REGISTRY)}."
        )
    return cls(command=cfg.command, continue_command=cfg.continue_command, timeout=cfg.agent_timeout)


__all__ = ["Adapter", "AdapterError", "REGISTRY", "available", "build"]
