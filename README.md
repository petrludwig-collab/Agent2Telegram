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

### Optional monitoring

If [Agents Monitoring](https://github.com/petrludwig-collab/AgentsMonitoring)
0.3.2+ is installed, `setup` and `connect` automatically register each bridge after
its process starts. Each config has an independent daemon/health row and restart
command; repeated registration updates the same entry. The registration preserves
the Python interpreter, `PYTHONPATH` and state directory without copying bot tokens
or changing Agents Monitoring's config directly. Existing stopped/enabled settings
in the monitor are preserved. Its keepalive must be enabled for automatic recovery.

Without the monitor, setup prints one informational sentence and continues normally.
An old/unreachable monitor produces a warning; upgrade it and run `agentsmon add`
to register an existing bridge. The bridge itself keeps running. Newly created
sessions require an explicit provider choice and confirmation; missing CLIs never
silently select another provider.

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

Manual Claude attach configurations are covered too: `run` checks and registers the Stop
hook in the active user settings (`CLAUDE_CONFIG_DIR/settings.json`, or
`~/.claude/settings.json`). Registration is atomic and idempotent, preserves other settings,
and recognises custom Python hooks that write this bridge's `turn_end` marker using a
literal path. Invalid/unreadable settings and disabled hooks produce a warning and are
left untouched. Project/managed policy overrides and dynamically computed custom hook
paths still need manual inspection.

`doctor --config /path/to/config.json` reports a missing hook. To repair it without starting
a second bridge, use `python3 -m agent2telegram claude-hook --config /path/to/config.json`,
then **resume the same Claude session** to load it. Registration never restarts your agent.
Until the bridge observes a Stop marker, 90 seconds without transcript activity suppresses
busy acknowledgements and typing. This only changes the indicator: the reply window and
cross-chat ownership guard stay in place, so a late answer retains its original destination.

You get the same live UX both ways: progress messages kept, a one-line italic status bubble for
tool calls that trails at the bottom and clears at the end, and a `typing…` indicator that stays
lit for the whole turn.

### Connect more agents (one bot each)

Running several agents (e.g. one per tmux session)? Give each its own bot in one step:

```bash
python3 -m agent2telegram connect            # pick a session → pick provider → paste token
```

`connect` writes a **separate** config (`~/.config/agent2telegram/<name>.json`), starts that
bridge alongside the others, and — for Claude Code — pins the session id so the Stop hook routes
each turn-end to the right bridge. Your existing bots are left untouched. Name it explicitly with
`--name`.

### Update

```bash
python3 -m agent2telegram update             # git pull + restart every running bridge on the new code
python3 -m agent2telegram update --no-restart # code only; restart with your existing supervisor
```

Pulls the latest code into `~/.agent2telegram-src` and restarts each running bridge, preserving
its own `--config` — no re-setup, no re-entering tokens.
The restart plan is checked **before** pulling changes. Linux restarts preserve each process's
original environment. On macOS, a bridge owned by the `com.agent2telegram.bridge` launchd
service restarts through `launchctl kickstart -k gui/<uid>/com.agent2telegram.bridge` (the PID
must match). A standalone bridge can also restart from an absolute `--config` path when the
file contains its token and explicit `state_dir`; unrelated bot overrides are not inherited.

When the original environment cannot be recovered (for example an older, environment-only
configuration on macOS), the source update succeeds with a **WARNING** and exit code 0.
That bridge stays running on its previous code. The warning identifies its PID and prints
a shell-quoted start command requiring the original config/state paths. Restart it with its
original supervisor, or stop the standalone bridge and use that command in its original
environment. Never guess a default state path: it can replay messages. For custom supervisors,
prefer `--no-restart`. Agent/tmux sessions are never restarted.

After a successful pull, missing bot usernames in saved configurations are backfilled with
Telegram `getMe` before restart, including with `--no-restart`; existing usernames are kept.
Local source changes or failed restart preflight stop the update before pulling. A failed pull
preserves the checkout; a failed restart after a successful update is reported as an error.

Source-archive installations update by re-running `install.sh`. Older installations without
an archive marker are recognised at the standard `~/.agent2telegram-src` location; the old
source is kept in a neighbouring `.backup.*` directory, including local files. Config and
state are outside that source tree and remain unchanged. Review/remove old backups yourself.
For unattended installation, use `bash install.sh --no-setup`; `--no-pip` installs only the
stdlib launcher. A source clone is installed from its current checkout; pull updates explicitly.

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
| `/voice` | toggle spoken replies — the agent's answer comes back as a voice note (needs an ElevenLabs key) |
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

Stop any custom keepalive/supervisor first. This stops the current user's bridges and removes
the default config (token included), state, installed source, shell launcher, Claude Stop hook,
and pip package. Agent tmux sessions, other users, custom source/state paths and archive backups
are left untouched. (If you prefer, the equivalent from a clone is `python3 -m agent2telegram
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
python3 -m agent2telegram notify "build finished ✅" --to-group  # exactly one allowed group
python3 -m agent2telegram notify "report ready" --to-group -1001234567890 \
  --file ~/.local/state/agent2telegram/outbox/report.pdf
```

It sends to the configured owner via the bot (Markdown rendered, same as chat replies). Handy
in a `cron` line or at the end of a script so the agent tells you when something finishes.

`--to-group [GROUP_ID]` explicitly selects a group from the chosen config's
`allowed_group_ids`. Without an ID, exactly one allowed group must be configured.
With multiple groups, pass the ID. Missing or unauthorized groups are refused before
any network request; the command never falls back to the owner's DM. Put the message
before a bare `--to-group`, or pipe it on stdin, so it cannot be mistaken for an ID.
`--file` is repeatable and enforces the same outbox allowlist as chat attachments.
Notification routing logs contain the destination ID, without the message text.

---

## Voice messages (optional)

Voice notes are transcribed with **ElevenLabs Scribe** (`scribe_v1`) and the transcript is
sent to the agent. It's **off by default** and uses **your own** API key — there is no shared
key and no extra Python dependency.

Enable it by setting your key (get one at <https://elevenlabs.io>):
```bash
export ELEVENLABS_API_KEY="sk_..."     # or put "elevenlabs_api_key" in config.json
```
Without a key, voice messages get a short "not enabled" notice. Images, videos and files work
with no extra setup (Telegram limits what a bot can download to 20 MB).

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

For groups, optional `"require_mention": true` together with `"bot_username": "your_bot"`
accepts only @mentions, commands addressed to that bot, and replies to its messages.
Other group messages are silently ignored, including receipt acknowledgments. Private chats
are unchanged. The default is `false`; the authorized-user allowlist is always enforced.

In attach mode, `"reply_to_origin_chat": true` sends the answer back to the authorized
message's chat. Set `allowed_group_ids` to the groups that may use the bridge.
`"group_interim": false` is the safe default, including for existing configurations:
group destinations receive the turn's final answer and allowed attachments (`[tg-file]`
or the agent's file-sending tool). Narration, tool/status bubbles, background progress,
typing indicators and queued-message acknowledgments are suppressed in groups.
Private chats keep their existing live progress. Explicitly set `"group_interim": true`
to restore live progress in groups. The policy also covers replies routed by `group_marker`.

Group text is held until Codex reports turn completion, the Claude Stop hook fires, or
a final-answer signal arrives. Explicit commentary and text followed by a tool call
cannot become a final answer through the backstop. If only inactivity ends a turn,
unclassified text stays private and a missing answer is logged; configure a working
Stop hook for Claude. Background jobs should use `notify --to-group` to address a group
explicitly instead of relying on the previous turn's destination.

After injecting a message, attach mode checks the last visible input prompt every 0.2 seconds.
If the message tail remains there for `"enter_verify_timeout": 8.0` seconds, the bridge checks
that the agent is still running, sends Enter once more, and logs `enter_resend session=...`.
If the input is still stuck after another 2 seconds, or verification fails, it logs
`enter_not_submitted session=...` and preserves turn tracking. The timeout must be a positive
finite number. This screen heuristic supports Claude Code and Codex prompt markers; hidden
paste previews and changed TUI layouts can prevent detection. It does not prove transcript receipt.

In attach mode, optional `"inbound_sender_label": true` adds sender/chat context immediately
after `origin_prefix`: `[From: Alex · DM]` or `[From: Alex · group "Team"]`.
The default language is English. With explicit `language: "cs"`, the original Czech
`[Od: ...]` prefix is preserved, including reactions, voice notes, and attachments.
Names come from the current Telegram sender, including voice messages, attachments and reactions;
reply quotes do not replace the sender. Display names (64 characters) and group titles (80)
are sanitized to one line without brackets, quotes or control characters.
These user-controlled descriptions are not proof of identity or approval: authorization still
uses Telegram user IDs. Routing is unchanged, and `notify` defaults to the owner's DM;
`--to-group` explicitly selects an allowed group.
The default is `false`, preserving existing prompts.

Operational notices: `"language": "cs"` translates the busy acknowledgment and the default
startup notice into Czech (`"en"` is the default; this does not configure speech recognition).
`"restart_notice": true` enables one short notice after successful bridge startup checks.
Attach mode sends it to its owner chat; a group adapter can supply its configured group.
One-shot mode sends it to the first authorized owner. `"restart_notice_text"` optionally
overrides the text with a literal single line, maximum 200 characters.
The default is off. A host-state marker enforces a 30-second cooldown across process restarts,
including failed/uncertain sends. The notice is not retried in a background loop; an uncertain
Telegram acknowledgement can mean no visible notice. Keep a distinct state directory for each bot.

New `setup`/`connect` configurations store an absolute `state_dir`; `connect` gives each named
bridge its own directory. For existing configurations, the old shared default remains unchanged
unless you explicitly set `state_dir` or `AGENT2TELEGRAM_STATE` (the environment wins). Preserve
the existing directory and offset when upgrading; do not choose a new empty path for a live bot.

Background progress (attach mode, Claude Code): `"background_progress": true` shows work that
runs outside a Telegram turn. Running sub-agents (Claude Code writes them to
`<session>/subagents/`) get ONE message, edited in place with each sub-agent's description,
elapsed time and latest tool call, and deleted when they finish or go silent. Turns not started
from Telegram (e.g. a background-task notification) get the usual one-line tool status bubble,
deleted at that turn's end. Only one short line per step is sent, never tool output, and nothing
is shown for history replayed at startup. `"background_edit_interval"` (seconds, default 10)
throttles edits; `"background_stale_after"` (default 300) drops progress that went silent. The
labels follow `"language"`. The default is off.

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

## Sending files from the agent

**If your agent has its own file-sending tool, just use it.** Claude Code's `SendUserFile` is
recognised automatically: the bridge sees the call in the transcript and delivers the files. No
marker, no configuration, nothing to remember — it works on the first try.

The harness runs that tool itself, so the bridge never sees a call, only its record in the
transcript. Before this was recognised the file silently vanished: the reply arrived, the
attachment did not, and nothing in the log said so.

There is also an explicit marker, which works with any agent — put a line like this in the reply:

```
[tg-file] /Users/me/.local/state/agent2telegram/outbox/clip.mp4
```

The bridge removes that line from the message, sends the text, then uploads the file.
The API method follows the extension, so video arrives playable, audio as a track and
images as photos; anything else goes as a document. Bots can upload at most 50 MB —
larger files fail immediately with a clear message instead of a long doomed upload.

**The path is checked against an allowlist, and that is a security boundary.** The path
comes from the agent's own reply text, so a wide allowlist would turn a prompt injection
into file exfiltration. By default only `~/.local/state/agent2telegram/outbox` is allowed;
symlinks are resolved *before* the check, so a link inside the outbox pointing at
`~/.ssh/id_rsa` is refused. Add more folders explicitly if you need them:

```json
{ "outbox_dirs": ["/Users/me/renders"] }
```

A refused attachment is always reported back into the chat — silently dropping a file
would be worse than a visible error.

From cron or a background job (where there is no Telegram turn to reply in) use
`notify`, which enforces the same allowlist:

```
python -m agent2telegram notify --file ~/.local/state/agent2telegram/outbox/clip.mp4 \
    "render finished"
```
