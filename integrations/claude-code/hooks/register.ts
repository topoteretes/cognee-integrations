// Cognee memory: the in-process layer of the plugin.
//
// The settings hooks in hooks.json run the memory pipeline (recall, capture,
// learning from the session, sync) exactly as before. This module adds what
// only an in-process module can:
//
//   * the status row, above the prompt on every surface: it runs the plugin's
//     own status-line renderer in `--json` mode and lays the result out, so
//     the band shows the row the settings-file status line shows (the badge
//     included). In a
//     terminal the module marks its session (`band/<session>.json`) and the
//     renderer then prints nothing for it, so the row is never drawn twice;
//     with the `band` option off the settings status line is back below;
//   * live activity: it reads the per-session marker the hooks write
//     (`~/.cognee-plugin/claude-code/phase/<session>.json`) and shows it on
//     the row and after the spinner (the answer's own credit line is the
//     agent's, from the recall hook's instruction);
//   * the dataset name on the row is a picker: choosing another writable
//     dataset, or typing the name of a new one, switches the session there
//     without a model turn (`/cognee-dataset <name>` does the same from the
//     prompt);
//   * a `see fix` button beside a red ✕ on the row: a dialog pane that says what
//     is wrong in plain words and how to fix it, runs the plugin's doctor, and
//     for a key or URL problem takes the value and writes it to ~/.cognee/.env;
//   * a first-run tour: three dim hint lines under the row, each about the
//     thing above it, shown once per machine (the plugin's store remembers)
//     and again on `/cognee-tour`;
//   * two tools the model calls directly, `mcp__cognee-memory__search` and
//     `mcp__cognee-memory__remember`, served by the plugin's own shell
//     wrappers: no skill file to read, no Bash row, structured arguments.
//
// Everything the module runs is one of the plugin's own scripts, as a local
// process; the only network traffic is theirs, on the person's or the model's
// explicit action, plus the listing of the writable datasets for the picker
// (once the SessionStart hook has run; a few more tries while a server is
// still starting). Nothing here runs on a fixed timer while the session is
// idle: the marker is read on the events the hooks fire, and polled once a
// second only while an operation is running (a server boot the hook handed
// off included) or for a short window after one was started.
//
// Only native events are hooked (prompt.submit, tool.call, turn.complete,
// session.compact, session.start, session.end), never the classic.* mirrors
// of the settings hooks: the built-in guard bypasses a user-installed mod's
// classic.* hooks for Team and Enterprise sign-ins and wherever managed
// settings exist, so nothing here would run for those users.
import type { Register, RenderElement } from 'claude-code'

type Activity = { kind: 'active' | 'result'; phase: string; text: string; age: number; ok: boolean; count?: number }

type Snapshot = {
  enabled: boolean
  state: string
  reason: string
  dataset: string
  mode: string
  credits: string
  credits_exhausted?: boolean
  billing_url?: string
  update: string
  text: string
}

// The brand lavender (#BC9BFF), a deeper violet on a light theme so it keeps
// its contrast on white.
const PURPLE_DARK = '#bc9bff'
const PURPLE_LIGHT = '#7c4dff'

type PhaseResult = { count?: unknown; at?: unknown; ok?: unknown; buffered?: unknown }
type PhaseMarker = {
  active?: { phase?: unknown; detail?: unknown; updated_at?: unknown } | null
  results?: Record<string, PhaseResult>
}

const FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
const FRAME_MS = 120
const POLL_MS = 1000
// How long after a hook fired the marker keeps being polled for the async
// work (saving, learning from the session) that hook started.
const POLL_WINDOW_MS = 30_000
// After a session start the connection marker lands when the SessionStart
// hook finishes, which can be well after the module's first snapshot. While
// the row still says "unknown", the snapshot is retaken every few seconds for
// this long, then left to the next prompt. A server the hook handed to its
// bootstrap worker keeps the `setting_up` phase alive for as long as it boots
// (minutes on a cold start); the poll follows that phase past the warm-up,
// retaking the snapshot every half minute, and stops when the phase ends.
const WARMUP_MS = 120_000
const WARMUP_RETRY_TICKS = 5
const BOOT_RETRY_TICKS = 30
// Mirrors the renderer: an active phase older than this is a hook that died.
const STALE_MS = 90_000
const DIM_AFTER_MS = 60_000
// A result older than this is no longer news: it leaves the row.
const HIDE_AFTER_MS = 300_000
const RENDERER_TIMEOUT_MS = 5000
const RENDERER_RETRY_MS = 60_000
// Phases whose settings hook runs async, so the terminal spinner shows no
// statusMessage for them; the mod adds its own suffix there. The sync hooks
// (recall, anchor, session start) already announce themselves in a terminal.
const ASYNC_PHASES = ['saving', 'consolidating', 'indexing', 'syncing']

const ACTIVE_LABELS = {
  connecting: 'connecting…',
  setting_up: 'setting up memory',
  recalling: 'recalling…',
  saving: 'saving…',
  consolidating: 'learning from this session…',
  indexing: 'indexing repository…',
  anchoring: 'preserving context…',
  syncing: 'saving session…',
  // The module's own work, never written to the marker.
  searching: 'searching memory…',
  remembering: 'remembering…',
  switching: 'switching dataset…',
} as const
type Phase = keyof typeof ACTIVE_LABELS
// What the module runs itself shows after a terminal spinner too: the tool row
// there says only which tool runs.
const OWN_PHASES = ['searching', 'remembering', 'switching']
// How a call to each of the plugin's tools is labelled in the transcript.
const TOOL_ROW_LABELS: Record<string, string> = {
  'mcp__cognee-memory__search': 'Cognee search',
  'mcp__cognee-memory__remember': 'Cognee remember',
}

const SEARCH_TIMEOUT_MS = 60_000
// Above the wrapper's own worst case (a 120 s POST, an 8 s wait for the
// cognify to become queryable, the interpreter's start), so the module never
// cuts off a write that is still in flight and then calls it not stored.
const REMEMBER_TIMEOUT_MS = 150_000
// What the remember tool takes in one call. Each call runs a cognify on the
// tenant's key (an LLM pass per chunk plus embeddings), and a typed tool
// whose description invites a whole file lowers the bar for a large one.
// Past these the call is refused with a pointer to the cognee-remember
// skill, which has no cap.
const REMEMBER_MAX_CHARS = 16_000
const REMEMBER_MAX_FILE_BYTES = 64_000
// And per session: each call is a paid cognify, and nothing else bounds how
// many the model makes. Past either the call is refused with a pointer to the
// skill, which has no allowance, so a person who wants more is never stopped.
const REMEMBER_SESSION_CALLS = 5
const REMEMBER_SESSION_BYTES = 64_000
let rememberCalls = 0
let rememberBytes = 0
// The listing makes several HTTP calls of up to 15 s each; its budget sits
// above their sum, so a slow server is a late list, not a wiped one.
const LIST_TIMEOUT_MS = 45_000
// A listing that fails is tried again: it runs before the SessionStart hook
// has written the launch record it needs, or against a server that is up but
// not answering. The wait grows with each attempt (5 s, 10 s, ...), and after
// the last the name stays plain text until the next prompt, /cognee-dataset
// or a switch starts the attempts over. A boot the hook handed to its worker
// is not an attempt: the listing waits for the `setting_up` phase to end.
const LIST_RETRY_MS = 5_000
const LIST_MAX_ATTEMPTS = 5
// A switch syncs the session it leaves first; that can take minutes.
const SWITCH_TIMEOUT_MS = 600_000
const NODE_SETS: Record<string, string> = {
  user: 'user_context',
  project: 'project_docs',
  agent: 'agent_actions',
}

// ── module state (reset on a reload, which is fine: it is all redrawn) ──────
let sessionId = ''
let phasePath = ''
let bandPath = ''
let rendererPython: string | null = null
let rendererFailures = 0
// When the renderer last failed for the third time: the cutoff below expires
// after RENDERER_RETRY_MS, so a slow start does not freeze the row for good.
let rendererFailedAt = 0
let snapshotInFlight = false
let snapshot: Snapshot | null = null
let active: Activity | null = null
let result: Activity | null = null
// When the shown activity was set, and whether the marker itself says one is
// running: a 'saving' the Stop hook announced but nothing ever reported (a
// cloud capture the worker could not deliver, no marker at all) expires
// after the marker's own stale window instead of spinning for the session.
let activeSince = 0
let markerActive = false
let frame = 0
let spin: { cancel: () => void } | null = null
let poll: { cancel: () => void } | null = null
let pollUntil = 0
let pollTicks = 0
let warmupUntil = 0
// The mark: cognee's waveform icon, drawn only where it can be drawn as
// itself, the official SVG on the desktop row, read from the plugin's assets
// at session start. Text gets no stand-in: no glyph in a terminal, nothing
// after the spinner or in toasts (a character never looks like the mark).
// The terminal's credit line draws the display logotype instead (LOGOTYPE).
let markOn = true
let waveSvg = ''
let purple = PURPLE_DARK
let bandOn = true
let spinnerOn = true
let toolsOn = true

