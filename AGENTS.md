# AGENTS.md — install playbook for an AI agent

> This file is written **for an AI coding agent** (Codex, Claude Code…)
> tasked with installing Agent2Telegram on a fresh machine for a non‑technical user.
> Follow it as a recipe. Do **not** improvise around the security rules.

## Your goal
Get the Agent2Telegram bridge running so the user can talk to their agent from Telegram,
then **verify it actually works** and hand the user a short summary.

## Hard rules (do not break)
1. **Never weaken security.** The bridge lets a Telegram user run commands on this machine.
   The `allowed_user_ids` list must contain **only** the owner's real Telegram user id.
   Never set it to empty "to make it work", never add unknown ids, never disable the check.
2. **Never print, paste, or commit the bot token.** It is a secret. It lives only in
   `~/.config/agent2telegram/config.json` (mode 0600) or the `TELEGRAM_BOT_TOKEN` env var.
3. **Do not use `shell=True`** or interpolate user input into shell strings anywhere.
4. If a step needs a secret or a decision only the user can make (token, which agent,
   their Telegram id), **ask the user** — don't guess.

## Prerequisites to check first
Run these and fix what's missing before installing:
- `python3 --version` → must be **3.10+**. If missing, install Python 3.
- `git --version` → needed to fetch the repo. Install if missing.
- The agent CLI to bridge must be installed **and logged in**:
  - Claude Code: `claude --version` and a prior `claude` login.
  - Codex: `codex --version` and a prior `codex` login.
  - Test it works headlessly, e.g. `claude -p "say hi"` or `codex exec "say hi"`.
  If the agent CLI isn't installed/authenticated, **stop and ask the user to do that**
  (it requires an interactive login you can't complete for them).

## Install steps
```bash
git clone https://github.com/petrludwig-collab/Agent2Telegram.git
cd Agent2Telegram
python3 -m pip install --user .          # or: --user --break-system-packages
```

## Configure
Prefer the interactive wizard if the user is present:
```bash
python3 -m agent2telegram setup
```
If you must configure non‑interactively, ask the user for (a) which agent and (b) the bot
token and their Telegram user id, then write `~/.config/agent2telegram/config.json`:
```json
{ "agent": "codex", "token": "<ASK THE USER>", "allowed_user_ids": [<ASK THE USER>] }
```
…and `chmod 600` it. To get the user's id: have them message the bot, then read it from
`getUpdates`, or ask them to send `/id` to the bot once it's running.

**Sending a file TO the user:** if your agent has its own file-sending tool (Claude Code's
`SendUserFile`), use it — the bridge picks it up from the transcript automatically. Otherwise put
`[tg-file] /path/to/file` on its own line in the reply. Either way the path must be inside the
allowed outbox (`~/.local/state/agent2telegram/outbox` by default); anything else is refused with
a visible message, never dropped silently.

**Attachments & voice:** images, videos and files work out of the box (they're downloaded and handed
to the agent). Voice transcription is optional — ask the user if they want it; if yes, add
their **own** ElevenLabs key as `elevenlabs_api_key` (or `ELEVENLABS_API_KEY`). Never use a
shared/hardcoded key.

## Verify (do not skip)
```bash
python3 -m agent2telegram doctor
```
Expected: config prints (token redacted), `agent '<name>': ✓ binary found`,
`telegram: ✓ @<botname>`, and a non‑empty `allowed_user_ids`. Fix anything that isn't ✓.

Then start it and confirm a real round‑trip:
```bash
python3 -m agent2telegram run        # leave running; ask the user to message the bot
```
Ask the user to send the bot a message and confirm they get a reply. Only then is it done.

## Make it persistent
```bash
python3 -m agent2telegram service     # prints a systemd/launchd unit + install hints
```
Install it per the printed hints so the bridge starts on boot and restarts on crash.

## Common failures → fixes
| Symptom | Cause | Fix |
|---|---|---|
| `doctor`: `binary NOT found` | agent CLI not on PATH | install it / fix PATH; re‑login |
| `telegram: ✗ ... Unauthorized` | wrong/typo'd token | re‑enter the token from @BotFather |
| Bot replies "not authorized" to the owner | wrong id in allow‑list | put the id from `/id` into `allowed_user_ids` |
| `Agent error: ... timed out` | agent run > `agent_timeout` | raise `agent_timeout` in config |
| Agent's flags differ from defaults | newer/older CLI | set a custom `command`/`continue_command` (use `{prompt}`) |
| `pip install` blocked (PEP 668) | system‑managed Python | add `--break-system-packages` or use a venv |

## When done, tell the user
- which agent is connected, the bot's @username,
- that **only their** Telegram account can use it,
- how to check status (`/status`) and toggle spoken replies (`/voice`),
- that `/help` in the chat lists everything the bridge itself handles.
