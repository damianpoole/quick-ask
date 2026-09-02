# Security model

Quick Ask runs inside the unsandboxed, long-lived Omarchy shell. Its external
process bridge is therefore intentionally narrow and fail-closed.

## Data flow

QML sends one JSON request to `quick_ask_helper.py` through stdin. The helper
validates its exact schema and UTF-8 byte length, detects the selected agent,
and starts only a supported adapter. It sends the prompt to that adapter through
stdin. Prompt content is never placed in argv, an environment variable, or a
filesystem path. Each request runs from a new private temporary directory, which
is removed when the request ends.

Codex writes its final message to an anonymous pipe inherited from the helper.
The helper drains and byte-limits that pipe while Codex runs, without applying
the answer limit to Codex's unrelated internal files. Claude returns its answer
on bounded stdout. Neither path creates a named file. Diagnostics are bounded in
memory and are not persisted.

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

Restricted configuration is the default. Codex runs with `--ignore-user-config`
and `--ignore-rules`; Claude runs with `--restricted`, no built-in or MCP tools,
and no session persistence. Setting `QUICK_ASK_INHERIT_AGENT_CONFIG=1` in the
Omarchy shell explicitly enables user configuration for the selected adapter.
The Codex read-only sandbox, Claude plan permission mode, private working
directory, and session non-persistence remain enforced after that opt-in.

Detection and agent processes receive a small allowlist of runtime, proxy,
certificate, authentication, locale, and XDG environment variables. Unrelated
variables from the long-lived shell are not inherited. Executables are pinned to
the absolute path discovered before launch while preserving multi-call shim
names required by launchers such as mise.

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
1 MiB, and locks the held parent descriptor against another helper invocation.
It replaces the target with a Linux `renameat2(RENAME_EXCHANGE)` operation, then
verifies that the exchanged inode is the exact version previously read. A
concurrent edit is exchanged back and preserved rather than overwritten. It
refuses to steal an occupied shortcut. Before each actual change it creates an
exclusive 0600 random backup beside the target. It validates the existing and
updated configs with `hyprctl reload` and `hyprctl configerrors`; a failed
post-write validation causes a descriptor-relative rollback to the original
bytes.

## Persistent state

Quick Ask does not persist prompts, answers, transcripts, agent output, logs, or
settings. Agent authentication and defaults remain owned by the selected CLI.
The optional helper persists only the user-requested marked binding and one
private backup for each real change. Closing Quick Ask or starting a new
conversation clears the in-memory transcript.

## Reporting

Please report suspected vulnerabilities privately to the repository owner
before opening a public issue when disclosure could expose users.
