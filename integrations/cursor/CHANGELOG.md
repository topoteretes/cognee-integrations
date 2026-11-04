# Changelog

All notable changes to the **cognee-memory** Cursor plugin are documented here.

The version here matches the `version` field in `.cursor-plugin/plugin.json` and the
`cursor` entry in `integrations/inventory.yml`.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0]

### Added
- The per-prompt memory block skips `Read` tool calls and arrives with the turn's
  next tool call: Cursor accepts a `Read`'s `additional_context` and then drops it.
- Credit in the answer, on by default as in Claude Code: recalled items are
  tagged `[mN]` and an answer that used one ends with a line led by the Cognee
  logo and naming the dataset ("…, based on the agent_sessions dataset."),
  without the tags or "Cognee memory" text. The answer check logs whether the
  line is there. `COGNEE_PLUGIN_CITE_MEMORY=false` turns it off, and the agent is
  then told credits are off, since memory still holds answers that had one.
  `cognee-search.sh` prints the instruction as the first line above graph hits,
  since Cursor shows a large result as a file, away from the hook's memory block.
- A save ends the answer the same way, with a receipt: `cognee-remember.sh`
  prints the instruction above a successful result ("Saved to the
  agent_sessions dataset: …"). The answer check does not count it as a credit.
- Credit lines, receipts and the instructions are cut from prompts, answers and
  tool traces before they are stored, and from recalled memory and search
  results before the agent reads them, so credits that Claude Code sessions
  store do not come back as examples to follow.
- Status bar extension for the Cursor IDE (`statusbar/`, classic windows only):
  the plugin's status line in the status bar, with a click that opens a
  dataset picker at once and lists the datasets as they load.
- First cut of the Cursor plugin: `.cursor-plugin/plugin.json`, `hooks/hooks.json`
  in Cursor's native format, and the Cognee skills, on top of the Codex plugin's
  hook runtime (Cognee 1.6.5 pin, HTTP-only hooks, local and cloud modes).
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
  `sessionEnd`. `beforeSubmitPrompt` replies only `{"continue": true}`, since Cursor
  documents no context field for it; the recall is held for the turn and returned
  as `additional_context` on its first `postToolUse` / `postToolUseFailure`.
  `sessionStart`'s context names the plugin root, which the skills need because
  Cursor does not export `CURSOR_PLUGIN_ROOT` to the agent's shell.
- `scripts/install-cursor-hooks.py`: writes the same hooks, with this checkout's
  absolute paths, into `~/.cursor/hooks.json` or a project's `.cursor/hooks.json`;
  idempotent, merges with existing hooks, `--uninstall`. It refuses (and leaves
  alone) a file whose shape it would otherwise corrupt, only treats a
  `run-cursor-hook` launcher running one of the plugin's scripts as its own, and
  keeps a symlinked file a symlink.
- One exit watcher per chat, keyed by Cursor's host process and the session, and
  stopped by that chat's `sessionEnd`. None is started when no Cursor process is
  found among the hook's ancestors (instead of watching the short-lived hook).
- Cursor CLI status line: `scripts/cognee_statusline_render.py` (the Claude Code
  renderer on Cursor's state: health glyph, dataset · mode, credits, recall hits and
  session activation, update nudge) behind `scripts/cognee-statusline.sh`;
  `scripts/_statusline_config.py` registers it in `~/.cursor/cli-config.json` on
  `sessionStart`, opt-in with `COGNEE_STATUSLINE=true` because it replaces the CLI's
  own footer, without touching a user's own `statusLine`. `session-context-lookup.py` writes the per-conversation recall marker
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