// The dataset picker on the band.
type DatasetRow = { name: string; id: string; current: boolean }
let switchTargets: DatasetRow[] = []
let switching = false
// The picker's last entry: picking it turns the picker into a text field for
// a dataset that does not exist yet (the switch script creates it). Spaces
// make the value one no dataset can be named, so it never collides.
const NEW_DATASET = '+ new dataset…'
let namingDataset = false
// In a terminal the picker stays closed until asked for: a click anywhere on
// the band gives the band the keys, and a focused picker shows its list.
let pickerOpen = false

// The first-run tour: one hint line under the row per step, about what is
// right above it. Shown once per machine (the store key survives sessions);
// bump TOUR_VERSION to show a rewritten tour once more. -1 is "not showing".
const TOUR_VERSION = 1
const TOUR_STEPS = [
  'The dataset name ↑ is a picker: pick another, or "+ new dataset…" to create one',
  '↑ ● means memory is connected · local/cloud is where it lives · the row ends with what memory is doing',
  'Claude uses what Cognee recalls on every prompt · type /cognee to see its commands · /cognee-tour replays this tutorial',
]
let tourOn = true
let tourStep = -1
let tourChecked = false

// The fix dialog: what the row's reason means and what to do about it. Each
// guide is plain words first, then steps; `field` names the env-file variable
// the pane takes a value for, when one value is the fix.
const FIX_PANE = 'cognee-fix'
const DOCTOR_TIMEOUT_MS = 30_000
type FixGuide = { title: string; what: string; steps: string[]; field?: { name: string; placeholder: string } }
const FIX_GUIDES: Record<string, FixGuide> = {
  incorrect_cognee_api_key: {
    title: 'The memory server rejected the plugin\'s API key',
    what: 'The key this plugin sends to the Cognee server (`COGNEE_API_KEY`) is missing, wrong or expired, so nothing can be recalled or saved.',
    steps: [
      '**Cloud:** paste the API key from your Cognee Cloud account below, or put `COGNEE_API_KEY=ck_…` in `~/.cognee/.env`. A key exported in your shell wins over the file.',
      '**Local:** delete `~/.cognee-plugin/api_key.json` and start a new session; the plugin mints a fresh key from the local server.',
      'With a plugin identity (`COGNEE_PLUGIN_IDENTITY`), **run doctor** below: it says whether the cached identity credential was the one rejected.',
      'A new prompt tries the new key; a new session applies it everywhere.',
    ],
    field: { name: 'COGNEE_API_KEY', placeholder: 'ck_… (saved to ~/.cognee/.env)' },
  },
  incorrect_llm_api_key: {
    title: 'The local server cannot reach its LLM',
    what: 'The Cognee server on this machine builds memory with an LLM, and the key it uses (`LLM_API_KEY`) is missing or the provider rejected it. The server itself is fine.',
    steps: [
      'Paste a working provider key below, or put `LLM_API_KEY=…` (and `LLM_PROVIDER` if not OpenAI) in `~/.cognee/.env`.',
      'Or use your Claude subscription instead: remove `LLM_API_KEY` from `~/.cognee/.env` and keep the `claude` CLI on PATH; the next session routes the server\'s LLM calls through it.',
      'Start a new session: the server reads its key at start.',
    ],
    field: { name: 'LLM_API_KEY', placeholder: 'sk-… (saved to ~/.cognee/.env)' },
  },
  missing_cognee_base_url: {
    title: 'Cloud mode is forced, but no server URL is configured',
    what: '`COGNEE_BACKEND=cloud` is exported in this shell, and no `COGNEE_BASE_URL` is set anywhere, so there is no server to talk to.',
    steps: [
      'Paste your Cognee Cloud URL below (it is saved as `COGNEE_BASE_URL` in `~/.cognee/.env`), and make sure `COGNEE_API_KEY` is set too.',
      'Or go local for this terminal: `unset COGNEE_BACKEND`, then start a new session.',
    ],
    field: { name: 'COGNEE_BASE_URL', placeholder: 'https://your-instance.cognee.ai' },
  },
  cognee_needs_python_3_10: {
    title: 'Local mode needs a Python the server can run on',
    what: 'The hooks run on any Python 3.9+, but the Cognee server itself needs Python 3.10 to 3.14, and none was found on this machine. The plugin never downloads one.',
    steps: [
      'Install one: `brew install python@3.12`, `uv python install 3.12`, pyenv, or python.org.',
      'Start a new session: the plugin builds its runtime under `~/.cognee-plugin/venv` and starts the server.',
      'Or use Cognee Cloud instead, which needs no local Python: set `COGNEE_BASE_URL` and `COGNEE_API_KEY` in `~/.cognee/.env`.',
    ],
  },
  unreachable: {
    title: 'The memory server cannot be reached',
    what: 'Connections are refused or the host does not resolve. In local mode the server process is not running (it may have died); in cloud mode the URL is wrong or the network is in the way.',
    steps: [
      '**Local:** start a new session; the plugin starts the server again. If it keeps failing, `~/.cognee-plugin/claude-code/hook.log` says why.',
      '**Cloud:** check `COGNEE_BASE_URL` in `~/.cognee/.env` (or your shell), then your network or VPN.',
      '**Run doctor** below: it shows the URL in use, the mode and the health check.',
      'The dot turns green again on the next successful recall.',
    ],
  },
  server_error: {
    title: 'The memory server is answering with errors',
    what: 'The server is up but replied with a server error (HTTP 5xx) to the last requests. Memory is not read or written until it recovers.',
    steps: [
      '**Run doctor** below for the health check and the server version.',
      '**Local:** start a new session to restart the server; `~/.cognee-plugin/claude-code/hook.log` holds the error.',
      '**Cloud:** retry in a moment; if it persists, the error belongs to the Cognee team.',
    ],
  },
  not_responding: {
    title: 'The memory server is not answering in time',
    what: 'The server accepts connections but several recalls in a row timed out. It is usually busy (a large memory build) rather than down.',
    steps: [
      'Wait a little and send another prompt; a single answer in time clears the mark.',
      'On a slow or loaded server, raise the per-prompt budget: `COGNEE_READY_PROBE_TIMEOUT=6` in `~/.cognee/.env`.',
      '**Run doctor** below to see whether the health check answers at all.',
    ],
  },
}
const FIX_FALLBACK: FixGuide = {
  title: 'Memory reported a problem',
  what: 'The row shows a reason the plugin recorded; the doctor report below has the details.',
  steps: ['**Run doctor** below.', 'A new session re-checks everything.'],
}
let fixReason = ''
let fixOpen = false
let doctorOutput = ''
let doctorRunning = false
let fixNote: { text: string; isError: boolean } | null = null
// One successful listing per session, refreshed after a switch or by
// /cognee-dataset; a failed one is retried a few times (LIST_MAX_ATTEMPTS).
let datasetsListed = false
let datasetsListing = false
let datasetsInFlight: Promise<void> | null = null
let datasetsAttempts = 0
let datasetsRetryAt = 0
// A listing put off because the worker was still booting the server.
let datasetsWaitingForBoot = false

function resetDatasets(): void {
  switchTargets = []
  datasetsListed = false
  datasetsAttempts = 0
  datasetsRetryAt = 0
  datasetsWaitingForBoot = false
}

// ── pure helpers ────────────────────────────────────────────────────────────
function plural(n: number, word: string): string {
  if (n === 1) return `${n} ${word}`
  return `${n} ${word.endsWith('y') ? word.slice(0, -1) + 'ies' : word + 's'}`
}

