# Cognee Memory Plugin for Claude Code

Adds persistent memory to Claude Code through Cognee.

The integration:
- captures prompts, tool traces, and assistant responses into session memory
- injects relevant context on prompt submit
- syncs session memory into graph memory on session end/final exit

## Install

Install from the Claude Code marketplace. The recommended way is from your shell, *before* launching Claude Code, so the first `claude` launch is a clean session that runs the plugin bootstrap automatically — no in-app restart needed:

```bash
claude plugin marketplace add topoteretes/cognee-integrations
claude plugin install cognee-memory@cognee
```

These CLI subcommands use the exact same plugin manager as the in-chat `/plugin` commands — same marketplace clone, same install cache, same global state — they just run before you enter the terminal. Default install scope is `user` (global); pass `--scope project` or `--scope local` to confine it.

Then configure your runtime mode — **once** — in `~/.cognee/.env`. The file is created with a commented template on the first session start; values in it act exactly like shell exports (a real `export` in your shell still overrides the file, per terminal). It is shared with the Codex plugin, so both read the same configuration. Lines may optionally start with `export `, so existing export lines can be pasted verbatim. Pick one of the two modes below — or configure **both** and flip a terminal with a single export (see [Which mode wins, and how to switch](#which-mode-wins-and-how-to-switch)).

**Cognee Cloud or a remote server** — set both (one paste, no editor needed):

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY="ck_..."
EOF
chmod 600 ~/.cognee/.env
```

> Cloud mode is a pure thin client: it talks to your remote server over HTTP only and does **not** install a local Cognee runtime. The bundled virtualenv (`~/.cognee-plugin/venv`) is built only in local mode, where an in-process server actually runs.

**Local mode** (default when `COGNEE_BASE_URL` is not set) — the plugin bootstraps a local Cognee API at `http://localhost:8011`. Only `LLM_API_KEY` is required; `COGNEE_API_KEY` is auto-minted if absent:

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
LLM_API_KEY="sk-..."
EOF
chmod 600 ~/.cognee/.env
```

**Windows (PowerShell)** — same idea, same file:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.cognee" | Out-Null
@'
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY="ck_..."
'@ | Add-Content "$env:USERPROFILE\.cognee\.env"
```

Re-running any of these blocks is safe: when a key appears more than once, the **last value wins**, so pasting again with a new value updates the configuration. Editing the file directly (`nano ~/.cognee/.env`) works too. Changes apply on the next session launch. Plain shell `export`s in the launching terminal still take precedence over `~/.cognee/.env` — useful to override the shared config for one terminal. See [Managing the env file](#managing-the-env-file) for the file format and how to add, change, or remove variables later.

### Which mode wins, and how to switch

You can configure **both modes at once** — keep `COGNEE_BASE_URL` + `COGNEE_API_KEY` *and* `LLM_API_KEY` in the file together. The mode is then decided per terminal, by three rules in order:

1. **A `COGNEE_BACKEND` export wins.** `export COGNEE_BACKEND=local` (or `=cloud`) pins that terminal to that mode. Nothing else needs to change.
2. **Otherwise, cloud wins when configured.** If `COGNEE_BASE_URL` is set (in the file or the shell), the plugin connects to it.
3. **Otherwise, local.** With no URL anywhere, the plugin boots the local server.

So with both modes in `~/.cognee/.env`:

```bash
claude                          # → cloud (the configured URL routes)
COGNEE_BACKEND=local claude     # → local, this launch only
export COGNEE_BACKEND=local     # → local for every launch from this shell
```

Details worth knowing:

- **The switch is pinned.** `COGNEE_BACKEND=cloud` with no `COGNEE_BASE_URL` configured still counts as cloud: the plugin does **not** silently fall back to local, and the status line shows `✕ (missing_cognee_base_url)` so you know exactly what to fix.
- **To go local, use the switch — not `unset COGNEE_BASE_URL`.** Unsetting doesn't work: the env file re-injects the URL at the next launch. (Deleting the line from the file works, but that changes the default for *every* terminal.)
- The shared `COGNEE_BACKEND` flips both the Claude Code **and** Codex plugins in that terminal. To flip only one, use `COGNEE_CLAUDE_BACKEND` / `COGNEE_CODEX_BACKEND` — the plugin-specific name beats the shared one.
- Accepted values: `local` (aliases: `native`, `sdk`) and `cloud` (aliases: `http`, `api`, `server`). Anything else is ignored.
- `COGNEE_BACKEND` can also live in `~/.cognee/.env` to make a mode the durable default; a shell export still overrides it per terminal.
- Not sure what a terminal resolved? The status line's mode field shows it live, and `doctor.py` prints the decision with its cause, e.g. `Mode: Local — forced by COGNEE_BACKEND=local`.

You can also set config in `~/.cognee-plugin/claude-code/config.json`:

```json
{
  "base_url": "https://your-instance.cognee.ai",
  "dataset": "agent_sessions"
}
```

Then launch `claude`. All setup happens in the `SessionStart` hook, which fires once per fresh launch — so with the shell install above, the first launch connects memory with no extra steps.

If you instead installed **from inside the chat** with the `/plugin` slash commands, you must **restart Claude Code** (start a new session) before memory connects: `/reload-plugins` makes the skills and agents available in the current session but does not run `SessionStart`. On a first-run marketplace install the marketplace is also fetched asynchronously, so `SessionStart` may not fire that session even with a reload. Either way, make sure your configuration is in `~/.cognee/.env` (or exported in the shell you launch from).

On startup you should see a "Cognee Memory Connected" system message.

## Auth

The integration uses a **single auth principal** — one API key, one user.

Key resolution order:
1. `COGNEE_API_KEY` env var
2. `~/.cognee-plugin/api_key.json` (cached from a previous mint)
3. Auto-mint from the default local user (local mode only), then cache to `api_key.json`

## Mode selection rules

At startup (`SessionStart`):
- `COGNEE_BACKEND` (or `COGNEE_CLAUDE_BACKEND`) exported → that mode, pinned — see [Which mode wins](#which-mode-wins-and-how-to-switch)
- otherwise `COGNEE_BASE_URL` set → `managed_endpoint`, either local, or on Cognee Cloud (API key needed in cloud case)
- otherwise → `integration_local` (local API bootstrap)

A forced-local switch also scrubs `COGNEE_BASE_URL`/`COGNEE_API_KEY` from the process environment, so the per-prompt recall/remember calls and every spawned worker resolve the same local endpoint — not just `SessionStart`. A forced-cloud switch with no URL configured never boots the local server; the connection attempt fails visibly instead (status line + doctor).

At hook runtime:
- hooks resolve mode through runtime endpoint auth (env + `api_key.json`), not only config intent
- `http` mode skips local SDK initialization

The hooks emit `mode_decision` logs with `mode`, `service_url`, `url_source`, `key_source`, `api_key_present`.

## Sessions

Each terminal launch maintains a small map file:

```
~/.cognee-plugin/claude-code/sessions/<host_session_id>.json
  → { "conn_uuid": "...", "session_id": "...", "host_key": "..." }
```

- **`session_id`** — which Cognee session this terminal writes to and recalls from. Fixed at launch.
- **`conn_uuid`** — per-launch liveness handle used for agent registration and server shutdown counting.

By default a new `session_id` is generated each launch. Set `COGNEE_SESSION_ID` to resume a specific session:

```bash
export COGNEE_SESSION_ID="my-project"
```

Two terminals can deliberately share a session by setting the same `COGNEE_SESSION_ID`.

## Dataset

All writes and recall are scoped to a single dataset. By default both the Claude Code and Codex plugins use `agent_sessions`, so memory is shared across both integrations automatically.

Set a custom dataset at launch:

```bash
export COGNEE_PLUGIN_DATASET="my-project-memory"
```

`~/.cognee-plugin/claude-code/config.json` may still show a `dataset` value for
visibility, but runtime dataset selection does not read it.

`COGNEE_PLUGIN_DATASET` seeds the dataset at launch. Recall searches only the active dataset.
Data added outside of Claude to the dataset (via SDK or the server for example) is visible in Claude via the Cognee plugin.

### Switching datasets mid-session

Run `/cognee-memory:cognee-switch-datasets` (optionally with a dataset name) to move the
running session to another dataset. Without a name it lists the datasets you can write to —
those owned by the principal behind your API key; datasets you can only read are counted but
never offered — and asks you to pick one. A name that is not listed is created for you.

A Cognee session never spans two datasets, so the switch:

1. syncs the current session into its dataset (aborts if that fails — nothing changes);
2. registers a **new** Cognee session on the chosen dataset under a fresh connection handle,
   then releases the old handle (register-then-unregister, so a local agent-mode server never
   sees zero connections);
3. repoints this launch's record so every hook, the shell wrappers, the idle/exit watchers and
   the status line follow it, and appends `· switched` to the status line.

The choice lives in the launch record (`~/.cognee-plugin/claude-code/sessions/<host id>.json`),
so it survives `--resume` and beats the shell's `COGNEE_PLUGIN_DATASET` (and a pinned
`COGNEE_SESSION_ID`) for the rest of the launch. Retired sessions stay in the record's `touched`
list and the session-end sync covers them again as a safety net. The script behind the skill is
`scripts/switch-dataset.py` (`--list [--json]`, `<name> [--force] [--json]`,
`--session-key <host id>` when several launches share a directory).

## Hooks

| Hook | Behavior |
|---|---|
| `SessionStart` | mode select, identity setup, dataset readiness, watcher bootstrap |
| `UserPromptSubmit` | dataset-scoped context lookup + async prompt staging |
| `PostToolUse` | async trace write |
| `Stop` | assistant answer write + optional transcript clear hook |
| `PreCompact` | memory anchor build before compaction |
| `SessionEnd` | trigger detached final sync worker |

Claude-specific contracts are preserved:
- `hookSpecificOutput` payload format
- async hook behavior for write hooks

## Memory preference

With the plugin active, Cognee is the **preferred** memory system: relevant memory is
auto-recalled into context on every `UserPromptSubmit` and writes are captured
automatically, so Claude consults Cognee first when answering. To reinforce this, the
`SessionStart` hook injects an `additionalContext` instruction telling Claude to treat
Cognee as authoritative and prefer the Cognee tools/skills over Claude Code's built-in
file memory (`MEMORY.md`).

Note: a plugin **cannot reliably disable** Claude Code's native auto memory
(`MEMORY.md` is injected as context, not a tool call that hooks can intercept). This
feature steers the model toward Cognee rather than hard-disabling native memory. To
turn the steer off, set `COGNEE_PREFER_MEMORY=false`. To additionally suppress native
auto memory yourself, disable it in Claude Code (e.g. `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`
in the launching shell, if your Claude Code version supports it).

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_PREFER_MEMORY` | `true` | Inject SessionStart steer asserting Cognee as the preferred memory |

## Session sync and watchers

Session→graph sync runs through Cognee's session-aware `improve` endpoint: the server bridges the session from its own session cache (feedback weights, Q&A persist, compact trace-feedback persist, distillation, enrichment) instead of the plugin re-posting the full accumulated session text — which used to trigger a complete re-cognify of the whole transcript on every sync. Servers without session-aware improve automatically fall back to the legacy document bridge.

An idle watcher runs in the background for the lifetime of each launch. It polls activity every `COGNEE_IDLE_POLL` seconds and fires an improve when the session has been quiet for `COGNEE_IDLE_THRESHOLD` seconds, then waits at least `COGNEE_IMPROVE_COOLDOWN` seconds before the next run. An automatic improve also fires every `COGNEE_AUTO_IMPROVE_EVERY` stored tool calls/stops.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_IDLE_POLL` | `10` | Poll interval in seconds |
| `COGNEE_IDLE_THRESHOLD` | `60` | Seconds of inactivity before idle improve fires |
| `COGNEE_IMPROVE_COOLDOWN` | `600` | Minimum seconds between idle improve runs |
| `COGNEE_AUTO_IMPROVE_EVERY` | `150` | Stored tool calls/stops between automatic improves (0 disables) |
| `COGNEE_IMPROVE_SUBMIT_TIMEOUT` | `180` | Read timeout for the improve POST (distillation runs inside the request) |
| `COGNEE_IMPROVE_POLL_DEADLINE` | `600` | Best-effort wait for cognify/memify completion after submit |
| `COGNEE_IMPROVE_BUSY_DEADLINE` | `600` | How long to wait for a concurrent improve's session lock before giving up |
| `COGNEE_IMPROVE_BUSY_RETRY_INTERVAL` | `15` | Seconds between re-submits while the session lock is held |

Final sync on session end is triggered by the `SessionEnd` detached worker, with an exit watcher as fallback if the process exits without firing `SessionEnd`.

## Skills

- `/cognee-memory:cognee-remember`
- `/cognee-memory:cognee-search`
- `/cognee-memory:cognee-sync`
- `/cognee-memory:cognee-code`
- `/cognee-memory:cognee-forget`
- `/cognee-memory:cognee-switch-datasets`

## Remember (write) behavior

`cognee-remember` and the auto-capture hooks POST to the server's `/api/v1/remember`
and ask it to build the graph **in the background** (`run_in_background=true`), so the
write returns as soon as it's enqueued instead of blocking the turn on a synchronous
cognify. A synchronous build can take tens of seconds, exceed the client timeout, and be
misread as "server unreachable" — which then triggers a `cognee-cli` fallback that can
double-write. The graph populates shortly after the call, so a recall in the same breath
may not see the new entry yet.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_REMEMBER_BACKGROUND` | `true` | Build the graph in the background; set `false` for a synchronous, immediately-queryable write |

A write that *times out* is reported as "submitted; timed out waiting for confirmation"
and does **not** fall back to `cognee-cli` (the write likely landed — a fallback would
risk a duplicate). Only a genuine connection failure falls back.

## Code graph

Repositories can be indexed into a deterministic **code graph** (symbols, calls,
imports, endpoints, dependencies) via cognee's enola-backed pipeline. Indexing makes
**no LLM or embedding calls** — it is fast and costs no tokens. Requires a cognee
server >= 1.5.3.

Opening the agent inside a git repository indexes it automatically at session start
(background, never blocking the first prompt), and re-indexes it after any turn that
changed the working tree. Index one explicitly — a different repo, a git URL, or one
automation declined — with:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-index-repo.sh <repo-path-or-git-url> [--dataset <name>] [--index-vectors] [--wait <seconds>]
```

Query it with `cognee-search.sh ... --code` (see the `cognee-code` skill for the
operations: `query_facts`, `explore`, `traverse`, `find_path`, `impact_analysis`,
`delta`). Prompts that mention an identifier-shaped token inside an indexed repo also
get code facts injected automatically by the per-prompt recall hook.

Each indexed repository gets its own dataset, named
`codebase-<repo-name>-<digest>` where the digest identifies the indexed path.
Narrow datasets keep code searches fast, and the digest matters for
correctness: with cognee's default backend every dataset is a separate graph
database, and two checkouts sharing a basename (`~/work/a/service`,
`~/work/b/service`) landing in one database would let each re-index's
stale-node sweep delete the other's nodes. `--code` searches resolve the
dataset from the current checkout, so the generated name rarely needs typing.

Indexing writes enola's snapshot into the indexed repository itself, at
`<repo>/.enola/` (untracked). Add `.enola/` to the repository's `.gitignore` or
your global excludes; the plugin's change detection already ignores it, so the
indexer's own output never triggers a re-index.

### What the graph reflects: working tree vs. pushed commits

**The freshness model differs by where the server runs.** This is a property of the
architecture, not a limitation to work around — but it is worth knowing which one you
are using, because the output looks identical either way.

| Server | Indexed from | Graph reflects | Updated by |
|---|---|---|---|
| **Local** (default) | The repository path on this machine | Your working tree, **including uncommitted and untracked changes** | Every turn that changes a file |
| **Cloud / remote** | A git URL the server clones | The **last pushed commit** on the cloned branch | Pushing, then re-indexing |

A remote server cannot read your disk. It only ever sees code you have pushed, so a
local edit — however recent — is invisible to it until it lands on the remote. The
plugin therefore does not re-submit URL-indexed repositories after local edits: doing
so would re-pull the same commits and change nothing.

The practical consequence on cloud: if you refactor locally and ask about the old
symbol, the graph answers from the pushed state and the answer *looks* authoritative.
**Push before relying on code answers about work in progress**, or use a local server
for branches you are actively editing. `{"operation": "delta"}` reports what the last
index actually changed, which is the quickest way to confirm what the graph currently
knows.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_CODE_AUTOINDEX` | `auto` | `auto`: auto-index new repositories only when the server is local (code never leaves the machine) · `always`: also auto-index against a remote server · `off`: never auto-index new repositories (explicitly indexed ones still refresh) |

Automatic indexing skips directories that are not git repositories, hold no source
files, or exceed 3000 source files. Explicit indexing has no size cap.

## Forget (delete) behavior

`cognee-forget` deletes memory the user asks to forget ("forget what we talked about
tennis"). The agent syncs the live session first (so unsynced content becomes a
deletable document), reads candidate documents' raw content to decide what matches,
confirms with the user, and deletes each matching document via `POST /api/v1/forget` —
which removes the raw data, its derived graph knowledge, and — best-effort — the session
Q&A turns whose answers cited the deleted graph elements (plus guidance derived from
them; agent trace entries are not matched). All server access goes through
`scripts/cognee-forget.sh`, which resolves the API key like the other wrappers (env →
`~/.cognee/.env` → the auto-minted local `api_key.json`) and always authenticates; it
refuses to run without a key rather than send requests that can only 401. Deletion is
irreversible; dataset-wide or delete-everything scopes require an explicit, unambiguous
user request.

## Status line

The status line displays `cognee: <dataset> · <mode>`, for example:

```
cognee: agent_sessions · local
cognee: my-project · cloud
```

`<dataset>` is the active Cognee dataset. `<mode>` is `local` when no `COGNEE_BASE_URL` is set or when it points to localhost, and `cloud` when it points to a remote host; an exported `COGNEE_BACKEND` / `COGNEE_CLAUDE_BACKEND` switch overrides that, so the bar always shows the mode the terminal actually resolved. The mode is rendered **bold and coloured** — cyan for `local`, magenta for `cloud` — because it is the one field worth a double-take: it tells you which memory you are about to write to. (Red/green/amber are left to the health glyph and the warnings; bold and colour are set together so a terminal that ignores one still shows the other.)

A connection glyph precedes the line:

```
● cognee: agent_sessions · cloud          # connected (server up and authenticated)
✕ (incorrect_cognee_api_key) cognee: … · cloud   # server reachable, but COGNEE_API_KEY was rejected
✕ (unreachable) cognee: … · cloud         # server positively absent (connection refused / DNS)
✕ (server_error) cognee: … · cloud        # server returned a 5xx
✕ (not_responding) cognee: … · cloud      # server up, but N consecutive recalls timed out
✕ (missing_cognee_base_url) cognee: … · cloud   # COGNEE_BACKEND=cloud is exported, but no COGNEE_BASE_URL is configured
```

`●` shows once the server is confirmed up **and** authenticated. On a failure the glyph flips to `✕ (<reason>)` — `incorrect_cognee_api_key` (a missing, wrong, or expired `COGNEE_API_KEY`), `unreachable` (server positively absent: connection refused or DNS failure, including a server that dies mid-session), `server_error` (5xx), or `not_responding` (the server accepts connections but hasn't answered for several consecutive prompts — a single slow response never triggers it, so a busy server is not misreported as unreachable). One reason is special: `missing_cognee_base_url` means the terminal was pinned to cloud with the `COGNEE_BACKEND` switch but no `COGNEE_BASE_URL` is configured anywhere — a misconfiguration the renderer proves directly from the environment, shown immediately rather than after a failed connection attempt. The state is recorded by the hooks that already talk to the server (SessionStart, and the per-prompt recall), so the line stays green until a failure is actually observed, and clears back to `●` on the next success. The glyph is read from local state only — no network on refresh. It is **colour-coded**: a bold green `●` when the connection is confirmed good, and a bold red `✕ (<reason>)` — reason included, so the whole verdict reads as one unit — when it is confirmed bad. The LLM-key failure is red as well — the two are told apart by the reason itself (`incorrect_cognee_api_key` for the key this plugin uses to reach the server, `incorrect_llm_api_key` for the key the local server uses to reach the LLM) rather than by colour.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_READY_PROBE_TIMEOUT` | `1.0` | Seconds the per-prompt readiness probe waits before giving up and skipping recall for that turn. It sits on the keystroke→answer path, so the default is deliberately tight; raise it on a slow or loaded server that is otherwise healthy. |

**Local-mode LLM key.** In local mode the plugin also surfaces problems with `LLM_API_KEY` (the key the local server uses to call the LLM) **in that same leading glyph slot**, with its own reasons:

```
✕ (incorrect_llm_api_key) cognee: agent_sessions · local   # missing, or rejected by the provider
```

The slot holds one sign, by precedence: a server-connection failure wins (if the server can't be reached or authenticated, its LLM key isn't the actionable problem), otherwise an LLM-key failure is shown **in place of** the green `●` — the `llm_*` reason already tells you the server side itself is fine, so you never see a contradictory `●` and `✕` side by side.

Both cases show the same reason — the fix is the same either way, and `llm-state.json` still records which of the two it was. Both verdicts come from a single authority: the background idle watcher (never the prompt path). It resolves the key exactly as the server does — Cognee's own config, so a key in `LLM_API_KEY`, a `.env`, or Cognee's config file all count — and validates it with one tiny `max_tokens=1` call through the same LLM stack Cognee uses. That makes it **provider-agnostic**: a rejection is caught for **any** provider (OpenAI, Anthropic, Gemini, Azure, Bedrock, …), not just OpenAI. Only a `401`/`403` counts as a key failure — providers authenticate before validating anything else, so any other response (including the `400` that reasoning models return when one token is too few to finish a message) proves the key works. A transport failure with no HTTP status is inconclusive and leaves the previous verdict alone. It runs once per idle-watcher launch — at session start, and again on any prompt that finds no live watcher (the watcher exits after each idle-bridge cycle) — never more often than once per `COGNEE_LLM_CHECK_INTERVAL` seconds; there is no periodic timer. The verdict clears once the key checks out, and expires after 30 minutes, so one left behind by a session that has ended never haunts the bar. See **Per-terminal status** below for how two terminals that disagree about the key each show their own truth. Local mode only (in cloud the LLM key lives on the remote server). Disable with `COGNEE_LLM_KEY_CHECK=false`.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_LLM_KEY_CHECK` | `true` | Background, provider-agnostic `LLM_API_KEY` validation (local mode) surfacing `✕ (incorrect_llm_api_key)` |
| `COGNEE_LLM_CHECK_INTERVAL` | `300` | Minimum seconds between LLM-key checks |

**Memory hits.** The line ends with what memory actually did — this turn, then (faint) over the session:

```
● cognee: agent_sessions · local · 5 memory hits (3 from past sessions) · 12/40 turns had hits this session
```

`5 memory hits` is how many memories this turn's lookup found and injected into context (across session turns, traces, graph context and agent guidance). `3 from past sessions` is the part of that Claude could not have known from this conversation: knowledge-graph passages that came from an earlier session (or from a `remember`-ed document) rather than from this session's own cache — omitted when zero. `12/40 turns had hits this session` is the running total — 40 prompts so far, memory fired on 12 of them. A session that has not had a single hit yet shows `memory warming up (7 turns)` instead of a bare `0/7`: the graph is usually still filling up. `UserPromptSubmit` writes these to `~/.cognee-plugin/claude-code/recall/<session>.json`, so the renderer stays network-free, and the counts are stamped with the session that produced them so a second terminal's numbers never show up here. The per-scope breakdown (`recall 4s/5t/0g/1a · saved 2p/41t/2a` — `s`ession turns, `t`races, `g`raph context, `a`gent guidance; saves as `p`rompts, `t`races, `a`nswers) is still available with `COGNEE_STATUSLINE_COUNTS=full`; hide the segment with `false`.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_STATUSLINE_COUNTS` | `true` | `true`: `N memory hits · H/T turns had hits this session`; `full`: per-scope `recall …/saved …` strip; `false`: hidden |

**Per-terminal status.** Every signal in the line answers *for this terminal*, not for the machine. That matters because terminals legitimately disagree: one shell exported `LLM_API_KEY` and another didn't, or two hold different `COGNEE_API_KEY`s against the same server. Both sessions show the truth at the same time:

```
terminal A (key exported)  →  ● cognee: agent_sessions · local
terminal B (no key)        →  ✕ (incorrect_llm_api_key) cognee: agent_sessions · local
```

Each writer keeps two records: the machine-wide marker (`server-ready.json`, `llm-state.json`) which stays **coordination** state — it gates recall and is shared with the Codex plugin, since both talk to one server — and a per-session copy under `conn-state/<session_key>.json`, `llm-state/<session_key>.json`, and `recall/<session_key>.json`, which is the **display** state the bar reads. Without the split, a single file meant the last writer decided what every other bar showed: a keyless launch's `not_set` reddening a healthy session, or a healthy one's `ok` hiding a genuinely missing key.

Resolution, in order: this session's own record wins; **except** that a fresher **server-wide** failure in the shared marker takes precedence — `unreachable` or `server_error`, because the server really is shared and a just-observed outage applies to everyone. `incorrect_cognee_api_key` is deliberately **not** propagated: it describes the credential the other session used, not the server, so a keyless cloud terminal starting up can't red a healthy local one. Nor does a fresher shared `ready` clear your own failure — their working key says nothing about yours. With no record of your own, the shared marker is used only when it is unattributed (an older writer, or a write made before the session key was known); a record belonging to another session is ignored and no glyph is drawn, exactly as during warm-up.

**Internal variables — do not set these.** A few `COGNEE_*` names in the environment
are the plugin's own inter-process plumbing, written by one hook and read back by the
detached workers it spawns: `COGNEE_USER_ID` (the resolved Cognee user for this
launch), `COGNEE_SESSION_KEY` (the host session key every hook of a launch resolves
through), `COGNEE_AGENT_SESSION_NAME`, `COGNEE_PLUGIN_IN_VENV` (the re-exec guard),
and `COGNEE_SYNC_DATASET` / `COGNEE_SYNC_SESSION_ID` (arguments to the final-sync
worker). Setting them yourself does not configure anything — the plugin overwrites
them during startup — and a stale value can misroute identity or session resolution.
Use `COGNEE_SESSION_ID` to pin a session and `COGNEE_PLUGIN_DATASET` to seed the dataset
(a mid-session `/cognee-memory:cognee-switch-datasets` overrides both for that launch).

It is configured automatically on first launch when no custom status line is already configured. SessionStart writes the correct path into `~/.claude/settings.json` and Claude Code hot-reloads it, so the status line appears from your first interaction onward. Existing non-Cognee `statusLine` settings are preserved; set `COGNEE_STATUSLINE=false` before launching Claude Code to opt out entirely.

The entry sets `refreshInterval: 2`, so Claude re-runs the (network-free, local-only) renderer every 2 seconds in addition to its event-driven updates. Without it, Claude only refreshes the status line on events (a new message, `/compact`, etc.), which go quiet while the session is idle — so a connection change detected right after launch (e.g. a rejected API key) wouldn't show until your next prompt. Tune it with `COGNEE_STATUSLINE_REFRESH_INTERVAL` (seconds; a value below `1`, e.g. `0`, disables idle polling and reverts to event-only refresh).

The status line reads only local state — no network calls on every refresh:
1. Dataset: this launch's record (`sessions/<host id>.json`, written at SessionStart and by a dataset switch), otherwise `COGNEE_PLUGIN_DATASET`, otherwise `agent_sessions`
2. Mode: `COGNEE_BACKEND` / `COGNEE_CLAUDE_BACKEND` switch, then `COGNEE_BASE_URL` env var, then `~/.cognee-plugin/claude-code/config.json` (`base_url`)
3. Default mode: `local`
4. Connection glyph: `conn-state/<session>.json`, then `server-ready.json` + `recall-breaker.json`
5. LLM key: `llm-state/<session>.json`, then `llm-state.json`
6. Counts: `recall/<session>.json`, then `last_recall.json`

## Auto-clear demo hook

For demo flows where each response should clear local transcript context:

```bash
export COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE=true
```

This clears the transcript file on `Stop` after memory capture.

## Logs and state

Claude Code-specific plugin state and logs are written under:

```bash
~/.cognee-plugin/claude-code/
```

Useful logs:

```bash
tail -f ~/.cognee-plugin/claude-code/hook.log
tail -f ~/.cognee-plugin/claude-code/subprocess.log
tail -f ~/.cognee-plugin/claude-code/watcher.log
tail -f ~/.cognee-plugin/claude-code/exit-watcher.log
tail -f ~/.cognee-plugin/claude-code/recall-audit.log
```

Shared state (used by both Claude Code and Codex plugins):

```bash
~/.cognee-plugin/api_key.json     # cached API key
~/.cognee-plugin/venv/            # shared Cognee virtualenv
```

## Usage metrics

For an offline usage rollup compiled purely from the local files above — no
network, no `cognee` import — run:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-plugin" metrics          # readable rollup
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-plugin" metrics --json   # JSON
```

It reports sessions, recalls and hit-rate, saves (prompt/trace/answer), the
local-vs-cloud mode split, and how often an open recall breaker skipped recall.

## Updating

The plugin is versioned with [semver](https://semver.org/) — see
[`CHANGELOG.md`](./CHANGELOG.md). Claude Code offers an update only when the
plugin's `version` changes (it's pinned in the marketplace entry), so a plain
reinstall of the same version reuses the cached copy.

**Update on demand:**

```
/plugin marketplace update cognee     # refresh the marketplace catalog
/plugin update cognee-memory@cognee   # apply a newer version if one exists
```

`/plugin update` reports "already at the latest version" when your installed
version matches the published one.

**Automatic updates (recommended):** Claude Code can refresh marketplaces and
update installed plugins in the background after startup, then prompt you to run
`/reload-plugins`. Auto-update is **enabled by default only for official
Anthropic marketplaces** — for the third-party `cognee` marketplace you must opt
in via Claude Code's per-marketplace auto-update setting (see the "Configure
auto-updates" section of the Claude Code plugin docs). With it on, new releases
land without any manual `/plugin update`.

**If updates still don't appear**, re-add the marketplace source and reinstall:

```
/plugin uninstall cognee-memory@cognee
/plugin marketplace remove cognee
/plugin marketplace add topoteretes/cognee-integrations
/plugin install cognee-memory@cognee
```

### Update notifications

When a newer version is published, the plugin surfaces it automatically — no
configuration needed to receive them:

- **Status line:** an amber `⬆ Cognee update available <installed>→<latest>`
  segment appears, and disappears once you update.
- **SessionStart:** a one-time message per new version, e.g. *"Cognee update
  available 1.0.0 → 1.2.0 — run `/plugin update cognee-memory@cognee`."*

A background check in the idle watcher runs **at most once per day** and fetches a
single public file — the marketplace manifest on the tracked git ref, via
`raw.githubusercontent.com` — to read the published version. It sends no data and
no telemetry, uses a conditional (ETag) request, fails silently when offline, and
skips local-path (dev) installs. Turn it off with `COGNEE_UPDATE_CHECK=false`.

| Env var | Default | Effect |
|---|---|---|
| `COGNEE_UPDATE_CHECK` | `true` | Background "update available" check + status-line/SessionStart nudges |
| `COGNEE_UPDATE_CHECK_INTERVAL` | `3600` | Minimum seconds between checks |

## Remove

```
/plugin uninstall cognee-memory@cognee
```

## Troubleshooting

**Terminal connects to cloud when you wanted local (or the reverse)**
- The mode is routed by `COGNEE_BASE_URL`: configured anywhere (env file or shell) → cloud; otherwise → local. An exported `COGNEE_BACKEND=local` / `=cloud` overrides that for the terminal — see [Which mode wins](#which-mode-wins-and-how-to-switch).
- `unset COGNEE_BASE_URL` does **not** go local: the env file re-injects the URL at the next launch. Export `COGNEE_BACKEND=local` instead.
- Check what the terminal actually resolved: the status line's mode field shows it live, and `doctor.py` prints the decision with its cause (`Mode: Local — forced by COGNEE_BACKEND=local`).
- If a mode seems stuck, check for a forgotten `COGNEE_BACKEND` / `COGNEE_CLAUDE_BACKEND` export in the shell or in `~/.cognee/.env` — the plugin-specific name silently beats the shared one.

**Recall returns empty but data was ingested**
- Recall is scoped to the active dataset (the one in the status line — `COGNEE_PLUGIN_DATASET` / `agent_sessions` at launch, or whatever you switched to).
- Data written via the Python SDK or `client.py` goes to `default_dataset` by default, if dataset not otherwise specified.
- To verify, call the recall API directly without a dataset filter: `curl -X POST "$COGNEE_BASE_URL/api/v1/recall" -d '{"query":"..."}'`

**Session not resolving / wrong session shown**
- Check `~/.cognee-plugin/claude-code/sessions/<host_session_id>.json` — this is the map file for your terminal.
- If it's missing, SessionStart may not have completed; check `~/.cognee-plugin/claude-code/hook.log`.

**Unauthorized / key errors**
- Check `~/.cognee-plugin/api_key.json`. Delete it to force a re-mint.
- Relevant logs: `api_key_cached`, `api_key_minted`, `agent_register_result`.

**Missing session key at startup**
- If the payload session key is missing, SessionStart refuses registration.
- Relevant logs: `session_key_resolved`, `missing_payload_session_id`.

**Final sync diagnostics**
- Check `~/.cognee-plugin/claude-code/hook.log` and `~/.cognee-plugin/claude-code/exit-watcher.log`.
- Relevant logs: `sync_deferred_to_shutdown_worker`, `final_sync_once_*`, `agent_unregister_result`.

## Configuration reference

Config precedence:
1. env vars (shell exports)
2. `~/.cognee/.env` (one-time setup file, shared with the Codex plugin; loaded into the environment at process start, so every env var below can live here)
3. `~/.cognee-plugin/claude-code/config.json`
4. defaults

One exception sits above all four layers: the `COGNEE_BACKEND` / `COGNEE_CLAUDE_BACKEND` mode switch. When exported, it pins the mode regardless of where the connection variables are defined — forced local ignores a configured `COGNEE_BASE_URL` entirely (it is scrubbed from the process environment), and forced cloud stays cloud even when the URL is missing. See [Which mode wins](#which-mode-wins-and-how-to-switch).

`~/.cognee/.env` is created with a commented template on first session start (permissions `0600`; the path can be overridden with `COGNEE_ENV_FILE`). Run `doctor.py` to see which keys the file defines, which are overridden by shell exports, and — in the mode row — whether a backend switch forced the mode decision.

### Managing the env file

`~/.cognee/.env` is a plain dotenv text file. Every env var in the table below can live in it, and you can manage it entirely from the command line — or open it in any editor.

**Add or change a variable without an editor** — append it. When a key appears more than once, the **last value wins**, so appending the same key again with a new value is also how you *change* it:

```bash
echo 'COGNEE_PLUGIN_DATASET="my-project-memory"' >> ~/.cognee/.env
```

```powershell
Add-Content "$env:USERPROFILE\.cognee\.env" 'COGNEE_PLUGIN_DATASET="my-project-memory"'
```

For secrets (`COGNEE_API_KEY`, `LLM_API_KEY`), prefer the editor route below — an `echo` puts the key into your shell history.

**Edit the file manually** — open it in any editor:

```bash
nano ~/.cognee/.env          # or vim, code, open -e (macOS)
```

```powershell
notepad "$env:USERPROFILE\.cognee\.env"
```

Since the file starts as a commented template, editing usually means uncommenting a line and filling in your value. The format:

```bash
# Comments start with '#'; blank lines are ignored.
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY=ck_abc123                # quotes are optional
export LLM_API_KEY="sk-..."             # a leading 'export ' is tolerated, so
                                        # shell profile lines paste verbatim
```

Keys are letters, digits, and underscores. Values are taken literally — no `$VAR` interpolation, no multi-line values. Malformed lines are skipped silently, never fatal, and process-critical variables (`PATH` and friends) are ignored by design.

**Remove a variable** — delete (or comment out) its line in the editor. To switch modes you usually don't need to remove anything: keep both modes' variables in the file and export the switch instead — `export COGNEE_BACKEND=local` (see [Which mode wins](#which-mode-wins-and-how-to-switch)). Remove the `COGNEE_BASE_URL` line only when you want local to become the permanent default for every terminal.

**Apply and verify** — the file is read at session start, so changes take effect on the next `claude` launch. If a value seems to be ignored, check whether the same variable is `export`ed in your shell: real exports always win over the file. The doctor's **Env File** row lists which keys the file defines and flags any that a shell export is overriding.

| Key | Env var(s) | Default | Notes |
|---|---|---|---|
| `dataset` | `COGNEE_PLUGIN_DATASET` | `agent_sessions` | Dataset for writes and recall at launch; `/cognee-memory:cognee-switch-datasets` changes it mid-session (config value is informational-only) |
| `session_id` | `COGNEE_SESSION_ID` | auto-generated per launch | Override to resume a named session |
| `session_strategy` | `COGNEE_SESSION_STRATEGY` | `per-directory` | `per-directory`, `git-branch`, `static` |
| `session_prefix` | `COGNEE_SESSION_PREFIX` | `cc` | Prefix for auto-generated session IDs |
| `base_url` | `COGNEE_BASE_URL` | unset | Set to enable managed endpoint mode |
| `api_key` | `COGNEE_API_KEY` | unset | API key; auto-minted if absent in local mode |
| mode switch | `COGNEE_BACKEND` | unset | `local` or `cloud` — pins the terminal's mode, overriding the URL rule; flips the Claude Code **and** Codex plugins |
| plugin-only mode switch | `COGNEE_CLAUDE_BACKEND` | unset | Same, for this plugin only; beats `COGNEE_BACKEND` |
| local URL override | `COGNEE_LOCAL_API_URL` | `http://localhost:8011` | Local API base URL |
| local LLM | `LLM_API_KEY`, `LLM_MODEL` | unset | Required for local mode runtime |
| demo auto-clear | `COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE` | disabled | Clear transcript on Stop after capture |
| idle watcher poll | `COGNEE_IDLE_POLL` | `10` | Idle watcher poll interval in seconds |
| idle watcher threshold | `COGNEE_IDLE_THRESHOLD` | `60` | Seconds of inactivity before idle improve fires |
| idle watcher cooldown | `COGNEE_IMPROVE_COOLDOWN` | `600` | Minimum seconds between idle improve runs |
| auto-improve threshold | `COGNEE_AUTO_IMPROVE_EVERY` | `150` | Stored tool calls/stops between automatic improves (0 disables) |
| improve submit timeout | `COGNEE_IMPROVE_SUBMIT_TIMEOUT` | `180` | Read timeout for the improve POST |
| improve poll deadline | `COGNEE_IMPROVE_POLL_DEADLINE` | `600` | Best-effort wait for pipeline completion after submit |
