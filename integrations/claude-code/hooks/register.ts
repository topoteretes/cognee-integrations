// Cognee memory: the in-process layer of the plugin.
//
// The settings hooks in hooks.json run the memory pipeline (recall, capture,
// learning from the session, sync). This module only draws what they report.
// It starts no program, opens no connection and writes no file: it reads the
// files the plugin's scripts write under ~/.cognee-plugin/claude-code, and
// keeps a few values in its own store for the scripts to read.
//
//   * the status row, above the prompt on every surface, from
//     status/<session>.json, which every hook and each run of the settings
//     status line keep current. The module records in its store that it
//     draws the row (`band:<session>`), and the settings status line then
//     prints nothing for that session, so the row is never drawn twice; with
//     the `band` option off the settings status line is back below;
//   * live activity: the per-session marker the hooks write
//     (phase/<session>.json), shown on the row and after the spinner (the
//     answer's own credit line is the agent's, from the recall hook's
//     instruction);
//   * the dataset name on the row is a picker: its list and the recall mode
//     come from mod/<session>.json. Choosing another writable dataset, or
//     typing the name of a new one, saves the pick in the store
//     (`pick:<session>`); the plugin's prompt hook switches the session
//     before it recalls, so the pick takes effect with the next message
//     (`/cognee-dataset <name>` does the same from the prompt);
//   * a `see fix` button beside a red ✕ on the row: a dialog pane that says
//     what is wrong in plain words and how to fix it;
//   * a first-run tour: three dim hint lines under the row, each about the
//     thing above it, shown once per machine (the store remembers) and again
//     on `/cognee-tour`.
//
// Nothing here runs on a fixed timer while the session is idle: the files
// are read on the events the hooks fire, and polled once a second only while
// an operation is running or for a short window after one was started.
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
  // 'auto' | 'pinned', from local state; absent from an older renderer.
  recall?: string
  mode: string
  credits: string
  credits_exhausted?: boolean
  billing_url?: string
  update: string
  text: string
  // Whether the row should use the light-background purple; absent from an
  // older renderer (then the dark-background one stands).
  light?: boolean
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
// How long after a /clear or /resume the band watches for the new session id.
const ADOPT_MS = 30_000
const WARMUP_RETRY_TICKS = 5
const BOOT_RETRY_TICKS = 30
// Mirrors the renderer: an active phase older than this is a hook that died.
const STALE_MS = 90_000
const DIM_AFTER_MS = 60_000
// A result older than this is no longer news: it leaves the row.
const HIDE_AFTER_MS = 300_000
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
// Phases the module started itself. None are left: it starts no work.
const OWN_PHASES: string[] = []

// A listing that fails is tried again: it runs before the SessionStart hook
// has written the launch record it needs, or against a server that is up but
// not answering. The wait grows with each attempt (5 s, 10 s, ...), and after
// the last the name stays plain text until the next prompt, /cognee-dataset
// or a switch starts the attempts over. A boot the hook handed to its worker
// is not an attempt: the listing waits for the `setting_up` phase to end.
const LIST_RETRY_MS = 5_000
const LIST_MAX_ATTEMPTS = 5

