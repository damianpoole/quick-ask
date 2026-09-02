# Marketplace revalidation notes

## Repository

https://github.com/damianpoole/quick-ask

## Manual prerequisites

Quick Ask intentionally leaves account and desktop configuration to the user.
The user must install and authenticate Codex or Claude Code, select it with
`omarchy default agent <name>`, and add the documented Hyprland binding. Plugin
installation does not edit agent or Hyprland configuration.

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
- Codex final output uses an anonymous inherited Linux `memfd`. The plugin has
  no prompt, answer, temporary, or log paths and never falls back to `/tmp`.
- Plugin-owned settings were removed. Agent authentication, model, and reasoning
  settings remain owned by the selected CLI. Quick Ask writes no configuration
  or state files.
- QML uses bounded chunk parsers instead of `StdioCollector`. Non-Markdown data
  is explicit plain text; assistant Markdown has raw HTML and images disabled.
  Only credential-free HTTP(S) links can reach a separate confirmation UI, and
  `Qt.openUrlExternally` is called only after confirmation.
- Automated regression tests cover `/proc` command-line privacy, stdin-only
  adapters, schema and byte limits, noisy producers, non-reading producers,
  deadlines, and descendant cleanup.

The detailed limits and trust boundaries are documented in `SECURITY.md`.

## Persistent data

None. The bounded conversation exists only in memory and is discarded when the
user closes Quick Ask, starts a new conversation, or the plugin unloads.
Quick Ask does not persist prompts, answers, transcripts, settings, or logs.
