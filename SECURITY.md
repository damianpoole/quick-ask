# Security model

Quick Ask runs inside the unsandboxed, long-lived Omarchy shell. Its external
process bridge is therefore intentionally narrow and fail-closed.

## Data flow

QML sends one JSON request to `quick_ask_helper.py` through stdin. The helper
validates its exact schema and UTF-8 byte length, detects the selected agent,
and starts only a supported adapter. It sends the prompt to that adapter through
stdin. Prompt content is never placed in argv, an environment variable, or a
filesystem path.

Codex writes its final message to a Linux `memfd` inherited from the helper.
Claude returns its answer on bounded stdout. Neither path creates a named file.
Diagnostics are bounded in memory and are not persisted.

## Limits

| Boundary | Limit |
|---|---:|
| QML user message | 4,000 characters |
| Conversation context sent to an agent | 24,000 characters plus latest message |
| Helper request frame | 256 KiB |
| UTF-8 prompt | 128 KiB |
| Agent answer | 256 KiB raw / 131,072 displayed characters |
| Agent stdout | 256 KiB |
| Agent stderr | 16 KiB |
| Agent detection stdout/stderr | 128 bytes / 2 KiB |
| Hyprland bindings file | 1 MiB |
| Binding-helper command stdout/stderr | 256 KiB / 16 KiB |
| Binding-helper command deadline | 10 seconds |
| Displayed error | 4,096 characters |
| External URL | 2,048 characters |
| Retained in-memory transcript | 192,000 characters |
| Complete request deadline | 120 seconds |

The helper counts subprocess output before decoding it. Timeout, byte overflow,
cancellation, and termination all stop and reap the agent process group.

The optional binding helper applies equivalent raw subprocess-output limits and
deadlines to `omarchy` and `hyprctl` commands before using their results.

## Supported agents

Only Codex and Claude Code currently have enabled adapters. Both have verified
stdin prompt modes and non-interactive, restricted-permission invocation. A
configured agent without a reviewed private-input adapter is rejected; there is
no argv fallback.

## Rendering and external actions

Only assistant answers use Markdown. Control characters are removed, raw HTML
is escaped, and Markdown images are disabled. All other externally derived text
uses `Text.PlainText`.

Only absolute HTTP(S) links without whitespace, control characters, or embedded
credentials can reach the confirmation UI. Quick Ask displays the authority and
full URL as plain text and calls `Qt.openUrlExternally` only after a separate
user confirmation.

## Optional Hyprland binding helper

Plugin installation and enablement never execute `scripts/bindings.py`. A user
must invoke it explicitly to add, change, inspect, or remove Quick Ask's marked
block in `~/.config/hypr/bindings.lua`.

The helper opens every parent directory and the target with no-follow flags,
requires a user-owned non-writable parent and regular target, caps the file at
1 MiB, detects concurrent replacement, and writes through an exclusive random
temporary file relative to the held parent descriptor. It refuses to steal an
occupied shortcut. Before each actual change it creates an exclusive 0600
random backup beside the target. It validates the existing and updated configs
with `hyprctl reload` and `hyprctl configerrors`; a failed post-write validation
causes a descriptor-relative rollback to the original bytes.

## Persistent state

Quick Ask does not persist prompts, answers, transcripts, agent output, logs, or
settings. Agent authentication and defaults remain owned by the selected CLI.
The optional helper persists only the user-requested marked binding and one
private backup for each real change. Closing Quick Ask or starting a new
conversation clears the in-memory transcript.

## Reporting

Please report suspected vulnerabilities privately to the repository owner
before opening a public issue when disclosure could expose users.
