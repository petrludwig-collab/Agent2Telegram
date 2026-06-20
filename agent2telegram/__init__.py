"""Agent2Telegram — connect a coding agent (Claude Code or Codex) to Telegram.

A small, dependency-free bridge: it long-polls Telegram for messages from authorized
users, hands each message to the configured agent CLI, and streams the reply back.

Design goals (in priority order):
  1. Robustness — degrade gracefully, never crash the main loop on a single bad message.
  2. Security — only allow-listed Telegram users can drive the agent (it runs code!).
  3. Zero install friction — the core uses only the Python standard library.
"""

__version__ = "1.1.0"