function toNumber(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

function activeLabel(phase: string, detail: string): string {
  const label = (ACTIVE_LABELS as Record<string, string>)[phase] ?? ''
  if (!label) return ''
  if (phase === 'setting_up') return detail ? `${label} · ${detail}…` : `${label}…`
  return label
}

// At rest: a number only for recall (what helped this turn), a ticked verb
// for everything that keeps work. Mirrors the renderer's _result_label.
function resultLabel(kind: string, entry: PhaseResult): string {
  const ok = entry.ok !== false
  const count = toNumber(entry.count)
  switch (kind) {
    case 'recalled':
      if (!ok) return 'recall failed'
      if (!count) return 'nothing relevant'
      return `${plural(count, 'memory')} used`
    case 'saved':
      if (ok || count) return '✓ memory saved'
      // Nothing landed this turn: spooled for replay, or dropped for good.
      return entry.buffered === true ? 'save queued' : 'save failed'
    case 'ready':
      return ok ? 'memory ready' : 'memory unavailable'
    case 'consolidated':
      return ok ? '✓ learned' : 'learning failed'
    case 'indexed':
      return ok ? '✓ repository indexed' : 'indexing failed'
    case 'anchored':
      return ok ? '✓ context kept' : 'context not kept'
    case 'synced':
      return ok ? '✓ session saved' : 'session sync failed'
    default:
      return ''
  }
}

// The same choice the renderer makes: an active phase wins, else the most
// recent finished result; a stale active phase is ignored.
function pickActivity(marker: PhaseMarker, nowMs: number): Activity | null {
  const current = marker.active
  if (current && typeof current === 'object' && typeof current.phase === 'string') {
    const updated = toNumber(current.updated_at)
    const ageMs = updated === null ? STALE_MS : nowMs - updated * 1000
    if (ageMs < STALE_MS) {
      const text = activeLabel(current.phase, typeof current.detail === 'string' ? current.detail : '')
      if (text) return { kind: 'active', phase: current.phase, text, age: ageMs / 1000, ok: true }
    }
  }
  const results = marker.results
  if (!results || typeof results !== 'object') return null
  let latestKind = ''
  let latest: PhaseResult = {}
  let latestAt = -1
  for (const [kind, entry] of Object.entries(results)) {
    if (!entry || typeof entry !== 'object') continue
    const at = toNumber(entry.at)
    if (at !== null && at > latestAt) {
      latestKind = kind
      latest = entry
      latestAt = at
    }
  }
  const text = latestKind ? resultLabel(latestKind, latest) : ''
  if (!text) return null
  const count = toNumber(latest.count)
  return {
    kind: 'result',
    phase: latestKind,
    text,
    age: Math.max(0, (nowMs - latestAt * 1000) / 1000),
    ok: latest.ok !== false,
    ...(count === null ? {} : { count }),
  }
}

function activity(phase: Phase): Activity {
  return { kind: 'active', phase, text: ACTIVE_LABELS[phase], age: 0, ok: true }
}

// Whether a tree draws anything: text that is not blank, or a control. The
// engine's own band is often an empty element, which still takes a row in a
// terminal when stacked under ours, so an empty one is left out.
// The credit line at the end of an answer, as the recall hook's instruction
// has the model write it: "> 🧠 Cognee memory [m4, m5]: what it remembered".
// Returns the text without it, the tags and the note; null when the text
// does not end with one.
const CREDIT_LINE = /^(?:>\s*)?🧠\s*Cognee memory \[([^\]]+)\]:\s*(.*)$/u

// The display logotype from cognee.ai/brand-resources (logotype-lavender.svg),
// which is built from squares on a grid, in braille: one square is one dot (a
// cell holds 2 x 4, so a dot is about square). The letters keep the SVG's
// widths (c, o, g 3 squares, n 4, e 2); the gap between letters is one dot,
// the smallest there is (the SVG's is 0.59 of a square).
const LOGOTYPE = ['⡏⠁⡏⡇⡏⡁⡧⣸⢸⠅⡯', '⠉⠁⠉⠁⠉⠁⠁⠈⠈⠁⠉']
function splitCredit(text: string): { body: string; tags: string; note: string } | null {
  const lines = text.replace(/\s+$/u, '').split('\n')
  const last = lines[lines.length - 1] ?? ''
  const m = CREDIT_LINE.exec(last.trim())
  if (!m) return null
  // The note continues the label's sentence ("...[m4]: the July notes"); drawn
  // without the label it opens one, so its first letter is capitalised.
  const note = (m[2] ?? '').trim().replace(/^\p{Ll}/u, c => c.toUpperCase())
  return { body: lines.slice(0, -1).join('\n').replace(/\s+$/u, ''), tags: (m[1] ?? '').trim(), note }
}

// Whether the row should use the light-background purple: the env override
// first, then Claude Code's theme row ("light", "light-daltonized", ...).
async function lightTheme($: any): Promise<boolean> {
  const forced = ((await $.env.get('COGNEE_STATUSLINE_THEME')) ?? '').toLowerCase()
  if (forced === 'light') return true
  if (forced === 'dark') return false
  try {
    const rows = (await $.config.list()) as { key?: string; value?: unknown }[]
    const theme = String(rows.find(r => r.key === 'theme')?.value ?? '').toLowerCase()
    return theme.startsWith('light')
  } catch {
    return false
  }
}

function drawsSomething(tree: unknown): boolean {
  if (tree === null || tree === undefined || tree === false) return false
  if (typeof tree === 'string') return tree.trim().length > 0
  if (typeof tree === 'number') return true
  if (Array.isArray(tree)) return tree.some(drawsSomething)
  if (typeof tree !== 'object') return false
  const el = tree as { type?: unknown; props?: { children?: unknown } }
  if (el.type !== 'Box' && el.type !== 'Text') return true
  return drawsSomething(el.props?.children)
}

