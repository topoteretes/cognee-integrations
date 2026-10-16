# Changelog

All notable changes to the **cognee-memory** Cursor plugin are documented here.

The version here matches the `version` field in `.cursor-plugin/plugin.json` and the
`cursor` entry in `integrations/inventory.yml`.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0]

### Added
- First cut of the Cursor plugin: `.cursor-plugin/plugin.json`, `hooks/hooks.json`
  in Cursor's native format, and the Cognee skills, on top of the Codex plugin's
  hook runtime (Cognee 1.6.0 pins, HTTP-only hooks, local and cloud modes).
- `scripts/cursor_hook.py`, the payload adapter between Cursor's hook contract
  and the shared Cognee hook scripts: `conversation_id` → `session_id`,
  `generation_id` → `turn_id`, `workspace_roots` → `cwd`, Cursor tool names
  (`Shell`, `Task`, `MCP:<tool>`) → the names the capture policy knows, JSON
  string `tool_input` / `tool_output` decoded. The prompt/answer pair is stored
  from `afterAgentResponse` — the end-of-turn hook the Cursor IDE actually fires
  (its `stop` was never observed to launch, IDE 3.16.17 / CLI 2026.09.26) and
  the one carrying the answer `text`. `stop` is a fallback: it skips a turn
  `afterAgentResponse` already stored (marker under
  `~/.cognee-plugin/cursor/responses/`) and otherwise scrapes the answer from the
  tail of Cursor's JSONL transcript. Replies are translated to what Cursor
  documents per hook; `stop` never returns a `followup_message`, and every
  failure prints the neutral reply and exits 0.
- Hooks registered: `sessionStart`, `beforeSubmitPrompt` (recall + prompt park),
  `postToolUse`, `postToolUseFailure`, `afterAgentResponse`, `stop`, `preCompact`,
  `sessionEnd`.
- `scripts/install-cursor-hooks.py`: writes the same hooks, with absolute paths,
  into `~/.cursor/hooks.json` or a project's `.cursor/hooks.json` (the latter also
  runs in cloud agents); idempotent, merges with existing hooks, `--uninstall`.
- Cursor CLI status line: `scripts/cognee_statusline_render.py` (the Claude Code
  renderer on Cursor's state: health glyph, dataset · mode, credits, recall hits and
  session activation, update nudge) behind `scripts/cognee-statusline.sh`;
  `scripts/_statusline_config.py` registers it in `~/.cursor/cli-config.json` on
  `sessionStart` without touching a user's own `statusLine` (`COGNEE_STATUSLINE=false`
  opts out). `session-context-lookup.py` writes the per-conversation recall marker
  (`recall/<conversation id>.json`) the bar reads, so several terminals each show
  their own counts. The plain-text `render_status_for_host` header stays for the IDE.
- `~/.cognee-plugin/cursor/adapter.log`: one JSON line per hook launch (Cursor event,
  inner script, conversation/turn, outcome, duration), written by the adapter itself so
  skipped and failed launches are visible too.
- Root `.cursor-plugin/marketplace.json` listing this plugin, so the repository can
  be imported as a Cursor (team) marketplace or submitted to the Cursor Marketplace,
  like `.claude-plugin/marketplace.json` for Claude Code; its version is checked by
  `scripts/check_version_consistency.py`.
- Plugin state under `~/.cognee-plugin/cursor/`; `COGNEE_CURSOR_BACKEND` pins the
  mode for this plugin only; the connection registers as type `cursor` with agent
  name `cursor-agent` and session ids `cursor_<conversation_id>`.
