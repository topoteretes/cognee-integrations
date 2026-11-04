# Cognee Memory Status for Cursor

Shows the [Cognee Cursor plugin](../README.md)'s status in Cursor's status bar, the IDE
counterpart of the Cursor CLI status line:

```
$(pulse) cognee agent_sessions · cloud · 1 memory hit
```

- **Text:** connection health, the chat's dataset, the mode and this turn's memory hits.
  The tooltip has the full line, with cloud credits and the session's hit ratio. The item
  turns red when the plugin reports a failure (`✕ …`).
- **Click:** pick another dataset for the chat (Cognee: Switch Dataset). The picker opens at once with a spinning "Loading datasets…" row and fills in when the server answers.
  Its first entry, `auto (best-matching dataset)`, sets recall back to auto (`switch-dataset.py auto`:
  the server picks the dataset for each recall when it can; saves stay where they are). The active
  dataset's row shows the recall mode; picking it restricts recall to it without a new session.
  Picking another dataset switches the chat and restricts recall to it. A dataset named `auto` (the
  name is reserved) is not offered, and as the active one it is shown but cannot be picked. The
  notification after a pick says what changed, e.g. that an older server keeps recall on the active
  dataset.
- **Which chat:** the most recent prompt across chats. Extensions can't see which Cursor
  chat has focus.

It needs the Cognee Cursor plugin installed, with its hooks running ("Include Third-Party
Plugins, Skills, and Other Configs" on). Everything is read from the plugin: the line comes
from `scripts/cognee_statusline_render.py` and the switch from `scripts/switch-dataset.py`
of the newest installed plugin copy. The extension stores nothing and calls no server.

Settings: `cognee.pluginRoot` (plugin folder, default: newest copy under
`~/.cursor/plugins`), `cognee.python` (default `python3`).

Build and install:

```bash
npx @vscode/vsce@3.2.1 package
cursor --install-extension cognee-cursor-status-0.1.1.vsix
```