function asText(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return ''
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

// One recall entry (the wrapper's /api/v1/recall item) as text.
function hitText(entry: Record<string, unknown>): string {
  const source = asText(entry.source)
  if (source === 'trace') {
    const parts = [`${asText(entry.origin_function) || '?'} — ${asText(entry.status)}`]
    if (entry.session_feedback) parts.push(`feedback: ${asText(entry.session_feedback)}`)
    if (entry.method_return_value) parts.push(`output: ${asText(entry.method_return_value).slice(0, 600)}`)
    return parts.join('\n')
  }
  const text = asText(entry.text) || asText(entry.content)
  if (text) return text
  const lines: string[] = []
  if (entry.question) lines.push(`Q: ${asText(entry.question)}`)
  if (entry.answer) lines.push(`A: ${asText(entry.answer)}`)
  return lines.join('\n')
}

// What a search wrapper printed, as the model and the pane read it.
function formatSearch(stdout: string, exitCode: number): { text: string; count: number; isError: boolean } {
  const raw = stdout.trim()
  if (!raw || raw === 'UNREACHABLE' || (exitCode !== 0 && !raw.startsWith('[') && !raw.startsWith('{'))) {
    return { text: 'Cognee server unreachable: the search did not run. Retry once memory is ready.', count: 0, isError: true }
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return { text: raw, count: raw ? 1 : 0, isError: exitCode !== 0 }
  }
  if (Array.isArray(parsed)) {
    const entries = parsed.filter(x => x && typeof x === 'object') as Record<string, unknown>[]
    if (entries.length === 0) return { text: 'No memories matched.', count: 0, isError: false }
    const blocks = entries.map((entry, i) => {
      const source = asText(entry.source) || 'memory'
      return `### ${i + 1}. ${source}\n${hitText(entry).trim()}`
    })
    return { text: blocks.join('\n\n'), count: entries.length, isError: false }
  }
  if (parsed && typeof parsed === 'object' && 'error' in parsed) {
    return { text: `Cognee search failed: ${asText((parsed as { error: unknown }).error)}`, count: 0, isError: true }
  }
  return { text: raw, count: 1, isError: false }
}

// What the remember wrapper printed, as one line for the model.
function formatRemember(stdout: string, exitCode: number, dataset: string): { text: string; isError: boolean } {
  const raw = stdout.trim()
  if (!raw || raw === 'UNREACHABLE') {
    return { text: 'Cognee server unreachable: nothing was stored. Retry once memory is ready.', isError: true }
  }
  let parsed: Record<string, unknown> = {}
  try {
    const value = JSON.parse(raw)
    if (value && typeof value === 'object') parsed = value as Record<string, unknown>
  } catch {
    return { text: raw, isError: exitCode !== 0 }
  }
  if (parsed.error) return { text: `Cognee remember failed: ${asText(parsed.error)}`, isError: true }
  if (parsed.ok === false) return { text: `Cognee rejected the write: ${raw}`, isError: true }
  const where = dataset || asText(parsed.dataset) || 'the active dataset'
  if (parsed.queryable === true) return { text: `Stored in ${where}; it is in the graph and recall will find it.`, isError: false }
  const outcome = asText(parsed.wait_outcome)
  if (outcome === 'errored') return { text: `Stored in ${where}, but building the graph from it failed; check the server log.`, isError: true }
  return { text: `Stored in ${where}; the graph is still being built from it, so recall finds it in a moment.`, isError: false }
}

function datasetRows(value: unknown): DatasetRow[] {
  if (!value || typeof value !== 'object') return []
  const rows = (value as { datasets?: unknown }).datasets
  if (!Array.isArray(rows)) return []
  return rows
    .filter(row => row && typeof row === 'object' && typeof (row as { name?: unknown }).name === 'string')
    .map(row => {
      const r = row as { name: string; id?: unknown; current?: unknown }
      return { name: r.name, id: asText(r.id), current: r.current === true }
    })
}

// ── helpers that reach the engine ───────────────────────────────────────────
async function stateDir($: any): Promise<string> {
  const home = (await $.env.get('HOME')) || (await $.env.get('USERPROFILE')) || ''
  return `${home}/.cognee-plugin/claude-code`
}

async function bindSession($: any): Promise<void> {
  sessionId = await $.session.id()
  const dir = await stateDir($)
  phasePath = `${dir}/phase/${sessionId}.json`
  bandPath = `${dir}/band/${sessionId}.json`
  await markBand($)
}

// Tell the settings-file status line that this session's row is drawn above
// the prompt: the renderer prints nothing for a session with this marker, so
// the row is not shown twice. Written only while the band is on.
async function markBand($: any): Promise<void> {
  if (!bandOn || !bandPath) return
  try {
    await $.fs.write(bandPath, JSON.stringify({ session_id: sessionId, at: (await $.clock.now()) / 1000, plugin: 'cognee-memory' }))
  } catch {
    // the status line then stays; nothing else depends on the marker
  }
}

function redraw($: any): void {
  $.ui.invalidate('ui.render')
}

function setActive($: any, next: Activity): void {
  active = next
  if (!spin) {
    spin = $.clock.every(FRAME_MS, () => {
      frame = (frame + 1) % FRAMES.length
      $.ui.invalidate('ui.render')
    })
  }
  redraw($)
}

function clearActive($: any): void {
  active = null
  if (spin) {
    spin.cancel()
    spin = null
  }
  redraw($)
}

async function readPhase($: any): Promise<void> {
  if (!phasePath) return
  markerActive = false
  let marker: PhaseMarker
  try {
    marker = JSON.parse(await $.fs.read(phasePath)) as PhaseMarker
  } catch {
    return
  }
  const now = await $.clock.now()
  const shown = pickActivity(marker, now)
  if (shown?.kind === 'active') {
    markerActive = true
    if (!active || active.phase !== shown.phase) {
      setActive($, shown)
      activeSince = now
    }
    return
  }
  result = shown
  if (active) clearActive($)
  else redraw($)
}

// Poll the marker once a second while something runs, and for a short window
// after a hook fired (its async work lands within seconds). Stops by itself.
async function startPoll($: any, windowMs: number): Promise<void> {
  const now = await $.clock.now()
  pollUntil = Math.max(pollUntil, now + windowMs)
  if (poll) return
  poll = $.clock.every(POLL_MS, async () => {
    await readPhase($)
    const at = await $.clock.now()
    pollTicks += 1
    if (active?.phase === 'saving' && !markerActive && at - activeSince > STALE_MS) clearActive($)
    // Warming up: the connection is unconfirmed until the SessionStart hook
    // writes its marker; retake the snapshot every few ticks until it is. A
    // boot the hook handed off keeps `setting_up` alive past the warm-up: the
    // poll runs for as long as the phase does (the `active` check below), and
    // the snapshot is retaken less often meanwhile.
    const settled = snapshot?.state === 'ok' || snapshot?.state === 'fail'
    if (!settled && (at < warmupUntil || active?.phase === 'setting_up')) {
      const every = at < warmupUntil ? WARMUP_RETRY_TICKS : BOOT_RETRY_TICKS
      if (pollTicks % every === 0) await refreshSnapshot($)
    }
    // The picker's listing failed (the launch record was not written yet, the
    // server was still booting): try again once its wait is over, or as soon
    // as the boot it was waiting for ends (the poll may stop on this tick).
    const bootEnded = datasetsWaitingForBoot && active?.phase !== 'setting_up'
    if (!datasetsListed && !datasetsListing && datasetsAttempts < LIST_MAX_ATTEMPTS && (at >= datasetsRetryAt || bootEnded)) {
      void loadDatasets($)
    }
    if (!active && at > pollUntil && poll) {
      poll.cancel()
      poll = null
      await refreshSnapshot($)
    }
  })
}

type Ran = { exitCode: number; stdout: string; stderr: string }

// One of the plugin's Python scripts, under whichever interpreter answers
// first (python3 on macOS and Linux, python or py on Windows). Null when none
// could start it.
// 'timeout' when the script ran and was killed at its time: the next
// interpreter is NOT tried then, since the script did run (a switch killed
// mid-sync must not be started over; an append must not land twice). Only a
// spawn that failed (no such interpreter) moves on to the next candidate.
async function runPython($: any, args: string[], init: { stdin?: string; timeoutMs: number }): Promise<Ran | 'timeout' | null> {
  // The interpreter that answered last goes first; the others stay as the
  // fallback for when it can no longer be spawned (a venv gone, a PATH change).
  const defaults = ['python3', 'python', 'py']
  const candidates = rendererPython ? [rendererPython, ...defaults.filter(c => c !== rendererPython)] : defaults
  for (const python of candidates) {
    try {
      const ran = (await $.process.run([python, ...args], init)) as Ran
      rendererPython = python
      return ran
    } catch (err) {
      const why = String(err)
      if (/time|outlast|kill/i.test(why)) return 'timeout'
      if (!/ENOENT|not found|no such|spawn|cannot (find|run|start)/i.test(why)) return null
      // the next candidate
    }
  }
  return null
}

// One of the plugin's shell wrappers (the same ones the skills run).
// Null when the wrapper could not run at all; 'timeout' when it ran past its
// time and was killed, which for a write means the outcome is unknown.
async function runWrapper($: any, script: string, args: string[], timeoutMs: number): Promise<Ran | 'timeout' | null> {
  try {
    return (await $.process.run(['bash', `${$.plugin.root}/scripts/${script}`, ...args], { timeoutMs })) as Ran
  } catch (err) {
    return /time|outlast|kill/i.test(String(err)) ? 'timeout' : null
  }
}

// A search, trimmed the way the prompt hook trims what it injects: a graph
// item on cognee >= 1.6.0 carries the whole prompt the server's LLM would
// have read, and only its retrieved context is worth showing. The trim is the
// plugin's own Python (recall-trim.py); when it cannot run, the raw answer is
// shown rather than nothing.
async function runSearch($: any, query: string, args: string[]): Promise<Ran | null> {
  const ran = await runWrapper($, 'cognee-search.sh', [query, ...args], SEARCH_TIMEOUT_MS)
  if (ran === 'timeout') return null
  if (!ran || ran.exitCode !== 0 || !ran.stdout.trimStart().startsWith('[')) return ran
  const trimmedRun = await runPython($, [`${$.plugin.root}/scripts/recall-trim.py`, '--query', query], {
    stdin: ran.stdout,
    timeoutMs: RENDERER_TIMEOUT_MS,
  })
  const trimmed = trimmedRun === 'timeout' ? null : trimmedRun
  if (!trimmed || trimmed.exitCode !== 0 || !trimmed.stdout.trim()) return ran
  return { ...ran, stdout: trimmed.stdout }
}

// One run of the plugin's own renderer in --json mode: the dataset, mode,
// connection state, credits and update nudge, as the terminal bar shows them.
// Local process, no network. Runs only on events, never on a timer.
async function refreshSnapshot($: any): Promise<void> {
  if (!sessionId || snapshotInFlight) return
  if (rendererFailures >= 3) {
    if ((await $.clock.now()) - rendererFailedAt < RENDERER_RETRY_MS) return
    rendererFailures = 0
  }
  snapshotInFlight = true
  try {
    const script = `${$.plugin.root}/scripts/cognee_statusline_render.py`
    const stdin = JSON.stringify({ session_id: sessionId })
    const ran = await runPython($, [script, '--json'], { stdin, timeoutMs: RENDERER_TIMEOUT_MS })
    if (ran && ran !== 'timeout' && ran.exitCode === 0 && ran.stdout.trim()) {
      try {
        snapshot = JSON.parse(ran.stdout) as Snapshot
        rendererFailures = 0
        redraw($)
        return
      } catch {
        // fall through
      }
    }
    rendererFailures += 1
    if (rendererFailures >= 3) rendererFailedAt = await $.clock.now()
  } finally {
    snapshotInFlight = false
  }
}

// ── the fix dialog ──────────────────────────────────────────────────────────
function fixGuide(reason: string): FixGuide {
  return FIX_GUIDES[reason] ?? FIX_FALLBACK
}

async function openFix($: any, reason: string): Promise<void> {
  fixReason = reason
  fixOpen = true
  fixNote = null
  doctorOutput = ''
  const opened = await $.ui.open({ id: FIX_PANE, title: 'Fix memory', focus: true, closeOnEscape: true, holdToasts: true, rows: 18 })
  if (!opened.isPlaced) $.ui.toast(`fix: ${opened.reason}`)
}

async function runDoctor($: any): Promise<void> {
  if (doctorRunning) return
  doctorRunning = true
  redraw($)
  try {
    const ran = await runWrapper($, 'cognee-doctor.sh', [], DOCTOR_TIMEOUT_MS)
    if (ran === 'timeout') doctorOutput = `the doctor gave no answer within ${DOCTOR_TIMEOUT_MS / 1000} s`
    else doctorOutput = ran ? (ran.stdout.trim() || ran.stderr.trim() || '(no output)') : 'could not run the doctor: bash not found'
  } finally {
    doctorRunning = false
    redraw($)
  }
}

// Append one variable to ~/.cognee/.env (the last value of a key wins there,
// so appending is also how a value is changed). Written by the module, so the
// secret never goes through a shell history.
async function saveEnvValue($: any, name: string, value: string): Promise<void> {
  const trimmed = value.trim()
  if (!trimmed) return
  if (/[\r\n]/.test(trimmed)) {
    fixNote = { text: 'one line only', isError: true }
    redraw($)
    return
  }
  // The plugin's own env-file module does the write: it resolves the file the
  // hooks read (COGNEE_ENV_FILE, else ~/.cognee/.env) and creates it 0600,
  // which the fs API cannot. The value travels on stdin, never on argv.
  const ran = await runPython($, [`${$.plugin.root}/scripts/_env_file.py`, '--append', name], { stdin: trimmed, timeoutMs: RENDERER_TIMEOUT_MS })
  if (ran === 'timeout') {
    fixNote = { text: `saving ${name} took too long; check the env file before trying again`, isError: true }
    redraw($)
    return
  }
  const written = ran && ran.exitCode === 0 ? ran.stdout.trim() : ''
  if (written) fixNote = { text: `${name} saved to ${written}; a new session applies it`, isError: false }
  else fixNote = { text: `could not save ${name}${ran?.stderr.trim() ? `: ${ran.stderr.trim()}` : ''}`, isError: true }
  redraw($)
}

// ── the tour ────────────────────────────────────────────────────────────────
// Once, at the first draw of the band (the person is looking at the row by
// then; at session start the Desktop app has no surface yet).
async function startTourIfFirstRun($: any): Promise<void> {
  if (tourChecked) return
  tourChecked = true
  if (!tourOn) return
  try {
    const seen = (await $.store.get('tour')) as { version?: unknown } | undefined
    if (seen && toNumber(seen.version) === TOUR_VERSION) return
  } catch {
    return
  }
  tourStep = 0
  redraw($)
}

async function endTour($: any, finished: boolean): Promise<void> {
  tourStep = -1
  redraw($)
  try {
    await $.store.set('tour', { version: TOUR_VERSION, at: (await $.clock.now()) / 1000, finished })
  } catch {
    // shown again next session, which is the worst case
  }
  if (finished) $.ui.toast(`that is the tour · /cognee-tour replays it`)
}

// ── the dataset picker ──────────────────────────────────────────────────────
function sessionKeyArgs(): string[] {
  return sessionId ? ['--session-key', sessionId] : []
}

// The datasets the picker offers: the writable ones, from the plugin's own
// switch script, once the SessionStart hook has run (it writes the launch
// record the script reads) and after each switch. A run that fails, times
// out or cannot start keeps the list it had and is tried again later; only
// an answer replaces it.
async function loadDatasets($: any): Promise<void> {
  if (!sessionId) return
  // A listing already running answers for this call too.
  if (datasetsInFlight) return datasetsInFlight
  // The server is not listening yet: the hook handed its boot to a worker,
  // which keeps `setting_up` alive for as long as the boot runs (minutes on a
  // cold start). Every try meanwhile would fail at once and spend the
  // attempts before the server is up, so none is made; the poll, which runs
  // for as long as the phase does, asks again once this wait is over.
  if (active?.phase === 'setting_up') {
    datasetsWaitingForBoot = true
    datasetsRetryAt = (await $.clock.now()) + LIST_RETRY_MS
    return
  }
  datasetsWaitingForBoot = false
  datasetsListing = true
  datasetsAttempts += 1
  datasetsInFlight = (async () => {
    let listed = false
    try {
      const switchable = await runPython($, [`${$.plugin.root}/scripts/switch-dataset.py`, '--list', '--json', ...sessionKeyArgs()], {
        timeoutMs: LIST_TIMEOUT_MS,
      })
      const parsed = switchable && switchable !== 'timeout' && switchable.stdout.trim() ? JSON.parse(switchable.stdout) : null
      if (parsed && typeof parsed === 'object' && !parsed.error) {
        switchTargets = datasetRows(parsed)
        listed = true
      }
    } catch {
      // the name stays plain text until the next try
    }
    datasetsListed = listed
    if (!listed) datasetsRetryAt = (await $.clock.now()) + LIST_RETRY_MS * datasetsAttempts
    datasetsListing = false
    datasetsInFlight = null
    redraw($)
  })()
  return datasetsInFlight
}

// A listing asked for outright (/cognee-dataset, the end of a switch) starts
// the attempts over.
async function relistDatasets($: any): Promise<void> {
  if (datasetsInFlight) await datasetsInFlight
  datasetsListed = false
  datasetsAttempts = 0
  datasetsRetryAt = 0
  await loadDatasets($)
}

// Move the session to another dataset: the plugin's switch script, which
// syncs the session it leaves and starts a new one. No model turn.
async function switchDataset($: any, target: string): Promise<string> {
  const name = target.trim()
  if (!name) return 'name a dataset'
  if (switching) return 'a switch is already running'
  switching = true
  setActive($, activity('switching'))
  let line = ''
  try {
    const ran = await runPython($, [`${$.plugin.root}/scripts/switch-dataset.py`, name, '--json', ...sessionKeyArgs()], {
      timeoutMs: SWITCH_TIMEOUT_MS,
    })
    let parsed: Record<string, unknown> = {}
    try {
      parsed = ran && ran !== 'timeout' && ran.stdout.trim() ? (JSON.parse(ran.stdout) as Record<string, unknown>) : {}
    } catch {
      parsed = {}
    }
    if (ran === 'timeout') line = `the switch ran past ${SWITCH_TIMEOUT_MS / 60_000} min and was stopped; /cognee-dataset shows where the session is`
    else if (!ran) line = 'could not start the switch: no python interpreter found'
    else if (parsed.error) line = asText(parsed.error)
    else if (parsed.switched === false) line = `already on ${asText(parsed.dataset) || name}`
    else if (parsed.switched === true) {
      const previous = (parsed.previous ?? {}) as { dataset?: unknown; synced?: unknown }
      const synced = previous.synced === false ? ' (previous session NOT synced)' : ''
      line = `switched to ${asText(parsed.dataset) || name}${synced}`
    } else line = `switch failed${ran.stderr.trim() ? `: ${ran.stderr.trim().split('\n').pop()}` : ''}`
    const failed = !parsed.switched && parsed.switched !== false
    $.ui.toast(failed ? `✕ ${line}` : `${line}`)
  } finally {
    switching = false
    clearActive($)
  }
  await refreshSnapshot($)
  void relistDatasets($)
  return line
}

export const register: Register = (on, options) => {
  markOn = options.icon !== 'none'
  bandOn = options.band !== false
  spinnerOn = options.spinner !== false
  toolsOn = options.tools !== false
  tourOn = options.tour !== false

  on('session.start', async ($, e, next) => {
    // A fresh start (or a reload): timers of an earlier life of the module
    // hold an engine interface that is gone; start over.
    if (poll) {
      poll.cancel()
      poll = null
    }
    if (spin) {
      spin.cancel()
      spin = null
    }
    pollTicks = 0
    resetDatasets()
    // The brand lavender reads on a dark background; a light one gets the
    // darker purple. COGNEE_STATUSLINE_THEME says which; without it, Claude
    // Code's own theme does (a light theme, or auto on a light system, is
    // only known to Claude Code when the theme is set explicitly).
    purple = (await lightTheme($)) ? PURPLE_LIGHT : PURPLE_DARK
    if (markOn && !waveSvg) {
      try {
        const source = await $.fs.read(`${$.plugin.root}/assets/icon-lavender.svg`)
        if (typeof source === 'string' && source.trimStart().startsWith('<svg')) waveSvg = source
      } catch {
        // the text mark then stands in on the desktop too
      }
    }
    await bindSession($)
    await readPhase($)
    await refreshSnapshot($)
    await $.command.register({
      name: 'cognee-dataset',
      description: 'Switch this session to another Cognee dataset; without a name, list them.',
      argumentHint: '[name]',
    })
    await $.command.register({
      name: 'cognee-tour',
      description: 'Replay the short tour of the Cognee status row.',
    })
    if (toolsOn) {
      await $.tool.register({
        name: 'search',
        description:
          'Search Cognee memory: the permanent knowledge graph of this session\'s dataset (scope "graph", the default), ' +
          'or the indexed repository\'s code graph (scope "code"). Memory is already recalled automatically on every prompt; ' +
          'call this for a deeper or differently worded search, for more results, or to read another dataset by its id. ' +
          'Returns the matching memories as text; an empty result from a reachable server is authoritative.',
        inputSchema: {
          type: 'object',
          properties: {
            query: { type: 'string', description: 'What to look for, as a question or a few keywords.' },
            top_k: { type: 'integer', minimum: 1, maximum: 50, default: 10, description: 'How many results to return.' },
            scope: { type: 'string', enum: ['graph', 'code'], default: 'graph', description: 'graph: memory; code: the indexed repository.' },
            dataset_id: {
              type: 'string',
              description: 'Read another dataset by its UUID (from the "Other Cognee datasets" hint). Leaves the session on the active dataset.',
            },
          },
          required: ['query'],
        },
      })
      await $.tool.register({
        name: 'remember',
        description:
          'Store what the person asked to remember in Cognee memory (the active dataset\'s knowledge graph): ' +
          'a preference, a correction, a fact about them, a decision, a note. Use it when they say "remember this", ' +
          '"save this to memory" or "store this file", not on your own initiative: the session is captured automatically, ' +
          'routine tool calls included, so do not store those or your working notes. ' +
          'Category "user" for their preferences and facts about them; "project" for facts about the codebase, docs and decisions; ' +
          '"agent" only when they ask you to keep a finding of yours. Pass "file" instead of "content" to store a file they named ' +
          '(code then rides the code path). Each call runs a cognify on their key: a session allows ' +
          `${REMEMBER_SESSION_CALLS} calls and ${REMEMBER_SESSION_BYTES} bytes in all; past that the cognee-remember skill is the way.`,
        inputSchema: {
          type: 'object',
          properties: {
            content: { type: 'string', description: 'The text to remember.' },
            category: { type: 'string', enum: ['user', 'project', 'agent'], default: 'user' },
            file: { type: 'string', description: 'A file path to store instead of inline content.' },
          },
        },
      })
    }
    // A session start still connecting or bootstrapping: watch it land, and
    // keep retaking the snapshot until the connection is confirmed.
    warmupUntil = (await $.clock.now()) + WARMUP_MS
    await startPoll($, snapshot?.state === 'ok' && !active ? POLL_WINDOW_MS : WARMUP_MS)
    // next(e) runs the plugin's SessionStart hook, which writes the launch
    // record the dataset listing reads: the listing starts after it, not
    // awaited (the session must not wait on a slow server). On a Desktop app
    // that reports no surface yet the band's first draw would start it too.
    const started = await next(e)
    if (bandOn) void loadDatasets($)
    return started
  })

  on('command.run', { command: 'cognee-tour' }, $ => {
    tourChecked = true
    tourStep = 0
    redraw($)
    return { text: 'Cognee: the tour is on the status row above the prompt.' }
  })

  // /cognee-dataset <name> switches from the prompt, on every surface; alone
  // it lists the datasets the picker offers and opens the terminal's picker.
  on('command.run', { command: 'cognee-dataset' }, async ($, e) => {
    const name = e.args.trim()
    if (!name) {
      await relistDatasets($)
      if (switchTargets.length === 0) return { text: 'Cognee: no datasets listed (is memory connected?).' }
      const rows = switchTargets.map(r => (r.current ? `  * ${r.name} (current)` : `  - ${r.name}`)).join('\n')
      pickerOpen = true
      redraw($)
      return {
        text: `Cognee datasets you can write to:\n${rows}\n\n/cognee-dataset <name> switches this session; the picker on the status row is open too (ctrl+x tab reaches it, arrows move, Enter picks).`,
      }
    }
    const line = await switchDataset($, name)
    return { text: `Cognee: ${line}.` }
  })

  // The model's direct line to memory: the same wrappers the skills run,
  // without the skill file and the Bash row. A registered tool answers with
  // the text the model reads (the engine takes a string or an array, never an
  // object); a failure is a deny, which the model reads as an error result.
  on('tool.call', { tool: 'mcp__cognee-memory__search' }, async ($, e) => {
    const query = asText(e.query).trim()
    if (!query) return { deny: 'query is required' }
    const topK = Math.max(1, Math.min(50, toNumber(e.top_k) ?? 10))
    const scope = e.scope === 'code' ? '--code' : '--graph'
    const datasetId = asText(e.dataset_id).trim()
    setActive($, activity('searching'))
    let shown
    try {
      const ran = await runSearch($, query, [String(topK), scope, ...(datasetId ? ['--dataset-id', datasetId] : [])])
      shown = ran ? formatSearch(ran.stdout, ran.exitCode) : formatSearch('', 1)
    } finally {
      clearActive($)
    }
    const where = datasetId ? `dataset ${datasetId}` : scope === '--code' ? 'the code graph' : 'the active dataset'
    if (shown.isError) return { deny: shown.text }
    return { result: `${plural(shown.count, 'result')} from ${where}.\n\n${shown.text}` }
  }).catch($ => {
    clearActive($)
    return { deny: 'Cognee search failed inside the plugin; try the cognee-search skill.' }
  })

  on('tool.call', { tool: 'mcp__cognee-memory__remember' }, async ($, e) => {
    const content = asText(e.content).trim()
    const file = asText(e.file).trim()
    if (!content && !file) return { deny: 'content or file is required' }
    if (content.length > REMEMBER_MAX_CHARS) {
      return {
        deny: `content is ${content.length} characters; the remember tool takes up to ${REMEMBER_MAX_CHARS}. Store a longer text with the cognee-remember skill.`,
      }
    }
    if (file) {
      const stat = await $.fs.stat(file)
      if (stat.kind !== 'file') return { deny: `${file} is not a file` }
      if (stat.size > REMEMBER_MAX_FILE_BYTES) {
        return {
          deny: `${file} is ${stat.size} bytes; the remember tool takes files up to ${REMEMBER_MAX_FILE_BYTES}. Store a larger file with the cognee-remember skill.`,
        }
      }
    }
    const size = file ? (await $.fs.stat(file)).size : content.length
    if (rememberCalls >= REMEMBER_SESSION_CALLS || rememberBytes + size > REMEMBER_SESSION_BYTES) {
      return {
        deny:
          `this session has used its remember allowance (${rememberCalls} of ${REMEMBER_SESSION_CALLS} calls, ` +
          `${rememberBytes} of ${REMEMBER_SESSION_BYTES} bytes). Store it with the cognee-remember skill if the person asked for it.`,
      }
    }
    const nodeSet = NODE_SETS[asText(e.category)] ?? 'user_context'
    // Counted before the call: a call that was cut off may have cost as much
    // as one that answered.
    rememberCalls += 1
    rememberBytes += size
    setActive($, activity('remembering'))
    let shown
    try {
      const args = file ? ['--file', file, '--node-set', nodeSet] : [content, '--node-set', nodeSet]
      const ran = await runWrapper($, 'cognee-remember.sh', args, REMEMBER_TIMEOUT_MS)
      if (ran === 'timeout') {
        // The POST may have landed; a resubmit would pay for a second cognify.
        shown = {
          text: `Cognee remember gave no answer within ${REMEMBER_TIMEOUT_MS / 1000} s; the write may have landed. Do not resubmit the same text: search for it first.`,
          isError: true,
        }
      } else shown = ran ? formatRemember(ran.stdout, ran.exitCode, snapshot?.dataset ?? '') : formatRemember('', 1, '')
    } finally {
      clearActive($)
    }
    if (shown.isError) return { deny: shown.text }
    return { result: shown.text }
  }).catch($ => {
    clearActive($)
    return { deny: 'Cognee remember failed inside the plugin; nothing was stored. Try the cognee-remember skill.' }
  })

  // Fires at each prompt, before the turn starts; next(e) runs the plugin's
  // recall hook (UserPromptSubmit), whose header "Cognee memory: recall N
  // memory ..." may come back in the context. The marker the hook wrote is
  // the fallback. A /clear or /resume changes the session id, so rebind here.
  on('prompt.submit', async ($, e, next) => {
    const id = await $.session.id()
    if (id !== sessionId) await bindSession($)
    setActive($, activity('recalling'))
    let ran
    try {
      ran = await next(e)
    } finally {
      clearActive($)
    }
    if (ran && 'drop' in ran) return ran
    await readPhase($)
    // Not awaited: a process spawn on the path from prompt to answer only
    // updates the row, and the poll below refreshes it a second later anyway.
    void refreshSnapshot($)
    // A listing that never landed gets another go once the person is back at
    // the prompt: the attempts start over, so the cap is per prompt rather
    // than per session.
    if (!datasetsListed && !datasetsListing) {
      if (datasetsAttempts >= LIST_MAX_ATTEMPTS) {
        datasetsAttempts = 0
        datasetsRetryAt = 0
      }
      void loadDatasets($)
    }
    // The turn's tool calls are saved as they happen; watch for them.
    await startPoll($, POLL_WINDOW_MS)
    return ran
  }).catch(($, e, next) => next(e))

  // Each tool call's PostToolUse hook saves a trace in the background once the
  // tool ran; next(e) is that run. Watch the marker for the save landing.
  on('tool.call', async ($, e, next) => {
    const ran = await next(e)
    await startPoll($, POLL_WINDOW_MS)
    return ran
  }).catch(($, e, next) => next(e))

  // Compaction runs the plugin's PreCompact hook (the memory anchor) inside next(e).
  on('session.compact', async ($, e, next) => {
    setActive($, activity('anchoring'))
    try {
      return await next(e)
    } finally {
      clearActive($)
      await readPhase($)
    }
  }).catch(($, e, next) => next(e))

  // /clear and /resume end the conversation and start another under a new id,
  // with no session.start: forget what was shown, and let the next prompt rebind.
  on('session.end', async ($, e, next) => {
    if (e.reason === 'clear' || e.reason === 'resume') {
      if (active) clearActive($)
      result = null
      snapshot = null
      namingDataset = false
      pickerOpen = false
      sessionId = ''
      rememberCalls = 0
      rememberBytes = 0
      // The next session has a launch record of its own: list again for it.
      resetDatasets()
    }
    return next(e)
  })

  // The Stop hook has started saving the answer by now; show it as it lands.
  // Never for a subagent's turn. The credit line inside an answer that used
  // memory is the agent's own, asked for by the recall hook.
  on('turn.complete', async ($, e, next) => {
    const ran = await next(e)
    if (e.agentId) return ran
    setActive($, activity('saving'))
    activeSince = await $.clock.now()
    await startPoll($, POLL_WINDOW_MS)
    return ran
  })

  on('ui.close', { id: FIX_PANE }, ($, e, next) => {
    fixOpen = false
    return next(e)
  }).catch(($, e, next) => next(e))

  // The fix dialog: the reason in plain words, the steps, the doctor's report
  // on request, and a field for the one value that fixes a key or URL problem.
  on('ui.render', { component: 'Pane', requestId: FIX_PANE }, async ($, e) => {
    const { Box, Text, Markdown, Button, Input, Code } = $.ui.resolve(e) as any
    const guide = fixGuide(fixReason)
    const rows: RenderElement[] = []
    rows.push(Text({ bold: true, color: 'red', children: [`✕ ${guide.title}`] }))
    rows.push(Text({ dimColor: true, children: [`reason: ${fixReason || 'unknown'}`] }))
    rows.push(Markdown({ text: `${guide.what}\n\n${guide.steps.map((step, i) => `${i + 1}. ${step}`).join('\n')}` }))
    if (guide.field && Input) {
      rows.push(Text({ children: [''] }))
      rows.push(
        Input({
          key: 'secret',
          label: `${guide.field.name} `,
          placeholder: guide.field.placeholder,
          submitLabel: 'save',
          onSubmit: (value: string) => void saveEnvValue($, guide.field!.name, value),
        }),
      )
    }
    if (fixNote) rows.push(Text({ color: fixNote.isError ? 'red' : 'green', children: [fixNote.text] }))
    rows.push(Text({ children: [''] }))
    rows.push(
      Box({
        flexDirection: 'row',
        children: [
          Button({ key: 'doctor', label: doctorRunning ? 'running doctor…' : 'run doctor', variant: 'primary', onPress: () => void runDoctor($) }),
          Text({ children: ['   '] }),
          Button({ key: 'closeFix', label: 'close', plain: true, dimColor: true, onPress: () => void $.ui.close({ id: FIX_PANE }) }),
        ],
      }),
    )
    if (doctorOutput) rows.push(Code({ source: doctorOutput }))
    return Box({ flexDirection: 'column', children: rows })
  })

  // The status row, above the prompt on the desktop and in a terminal (where
  // the settings-file status line is told to stay quiet for this session), the
  // dataset name a picker: open on the desktop; in a terminal a button that
  // opens it (or /cognee-dataset alone). Once open, ctrl+x tab or a click
  // reaches it; arrows move, Enter picks, Escape returns to the prompt.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const theirs = await next(e)
    if (!bandOn || e.props.hasSurvey) return theirs
    if (e.surface !== 'desktop' && e.surface !== 'terminal') return theirs
    const els = $.ui.resolve(e) as any
    const { Box, Text, Select, Input, Button, Link } = els
    const snap = snapshot
    // The row is laid out in segments that are never split: each is a Box
    // that does not shrink, with its " · " separator inside, so a narrow
    // terminal wraps whole segments onto a second line (or drops the least
    // useful ones first, below) instead of breaking "agent_sessions" into
    // "agent_sess / ions". `width` is the segment's estimate in cells; `drop`
    // ranks what goes when the row is too wide, 0 being never.
    type Segment = { width: number; drop: number; children: RenderElement[] }
    const segments: Segment[] = []
    const segment = (width: number, drop: number, ...children: RenderElement[]) => segments.push({ width, drop, children })
    if (!datasetsListed && !datasetsListing && datasetsAttempts < LIST_MAX_ATTEMPTS && datasetsRetryAt === 0) void loadDatasets($)
    void startTourIfFirstRun($)
    const current = switchTargets.find(r => r.current)?.name ?? snap?.dataset ?? ''
    // A picker whenever the server listed anything. The active dataset may be
    // missing from the writable list (read-only, or created since the
    // listing); it heads the options anyway, so the row still reads and the
    // other datasets stay reachable.
    const canPick = switchTargets.length > 0
    const options = current && !switchTargets.some(r => r.name === current) ? [{ name: current, id: '', current: true }, ...switchTargets] : switchTargets
    const picker = () =>
      Select({
        key: 'dataset',
        options: [...options.map(r => ({ value: r.name, label: r.name })), { value: NEW_DATASET, label: NEW_DATASET }],
        value: current,
        ...(e.surface === 'terminal' ? { autoFocus: true as const } : {}),
        onSelect: (value: string) => {
          pickerOpen = false
          if (value === NEW_DATASET) {
            namingDataset = true
            redraw($)
            return
          }
          if (value !== current) void switchDataset($, value)
          else redraw($)
        },
      })
    // The terminal's closed form: the name as a button that opens the picker.
    const closedPicker = () =>
      Button({
        key: 'dataset',
        label: `${current} ▾`,
        plain: true,
        onPress: () => {
          pickerOpen = true
          redraw($)
        },
      })
    // The text field the "new dataset…" entry turns the picker into. Enter
    // with a name switches there (creating it); Enter on nothing, or the ✕,
    // brings the picker back.
    const namer = () =>
      Box({
        flexDirection: 'row',
        children: [
          Input({
            key: 'newDataset',
            placeholder: 'new dataset name',
            submitLabel: 'create',
            autoFocus: true,
            onSubmit: (value: string) => {
              namingDataset = false
              if (value.trim()) void switchDataset($, value)
              else redraw($)
            },
          }),
          Text({ children: [' '] }),
          Button({
            key: 'cancelNew',
            label: '✕',
            plain: true,
            dimColor: true,
            onPress: () => {
              namingDataset = false
              redraw($)
            },
          }),
        ],
      })


    // The health slot, as the text line draws it: the spinner while something
    // runs, a green ● once the server is confirmed up and authenticated, the
    // red verdict with its button on a failure, and a dim ○ while nothing is
    // known yet (warming up, no marker). One segment with the mark and the
    // wordmark after it: the official waveform as an inline SVG where the
    // surface draws one (the desktop; the terminal's table lists an Svg too,
    // but draws it as an empty box, so a terminal gets the wordmark alone),
    // then "cognee" bold in the brand lavender.
    const badge: RenderElement[] = []
    let badgeWidth = 2
    if (active) {
      badge.push(Text({ color: 'yellow', children: [`${FRAMES[frame % FRAMES.length] ?? '⠋'} `] }))
    } else if (snap?.state === 'ok') {
      badge.push(Text({ color: 'green', bold: true, children: ['● '] }))
    } else if (snap?.state === 'fail') {
      // The verdict stays red text: a button's text takes the surface's own
      // colour on the desktop, whatever is put inside it. The small button
      // right after it is the press that opens the fix dialog.
      badge.push(Text({ color: 'red', bold: true, children: [`✕ (${snap.reason})`] }))
      badge.push(Text({ children: [' '] }))
      badge.push(Button({ key: 'fix', label: 'see fix', variant: 'secondary', onPress: () => void openFix($, snap.reason) }))
      badge.push(Text({ children: [' '] }))
      badgeWidth = 4 + snap.reason.length + 11
    } else {
      badge.push(Text({ dimColor: true, children: ['○ '] }))
    }
    if (markOn && waveSvg && els.Svg && e.surface === 'desktop') {
      badge.push(els.Svg({ source: waveSvg, alt: 'cognee', width: 18, height: 18 }))
      badge.push(Text({ children: [' '] }))
      badgeWidth += 3
    }
    badge.push(Text({ bold: true, color: purple, children: ['cognee '] }))
    segment(badgeWidth + 7, 0, ...badge)
    // The dataset: a control when there is a list, the name otherwise.
    if (canPick) {
      const open = e.surface !== 'terminal' || pickerOpen
      segment(namingDataset ? 26 : current.length + 2, 0, namingDataset ? namer() : open ? picker() : closedPicker())
    } else {
      const name = snap?.dataset ?? 'memory'
      segment(name.length, 0, Text({ children: [name] }))
    }
    const mode = snap?.mode ?? '…'
    segment(3 + mode.length, 0, Text({ children: [' · '] }), Text({ bold: true, color: purple, children: [mode] }))
    if (snap?.credits) {
      // As on the text line: the balance green, red once exhausted or a
      // request was refused; the last-operation cost after it at normal
      // weight. The text line's "top up: <url>" becomes a link here, the way
      // out one click away (an anchor on the desktop, OSC 8 in a terminal).
      // A balance that is out, or a refusal, is something to act on: it is
      // never dropped for width; a healthy balance goes before the activity,
      // the last-operation cost before both.
      const [balance = '', ...rest] = snap.credits.split(' · ')
      segment(3 + balance.length, snap.credits_exhausted ? 0 : 2, Text({ color: snap.credits_exhausted ? 'red' : 'green', children: [` · ${balance}`] }))
      for (const part of rest) {
        if (part.startsWith('top up:') && snap.billing_url && Link) {
          segment(9, 0, Text({ children: [' · '] }), Link({ href: snap.billing_url, label: 'top up' }))
        } else {
          segment(3 + part.length, 3, Text({ children: [` · ${part}`] }))
        }
      }
    }
    // What memory is doing, or did last. A failed `ready` result is not
    // shown next to a green ●: the verdict predates the connection marker
    // (a boot that finished after the hook gave up), and the marker wins.
    const shownResult =
      result && result.age * 1000 < HIDE_AFTER_MS && !(result.phase === 'ready' && !result.ok && snap?.state === 'ok') ? result : null
    if (active) {
      segment(3 + active.text.length, 1, Text({ color: 'yellow', children: [` · ${active.text}`] }))
    } else if (shownResult) {
      // At rest the result reads in the brand colour and bold.
      segment(
        3 + shownResult.text.length,
        1,
        Text({ color: purple, bold: true, dimColor: shownResult.age * 1000 >= DIM_AFTER_MS, children: [` · ${shownResult.text}`] }),
      )
    }
    if (snap?.update) segment(3 + snap.update.length, 0, Text({ color: 'yellow', children: [` · ${snap.update}`] }))

    // Fit: while the row runs past the band's width, the segment that is
    // least worth its cells goes first (the highest `drop`, the last of equals).
    // What must stay (drop 0) wraps as whole segments instead.
    const columns = e.props.bodyColumns
    let shown = segments
    const widthOf = (list: Segment[]) => list.reduce((sum, s) => sum + s.width, 0)
    while (widthOf(shown) > columns) {
      let victim = -1
      for (let i = 0; i < shown.length; i += 1) {
        if (shown[i]!.drop > 0 && (victim < 0 || shown[i]!.drop >= shown[victim]!.drop)) victim = i
      }
      if (victim < 0) break
      shown = shown.filter((_, i) => i !== victim)
    }
    const parts = shown.map(s => Box({ flexDirection: 'row', flexShrink: 0, children: s.children }))
    const row = Box({ flexDirection: 'row', flexWrap: 'wrap', width: columns, children: parts })
    const rows: RenderElement[] = [row]
    const hint = TOUR_STEPS[tourStep]
    if (hint !== undefined) {
      // The hint wraps as a paragraph in the room the buttons leave; the
      // buttons keep their cells and never split.
      const last = tourStep === TOUR_STEPS.length - 1
      rows.push(
        Box({
          flexDirection: 'row',
          width: columns,
          children: [
            Box({ flexGrow: 1, flexShrink: 1, children: [Text({ dimColor: true, wrap: 'wrap', children: [hint] })] }),
            Box({
              flexDirection: 'row',
              flexShrink: 0,
              children: [
                Text({ children: ['  '] }),
                Button({
                  key: 'tourNext',
                  label: last ? 'done' : 'next',
                  onPress: () => {
                    if (last) void endTour($, true)
                    else {
                      tourStep += 1
                      redraw($)
                    }
                  },
                }),
                Text({ children: [' '] }),
                Button({ key: 'tourSkip', label: 'skip', plain: true, dimColor: true, onPress: () => void endTour($, false) }),
              ],
            }),
          ],
        }),
      )
    }
    if (drawsSomething(theirs)) rows.push(theirs as RenderElement)
    return rows.length === 1 ? row : Box({ flexDirection: 'column', children: rows })
  })

  // While Claude works: "Thinking · 🧠 recalling…". On the desktop for every
  // phase; in a terminal only for the async phases, whose settings hooks show
  // no statusMessage of their own there.
  // The credit line under an answer that used memory ("> 🧠 Cognee memory
  // [m4]: …", written by the model on the recall hook's instruction): the
  // brain gives way to the wave mark on the desktop and to the display
  // logotype in a terminal, which draws no Svg. The engine still draws the
  // reply, with the line taken off its text; the line is drawn under it as a
  // row of its own. The stored message is never changed (ctrl+o, the Stop
  // hook's check).
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    if (!markOn || e.props.isSummary) return next(e)
    if (e.surface !== 'desktop' && e.surface !== 'terminal') return next(e)
    const split = splitCredit(e.props.text)
    if (!split) return next(e)
    const els = $.ui.resolve(e) as any
    const { Box, Text } = els
    let mark: RenderElement
    if (e.surface === 'desktop') {
      if (!waveSvg || !els.Svg) return next(e)
      mark = els.Svg({ source: waveSvg, alt: 'cognee', width: 30, height: 30 })
    } else {
      mark = Box({
        flexDirection: 'column',
        flexShrink: 0,
        // Centred on the note when it runs longer than the mark; the rule on
        // the left still spans the whole row.
        alignSelf: 'center',
        // Braille dots are thin: bold and the darker purple on every theme
        // (the lavender washes out on a light background).
        children: LOGOTYPE.map(row => Text({ color: PURPLE_LIGHT, bold: true, children: [row] })),
      })
    }
    const theirs = await next({ ...e, props: { ...e.props, text: split.body } })
    // Set off from the reply the way a quote is: two blank lines above and a
    // rule down its left edge (a one-cell box with a background; a row's
    // items stretch to its height), so it reads as the plugin's note, not
    // the answer.
    const credit = Box({
      flexDirection: 'row',
      marginTop: e.surface === 'desktop' ? 2 : 1,
      children: [
        Box({ width: 1, flexShrink: 0, backgroundColor: 'gray' }),
        Text({ children: ['  '] }),
        mark,
        Text({ children: [e.surface === 'desktop' ? ' ' : '  '] }),
        // The mark says whose memory it is; the tags are bookkeeping the Stop
        // hook reads from the stored line, not something to show. One text,
        // so the note wraps as one paragraph beside the mark; lighter than the
        // reply, as a quote reads.
        Text({ dimColor: true, children: [split.note] }),
      ],
    })
    const rows: RenderElement[] = []
    if (drawsSomething(theirs)) rows.push(theirs as RenderElement)
    rows.push(credit)
    return Box({ flexDirection: 'column', children: rows })
  }).catch(($, e, next) => next(e))

  // The transcript row of a call to one of the plugin's tools. The engine
  // labels every registered tool "<plugin> - <tool> (MCP)" from its wire
  // name; that reads as a separate server to a person, so the row names the
  // tool plainly. The call and its result are untouched.
  on('ui.render', { component: 'ToolUse' }, ($, e, next) => {
    const label = TOOL_ROW_LABELS[e.props.tool]
    if (!label) return next(e)
    return next({ ...e, props: { ...e.props, tool: label } })
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Spinner' }, async ($, e, next) => {
    if (!spinnerOn || !active) return next(e)
    if (e.surface !== 'desktop' && !ASYNC_PHASES.includes(active.phase) && !OWN_PHASES.includes(active.phase)) return next(e)
    return next({ ...e, props: { ...e.props, suffix: ` · ${active.text}` } })
  })
}
