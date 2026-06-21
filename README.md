# Agent2Telegram

[![CI](https://github.com/petrludwig-collab/Agent2Telegram/actions/workflows/ci.yml/badge.svg)](https://github.com/petrludwig-collab/Agent2Telegram/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

Talk to your coding agent — **Claude Code** or **Codex** — from **Telegram**.

Agent2Telegram is a tiny, dependency‑free bridge. It long‑polls Telegram for messages from
**you** (and only you), hands each one to the agent CLI of your choice, and sends the reply
back. No public IP, no webhook, no cloud — it runs on your own machine, behind your router.

```
Telegram  ⇄  Agent2Telegram  ⇄  claude / codex
```

**What you can send:** plain text, **images** and **files** (downloaded and handed to the
agent), and **voice messages** — transcribed automatically when you add your own ElevenLabs
key (see *Voice messages* below).

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

### What the wizard asks (3 steps)
1. **Provider** — it auto-detects which agents are installed (Claude Code / Codex) and you pick one.
2. **Session** — attach to an existing **tmux** session or create a fresh one (the wizard
   launches the chosen agent in it for you).
3. **Telegram** — paste the bot token from [@BotFather](https://t.me/BotFather); the wizard
   verifies it live, then captures your user id from the first message you send the bot and wires
   everything up. It can start the bridge for you on the spot.

Codex needs no extra setup — its rollout log records turn boundaries. For Claude Code the wizard
also registers the end-of-turn **Stop hook** automatically.

You get the same live UX both ways: progress messages kept, a one-line italic status bubble for
tool calls that trails at the bottom and clears at the end, and a `typing…` indicator that stays
lit for the whole turn.

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

The bridge shells out to these tools, so whatever they can do in your terminal, they can do
from Telegram.

---

## Commands (in chat)

| Command | What it does |
|---|---|
| *(any text)* | sent straight to the live agent |
| `/start`, `/help` | short intro + what you can send |
| `/status` | which agent + tmux session you're connected to (and whether voice is on) |
| `/setkey <key>` | enable voice transcription with your ElevenLabs key — your message is deleted right after so the key isn't left in the chat |
| `/id` | show your user / chat id (handy for the allow‑list) |

Anything that isn't one of these (including other `/commands`) is passed through to the agent.

---

## Self-test (no bot needed)

Run the whole attach experience against a *real* agent with a fake Telegram — it needs no bot
token and touches nothing live. It spins up a throwaway tmux session, launches the agent, and
checks text round-trip, a ❤️ reaction, a multi-step task (tool-call status bubble), and voice
transcription:

```bash
python3 -m agent2telegram selftest --agent codex
python3 -m agent2telegram selftest --agent claude-code
```

Both report `6/6 checks passed` on a working setup.

---

## Uninstall

One line — the exact mirror of the install one‑liner:

```bash
curl -fsSL https://raw.githubusercontent.com/petrludwig-collab/Agent2Telegram/main/uninstall.sh | bash
```

That stops any running bridge and removes **everything**: the config (token included), state,
the source clone, the launcher, the Claude Code Stop hook, and the pip package — no leftovers,
no manual steps. (If you prefer, the equivalent from a clone is `python3 -m agent2telegram
uninstall`; add `--yes` to skip the confirmation prompt.)

---

## Proactive notifications (cron / background jobs)

The bridge forwards the agent's replies **during a Telegram‑originated turn**. A cron job, a
background task, or a long build can't reach you that way — there's no turn to reply in. For
those, push a message to yourself with:

```bash
python3 -m agent2telegram notify "build finished ✅"
echo "deploy done" | python3 -m agent2telegram notify          # or pipe it on stdin
python3 -m agent2telegram notify --config ~/.config/agent2telegram/codex-config.json "…"
```

It sends to the configured owner via the bot (Markdown rendered, same as chat replies). Handy
in a `cron` line or at the end of a script so the agent tells you when something finishes.

---

## Voice messages (optional)

Voice notes are transcribed with **ElevenLabs Scribe** (`scribe_v1`) and the transcript is
sent to the agent. It's **off by default** and uses **your own** API key — there is no shared
key and no extra Python dependency.

Enable it by setting your key (get one at <https://elevenlabs.io>):
```bash
export ELEVENLABS_API_KEY="sk_..."     # or put "elevenlabs_api_key" in config.json
```
Without a key, voice messages get a short "not enabled" notice. Images and files work with no
extra setup.

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
- **The wizard launches the agent with its "run without asking" flag** (Codex
  `--dangerously-bypass-approvals-and-sandbox`, Claude Code `--dangerously-skip-permissions`) —
  the bridge drives it unattended, so it can't stop to ask you to approve each command. That
  makes the allow‑list and a least‑privileged user the real safeguards. If you'd rather approve
  every action by hand, start the agent yourself without the flag and point the wizard at that
  tmux session instead of creating a new one.
- Consider running the agent under a dedicated, least‑privileged user.

---

## Development

```bash
python3 -m unittest discover -s tests -v   # zero-dependency test suite
```

## License

MIT — see [LICENSE](LICENSE).
