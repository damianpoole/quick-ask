# Marketplace revalidation notes

## Repository

https://github.com/damianpoole/quick-ask

## Manual prerequisites

Quick Ask intentionally leaves account and desktop configuration to the user.
The user must install and authenticate Codex or Claude Code, select it with
`omarchy default agent <name>`, and explicitly run the optional binding helper
or add the documented Hyprland binding manually. Plugin installation and
enablement do not edit agent or Hyprland configuration or execute an install
hook.

External dependencies are Omarchy Quattro, Python 3, and one supported agent
CLI. No elevated privileges, install hooks, services, or remote build steps are
used.

## Security-review remediation

- QML launches a fixed Python helper command and sends the bounded prompt as one
  JSON record through stdin. The helper sends it to Codex or Claude through
  stdin; no prompt or conversation content is placed in argv or the environment.
- The helper enforces raw stdout/stderr byte limits and a monotonic 120-second
  total deadline. It owns a separate agent process group and terminates and
  reaps that tree on timeout, overflow, cancellation, signal, or abnormal exit.
- Codex final output uses an anonymous bounded pipe that is drained while the
  process runs, so its internal files are not subject to the answer limit. Each
  agent runs from a private empty temporary directory that is removed after the
  request; no prompt, answer, session, or log file is created.
- Restricted agent configuration is the default. Codex ignores user config and
  exec-policy rules; Claude uses restricted mode with tools, MCP, and session
  persistence disabled. A visible, explicit environment opt-in enables full
  agent configuration while retaining the filesystem and permission limits.
- Agent processes receive an adapter-specific environment allowlist rather than
  every variable held by the long-lived shell.
- The optional, user-invoked binding helper refuses occupied keys and unsafe
  parent/target paths, caps file and subprocess reads, makes exclusive 0600
  random backups, uses a verified atomic exchange that preserves concurrent
  edits, and rolls back unless Hyprland reload and config-error validation both
  succeed.
- QML uses bounded chunk parsers instead of `StdioCollector`. Non-Markdown data
  is explicit plain text; assistant Markdown has raw HTML and images disabled.
  Only credential-free HTTP(S) links can reach a separate confirmation UI, and
  `Qt.openUrlExternally` is called only after confirmation.
- Automated regression tests cover `/proc` command-line privacy, stdin-only
  adapters, schema and byte limits, noisy producers, non-reading producers,
  deadlines, descendant cleanup, restricted/full configuration modes,
  environment filtering, and concurrent binding edits.

The detailed limits and trust boundaries are documented in `SECURITY.md`.

## Persistent data

The bounded conversation exists only in memory and is discarded when the user
closes Quick Ask, starts a new conversation, or the plugin unloads. Quick Ask
does not persist prompts, answers, transcripts, settings, or logs.

If explicitly invoked, `scripts/bindings.py` persists one clearly marked
binding in the user's Hyprland configuration and creates one private random
backup beside `bindings.lua` per real change. The removal command removes only
the owned block and also creates a recovery backup.
