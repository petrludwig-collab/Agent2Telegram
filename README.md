# Agent2Telegram

[![CI](https://github.com/petrludwig-collab/Agent2Telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/petrludwig-collab/Agent2Telegram/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

Talk to your coding agent — **Claude Code**, **Codex**, or **Antigravity** — from **Telegram**.

Agent2Telegram is a tiny, dependency‑free bridge. It long‑polls Telegram for messages from
**you** (and only you), hands each one to the agent CLI of your choice, and sends the reply
back. No public IP, no webhook, no cloud — it runs on your own machine, behind your router.

```
Telegram  ⇄  Agent2Telegram  ⇄  claude / codex / antigravity
```

---

## Why it’s built this way

- **Robust by default** — one bad message never crashes the loop; network errors, Telegram
  flood‑control (`429`), 4096‑char limits and Markdown parse failures are all handled.
- **Secure** — the agent can run code on your machine, so only **allow‑listed Telegram
  users** can drive it. Everyone else is politely refused.
- **Zero install friction** — the core uses **only the Python standard library**. Nothing to
  `pip install` for it to work, which means far fewer “it doesn’t run on my machine” moments.
- **Works behind NAT** — long polling, so no port‑forwarding or domain needed.

---

## Quick start

```bash
# 1) Get the code
git clone https://github.com/petrludwig-collab/Agent2Telegram.git
cd Agent2Telegram

# 2) Run the setup wizard (pick agent → paste token → authorize yourself)
python3 -m agent2telegram setup

# 3) Start the bridge
python3 -m agent2telegram run
```

…or the one‑liner:

```bash
curl -fsSL https://raw.githubusercontent.com/petrludwig-collab/Agent2Telegram/main/install.sh | bash
```

### What the wizard asks
1. **Which agent** — Claude Code, Codex, Antigravity, or a generic CLI. It checks whether the
   binary is on your `PATH`.
2. **Telegram bot token** — create a bot with [@BotFather](https://t.me/BotFather) and paste
   the token. The wizard verifies it live.
3. **Authorize yourself** — send your bot any message; the wizard captures your user id and
   adds it to the allow‑list.

---

## Install with an agent (easiest for beginners, fresh server)

If you already have **Codex** (or Claude Code) installed and logged in, you can let it do
the whole install and fix any environment hiccups itself. The repo ships an
[`AGENTS.md`](AGENTS.md) playbook the agent follows step by step.

Three steps:
1. **Install the agent CLI and log in** (the one genuinely manual part on a clean machine):
   - Codex — <https://github.com/openai/codex>, then run `codex` once to sign in.
   - or Claude Code — <https://docs.claude.com/claude-code>, then run `claude` once.
2. **Have a Telegram bot token ready** (create a bot with [@BotFather](https://t.me/BotFather)).
3. **Paste this prompt** into your agent:

   > Install **Agent2Telegram** from `https://github.com/petrludwig-collab/Agent2Telegram`
   > by following its `AGENTS.md` exactly. Connect it to **me on Telegram**. Ask me for the
   > bot token and my Telegram user id when you need them. Do not weaken the security rules.
   > When finished, verify with `python3 -m agent2telegram doctor` and confirm I get a reply
   > from the bot.

The agent reads `AGENTS.md`, checks prerequisites, installs, configures, verifies with
`doctor`, and sets up auto‑start. Because the package is dependency‑free and self‑diagnosing,
the agent has an easy, checkable job — and can repair the rare clean‑server quirk on its own.

> Prefer a deterministic install with no agent? Use the **Quick start** above or the one‑liner.

## Prerequisites

- **Python 3.10+**
- The agent you want to connect, **installed and logged in**:
  - Claude Code — <https://docs.claude.com/claude-code> (run `claude` once to sign in)
  - Codex — <https://github.com/openai/codex> (run `codex` once to sign in)
  - Antigravity — Google’s agent CLI (set the exact command in config; see below)

The bridge shells out to these tools, so whatever they can do in your terminal, they can do
from Telegram.

---

## Commands (in chat)

| Command | What it does |
|---|---|
| *(any text)* | sent to the agent as a prompt |
| `/reset` | start a fresh conversation |
| `/id` | show your user / chat id (handy for the allow‑list) |
| `/status` | bridge + agent status |
| `/help` | help |

---

## Configuration

Stored at `~/.config/agent2telegram/config.json` (mode `0600`). The token may instead be
provided via the `TELEGRAM_BOT_TOKEN` environment variable to keep it out of the file.

```json
{
  "agent": "claude-code",
  "token": "123456:ABC...",
  "allowed_user_ids": [123456789],
  "agent_timeout": 600,
  "command": null,
  "continue_command": null
}
```

**Custom command** — the default invocation for each agent is overridable, because these CLIs
evolve. Use `{prompt}` where the message should go:

```json
{
  "agent": "codex",
  "command": ["codex", "exec", "--model", "gpt-5.5", "{prompt}"],
  "continue_command": ["codex", "exec", "--last", "{prompt}"]
}
```

Run `python3 -m agent2telegram doctor` to validate everything (config, token, agent binary).

---

## Run it forever (boot + auto‑restart)

```bash
# Prints a systemd unit (Linux) or launchd plist (macOS) to stdout, hints to stderr:
python3 -m agent2telegram service
```

Follow the printed steps. On Linux you’ll typically:

```bash
mkdir -p ~/.config/systemd/user
python3 -m agent2telegram service > ~/.config/systemd/user/agent2telegram.service
systemctl --user enable --now agent2telegram
loginctl enable-linger "$USER"
```

---

## Docker

The image is tiny, but the **agent CLI and its login are not baked in** (auth must stay out of
images). Mount an authenticated agent and your config:

```bash
docker build -t agent2telegram .
docker run -d --name agent2telegram \
  -v "$HOME/.config/agent2telegram:/data" \
  -v "$HOME/.claude:/root/.claude" \      # example: bring your Claude Code login
  agent2telegram
```

---

## Security notes

- **Allow‑list is the only thing between a stranger and code execution on your box.** Keep it
  tight. An unauthorized user gets a refusal and their own id (so you can add them on purpose).
- The bot token is a secret: the config file is `0600`, the token is never logged, and `/status`
  / `doctor` always print it redacted.
- Prompts are passed to the agent as a single `argv` element (never through a shell), so a
  message can’t inject shell syntax.
- Consider running the agent under a dedicated, least‑privileged user.

---

## Development

```bash
python3 -m unittest discover -s tests -v   # zero-dependency test suite
```

## License

MIT — see [LICENSE](LICENSE).
