# Changelog

## Unreleased

- Videos, video notes and GIFs sent to the bot reach the agent as files, like documents (#12).
  A gallery video or a screen recording arrives as `video`, not `document`, and was dropped
  without any reply. The 20 MB Bot API limit and its message apply to them too. Stickers,
  locations, contacts, polls and dice now get a short "can't pass this on" notice instead of
  silence.
- Attach mode defaults to `group_interim: false`: group destinations receive the final
  answer and allowed attachments, with narration, tool/status bubbles, background progress,
  typing and queued-message acknowledgments suppressed. Private-chat progress is unchanged;
  `group_interim: true` restores group progress. Group-marker routing follows the same policy.
- Turn-end recovery preserves withheld final answers without promoting explicit commentary
  or narration followed by a tool call. Inactivity alone cannot publish unclassified group text.
- `notify --to-group [GROUP_ID]` sends text and outbox files to an explicitly allowed group.
  The ID may be omitted when exactly one group is configured; ambiguous or unauthorized
  destinations are refused before sending. Routing logs record the destination ID only.

## 1.2.6 – 2026-10-21

- Bridges restart on macOS also when `launchctl print` cannot inspect the GUI domain (exit code 125 over SSH or in tmux). Without the launchd plist the service cannot be running the bridge, so the bridges restart directly instead of aborting.
- `set-elevenlabs` warns loudly when bridges were not restarted. A quiet "restart skipped" line left them on the old key and voice notes failed with HTTP 401.

## 1.2.5 – 2026-10-21

- Claude turns end correctly after Claude Code continues in a new conversation. The Stop hook matched only the session id stored at connect time; when it went stale, no turn ever ended and every next message got the "Got your message" busy notice. The hook now falls back to the tmux session that runs the calling Claude process.

## 1.2.4 – 2026-10-21

- `set-elevenlabs` restarts the running bridges again, so voice messages work right after the key is set. It passed raw process tuples to the restart step, which failed with "restart skipped".

## 1.2.3 – 2026-10-19

- Use launcher commands in setup/service hints; updates without a valid configuration guide users to setup.
- Mask all typed secret characters and redact Telegram tokens from response errors, retries and tracebacks. Explain rejected tokens with a BotFather recovery hint.
- Show only sessions matching the selected provider in the setup wizard.

## 1.2.2 – 2026-10-19

- Setup and connect optionally register each started bridge with Agents Monitoring
  0.3.2+ through its public CLI. Config, interpreter, import path and state are
  preserved; repeated registration updates the same entry and never copies tokens.
  Missing monitoring is informational; registration failures leave the bridge running
  and print a warning. A process that exits during startup is not registered.
- Keep provider menu numbers stable, require an explicit choice, find common user-bin
  installations, and confirm the absolute CLI before creating a new tmux session.
- Refresh checked-hash bytecode during installation so same-second upgrades cannot
  import stale code even when optional pip installation is skipped.

## 1.2.1 – 2026-10-18

- Claude attach startup and setup share atomic, idempotent Stop-hook registration, honour
  `CLAUDE_CONFIG_DIR`, and preserve recognised custom marker hooks. Doctor checks hook
  coverage; `claude-hook --config ...` repairs it without starting another bridge.
  Resume the same Claude session to load a newly registered hook. Unconfirmed hooks no
  longer cause stale busy acknowledgements/typing after 90 seconds of transcript silence;
  late-answer delivery and cross-chat ownership remain unchanged.
- Default sender labels, comments, documentation, and operational logs are English.
  Explicit `language: "cs"` retains the existing Czech labels and notices.
- Public-language regression checks also work in source archives without Git metadata.
- Installer retries preserve existing Git checkouts on failure. Archive updates recognise
  older unmarked installations and keep the previous source in a neighbouring backup.
  Unknown directories and symlink targets are refused. Added `--no-setup` and `--no-pip`.
- Setup stops cleanly at end of input. New wizard configurations record an explicit
  state directory, so multiple bots have separate offsets and queues. Existing configs
  keep their previous state paths; `AGENT2TELEGRAM_STATE` still takes precedence.
- Update disables Git credential prompts, checks for local changes, and retries failed pulls.
  `update --no-restart` supports supervisor-managed bridges. Automatic restarts preserve
  each process's environment on Linux. Restart planning happens before pull; macOS supports
  PID-matched launchd kickstart and standalone file-backed config/state. Unknown environments
  keep running, with a successful source update and an explicit manual-restart warning.
  Restore missing bot username backfill after pull, before restart. Agent tmux sessions are
  never restarted.
- Doctor fails when no user is authorized. Uninstall limits process selection to the
  current user's bridge processes and leaves tmux agents running. Service exports include
  the source import path and an explicitly selected state directory.
- Package metadata and the CLI report the same version.
- Prevent concurrent inbox recovery and live polling from injecting the same durable
  message twice, including the case where recovery completes before the live submit.
