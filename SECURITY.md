# Security Policy

Agent2Telegram lets a Telegram user run a coding agent — which can execute commands — on
the host machine. Security is therefore a first-class concern.

## Threat model & built-in protections
- **Authorization:** only Telegram user ids in `allowed_user_ids` may drive the agent.
  Everyone else is refused. Keep this list to the device owner(s) only.
- **No shell injection:** messages are passed to the agent as a single `argv` element;
  the bridge never uses `shell=True`.
- **Secret handling:** the bot token lives only in a `0600` config file (its directory is
  `0700`) or the `TELEGRAM_BOT_TOKEN` env var. It is never logged and is redacted in
  `doctor` / `/status`.
- **No inbound exposure:** the bridge uses Telegram long polling, so no port needs to be
  opened to the internet.

## Hardening recommendations
- Run the bridge under a dedicated, least-privileged OS user.
- Keep the connected agent CLI updated and logged in only to the intended account.
- Review `allowed_user_ids` periodically.

## Reporting a vulnerability
Please open a private security advisory on GitHub (Security → Report a vulnerability) or
contact the maintainer rather than filing a public issue. We aim to respond within a few days.