// ── module state (reset on a reload, which is fine: it is all redrawn) ──────
let sessionId = ''
// The id a /clear or /resume ended, and the watch for the id that follows it:
// the engine sends no session.start for the new conversation, so the band
// adopts it on its own instead of drawing an unbound row until the next prompt.
let endedSessionId = ''
let adopting = false
let adoptWatch: { cancel: () => void } | null = null
// Until when the row reads "connecting…" after a /clear or /resume while the
// new conversation is not bound yet (the adopt watch's own window).
let adoptUntil = 0
let stateRoot = ''
// The pick waiting for the prompt hook: when it was saved, and how many
// prompts have gone by since without the hook reporting on it.
let pickAt = 0
let pickPrompts = 0
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
// Whether a terminal has drawn the band, the only surface whose spinner
// frames are worth a redraw (see startSpin).
let terminalBand = false
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
// The Cognee mark, the same drawing as assets/icon-lavender.svg (a test keeps
// the two equal). Held here so the module opens no file to draw it.
const WAVE_SVG = `<svg width="1516" height="1516" viewBox="0 0 1516 1516" fill="none" xmlns="http://www.w3.org/2000/svg">
<g clip-path="url(#clip0_9804_26551)">
<path d="M869.871 1243.28C817.666 1243.28 775.207 1198.06 775.207 1142.48V373.319C775.207 359.61 766.206 350.058 758.137 350.058C750.067 350.058 741.066 359.61 741.066 373.319V1142.48C741.066 1198.06 698.607 1243.28 646.403 1243.28C594.198 1243.28 551.74 1198.06 551.74 1142.48V540.797C551.74 527.089 542.739 517.536 534.669 517.536C526.599 517.536 517.599 527.089 517.599 540.797V919.174C517.599 974.752 475.14 1019.97 422.935 1019.97C370.731 1019.97 328.272 974.752 328.272 919.174V736.188C328.272 680.61 373.524 635.391 429.143 635.391C432.929 635.391 436.53 635.95 440.006 636.942V540.797C440.006 485.219 482.465 440 534.669 440C586.874 440 629.333 485.219 629.333 540.797V1142.48C629.333 1156.19 638.333 1165.74 646.403 1165.74C654.473 1165.74 663.473 1156.19 663.473 1142.48V373.319C663.473 317.741 705.932 272.521 758.137 272.521C810.341 272.521 852.8 317.741 852.8 373.319V1142.48C852.8 1156.19 861.801 1165.74 869.871 1165.74C877.94 1165.74 886.941 1156.19 886.941 1142.48V540.797C886.941 485.219 929.4 440 981.604 440C1033.81 440 1076.27 485.219 1076.27 540.797V636.942C1079.74 635.95 1083.34 635.391 1087.13 635.391C1142.75 635.391 1188 680.61 1188 736.188V919.174C1188 974.752 1145.54 1019.97 1093.34 1019.97C1041.13 1019.97 998.675 974.752 998.675 919.174V540.797C998.675 527.089 989.674 517.536 981.604 517.536C973.535 517.536 964.534 527.089 964.534 540.797V1142.48C964.534 1198.06 922.075 1243.28 869.871 1243.28ZM1076.27 711.377V919.174C1076.27 932.883 1085.27 942.435 1093.34 942.435C1101.41 942.435 1110.41 932.883 1110.41 919.174V736.188C1110.41 723.597 1099.73 712.928 1087.13 712.928C1083.34 712.928 1079.74 712.369 1076.27 711.377ZM440.006 711.377C436.53 712.369 432.929 712.928 429.143 712.928C416.542 712.928 405.865 723.597 405.865 736.188V919.174C405.865 932.883 414.866 942.435 422.935 942.435C431.005 942.435 440.006 932.883 440.006 919.174V711.377Z" fill="#BC9BFF"/>
</g>
<defs>
<clipPath id="clip0_9804_26551">
<rect width="859.73" height="970.754" fill="white" transform="translate(328.272 272.521)"/>
</clipPath>
</defs>
</svg>`
let purple = PURPLE_DARK
let bandOn = true
let spinnerOn = true

