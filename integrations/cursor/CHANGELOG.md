# Changelog

All notable changes to the **cognee-memory** Cursor plugin are documented here.

The version here matches the `version` field in `.cursor-plugin/plugin.json` and the
`cursor` entry in `integrations/inventory.yml`.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this
project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.2]

### Added
- **Recall is auto by default (SDK-1393).** Unless recall is pinned, the graph recall on
  each prompt, and a `cognee-search` that names no dataset, send no dataset: a cognee 1.6.6 server
  picks the readable dataset(s) most likely to hold the answer, or searches every readable one
  when it cannot narrow (dataset routing, SDK-1317; best effort). Routing needs a cognee 1.6.6 server;
  an older one searches every readable dataset. Pin a dataset to keep recall on one.
  Saves still go to the active dataset. While recall is auto, the credit line says "based on
  Cognee memory" instead of naming the active dataset. Session history and guidance, the code
  lane and a `COGNEE_PLUGIN_READ_DATASET_IDS` read set keep their explicit scope; shared agent
  memory's dataset ids are dropped with the dataset under auto recall and kept while restricted; recap, evidence and file-context reads stay on the active dataset.
- **Picking a dataset restricts recall to it.** A switch restricts recall to the new dataset
  (stored as `recall_pinned` in the launch record); the new `switch-dataset.py pin` restricts
  recall to the active dataset without a new session, and so does naming the active dataset (by
  name or id, also when another principal owns it); `switch-dataset.py auto` lifts the
  restriction and leaves saves where they are. `--list` reports the mode in effect (`recall:
  auto|pinned`, and when pinned why: `dataset_picked`, `configured_pin` or `read_dataset_ids`). A
  session switched before this release stays restricted. `auto` is a reserved name: no dataset can
  be switched to or created as `auto`, and `COGNEE_PLUGIN_DATASET=auto` means the default dataset.
- **One message per result.** `switch-dataset.py --json` adds `message`, one or two plain
  sentences for the user, to the `--list` output (which recall is on and why) and to every switch,
  `pin` and `auto` result; the text output prints the same sentences, and the skills relay them.
