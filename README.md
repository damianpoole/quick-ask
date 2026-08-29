# Quick Ask

Quick Ask is a Raycast-style Omarchy overlay for asking short questions without
leaving the current workspace. It uses the agent selected by
`omarchy-default-agent`, renders Markdown answers, and remembers a separate
model preference for each supported agent.

Follow-up messages are conversational even though the underlying agent CLIs
run one request at a time. Quick Ask bundles the recent transcript into the
next prompt, keeping up to 24,000 characters of context. The transcript stays
local to the running Omarchy shell and is not written to disk.

## Install

```bash
omarchy plugin add https://github.com/damianpoole/quick-ask.git --enable
```

Add a Hyprland binding in `~/.config/hypr/bindings.lua`:

```lua
hl.unbind("SUPER + grave")
o.bind("SUPER + grave", "Quick Ask", "omarchy-shell shell toggle damianpoole.ask")
```

## Configure

Open the overlay and select **Settings**, or press `Ctrl+,`. Model preferences
are stored by agent in:

```text
~/.config/omarchy/plugin-settings/damianpoole.ask.json
```

Leaving the model blank uses that agent's configured default. Codex users can
select a reasoning level or use the Luna/low quick preset. Custom model IDs are
passed directly to the selected agent CLI.

Press `Ctrl+N` or select **New** to clear the visible transcript and start a
question without prior context. The input clears immediately after each
submission so it is ready for a follow-up.

Supported agents: Codex, Claude, Gemini, OpenCode, Crush, Copilot, Grok, Pi,
and OMP.

## Local development

Clone this repository separately from the runtime plugin directory, then clone
it into Omarchy's plugin folder:

```bash
git clone ~/git/quick-ask ~/.config/omarchy/plugins/damianpoole.ask
omarchy plugin enable damianpoole.ask
omarchy restart shell
```

After committing a development change, update the runtime clone with:

```bash
git -C ~/.config/omarchy/plugins/damianpoole.ask pull --ff-only
omarchy restart shell
```

## License

MIT