// The dataset picker on the band.
type DatasetRow = { name: string; id: string; current: boolean }
let switchTargets: DatasetRow[] = []
let switching = false
// Where a running switch is going: the row reads "<current> → <target>" until
// it lands, instead of a picker already showing the pick.
let switchingTo = ''
// The picker's last entry: picking it turns the picker into a text field for
// a dataset that does not exist yet (the switch script creates it). Spaces
// make the value one no dataset can be named, so it never collides.
const NEW_DATASET = '+ new dataset…'
// The picker's first entry: recall from the best-matching dataset (dataset
// routing, a cognee 1.6.6 server) instead of only the one saves go to. Spaces keep it
// apart from any dataset name, like NEW_DATASET.
const AUTO_RECALL = 'auto (best-matching dataset)'
// The placeholder entry while the datasets are being listed; a pick of it
// does nothing. Spaces keep it apart from any dataset name.
const LOADING_DATASETS = 'loading datasets…'
// Recall mode in effect from the last listing: 'auto' (routed) or 'pinned' to
// the active dataset; '' until listed. Anything but 'auto' reads as restricted.
let recallMode: '' | 'auto' | 'pinned' = ''
// What the same listing says about recall, in switch-dataset.py's own words
// (its `message`); '' until listed or when an older script sent none.
let recallMessage = ''
let namingDataset = false

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
// guide is plain words first, then steps. The dialog runs nothing and writes
// nothing: the steps say what to run or which line to add.
const FIX_PANE = 'cognee-fix'
type FixGuide = { title: string; what: string; steps: string[] }
const FIX_GUIDES: Record<string, FixGuide> = {
  incorrect_cognee_api_key: {
    title: 'The memory server rejected the plugin\'s API key',
    what: 'The key this plugin sends to the Cognee server (`COGNEE_API_KEY`) is missing, wrong or expired, so nothing can be recalled or saved.',
    steps: [
      '**Cloud:** put the API key from your Cognee Cloud account in `~/.cognee/.env` as `COGNEE_API_KEY=ck_…`. A key exported in your shell wins over the file.',
      '**Local:** delete `~/.cognee-plugin/api_key.json` and start a new session; the plugin mints a fresh key from the local server.',
      'With a plugin identity (`COGNEE_PLUGIN_IDENTITY`), run the doctor (ask Claude to run `scripts/cognee-doctor.sh` from this plugin): it says whether the cached identity credential was the one rejected.',
      'A new prompt tries the new key; a new session applies it everywhere.',
    ],
  },
  incorrect_llm_api_key: {
    title: 'The local server cannot reach its LLM',
    what: 'The Cognee server on this machine builds memory with an LLM, and the key it uses (`LLM_API_KEY`) is missing or the provider rejected it. The server itself is fine.',
    steps: [
      'Put a working provider key in `~/.cognee/.env` as `LLM_API_KEY=…` (and `LLM_PROVIDER` if not OpenAI).',
      'Or use your Claude subscription instead: remove `LLM_API_KEY` from `~/.cognee/.env` and keep the `claude` CLI on PATH; the next session routes the server\'s LLM calls through it.',
      'Start a new session: the server reads its key at start.',
    ],
  },
  missing_cognee_base_url: {
    title: 'Cloud mode is forced, but no server URL is configured',
    what: '`COGNEE_BACKEND=cloud` is exported in this shell, and no `COGNEE_BASE_URL` is set anywhere, so there is no server to talk to.',
    steps: [
      'Put your Cognee Cloud URL in `~/.cognee/.env` as `COGNEE_BASE_URL=…`, and make sure `COGNEE_API_KEY` is set too.',
      'Or go local for this terminal: `unset COGNEE_BACKEND`, then start a new session.',
    ],
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
      'Run the doctor (ask Claude to run `scripts/cognee-doctor.sh` from this plugin): it shows the URL in use, the mode and the health check.',
      'The dot turns green again on the next successful recall.',
    ],
  },
  server_error: {
    title: 'The memory server is answering with errors',
    what: 'The server is up but replied with a server error (HTTP 5xx) to the last requests. Memory is not read or written until it recovers.',
    steps: [
      'Run the doctor (ask Claude to run `scripts/cognee-doctor.sh` from this plugin) for the health check and the server version.',
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
      'Run the doctor (ask Claude to run `scripts/cognee-doctor.sh` from this plugin) to see whether the health check answers at all.',
    ],
  },
}
const FIX_FALLBACK: FixGuide = {
  title: 'Memory reported a problem',
  what: 'The row shows a reason the plugin recorded; the doctor report has the details.',
  steps: ['Run the doctor: ask Claude to run `scripts/cognee-doctor.sh` from this plugin.', 'A new session re-checks everything.'],
}
let fixReason = ''
let fixOpen = false
// One successful listing per session, refreshed after a switch or by
// /cognee-dataset; a failed one is retried a few times (LIST_MAX_ATTEMPTS).
let datasetsListed = false
let datasetsListing = false
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
// has the model write it: "> Cognee memory: what it remembered, from the
// <dataset> dataset". Older answers cite tags, "> 🧠 Cognee memory
// [m4, m5]: …"; the tags and the brain are taken off those too.
// Returns the text without it, the tags ('' when untagged) and the note; null
// when the text does not end with one.
const CREDIT_LINE = /^(?:>\s*)?(?:🧠\s*)?Cognee memory(?: \[([^\]]+)\])?:\s*(.*)$/u

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

// "auto" names the recall mode in switch-dataset.py, so no dataset can be named so.
function isReservedName(name: string): boolean {
  return name.trim().toLowerCase() === 'auto'
}