- **The status bar extension's picker knows auto recall.** Its first entry, `auto (best-matching
  dataset)`, sets recall back to auto; the active dataset's row shows the recall mode (with the
  script's `message` saying why) and picking it runs `switch-dataset.py pin`, restricting recall to
  it instead of doing nothing; a dataset named `auto` is no longer offered (picking it ran
  `switch-dataset.py auto`). The notification after a pick is the script's `message`.
- **`COGNEE_PLUGIN_RECALL_MODE` sets the recall mode a session starts in.** `auto` (the default)
  or `pinned` (recall restricted to the session's dataset, reason `configured_pin`), exported or
  in `~/.cognee/.env`; any other value is `auto`. A choice made in the session (`pin`, `auto`, or
  a switch) wins over it for that session. **Behaviour change:** a configured non-default
  `COGNEE_PLUGIN_DATASET` no longer restricts recall: it only names the dataset saves go to. Set
  `COGNEE_PLUGIN_RECALL_MODE=pinned` to keep recall on it. Session start says so once per machine
  and dataset, in the message the user sees, when a non-default dataset is configured,
  `COGNEE_PLUGIN_RECALL_MODE` was never set and the session has not pinned recall itself.
- **`cognee-search`'s CLI fallback follows the recall mode.** With the server unreachable, the
  `cognee-cli` fallback sends no dataset under auto recall and the active dataset when pinned.

### Changed
- **Local mode installs cognee 1.6.6** (was 1.6.5), the first release with dataset routing.
- **The status says when recall is auto.** The status at the top of the injected context, the
  CLI status line and the IDE status bar read `cognee: auto (<dataset>) · local` while recall is
  not pinned, as Claude Code's row does, and the bare name once it is.
- **Recalled memory is marked with its dataset (SDK-1393).** Every graph item is marked
  `[dataset <name>]` instead of `[mN · origin]` (an item from a server that names no dataset,
  and every other item, keeps its origin mark); the logo-led credit line is unchanged. The
  per-session tags (`tags.json`) are gone: "Cognee found N memories" counts the marked items in
  the context, the recall hook records the turn it injected memory on (`injected_turn` in
  `credit.json`), and the answer check counts a credit only on that turn. The `memory_credit`
  event now carries `{injected, credited}`, and `memory_tags_write_failed` is now
  `memory_credit_write_failed`.

### Removed
- **The status line no longer shows the last operation's cost** (`last turn ~$0.06`). It was
  an approximate, often stale reading; the credit balance and the top-up hint stay. The
  credits marker still records `last_op`. Claude Code dropped it the same way.
- **The cross-dataset hint.** Auto-recall no longer appends the "Other Cognee datasets you can
  search" block or asks the user to pick a dataset. `COGNEE_RECALL_DATASET_HINT`,
  `COGNEE_DATASETS_CACHE_TTL`, `COGNEE_DATASETS_CACHE_RETRY` and the `readable-datasets.json`
  cache are gone. A one-off search of one dataset (`cognee-search.sh --dataset-id`,
  `list-datasets.py`) is unchanged.

### Fixed
- **Switching to a dataset you don't own under shared memory lands in it (SDK-1393).** Under
  shared agent memory the parent account owns the datasets, so the switch addressed the target
  by its id, and the shared-memory resolver read that id as a *name*: it pinned the session to
  an empty dataset named after the id (creating one when none existed) while reporting the right
  id. A dataset picked by id now resolves as that dataset, the record keeps its name with its
  canonical id, and a dataset the shared role cannot hold is addressed by its id instead of
  forking a copy. Picked by id among datasets that share a name, it also stays the write target,
  including across the periodic refresh, rather than giving way to the parent's same-named copy.
  Present since shared memory became the default. Delete any leftover empty dataset whose name
  is a UUID.
- **No empty agent-owned copy of the shared dataset (SDK-1393).** Under shared memory the sync's
  pre-improve dataset ensure posted the launch's dataset by name with the agent key, and the server,
  which matches a name only among the caller's own datasets, created an empty agent-owned
  `agent_sessions` beside the canonical one. The ensure now skips the by-name create when the name
  has a canonical id, and the dataset listing marks only the canonical row as current, not every
  dataset of that name.
- **Searching another dataset while recall is restricted takes the person's pick (SDK-1393).**
  The skills already said to list the other datasets and ask before a one-off search of one, but
  `cognee-search.sh --dataset-id` searched any id it was given, so an agent could leave a pinned
  dataset on an id it found in a recalled memory. While recall is pinned or limited to a read set,
  the wrapper now refuses a dataset other than the active one (exit 2, a JSON `error` saying how to
  ask, no request sent) unless `--user-picked` marks it as the person's choice: one they picked
  when asked or named in their request. The skills carry the flag. Auto recall, and the active
  dataset named by hand, need no flag.
- **The status bar picker marks only the active row current.** Under shared memory the listing
  can hold two datasets with the active name (yours and another principal's copy); both showed as
  current, and picking the other one ran `switch-dataset.py pin` instead of switching to it. Only
  the row the switch script marks current is the active one now, and a dataset whose name another
  row shares is switched to by its id.

## [0.1.1]

### Security
- **`<private>…</private>` keeps text out of memory, logs and requests.** Anything you
  wrap in the tag is replaced with `[private]` before the plugin writes it to a file or
  log, sends it as a query, or stores it; the text around it is kept. Spans don't nest:
  each opener ends at the next close, and text between two spans is kept; a span may
  cross lines and carry attributes. An opener with no close is left as plain text. No setting turns this off. Cursor still sees the span in the live turn.
- **Explicit remember is sanitized like automatic capture.** `cognee-remember.sh` text,
  and its `cognee-cli` fallback, now goes through the same `<private>` stripping and
  secret patterns, whatever `COGNEE_CAPTURE` says. So does text passed to
  `cognee-cli.sh remember|add|recall|search`, the source-checkout fallback; a file path
  given there, like a `--file` upload, is still sent byte for byte.
- **Queries are sanitized**: the per-prompt recall, `cognee-search.sh` and its CLI
  fallback, and the timeline topic. A `--code-query` (here or through `cognee-cli.sh`)
  is sanitized field by field, so it stays valid JSON.
- **Fewer secrets on disk.** `recall-audit.log` sanitizes the prompt and context it
  keeps; the `sync_payload` line in `hook.log` records field names and size instead of
  the payload's first 200 characters.

### Changed
- `COGNEE_CAPTURE_REDACT=false` still turns the secret patterns off, but no longer
  affects `<private>` spans.
- The `cognee-forget` skill searches for what surrounds a secret, never the value itself.

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