function recallModeOf(value: unknown): '' | 'auto' | 'pinned' {
  const mode = value && typeof value === 'object' ? (value as { recall?: unknown }).recall : undefined
  return mode === 'auto' || mode === 'pinned' ? mode : ''
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
// The module starts no program and writes no file. It reads what the plugin's
// scripts write under ~/.cognee-plugin/claude-code: phase/<session>.json (the
// activity marker), status/<session>.json (the row's data) and
// mod/<session>.json (the picker's datasets and what became of a pick). What
// it has to tell the scripts goes into its own store (see markBand, switchDataset).
async function bindSession($: any): Promise<void> {
  sessionId = await $.session.id()
  const home = (await $.env.get('HOME')) || (await $.env.get('USERPROFILE')) || ''
  stateRoot = `${home}/.cognee-plugin/claude-code`
  await markBand($)
}

// Tell the settings-file status line that this session's row is drawn above
// the prompt: the renderer prints nothing for a session with this entry in
// the module's store, so the row is not shown twice. Only while the band is on.
async function markBand($: any): Promise<void> {
  if (!bandOn || !sessionId) return
  try {
    await $.store.set(`band:${sessionId}`, { at: (await $.clock.now()) / 1000 })
  } catch {
    // the status line then stays; nothing else depends on the entry
  }
}

// A file the plugin's scripts wrote, as text; '' when it is not there yet.
async function readText($: any, path: string): Promise<string> {
  try {
    const text = await $.fs.read(path)
    return typeof text === 'string' ? text : ''
  } catch {
    return ''
  }
}

// After a /clear or /resume: bind the conversation that replaced the ended
// one as soon as the engine reports its id, and set it up the way a session
// start does (the snapshot, the warm-up poll, the dataset listing).
async function adoptSession($: any): Promise<void> {
  if (adopting || sessionId) return
  adopting = true
  try {
    const id = await $.session.id()
    if (!id || id === endedSessionId || sessionId) return
    stopAdoptWatch()
    await bindSession($)
    // The warm-up starts before the first snapshot: its redraw must already
    // read as connecting, not drop to the dim ○ for a frame.
    warmupUntil = (await $.clock.now()) + WARMUP_MS
    await readPhase($)
    await refreshSnapshot($)
    await startPoll($, WARMUP_MS)
    if (bandOn) void loadDatasets($)
    redraw($)
  } catch {
    // the next prompt binds it
  } finally {
    adopting = false
  }
}

function stopAdoptWatch(): void {
  if (adoptWatch) {
    adoptWatch.cancel()
    adoptWatch = null
  }
}

function redraw($: any): void {
  $.ui.invalidate('ui.render')
}

// Nothing confirmed yet about the connection while the session is still
// coming up: the row shows the spinner and "connecting…" instead of a bare
// dim ○, which reads as nothing happening. Bounded by the warm-up (and, before
// the first snapshot, by the renderer still being tried), so a server that
// never answers settles on the ○ instead of spinning for the session. After a
// /clear or /resume the row is connecting too while the new conversation is
// being bound (at most ADOPT_MS, the adopt watch's window): the snapshot was
// dropped with the ended session and the new one's warm-up has not started.
function awaitingConnection(now: number): boolean {
  if (active) return false
  if (snapshot?.state === 'ok' || snapshot?.state === 'fail') return false
  if (now < warmupUntil) return true
  if (!sessionId && now < adoptUntil) return true
  return snapshot === null && !!sessionId
}

function startSpin($: any): void {
  if (spin) return
  spin = $.clock.every(FRAME_MS, async () => {
    frame = (frame + 1) % FRAMES.length
    // The connection wait has no hook to end it: the frame stops itself.
    if (!active && !awaitingConnection(await $.clock.now()) && spin) {
      spin.cancel()
      spin = null
    }
    // Only a terminal band animates. The desktop draws a still mark: a redraw
    // eight times a second would re-send its dataset picker, which closes it
    // and snaps it back to the active dataset while it is being used.
    if (terminalBand) $.ui.invalidate('ui.render')
  })
}

function setActive($: any, next: Activity): void {
  active = next
  startSpin($)
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

function resultKey(r: Activity | null): string {
  if (!r) return ''
  const ms = r.age * 1000
  return JSON.stringify([r.kind, r.phase, r.text, r.ok, r.count ?? null, ms >= DIM_AFTER_MS, ms >= HIDE_AFTER_MS])
}

async function readPhase($: any): Promise<void> {
  if (!sessionId || !stateRoot) return
  markerActive = false
  let marker: PhaseMarker
  try {
    marker = JSON.parse(await $.fs.read(`${stateRoot}/phase/${sessionId}.json`)) as PhaseMarker
  } catch {
    return
  }
  const now = await $.clock.now()
  const shown = pickActivity(marker, now)
  // The module's own work (a switch, a search, a remember) keeps the row until
  // it ends and clears it itself; a hook finishing meanwhile must not tick
  // "memory saved" over a switch that is still running.
  const own = !!active && (OWN_PHASES as readonly string[]).includes(active.phase)
  if (shown?.kind === 'active') {
    markerActive = true
    if (own) return
    if (!active || active.phase !== shown.phase) {
      setActive($, shown)
      activeSince = now
    }
    return
  }
  // The poll reads the marker every second; a redraw re-sends the dataset
  // picker, which on the desktop closes it and snaps it back to the active
  // dataset. Redraw only when what the row shows changes: the result, and its
  // age only where it crosses the dim and hide thresholds.
  const before = resultKey(result)
  result = shown
  if (active && !own) clearActive($)
  else if (resultKey(shown) !== before) redraw($)
}

// Poll the marker once a second while something runs, and for a short window
// after a hook fired (its async work lands within seconds). Stops by itself.
async function startPoll($: any, windowMs: number): Promise<void> {
  const now = await $.clock.now()
  pollUntil = Math.max(pollUntil, now + windowMs)
  if (poll) return
  poll = $.clock.every(POLL_MS, async () => {
    await readPhase($)
    // A file read, so it runs with every tick while the poll is open.
    await refreshSnapshot($)
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


// The row's data: the dataset, mode, connection state, credits and update
// nudge, as the terminal bar shows them. The plugin's scripts keep
// status/<session>.json current (every hook, and each run of the settings
// status line); the module only reads it.
async function refreshSnapshot($: any): Promise<void> {
  if (!sessionId || !stateRoot || snapshotInFlight) return
  snapshotInFlight = true
  try {
    const text = await readText($, `${stateRoot}/status/${sessionId}.json`)
    if (text.trim()) {
      try {
        const previous = snapshot === null ? null : JSON.stringify(snapshot)
        snapshot = JSON.parse(text) as Snapshot
        purple = snapshot?.light === true ? PURPLE_LIGHT : PURPLE_DARK
        // An unchanged snapshot must not redraw (and so reset) an open picker.
        if (JSON.stringify(snapshot) !== previous) redraw($)
      } catch {
        // a file caught mid-write: the next read has it whole
      }
    }
    await loadDatasets($)
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
  const opened = await $.ui.open({ id: FIX_PANE, title: 'Fix memory', focus: true, closeOnEscape: true, holdToasts: true, rows: 18 })
  if (!opened.isPlaced) $.ui.toast(`fix: ${opened.reason}`)
}

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
// The datasets the picker offers: the writable ones, with the recall mode, as
// the plugin's scripts last listed them (mod/<session>.json: at session start,
// after each switch, and again when the list is old). The same file says what
// became of the last pick.
async function loadDatasets($: any): Promise<void> {
  if (!sessionId || !stateRoot) return
  let bridge: Record<string, any> = {}
  try {
    bridge = JSON.parse(await readText($, `${stateRoot}/mod/${sessionId}.json`)) as Record<string, any>
  } catch {
    bridge = {}
  }
  const listing = bridge.listing
  const before = JSON.stringify([switchTargets, recallMode, recallMessage, datasetsListed])
  if (listing && typeof listing === 'object' && !listing.error) {
    switchTargets = datasetRows(listing)
    recallMode = recallModeOf(listing)
    recallMessage = asText(listing.message)
    datasetsListed = true
  }
  datasetsListing = false
  await settlePick($, bridge.pick)
  if (JSON.stringify([switchTargets, recallMode, recallMessage, datasetsListed]) !== before) redraw($)
}

async function relistDatasets($: any): Promise<void> {
  await loadDatasets($)
}

// The prompt hook reports what it did with a pick, by the time the module
// saved it. Once it has, the pending state ends and the store entry goes.
async function settlePick($: any, done: unknown): Promise<void> {
  if (!switching || !pickAt) return
  const result = done && typeof done === 'object' ? (done as Record<string, unknown>) : null
  if (!result || toNumber(result.at) !== pickAt) return
  const failed = result.state !== 'done'
  const line = asText(result.message) || (failed ? 'the switch failed' : `switched to ${asText(result.dataset) || switchingTo}`)
  $.ui.toast(failed ? `✕ ${line}` : line)
  await endPick($)
}

async function endPick($: any): Promise<void> {
  switching = false
  switchingTo = ''
  pickAt = 0
  pickPrompts = 0
  try {
    await $.store.delete(`pick:${sessionId}`)
  } catch {
    // the hook skips a pick it already handled
  }
  redraw($)
}

// Move the session to another dataset. The module starts no program: it saves
// the pick in its store, and the plugin's prompt hook switches the session
// before it recalls, so the next message is already answered from the new
// dataset. Until then the row shows the pick as pending.
async function switchDataset($: any, target: string): Promise<string> {
  const name = target.trim()
  if (!name) return 'name a dataset'
  if (switching) return 'a switch is already waiting for your next message'
  // `pin` and `auto` stay on the active dataset; an id reads as its name.
  const action = name.toLowerCase()
  const label = action === 'pin' || action === 'auto' ? '' : switchTargets.find(r => r.id === name)?.name ?? name
  const at = (await $.clock.now()) / 1000
  try {
    await $.store.set(`pick:${sessionId}`, { target: name, at })
  } catch {
    return 'could not save the pick; use the cognee-switch-datasets skill'
  }
  switching = true
  switchingTo = label
  pickAt = at
  pickPrompts = 0
  const line =
    action === 'auto'
      ? 'recall goes back to auto with your next message'
      : action === 'pin'
        ? 'recall is restricted to the active dataset with your next message'
        : `switching to ${label} with your next message`
  $.ui.toast(line)
  redraw($)
  return line
}

// A pick the prompt hook never reported on (the hook could not see the
// module's store): say so once and point at the skill, rather than leave the
// row pending for good.
async function givePickUp($: any): Promise<void> {
  if (!switching || !pickAt) return
  pickPrompts += 1
  if (pickPrompts < 2) return
  $.ui.toast('✕ the dataset pick did not reach the plugin; use the cognee-switch-datasets skill')
  await endPick($)
}

export const register: Register = (on, options) => {
  markOn = options.icon !== 'none'
  bandOn = options.band !== false
  spinnerOn = options.spinner !== false
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
    // darker purple. The renderer says which with each snapshot (`light`):
    // COGNEE_STATUSLINE_THEME first, then Claude Code's own theme setting.
    // The module reads neither itself; until the first snapshot the
    // dark-background purple stands.
    if (markOn && !waveSvg) waveSvg = WAVE_SVG
    await bindSession($)
    await readPhase($)
    await refreshSnapshot($)
    await $.command.register({
      name: 'cognee-dataset',
      description: 'Switch this session to another Cognee dataset (recall is then restricted to it); "pin" restricts recall to the active dataset; "auto" lets the server pick the best-matching dataset for each recall when it can; without a name, list them.',
      argumentHint: '[name|pin|auto]',
    })
    await $.command.register({
      name: 'cognee-tour',
      description: 'Replay the short tour of the Cognee status row.',
    })
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
      const current = switchTargets.find(r => r.current)?.name ?? 'the active dataset'
      // switch-dataset.py says which recall is on and why; the generic line is
      // for an older script that sends no message.
      const recall = recallMessage || (recallMode === 'auto' ? 'Recall is auto.' : `Recall is restricted to ${current}.`)
      redraw($)
      return {
        text: `Cognee datasets you can write to:\n${rows}\n\n${recall}\n/cognee-dataset <name> switches this session, /cognee-dataset pin restricts recall to the active dataset, /cognee-dataset auto sets it back to auto; the dataset name on the status row is a picker too (a click or ctrl+x tab opens it, arrows move, Enter picks).`,
      }
    }
    const line = await switchDataset($, name)
    return { text: `Cognee: ${line.endsWith('.') ? line : `${line}.`}` }
  })

  // Fires at each prompt, before the turn starts; next(e) runs the plugin's
  // recall hook (UserPromptSubmit), whose header "Cognee memory: recall N
  // memory ..." may come back in the context. The marker the hook wrote is
  // the fallback. A /clear or /resume changes the session id, so rebind here.
  on('prompt.submit', async ($, e, next) => {
    const id = await $.session.id()
    stopAdoptWatch()
    if (id !== sessionId) {
      // Bound here only when the adopt watch missed it (after a /clear): the
      // new conversation gets the warm-up a session start gives.
      if (!sessionId) warmupUntil = (await $.clock.now()) + WARMUP_MS
      await bindSession($)
    }
    setActive($, activity('recalling'))
    let ran
    try {
      ran = await next(e)
    } finally {
      clearActive($)
    }
    if (ran && 'drop' in ran) return ran
    await readPhase($)
    // The prompt hook has run by now: it applied a pending dataset pick and
    // rewrote the row's data. Read both, then give up on a pick it never saw.
    await refreshSnapshot($)
    await givePickUp($)
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

  // The Desktop app joins the session after session.start has run: every
  // redraw asked for while the session came up had no surface to draw on, and
  // without another one the band stays empty until the first prompt. Draw it
  // as the client attaches, with a fresh snapshot (the SessionStart hook may
  // have confirmed the connection since).
  on('session.attach', async ($, e, next) => {
    const ran = await next(e)
    if (!bandOn || (e.surface !== 'desktop' && e.surface !== 'terminal')) return ran
    if (!sessionId) void adoptSession($)
    else await refreshSnapshot($)
    redraw($)
    return ran
  }).catch(($, e, next) => next(e))

  // /clear and /resume end the conversation and start another under a new id,
  // with no session.start: forget what was shown, and let the next prompt rebind.
  on('session.end', async ($, e, next) => {
    // The ended session's entries leave the module's store: the status line
    // is told nothing about a session that is gone, and a pick dies with it.
    const ending = (typeof e.sessionId === 'string' && e.sessionId) || sessionId
    if (ending) {
      try {
        await $.store.delete(`band:${ending}`)
        await $.store.delete(`pick:${ending}`)
      } catch {
        // left behind: the entries name a session id that is never used again
      }
    }
    switching = false
    switchingTo = ''
    pickAt = 0
    pickPrompts = 0
    if (e.reason === 'clear' || e.reason === 'resume') {
      if (active) clearActive($)
      // The ended session's marker must not be read into the new one's row
      // (its SessionEnd sync would show as "✓ session saved").
      if (poll) {
        poll.cancel()
        poll = null
      }
      endedSessionId = (typeof e.sessionId === 'string' && e.sessionId) || sessionId
      result = null
      snapshot = null
      namingDataset = false
      sessionId = ''
      // The next session has a launch record of its own: list again for it.
      resetDatasets()
      const ran = await next(e)
      // Bind the new conversation once its id is out, for up to ADOPT_MS.
      stopAdoptWatch()
      const until = (await $.clock.now()) + ADOPT_MS
      adoptUntil = until
      adoptWatch = $.clock.every(POLL_MS, async () => {
        if (sessionId || (await $.clock.now()) > until) stopAdoptWatch()
        else await adoptSession($)
      })
      // The row reads "connecting…" while the new conversation binds.
      redraw($)
      void adoptSession($)
      return ran
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

  // The terminal picker expands as a click gives it the focus. Datasets come
  // and go outside this session (deleted, created by another agent): that
  // focus lists them afresh, and the options redraw when the answer lands.
  on('ui.focus', async ($, e, next) => {
    const ran = await next(e)
    if (e.component === 'Pane') return ran
    // Not a move a plugin made (its own autoFocus, a `$.ui.focus`).
    const element = e.element ?? (e as { key?: string }).key
    if (element === 'dataset' && e.origin?.kind !== 'plugin' && !datasetsListing) void relistDatasets($)
    return ran
  }).catch(($, e, next) => next(e))

  on('ui.close', { id: FIX_PANE }, ($, e, next) => {
    fixOpen = false
    return next(e)
  }).catch(($, e, next) => next(e))

  // The fix dialog: the reason in plain words and the steps. It runs nothing
  // and writes nothing; the steps name the doctor command and the settings
  // file where they apply.
  on('ui.render', { component: 'Pane', requestId: FIX_PANE }, async ($, e) => {
    const { Box, Text, Markdown, Button } = $.ui.resolve(e) as any
    const guide = fixGuide(fixReason)
    const rows: RenderElement[] = []
    rows.push(Text({ bold: true, color: 'red', children: [`✕ ${guide.title}`] }))
    rows.push(Text({ dimColor: true, children: [`reason: ${fixReason || 'unknown'}`] }))
    rows.push(Markdown({ text: `${guide.what}\n\n${guide.steps.map((step, i) => `${i + 1}. ${step}`).join('\n')}` }))
    rows.push(Text({ children: [''] }))
    rows.push(Button({ key: 'closeFix', label: 'close', plain: true, dimColor: true, onPress: () => void $.ui.close({ id: FIX_PANE }) }))
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
    if (!sessionId) void adoptSession($)
    if (!datasetsListed && !datasetsListing && datasetsAttempts < LIST_MAX_ATTEMPTS && datasetsRetryAt === 0) void loadDatasets($)
    void startTourIfFirstRun($)
    // The snapshot names the active dataset from local state, retaken every
    // prompt; the listing needs the server and is retaken only on a switch,
    // and a listing that fails keeps the rows it had. When the two disagree
    // (a switch whose relist timed out, a switch from a shell) the snapshot
    // wins, and the rows are marked by it. The launch record, and so the
    // snapshot, may hold a dataset by its id rather than its name.
    const listedRow = switchTargets.find(r => r.current)
    const recorded = snap?.dataset ?? ''
    const agrees = !recorded || (!!listedRow && (listedRow.name === recorded || listedRow.id === recorded))
    const targets = agrees ? switchTargets : switchTargets.map(r => ({ ...r, current: r.name === recorded || r.id === recorded }))
    const current = targets.find(r => r.current)?.name ?? recorded
    // A picker whenever the server listed anything. The active dataset may be
    // missing from the writable list (read-only, or created since the
    // listing); it heads the options anyway, so the row still reads and the
    // other datasets stay reachable.
    const canPick = targets.length > 0
    const listed = current && !targets.some(r => r.name === current) ? [{ name: current, id: '', current: true }, ...targets] : targets
    // One option per name: the engine refuses a Select whose values repeat and
    // drops the whole row, and a switch goes by name anyway. Same-named
    // datasets (another principal's copy) collapse onto the current row, or
    // the first one listed.
    const byName = new Map<string, (typeof listed)[number]>()
    for (const r of listed) if (!byName.has(r.name) || (r.current && !byName.get(r.name)!.current)) byName.set(r.name, r)
    const options = listed.filter(r => byName.get(r.name) === r)
    // Auto recall reads "auto (<dataset saves go to>)"; a pinned one, the name.
    // The listing's answer once it is in (a switch updates it); before that,
    // the snapshot's, read from local state at the first draw.
    const auto = (recallMode || snap?.recall) === 'auto'
    const pickerLabel = auto ? `auto (${current})` : current
    const picker = () =>
      Select({
        key: 'dataset',
        options: [
          // Selected, it names the dataset saves go to, as the closed form does.
          { value: AUTO_RECALL, label: auto ? pickerLabel : AUTO_RECALL },
          // "auto" is reserved for the entry above: a dataset so named is not offered.
          ...options.filter(r => r.current || !isReservedName(r.name)).map(r => ({ value: r.name, label: r.name })),
          { value: NEW_DATASET, label: NEW_DATASET },
        ],
        value: auto ? AUTO_RECALL : current,
        ...(e.surface === 'terminal' ? { autoFocus: true as const } : {}),
        onSelect: (value: string) => {
          if (value === NEW_DATASET) {
            namingDataset = true
            redraw($)
            return
          }
          // Picking a dataset, the active one included, pins recall to it. The
          // active one is `pin` (no new session), sent even when recall already
          // reads "pinned": a pin by COGNEE_PLUGIN_RECALL_MODE is then recorded
          // for this launch and outlives a later change to that configuration.
          // `pin` is the script's action, so another dataset so named is sent
          // by its id.
          if (value === AUTO_RECALL) {
            if (!auto) void switchDataset($, 'auto')
            else redraw($)
          } else {
            const row = options.find(r => r.name === value)
            if (row?.current) void switchDataset($, 'pin')
            else void switchDataset($, row?.id && value.trim().toLowerCase() === 'pin' ? row.id : value)
          }
        },
      })
    // In a terminal the picker is drawn from the start: the engine shows it
    // compact (the selected name) until a click gives it the focus, expanded
    // while it has it, and compact again on Escape. A Select has no hover style
    // of its own, so a keyed Box around it lights while the pointer is over it.
    const hoverable = (child: RenderElement) =>
      Box({ key: 'datasetHover', flexDirection: 'row', hover: { backgroundColor: 'subtle' }, children: [child] })
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
              if (isReservedName(value)) {
                $.ui.toast('✕ "auto" is reserved for auto recall; pick another name')
                return
              }
              // `switch-dataset.py pin` pins recall instead of creating "pin".
              if (value.trim().toLowerCase() === 'pin') {
                $.ui.toast('✕ "pin" restricts recall to the active dataset; pick another name')
                return
              }
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
    // red verdict with its button on a failure, the spinner and "connecting…"
    // while the session warms up with nothing confirmed, and a dim ○ once
    // the warm-up is over and still nothing is known. One segment with the mark and the
    // wordmark after it: the official waveform as an inline SVG where the
    // surface draws one (the desktop; the terminal's table lists an Svg too,
    // but draws it as an empty box, so a terminal gets the wordmark alone),
    // then "cognee" bold in the brand lavender.
    const badge: RenderElement[] = []
    let badgeWidth = 2
    const connecting = awaitingConnection(await $.clock.now())
    if (e.surface === 'terminal') terminalBand = true
    if (connecting) startSpin($)
    if (active || connecting) {
      const glyph = e.surface === 'desktop' ? '◌' : (FRAMES[frame % FRAMES.length] ?? '⠋')
      badge.push(Text({ color: 'yellow', children: [`${glyph} `] }))
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
    if (switching) {
      // No picker while a switch runs: it would show the pick as if it had
      // already landed.
      const name = switchingTo ? `${current || 'memory'} → ${switchingTo}` : current || 'memory'
      segment(name.length, 0, Text({ children: [name] }))
    } else if (canPick) {
      const drawn = namingDataset ? namer() : e.surface === 'terminal' ? hoverable(picker()) : picker()
      segment(namingDataset ? 26 : pickerLabel.length + 2, 0, drawn)
    } else {
      const dataset = snap?.dataset ?? 'memory'
      const name = snap && (recallMode || snap.recall) === 'auto' ? `auto (${dataset})` : dataset
      if (snap && !datasetsListed && datasetsAttempts < LIST_MAX_ATTEMPTS) {
        // The listing needs the server, which a new session's first recall
        // keeps busy for a while: the picker is there from the first draw,
        // offering nothing to pick until the datasets are listed. A pick
        // changes nothing (an option cannot be disabled).
        const loading = Select({
          key: 'dataset',
          options: [
            { value: name, label: name },
            { value: LOADING_DATASETS, label: 'loading datasets…' },
          ],
          value: name,
          onSelect: () => redraw($),
        })
        segment(name.length + 2, 0, e.surface === 'terminal' ? hoverable(loading) : loading)
      } else {
        segment(name.length, 0, Text({ children: [name] }))
      }
    }
    const mode = snap?.mode ?? '…'
    segment(3 + mode.length, 0, Text({ children: [' · '] }), Text({ bold: true, color: purple, children: [mode] }))
    if (snap?.credits) {
      // As on the text line: the balance green, red once exhausted or a
      // request was refused. The text line's "top up: <url>" becomes a link
      // here, the way out one click away (an anchor on the desktop, OSC 8 in
      // a terminal). A balance that is out, or a refusal, is something to act
      // on: it is never dropped for width; a healthy balance goes before the
      // activity.
      const [balance = '', ...rest] = snap.credits.split(' · ')
      segment(3 + balance.length, snap.credits_exhausted ? 0 : 2, Text({ color: snap.credits_exhausted ? 'red' : 'green', children: [` · ${balance}`] }))
      if (snap.billing_url && Link && rest.some(part => part.startsWith('top up:'))) {
        segment(9, 0, Text({ children: [' · '] }), Link({ href: snap.billing_url, label: 'top up' }))
      }
    }
    // What memory is doing, or did last. A failed `ready` result is not
    // shown next to a green ●: the verdict predates the connection marker
    // (a boot that finished after the hook gave up), and the marker wins.
    const shownResult =
      result && result.age * 1000 < HIDE_AFTER_MS && !(result.phase === 'ready' && !result.ok && snap?.state === 'ok') ? result : null
    if (active) {
      segment(3 + active.text.length, 1, Text({ color: 'yellow', children: [` · ${active.text}`] }))
    } else if (connecting) {
      segment(3 + ACTIVE_LABELS.connecting.length, 1, Text({ color: 'yellow', children: [` · ${ACTIVE_LABELS.connecting}`] }))
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
  // The credit line under an answer that used memory ("> Cognee memory:
  // …", written by the model on the recall hook's instruction): the
  // label gives way to the wave mark on the desktop and to the display
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

  on('ui.render', { component: 'Spinner' }, async ($, e, next) => {
    if (!spinnerOn || !active) return next(e)
    if (e.surface !== 'desktop' && !ASYNC_PHASES.includes(active.phase) && !OWN_PHASES.includes(active.phase)) return next(e)
    return next({ ...e, props: { ...e.props, suffix: ` · ${active.text}` } })
  })
}
