// Tests for the status row, the dataset picker and the /cognee-dataset command.
// The module starts no program: it reads the files the plugin's scripts write
// and saves a pick in its store. `wire` plays the scripts' side: what a script
// would have written is answered here by argv, as before, and served to the
// module as the file it reads; a pick in the store is applied at the next
// prompt, as the prompt hook does. Nothing touches a server or a real file.
import { expect, mock, test } from 'claude-code/testing'

const SESSION = 's1'
const HOME = '/home/tester'
const PLUGIN = 'cognee-memory'

const SNAPSHOT = {
  enabled: true,
  state: 'ok',
  reason: '',
  dataset: 'agent_sessions',
  mode: 'local',
  credits: '',
  update: '',
  text: 'cognee: agent_sessions · local',
}

const SWITCH_LIST = {
  current: 'agent_sessions',
  session_id: 'cognee-1',
  datasets: [
    { name: 'agent_sessions', id: 'a1', writable: true, current: true },
    { name: 'demo', id: 'd2', writable: true, current: false },
  ],
  hidden_readonly: 0,
  filtered: true,
}

const BAND = {
  component: 'AbovePrompt' as const,
  props: {
    hasSurvey: false,
    isWorking: false,
    maxRows: 4,
    bodyColumns: 120,
    scroll: { offset: 0, bodyRows: 4 },
    view: {},
  },
}

type Call = { argv: readonly string[]; stdin?: string }
type Write = { path: string; text: string }
// The mark the engine's session start leaves among the calls (see wire).
const HOOK_RAN = '<SessionStart hook ran>'

function ok(stdout: string) {
  return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
}

// The world beneath the plugin, with every script answered by its name.
function wire(
  on: Parameters<Parameters<typeof test>[1]>[1],
  calls: Call[],
  answers: Partial<Record<string, (argv: readonly string[]) => { value: unknown }>> = {},
  writes: Write[] = [],
  registrations: { tools: string[]; commands: string[] } = { tools: [], commands: [] },
  // The tool labels the engine's own row renderer would receive, in order.
  toolRows: string[] = [],
  // What a read of a file answers; undefined reads as empty.
  readFile: (path: string) => string | undefined = () => undefined,
  // The engine's current session id (a /clear changes it).
  currentId: () => string = () => SESSION,
) {
  const clock = mock.clock(on, { now: 1_700_000_000_000 })
  mock.env(on, { HOME })
  const store = wiredStore
  store.clear()
  let pickResult: Record<string, unknown> | null = null
  // The dataset the session is on, as the scripts' local state holds it: a
  // switch moves it, and the renderer and the listing then answer with it.
  let activeDataset = SWITCH_LIST.current
  // The engine's own session start, where the settings hooks (SessionStart)
  // run: it leaves a mark among the calls so a test can tell what ran before
  // it and what after.
  on('session.start', ($, e) => {
    calls.push({ argv: [HOOK_RAN] })
    return { cwd: e.cwd }
  })
  on('turn.complete', () => ({ text: '' }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  on('ui.render', ($, e) => {
    if (e.component === 'ToolUse') toolRows.push(e.props.tool)
    const { Text } = $.ui.resolve(e)
    return Text({ children: [''] })
  })
  on('session.id', () => ({ value: currentId() }))
  // A focus move the engine lets through (the band holds the keys).
  on('ui.focus', () => ({}))
  // The files the plugin's scripts keep for the module, built from what each
  // script answers: the row's data (the renderer), the picker's listing and
  // the last pick's outcome (the switch script), the activity marker.
  on('fs.read', ($, e) => {
    const root = `${HOME}/.cognee-plugin/claude-code`
    const session = (e.path.match(/\/([^/]+)\.json$/) ?? [])[1] ?? ''
    if (e.path.startsWith(`${root}/status/`)) {
      return { value: stdoutOf(script('cognee_statusline_render.py', ['--json'], JSON.stringify({ session_id: session }), false)) }
    }
    if (e.path.startsWith(`${root}/mod/`)) {
      let listing: unknown = null
      try {
        listing = JSON.parse(stdoutOf(script('switch-dataset.py', ['--list', '--json', '--session-key', session], undefined, false)))
      } catch {
        listing = null
      }
      const bridge: Record<string, unknown> = {}
      if (listing && typeof listing === 'object' && !(listing as { error?: unknown }).error) bridge.listing = listing
      if (pickResult) bridge.pick = pickResult
      return { value: JSON.stringify(bridge) }
    }
    return { value: readFile(e.path) ?? '' }
  })
  on('fs.write', ($, e) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  // The registrations session start makes; a test that inspects them
  // passes `registrations` and reads it back.
  on('tool.register', ($, e) => {
    registrations.tools.push(e.name)
    return { value: { tool: `mcp__${PLUGIN}__${e.name}` } }
  })
  on('command.register', ($, e) => {
    registrations.commands.push(e.name)
    return { value: { command: e.name } }
  })
  // The module must start nothing.
  on('process.run', ($, e) => {
    throw new Error(`the module started a process: ${e.argv.join(' ')}`)
  })
  // The store the module saves its band entry and its picks in.
  on('store.get', ($, e) => ({ value: store.get(e.key) }))
  on('store.set', ($, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  on('store.delete', ($, e) => {
    store.delete(e.key)
    return { value: undefined }
  })
  // The plugin's prompt hook applies a pick the module saved, before it recalls.
  on('prompt.submit', ($, e) => {
    applyPick()
    return { text: e.text }
  })
  return clock

  function applyPick(): void {
    const session = currentId()
    const pick = store.get(`pick:${session}`) as { target?: string; at?: number } | undefined
    if (!pick || !pick.target || (pickResult && pickResult.at === pick.at)) return
    let answer: Record<string, unknown> = {}
    try {
      answer = JSON.parse(stdoutOf(script('switch-dataset.py', [pick.target, '--json', '--session-key', session]))) as Record<string, unknown>
    } catch {
      answer = { error: 'the dataset script gave no answer' }
    }
    const failed = !!answer.error || (answer.switched !== true && answer.switched !== false)
    pickResult = {
      at: pick.at,
      target: pick.target,
      state: failed ? 'failed' : 'done',
      message: String(answer.error ?? answer.message ?? ''),
      dataset: String(answer.dataset ?? ''),
      recall: String(answer.recall ?? ''),
    }
  }

  // One run of a plugin script, as the hooks would make it. `record` false
  // keeps the refreshes of the files (every read) out of `calls`, which tests
  // read as the work that was started.
  function script(name: string, args: string[], stdin?: string, record = true): { value: unknown } {
    const argv = [`/plugin/scripts/${name}`, ...args]
    if (record) calls.push({ argv, stdin })
    const e = { argv, init: { stdin } }
    const answer = answers[name]
    if (answer) return answer(e.argv)
    switch (name) {
      case 'cognee_statusline_render.py':
        return ok(JSON.stringify({ ...SNAPSHOT, dataset: activeDataset }))
      case 'switch-dataset.py': {
        if (e.argv.includes('--list')) {
          // A dataset the switch created is listed too, as the current one.
          const rows = SWITCH_LIST.datasets.map(r => ({ ...r, current: r.name === activeDataset }))
          if (!rows.some(r => r.current)) rows.push({ name: activeDataset, id: 'n9', writable: true, current: true })
          return ok(JSON.stringify({ ...SWITCH_LIST, current: activeDataset, datasets: rows }))
        }
        const target = e.argv[1] ?? ''
        // `pin` and `auto` change the recall mode and stay on the dataset.
        if (target === 'pin' || target === 'auto') {
          return ok(JSON.stringify({ switched: false, reason: target === 'pin' ? 'recall_restricted' : 'recall_auto', dataset: activeDataset, session_id: 'cognee-1', recall: target === 'pin' ? 'pinned' : 'auto' }))
        }
        const previous = activeDataset
        activeDataset = target
        return ok(JSON.stringify({ switched: true, dataset: target, session_id: 'cognee-2', previous: { dataset: previous, synced: true } }))
      }
      default:
        throw new Error(`unexpected process: ${e.argv.join(' ')}`)
    }
  }
}

function stdoutOf(ran: { value: unknown }): string {
  return String((ran.value as { stdout?: unknown })?.stdout ?? '')
}

// The plugin's cross-session store, as a map the test can read back. `wire`
// serves it; call this after `wire` to seed entries.
const wiredStore = new Map<string, unknown>()
function fakeStore(on: Parameters<Parameters<typeof test>[1]>[1], entries: Record<string, unknown> = {}) {
  for (const [key, value] of Object.entries(entries)) wiredStore.set(key, value)
  return wiredStore
}

function argvOf(calls: Call[], script: string): readonly string[] | undefined {
  return calls.find(c => c.argv.some(a => a.endsWith(`/scripts/${script}`)))?.argv
}

// The one run of the switch script the prompt hook made for a pick (the
// listings behind the bridge file are not among the calls).
function switchArgv(calls: Call[]): readonly string[] | undefined {
  return calls.find(c => c.argv.some(a => a.endsWith('/scripts/switch-dataset.py')) && !c.argv.includes('--list'))?.argv
}

function argsAfter(argv: readonly string[], script: string): string[] {
  const i = argv.findIndex(a => a.endsWith(`/scripts/${script}`))
  return argv.slice(i + 1)
}

// The person's click on the terminal picker: it gives the picker the focus,
// which the engine shows by opening its list.
async function focusPicker(
  $: { ui: { focus: (args: { requestId: string; key: string }) => Promise<unknown> } },
  ui: { find: (q: { type: 'Select'; key: string }) => Promise<unknown> },
) {
  await $.ui.focus({ requestId: 'above-prompt', key: 'dataset' })
  await new Promise(r => setTimeout(r, 20))
  await ui.find({ type: 'Select', key: 'dataset' })
}

// The pick the module saved for the prompt hook, as its store holds it.
function savedPick(session = SESSION): { target?: string; at?: number } | undefined {
  return wiredStore.get(`pick:${session}`) as { target?: string; at?: number } | undefined
}

// A machine that has seen the tour: the row is drawn without the hint line.
const TOUR_SEEN = { tour: { version: 1, at: 1, finished: true } }

test('session start registers the two commands and no tool', async ($, on) => {
  const calls: Call[] = []
  const registrations = { tools: [] as string[], commands: [] as string[] }
  wire(on, calls, {}, [], registrations)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  // The search and remember tools are gone with the programs they ran.
  expect(registrations.tools).toEqual([])
  expect(registrations.commands).toEqual(['cognee-dataset', 'cognee-tour'])
})

test('after a /clear the band binds the new session without waiting for a prompt', async ($, on) => {
  const calls: Call[] = []
  const writes: Write[] = []
  let id = SESSION
  // The ended session's marker holds its SessionEnd sync; the new one has none.
  const synced = JSON.stringify({ results: { synced: { ok: true, at: 1_700_000_000 } } })
  const clock = wire(
    on,
    calls,
    {},
    writes,
    undefined,
    [],
    path => (path.endsWith(`/phase/${SESSION}.json`) ? synced : undefined),
    () => id,
  )
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })

  id = 's2'
  await $.session.end({ sessionId: SESSION, reason: 'clear' })
  await clock.advance(1100)
  // The module writes no file and starts no program: the new session's entry
  // in its store is what keeps the settings status line quiet for it, and the
  // ended session's entry is gone.
  expect(writes).toEqual([])
  expect(calls.map(c => c.argv[0])).toEqual([HOOK_RAN])
  expect(wiredStore.has('band:s2')).toBe(true)
  expect(wiredStore.has(`band:${SESSION}`)).toBe(false)
  expect(await ui.find({ type: 'Text', text: /session saved/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /local/ })).toBeDefined()
  await ui.unmount()
})

test('/cognee-dataset <name> saves the pick, and the next message switches', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  fakeStore(on, TOUR_SEEN)
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  // The command switches nothing itself: the pick goes into the store, where
  // the prompt hook reads it, and the answer says when it applies.
  const answer = await $.command.run({ command: 'cognee-dataset', args: 'demo' })
  expect(answer.text).toBe('Cognee: switching to demo with your next message.')
  expect(savedPick()).toEqual({ target: 'demo', at: 1_700_000_000 })
  expect(toasts).toContain('switching to demo with your next message')
  expect(switchArgv(calls)).toBeUndefined()
  let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: 'agent_sessions → demo' })).toBeDefined()
  await ui.unmount()

  // The next message: the prompt hook switches, and the module reads what it
  // reported and the row's new data.
  await $.prompt.submit({ text: 'next' })
  expect(argsAfter(switchArgv(calls)!, 'switch-dataset.py')).toEqual(['demo', '--json', '--session-key', SESSION])
  expect(toasts).toContain('switched to demo')
  expect(savedPick()).toBeUndefined()
  ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect((await ui.find({ type: 'Select', key: 'dataset' }))!.props.value).toBe('demo')
  await ui.unmount()
})

test('/cognee-dataset alone lists the datasets the picker offers', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  const answer = await $.command.run({ command: 'cognee-dataset', args: '' })
  expect(answer.text).toContain('* agent_sessions (current)')
  expect(answer.text).toContain('- demo')
  expect(answer.text).toContain('Recall is restricted to agent_sessions.')
  // The list is the one the scripts keep in the bridge file: nothing was run.
  expect(argvOf(calls, 'switch-dataset.py')).toBeUndefined()
})

test('a switch the script refuses is reported, not hidden', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls, {
    'switch-dataset.py': argv =>
      argv.includes('--list') ? ok(JSON.stringify(SWITCH_LIST)) : ok(JSON.stringify({ error: 'Selected dataset is not writable', code: 5 })),
  })
  fakeStore(on, TOUR_SEEN)
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  const answer = await $.command.run({ command: 'cognee-dataset', args: 'demo' })
  expect(answer.text).toBe('Cognee: switching to demo with your next message.')

  // The prompt hook's failure comes back through the bridge file, in the
  // script's own words, and the pending pick ends: the picker is back on the
  // dataset the session stayed on.
  await $.prompt.submit({ text: 'next' })
  expect(toasts).toContain('✕ Selected dataset is not writable')
  expect(savedPick()).toBeUndefined()
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /→ demo/ })).toBeUndefined()
  expect((await ui.find({ type: 'Select', key: 'dataset' }))!.props.value).toBe('agent_sessions')
  await ui.unmount()
})

test('on the desktop band the dataset name is a picker, and a pick switches with the next message', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  let picker = await ui.find({ type: 'Select', key: 'dataset' })
  expect(picker).toBeDefined()
  expect(picker!.props.value).toBe('agent_sessions')
  expect((picker!.props.options as { value: string }[]).map(o => o.value)).toEqual(['auto (best-matching dataset)', 'agent_sessions', 'demo', '+ new dataset…'])
  // The name is drawn once, as the picker, not again as text.
  expect(await ui.find({ type: 'Text', text: /agent_sessions/ })).toBeUndefined()

  // The pick is saved for the prompt hook; nothing is run from here.
  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: 'demo' })
  expect(savedPick()).toEqual({ target: 'demo', at: 1_700_000_000 })
  expect(toasts).toContain('switching to demo with your next message')
  expect(switchArgv(calls)).toBeUndefined()

  await $.prompt.submit({ text: 'next' })
  expect(argsAfter(switchArgv(calls)!, 'switch-dataset.py')).toEqual(['demo', '--json', '--session-key', SESSION])
  expect(toasts).toContain('switched to demo')
  picker = await ui.find({ type: 'Select', key: 'dataset' })
  expect(picker!.props.value).toBe('demo')
  await ui.unmount()
})

// Until the prompt hook has switched, the row says where the pick is going,
// not a picker already showing it. Nothing of the module's own runs meanwhile,
// so the hooks' activity (the turn's save) shows beside it as usual.
test('a pick waiting for the next message reads "<current> → <target>" until the prompt hook reports on it', async ($, on) => {
  const calls: Call[] = []
  let markerText = ''
  const clock = wire(on, calls, {}, [], undefined, undefined, path => (path.endsWith(`/phase/${SESSION}.json`) ? markerText : undefined))
  on('ui.toast', () => ({ value: undefined }))
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Select', key: 'dataset' })).toBeDefined()

  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: 'demo' })
  expect(await ui.find({ type: 'Select', key: 'dataset' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'agent_sessions → demo' })).toBeDefined()

  // The turn's save lands while the pick waits: the poll reads the marker and
  // the bridge file, which says nothing about the pick yet, so it stays.
  markerText = JSON.stringify({ session_key: SESSION, active: null, results: { saved: { at: clock.now() / 1000, ok: true, count: 1 } } })
  await clock.advance(1000)
  expect(await ui.find({ type: 'Text', text: 'agent_sessions → demo' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /memory saved/ })).toBeDefined()

  // One pick at a time: a second is refused and the first stays saved.
  const second = await $.command.run({ command: 'cognee-dataset', args: 'other' })
  expect(second.text).toBe('Cognee: a switch is already waiting for your next message.')
  expect(savedPick()?.target).toBe('demo')

  await $.prompt.submit({ text: 'next' })
  const picker = await ui.find({ type: 'Select', key: 'dataset' })
  expect(picker).toBeDefined()
  expect(picker!.props.value).toBe('demo')
  expect(await ui.find({ type: 'Text', text: /→ demo/ })).toBeUndefined()
  expect(switchArgv(calls)?.[1]).toBe('demo')
  await ui.unmount()
})

// The active dataset, picked, is `pin`: recall pinned, no new session. A
// COGNEE_PLUGIN_RECALL_MODE pin reads "pinned" with nothing in the launch
// record; the pick must still reach the switch script, or it would not
// outlive a later change to that configuration.
for (const [initial, closed, autoEntry] of [
  ['auto', 'auto (best-matching dataset)', 'auto (agent_sessions)'],
  ['pinned', 'agent_sessions', 'auto (best-matching dataset)'],
]) {
  test(`with recall ${initial} the picker reads "${closed}", picking the active dataset saves \`pin\`, and auto lifts it`, async ($, on) => {
    const calls: Call[] = []
    let recall = initial
    const clock = wire(on, calls, {
      'switch-dataset.py': argv => {
        if (argv.includes('--list')) return ok(JSON.stringify({ ...SWITCH_LIST, recall }))
        if (argv.includes('auto')) {
          recall = 'auto'
          return ok(JSON.stringify({ switched: false, reason: 'recall_auto', dataset: 'agent_sessions', session_id: 'cognee-1', recall, message: 'MSG auto again.' }))
        }
        recall = 'pinned'
        return ok(JSON.stringify({ switched: false, reason: 'recall_restricted', dataset: 'agent_sessions', session_id: 'cognee-1', recall, message: 'MSG pinned.' }))
      },
    })
    const toasts: string[] = []
    on('ui.toast', ($, e) => {
      toasts.push(e.text)
      return { value: undefined }
    })
    await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

    let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
    let picker = await ui.find({ type: 'Select', key: 'dataset' })
    expect(picker!.props.value).toBe(closed)
    // While recall is auto the open picker shows where saves go, like the terminal's closed form.
    const autoLabel = (pick: typeof picker) =>
      (pick!.props.options as { value: string; label: string }[]).find(o => o.value === 'auto (best-matching dataset)')!.label
    expect(autoLabel(picker)).toBe(autoEntry)

    await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: 'agent_sessions' })
    expect(savedPick()?.target).toBe('pin')
    expect(toasts).toContain('recall is restricted to the active dataset with your next message')
    // A pin goes nowhere, so the waiting row names the dataset alone, no arrow.
    expect(await ui.find({ type: 'Select', key: 'dataset' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: 'agent_sessions' })).toBeDefined()
    await $.prompt.submit({ text: 'next' })
    expect(argsAfter(switchArgv(calls)!, 'switch-dataset.py')).toEqual(['pin', '--json', '--session-key', SESSION])
    // The toast is the script's own message.
    expect(toasts).toContain('MSG pinned.')
    await ui.unmount()

    ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
    picker = await ui.find({ type: 'Select', key: 'dataset' })
    expect(picker!.props.value).toBe('agent_sessions')
    expect(autoLabel(picker)).toBe('auto (best-matching dataset)')
    // A pick is told from the one before it by the time it was saved.
    await clock.advance(1000)
    calls.length = 0
    await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: 'auto (best-matching dataset)' })
    expect(savedPick()?.target).toBe('auto')
    expect(toasts).toContain('recall goes back to auto with your next message')
    await $.prompt.submit({ text: 'next' })
    expect(argsAfter(switchArgv(calls)!, 'switch-dataset.py')).toEqual(['auto', '--json', '--session-key', SESSION])
    expect(toasts).toContain('MSG auto again.')
    await ui.unmount()

    ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
    picker = await ui.find({ type: 'Select', key: 'dataset' })
    expect(picker!.props.value).toBe('auto (best-matching dataset)')
    expect(autoLabel(picker)).toBe('auto (agent_sessions)')
    await ui.unmount()
  })
}

test('the active dataset owned by someone else is picked as `pin`, another named "pin" by its id', async ($, on) => {
  // The record holds a foreign-owned dataset by its id; picking it by name
  // used to start a full switch. `pin` never names it.
  const calls: Call[] = []
  const clock = wire(on, calls, {
    // The renderer reads the record, which holds the dataset by its id.
    'cognee_statusline_render.py': () => ok(JSON.stringify({ ...SNAPSHOT, dataset: 'team-id' })),
    'switch-dataset.py': argv =>
      argv.includes('--list')
        ? ok(
            JSON.stringify({
              ...SWITCH_LIST,
              current: 'team-id',
              recall: 'auto',
              datasets: [
                { name: 'team', id: 'team-id', writable: true, current: true },
                { name: 'pin', id: 'pin-id', writable: true, current: false },
              ],
            }),
          )
        : ok(JSON.stringify({ switched: false, reason: 'recall_restricted', dataset: 'team-id', session_id: 'cognee-1', recall: 'pinned', message: 'MSG pinned.' })),
  })
  on('ui.toast', () => ({ value: undefined }))
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: 'team' })
  expect(savedPick()?.target).toBe('pin')
  await $.prompt.submit({ text: 'next' })
  expect(argsAfter(switchArgv(calls)!, 'switch-dataset.py')).toEqual(['pin', '--json', '--session-key', SESSION])

  // The dataset named "pin" is saved by its id, and the waiting row still
  // reads its name.
  await clock.advance(1000)
  calls.length = 0
  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: 'pin' })
  expect(savedPick()?.target).toBe('pin-id')
  expect(await ui.find({ type: 'Text', text: 'team → pin' })).toBeDefined()
  await $.prompt.submit({ text: 'next' })
  expect(argsAfter(switchArgv(calls)!, 'switch-dataset.py')).toEqual(['pin-id', '--json', '--session-key', SESSION])
  await ui.unmount()
})

for (const [action, reason, recall, message, pending] of [
  ['pin', 'recall_restricted', 'pinned', "Recall is now pinned to 'agent_sessions', the active dataset; saves go there too.", 'recall is restricted to the active dataset with your next message'],
  ['auto', 'recall_auto', 'auto', "Recall is auto; saves go to 'agent_sessions'.", 'recall goes back to auto with your next message'],
]) {
  test(`/cognee-dataset ${action} saves the ${action} action, and the next message relays the script's message`, async ($, on) => {
    const calls: Call[] = []
    wire(on, calls, {
      'switch-dataset.py': argv =>
        argv.includes('--list')
          ? ok(JSON.stringify(SWITCH_LIST))
          : ok(JSON.stringify({ switched: false, reason, dataset: 'agent_sessions', session_id: 'cognee-1', recall, message })),
    })
    const toasts: string[] = []
    on('ui.toast', ($, e) => {
      toasts.push(e.text)
      return { value: undefined }
    })
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

    const answer = await $.command.run({ command: 'cognee-dataset', args: action })
    expect(answer.text).toBe(`Cognee: ${pending}.`)
    expect(savedPick()?.target).toBe(action)
    expect(switchArgv(calls)).toBeUndefined()

    await $.prompt.submit({ text: 'next' })
    expect(argsAfter(switchArgv(calls)!, 'switch-dataset.py')).toEqual([action, '--json', '--session-key', SESSION])
    expect(toasts).toContain(message)
    expect(savedPick()).toBeUndefined()
  })
}
test('in a terminal the picker reads "auto (<dataset>)" while recall is auto', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls, {
    'switch-dataset.py': argv => (argv.includes('--list') ? ok(JSON.stringify({ ...SWITCH_LIST, recall: 'auto' })) : ok('{}')),
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  // The first draw starts the listing; the next one has the mode.
  let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  await ui.unmount()

  ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  // Drawn compact, the engine shows the selected entry: auto, named after the
  // dataset saves go to.
  const picker = await ui.find({ type: 'Select', key: 'dataset' })
  expect(picker!.props.value).toBe('auto (best-matching dataset)')
  expect((picker!.props.options as { label?: string }[])[0]!.label).toBe('auto (agent_sessions)')
  await ui.unmount()
})

test('same-named datasets are offered once, so the open terminal picker still draws', async ($, on) => {
  const calls: Call[] = []
  // Another principal's copy of the active dataset's name, listed beside it.
  const list = {
    ...SWITCH_LIST,
    recall: 'auto',
    datasets: [
      { name: 'agent_sessions', id: 'a2', writable: true, current: false },
      { name: 'agent_sessions', id: 'a1', writable: true, current: true },
      { name: 'demo', id: 'd2', writable: true, current: false },
    ],
  }
  wire(on, calls, { 'switch-dataset.py': argv => (argv.includes('--list') ? ok(JSON.stringify(list)) : ok('{}')) })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  await ui.unmount()

  ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  const picker = await ui.find({ type: 'Select', key: 'dataset' })
  expect(picker).toBeDefined()
  expect((picker!.props.options as { value: string }[]).map(o => o.value)).toEqual(['auto (best-matching dataset)', 'agent_sessions', 'demo', '+ new dataset…'])
  await ui.unmount()
})

test('a switch made outside the picker is read at the next prompt, so the row shows the new dataset', async ($, on) => {
  const calls: Call[] = []
  // The skill's shell switch moved the launch record; the renderer reads it.
  let dataset = 'agent_sessions'
  // Each read of the bridge file is one listing here.
  let listings = 0
  const clock = wire(on, calls, {
    'cognee_statusline_render.py': () => ok(JSON.stringify({ ...SNAPSHOT, dataset })),
    'switch-dataset.py': () => {
      listings += 1
      const current = dataset
      return ok(JSON.stringify({ ...SWITCH_LIST, datasets: [{ name: 'agent_sessions', id: 'a1', writable: true, current: current === 'agent_sessions' }, { name: 'demo', id: 'd2', writable: true, current: current === 'demo' }] }))
    },
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'cognee-dataset', args: '' })
  const first = listings

  dataset = 'demo'
  await $.prompt.submit({ text: 'next' })
  await clock.advance(1100)
  expect(listings).toBeGreaterThan(first)
  const answer = await $.command.run({ command: 'cognee-dataset', args: '' })
  expect(answer.text).toContain('* demo (current)')
})

test('a switch answered with an error leaves the recall mode as it was', async ($, on) => {
  const calls: Call[] = []
  // Only the first listing answers, so no later one can set the mode back:
  // what the row and the command say afterwards is what the module kept.
  let listings = 0
  wire(on, calls, {
    'switch-dataset.py': argv =>
      argv.includes('--list')
        ? ok(JSON.stringify(listings++ === 0 ? { ...SWITCH_LIST, recall: 'auto' } : { error: 'no launch record' }))
        : ok(JSON.stringify({ error: 'Selected dataset is not writable', code: 5 })),
  })
  fakeStore(on, TOUR_SEEN)
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  await $.command.run({ command: 'cognee-dataset', args: 'demo' })
  await $.prompt.submit({ text: 'next' })
  expect(toasts).toContain('✕ Selected dataset is not writable')
  // Still auto: an error carries no mode, and an unknown one would read as
  // restricted to the active dataset.
  const answer = await $.command.run({ command: 'cognee-dataset', args: '' })
  expect(answer.text).toContain('Recall is auto.')
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  const picker = await ui.find({ type: 'Select', key: 'dataset' })
  expect(picker!.props.value).toBe('auto (best-matching dataset)')
  expect((picker!.props.options as { label?: string }[])[0]!.label).toBe('auto (agent_sessions)')
  await ui.unmount()
})

test('"auto" is reserved: a dataset so named is not offered, and the namer refuses it', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls, {
    'switch-dataset.py': argv =>
      argv.includes('--list')
        ? ok(JSON.stringify({ ...SWITCH_LIST, datasets: [...SWITCH_LIST.datasets, { name: 'Auto', id: 'x3', writable: true, current: false }] }))
        : ok(JSON.stringify({ switched: true, dataset: argv[argv.indexOf('--json') - 1], session_id: 'cognee-2', previous: { synced: true } })),
  })
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  const picker = await ui.find({ type: 'Select', key: 'dataset' })
  expect((picker!.props.options as { value: string }[]).map(o => o.value)).toEqual(['auto (best-matching dataset)', 'agent_sessions', 'demo', '+ new dataset…'])

  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: '+ new dataset…' })
  calls.length = 0
  await $.ui.input({ plugin: PLUGIN, key: 'newDataset', text: ' AUTO ' })
  expect(argvOf(calls, 'switch-dataset.py')).toBeUndefined()
  expect(toasts.some(t => t.includes('"auto" is reserved'))).toBe(true)
  // The field stays open for another name.
  expect(await ui.find({ type: 'Input', key: 'newDataset' })).toBeDefined()
  // "pin" is the script's pin action: as a new name it would pin, not create.
  await $.ui.input({ plugin: PLUGIN, key: 'newDataset', text: 'Pin' })
  expect(argvOf(calls, 'switch-dataset.py')).toBeUndefined()
  expect(toasts.some(t => t.includes('"pin" restricts recall'))).toBe(true)
  expect(await ui.find({ type: 'Input', key: 'newDataset' })).toBeDefined()
  await ui.unmount()
})

test('the picker ends with a "new dataset" entry that turns it into a text field', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  const picker = await ui.find({ type: 'Select', key: 'dataset' })
  const values = (picker!.props.options as { value: string }[]).map(o => o.value)
  expect(values).toEqual(['auto (best-matching dataset)', 'agent_sessions', 'demo', '+ new dataset…'])
  expect(await ui.find({ type: 'Input', key: 'newDataset' })).toBeUndefined()

  // Picking it switches nothing: it asks for a name.
  calls.length = 0
  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: '+ new dataset…' })
  expect(argvOf(calls, 'switch-dataset.py')).toBeUndefined()
  expect(await ui.find({ type: 'Input', key: 'newDataset' })).toBeDefined()
  expect(await ui.find({ type: 'Select', key: 'dataset' })).toBeUndefined()

  // The name typed is the pick: the field closes and the row waits for the
  // next message, like any other pick.
  await $.ui.input({ plugin: PLUGIN, key: 'newDataset', text: 'fresh-start' })
  expect(savedPick()?.target).toBe('fresh-start')
  expect(toasts).toContain('switching to fresh-start with your next message')
  expect(argvOf(calls, 'switch-dataset.py')).toBeUndefined()
  expect(await ui.find({ type: 'Input', key: 'newDataset' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'agent_sessions → fresh-start' })).toBeDefined()

  // The prompt hook's switch creates a dataset that does not exist; the picker
  // is back, on it.
  await $.prompt.submit({ text: 'next' })
  expect(argsAfter(argvOf(calls, 'switch-dataset.py')!, 'switch-dataset.py')).toEqual(['fresh-start', '--json', '--session-key', SESSION])
  expect(toasts).toContain('switched to fresh-start')
  const landed = await ui.find({ type: 'Select', key: 'dataset' })
  expect(landed!.props.value).toBe('fresh-start')
  expect(await ui.find({ type: 'Input', key: 'newDataset' })).toBeUndefined()
  await ui.unmount()
})

test('an empty name, or the cancel mark, brings the picker back without a switch', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  await ui.unmount()
  const again = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })

  // In a terminal the picker is drawn from the start, and again after.
  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: '+ new dataset…' })
  calls.length = 0
  await $.ui.input({ plugin: PLUGIN, key: 'newDataset', text: '   ' })
  expect(switchArgv(calls)).toBeUndefined()
  expect(await again.find({ type: 'Select', key: 'dataset' })).toBeDefined()

  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: '+ new dataset…' })
  expect(await again.find({ type: 'Input', key: 'newDataset' })).toBeDefined()
  await $.ui.press({ plugin: PLUGIN, key: 'cancelNew' })
  expect(switchArgv(calls)).toBeUndefined()
  expect(await again.find({ type: 'Select', key: 'dataset' })).toBeDefined()
  await again.unmount()
})

test('a name the switch script refuses is reported and the picker is back', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls, {
    'switch-dataset.py': argv =>
      argv.includes('--list')
        ? ok(JSON.stringify(SWITCH_LIST))
        : ok(JSON.stringify({ error: "invalid dataset name 'my notes': cognee rejects spaces and dots (try 'my_notes')", code: 1 })),
  })
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: '+ new dataset…' })
  await $.ui.input({ plugin: PLUGIN, key: 'newDataset', text: 'my notes' })
  // The module does not judge the name: it waits for the script, which the
  // prompt hook runs with the next message.
  expect(savedPick()?.target).toBe('my notes')
  expect(await ui.find({ type: 'Text', text: 'agent_sessions → my notes' })).toBeDefined()
  await $.prompt.submit({ text: 'next' })
  expect(toasts.some(t => t.includes("✕ invalid dataset name 'my notes'") && t.includes("try 'my_notes'"))).toBe(true)
  expect(savedPick()).toBeUndefined()
  expect((await ui.find({ type: 'Select', key: 'dataset' }))!.props.value).toBe('agent_sessions')
  await ui.unmount()
})

test('the row is the whole band when the engine draws nothing of its own, so no blank line', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  // Past the first run: the tour's hint line would be a second row.
  fakeStore(on, TOUR_SEEN)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  const drawn = (await ui.drawn()) as { type?: string; props?: { flexDirection?: string } }
  // The wire's engine band is an empty Text: it is left out, so the tree is
  // our one row, not a column with an empty row under it.
  expect(drawn.props?.flexDirection).toBe('row')
  await ui.unmount()
})

test('the first session on a machine gets the tour under the row, once', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  const store = fakeStore(on)
  const toasts: string[] = []
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /The dataset name ↑ is a picker/ })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'tourNext' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^cognee / })).toBeDefined()

  await $.ui.press({ plugin: PLUGIN, key: 'tourNext' })
  expect(await ui.find({ type: 'Text', text: /means memory is connected/ })).toBeDefined()
  await $.ui.press({ plugin: PLUGIN, key: 'tourNext' })
  expect(await ui.find({ type: 'Text', text: /Claude uses what Cognee recalls on every prompt/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /\/cognee-tour replays this tutorial/ })).toBeDefined()
  expect((await ui.find({ type: 'Button', key: 'tourNext' }))!.props.label).toBe('done')
  await $.ui.press({ plugin: PLUGIN, key: 'tourNext' })
  expect(await ui.find({ type: 'Button', key: 'tourNext' })).toBeUndefined()
  expect(toasts.some(t => t.includes('/cognee-tour replays it'))).toBe(true)
  await ui.unmount()

  const seen = store.get('tour') as { version: number; finished: boolean }
  expect(seen.version).toBe(1)
  expect(seen.finished).toBe(true)
})

test('skip ends the tour and it is not shown again', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  const store = fakeStore(on)
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Button', key: 'tourSkip' })).toBeDefined()
  await $.ui.press({ plugin: PLUGIN, key: 'tourSkip' })
  expect(await ui.find({ type: 'Button', key: 'tourSkip' })).toBeUndefined()
  await ui.unmount()
  const seen = store.get('tour') as { version: number; finished: boolean }
  expect(seen.finished).toBe(false)
})

test('a machine that has seen this tour gets no hints', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  fakeStore(on, { tour: { version: 1, at: 1, finished: true } })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Button', key: 'tourNext' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^cognee / })).toBeDefined()
  await ui.unmount()
})

test('/cognee-tour replays it, and the tour row in /config turns it off', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  fakeStore(on, { tour: { version: 1, at: 1, finished: true } })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const answer = await $.command.run({ command: 'cognee-tour', args: '' })
  expect(answer.text).toContain('tour')
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /The dataset name ↑ is a picker/ })).toBeDefined()
  await ui.unmount()
})

test('with the tour off a first session gets no hints', { options: { tour: false } }, async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  fakeStore(on)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Button', key: 'tourNext' })).toBeUndefined()
  await ui.unmount()
})

const FAIL_SNAPSHOT = { ...SNAPSHOT, state: 'fail', reason: 'incorrect_cognee_api_key', text: '✕ (incorrect_cognee_api_key) cognee: agent_sessions · local' }
const FIX_PROPS = { title: 'Fix memory', isFocused: true, bodyColumns: 100, placement: 'inline' as const, scroll: { offset: 0, bodyRows: 18 }, view: {} }

test('a red ✕ (reason) on the row has a see-fix button beside it that opens the guide', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls, { 'cognee_statusline_render.py': () => ok(JSON.stringify(FAIL_SNAPSHOT)) })
  const opened: string[] = []
  on('ui.open', ($, e) => {
    opened.push(e.id)
    return { value: { isPlaced: true } }
  })
  const closed: string[] = []
  on('ui.close', ($, e) => {
    closed.push(e.id)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  // The verdict stays red text; the small button beside it is the press.
  const verdict = await band.find({ type: 'Text', text: /✕ \(incorrect_cognee_api_key\)/ })
  expect(verdict!.props.color).toBe('red')
  const button = await band.find({ type: 'Button', key: 'fix' })
  expect(button!.props.label).toBe('see fix')
  expect(button!.props.variant).toBe('secondary')
  await $.ui.press({ plugin: PLUGIN, key: 'fix' })
  expect(opened).toEqual(['cognee-fix'])
  await band.unmount()

  for (const surface of ['terminal', 'desktop'] as const) {
    const pane = await $.ui.mount({ plugin: PLUGIN, surface, component: 'Pane', requestId: 'cognee-fix', props: FIX_PROPS })
    expect(await pane.find({ type: 'Text', text: /rejected the plugin's API key/ })).toBeDefined()
    expect(await pane.find({ type: 'Markdown', text: /delete `~\/.cognee-plugin\/api_key.json`/ })).toBeDefined()
    // The guide explains and is closed; it takes no key and runs no doctor.
    expect(await pane.find({ type: 'Input' })).toBeUndefined()
    expect(await pane.find({ type: 'Button', key: 'doctor' })).toBeUndefined()
    expect(await pane.find({ type: 'Button', key: 'closeFix' })).toBeDefined()
    await $.ui.press({ plugin: PLUGIN, key: 'closeFix' })
    await pane.unmount()
  }
  expect(closed).toEqual(['cognee-fix', 'cognee-fix'])
  expect(calls.map(c => c.argv[0])).toEqual([HOOK_RAN])
})

test('a healthy row has no fix button', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await band.find({ type: 'Button', key: 'fix' })).toBeUndefined()
  await band.unmount()
})

test('the row keeps checking after start until the connection marker lands', async ($, on) => {
  const calls: Call[] = []
  let renders = 0
  const clock = wire(on, calls, {
    // The first snapshot comes before the SessionStart hook wrote its marker.
    'cognee_statusline_render.py': () => {
      renders += 1
      return ok(JSON.stringify(renders < 3 ? { ...SNAPSHOT, state: '' } : SNAPSHOT))
    },
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  // Not confirmed yet: the row says it is connecting rather than sitting blank.
  let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /connecting…/ })).toBeDefined()
  await ui.unmount()

  // Ten seconds on, the snapshot was retaken and the marker is there.
  for (let i = 0; i < 11; i += 1) await clock.advance(1000)
  ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /●/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /○/ })).toBeUndefined()
  await ui.unmount()
  expect(renders).toBeGreaterThanOrEqual(3)
})

test('credits read green on the row, red once exhausted', async ($, on) => {
  const calls: Call[] = []
  let exhausted = false
  wire(on, calls, {
    'cognee_statusline_render.py': () =>
      ok(JSON.stringify({ ...SNAPSHOT, mode: 'cloud', credits: exhausted ? 'credits: -$0.40' : 'credits: $4,990.93', credits_exhausted: exhausted })),
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  const balance = await ui.find({ type: 'Text', text: /credits: \$4,990.93/ })
  expect(balance!.props.color).toBe('green')
  await ui.unmount()

  exhausted = true
  await $.prompt.submit({ text: 'another prompt refreshes the snapshot' })
  ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  expect((await ui.find({ type: 'Text', text: /credits: -\$0.40/ }))!.props.color).toBe('red')
  await ui.unmount()
})

test('out of credits, the row links to billing where the text line prints the URL', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls, {
    'cognee_statusline_render.py': () =>
      ok(
        JSON.stringify({
          ...SNAPSHOT,
          mode: 'cloud',
          credits: 'credits: -$0.40 · top up: https://platform.cognee.ai/billing',
          credits_exhausted: true,
          billing_url: 'https://platform.cognee.ai/billing',
        }),
      ),
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  for (const surface of ['desktop', 'terminal'] as const) {
    const ui = await $.ui.mount({ plugin: PLUGIN, surface, ...BAND })
    const link = await ui.find({ type: 'Link' })
    expect(link).toBeDefined()
    expect(link!.props.href).toBe('https://platform.cognee.ai/billing')
    expect(link!.props.label).toBe('top up')
    // The URL itself is not repeated as text beside the link.
    expect(await ui.find({ type: 'Text', text: /platform.cognee.ai/ })).toBeUndefined()
    await ui.unmount()
  }
})

test('a session that reports no surface at start (the Desktop app) has its list when the band is first drawn', async ($, on) => {
  const calls: Call[] = []
  // Each read of the bridge file is one listing here.
  let listings = 0
  wire(on, calls, {
    'switch-dataset.py': () => {
      listings += 1
      return ok(JSON.stringify(SWITCH_LIST))
    },
  })
  await $.session.start({ cwd: '/work', surface: null, isInteractive: true })
  expect(listings).toBeGreaterThan(0)

  let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Select', key: 'dataset' })).toBeDefined()
  await ui.unmount()
  const listed = listings
  ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  await ui.unmount()
  // Drawn again, the band does not read the list again.
  expect(listings).toBe(listed)
})

test('in a terminal the band is the whole status row, the dataset name a picker', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /^cognee / })).toBeDefined()
  await ui.unmount()

  ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  // The picker from the start: the engine shows it compact until a click
  // gives it the focus. It sits in a keyed Box, the scope of its hover
  // highlight (a Select has none of its own).
  expect(await ui.find({ type: 'Button', key: 'dataset' })).toBeUndefined()
  expect((await ui.find({ type: 'Box', key: 'datasetHover' }))?.children[0]?.props.key).toBe('dataset')
  expect(await ui.find({ type: 'Text', text: /local/ })).toBeDefined()

  const picker = await ui.find({ type: 'Select', key: 'dataset' })
  expect(picker).toBeDefined()
  expect(picker!.props.value).toBe('agent_sessions')
  expect(picker!.props.autoFocus).toBe(true)

  // A pick is saved for the prompt hook, and the row waits for it.
  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: 'demo' })
  expect(savedPick()).toEqual({ target: 'demo', at: 1_700_000_000 })
  expect(argvOf(calls, 'switch-dataset.py')).toBeUndefined()
  expect(await ui.find({ type: 'Select', key: 'dataset' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'agent_sessions → demo' })).toBeDefined()
  await ui.unmount()
})

test('a terminal session is marked so the settings status line stays quiet for it', async ($, on) => {
  const calls: Call[] = []
  const writes: Write[] = []
  wire(on, calls, {}, writes)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  // The mark is an entry in the module's own store, which the settings status
  // line reads: no file is written and no program run for it.
  expect(writes).toEqual([])
  expect(calls.map(c => c.argv[0])).toEqual([HOOK_RAN])
  expect(wiredStore.get(`band:${SESSION}`)).toEqual({ at: 1_700_000_000 })
})

test('with the band off no session is marked: the settings status line stays', { options: { band: false } }, async ($, on) => {
  const calls: Call[] = []
  const writes: Write[] = []
  wire(on, calls, {}, writes)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  expect(writes).toEqual([])
  // No entry in the store, so the settings status line prints its row.
  expect(wiredStore.has(`band:${SESSION}`)).toBe(false)
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /cognee/ })).toBeUndefined()
  await ui.unmount()
})

test('the picker is there before the datasets are listed, offering only a loading entry', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls, { 'switch-dataset.py': () => ok(JSON.stringify({ error: 'no launch record found', code: 2 })) })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /^cognee / })).toBeDefined()
  const picker = (await ui.find({ type: 'Select', key: 'dataset' })) as any
  expect(picker).toBeDefined()
  expect(picker.props.value).toBe('agent_sessions')
  expect(picker.props.options.map((o: { label: string }) => o.label)).toEqual(['agent_sessions', 'loading datasets…'])
  // Picking either entry switches nothing.
  const before = calls.filter(c => c.argv.some(a => a.endsWith('/scripts/switch-dataset.py'))).length
  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: 'loading datasets…' })
  await $.ui.select({ plugin: PLUGIN, key: 'dataset', value: 'agent_sessions' })
  expect(calls.filter(c => c.argv.some(a => a.endsWith('/scripts/switch-dataset.py'))).length).toBe(before)
  await ui.unmount()
})

test('the active dataset heads the picker when the writable list lacks it', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls, {
    'switch-dataset.py': () =>
      ok(JSON.stringify({ ...SWITCH_LIST, current: 'agent_sessions', datasets: [{ name: 'demo', id: 'd2', writable: true, current: false }] })),
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', ...BAND })
  const picker = await ui.find({ type: 'Select', key: 'dataset' })
  expect(picker).toBeDefined()
  expect(picker!.props.value).toBe('agent_sessions')
  expect((picker!.props.options as { value: string }[]).map(o => o.value)).toEqual(['auto (best-matching dataset)', 'agent_sessions', 'demo', '+ new dataset…'])
  await ui.unmount()
})

test('a narrow band drops the balance, then the activity, and never splits a segment', async ($, on) => {
  const calls: Call[] = []
  let exhausted = false
  wire(on, calls, {
    'cognee_statusline_render.py': () =>
      ok(JSON.stringify({ ...SNAPSHOT, credits: 'credits: $9.76', credits_exhausted: exhausted, update: '' })),
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  // Wide: everything. "● cognee " + "agent_sessions ▾" + " · local" is 33 cells.
  let ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /credits: \$9.76/ })).toBeDefined()
  await ui.unmount()

  // 45 cells: the balance goes too; the badge, the dataset and the mode stay.
  ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND, props: { ...BAND.props, bodyColumns: 45 } })
  expect(await ui.find({ type: 'Text', text: /credits/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^cognee / })).toBeDefined()
  expect(await ui.find({ type: 'Select', key: 'dataset' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /local/ })).toBeDefined()
  await ui.unmount()

  // An exhausted balance is something to act on: it stays at any width.
  exhausted = true
  await $.prompt.submit({ text: 'x' }).catch(() => undefined)
  await $.command.run({ command: 'cognee-dataset', args: 'demo' })
  ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND, props: { ...BAND.props, bodyColumns: 45 } })
  const balance = await ui.find({ type: 'Text', text: /credits: \$9.76/ })
  expect(balance).toBeDefined()
  expect(balance!.props.color).toBe('red')
  await ui.unmount()
})

test('the row wraps by whole segments: each segment is a box that does not shrink', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND })
  // The row is the Box whose first segment holds the wordmark; the boxes
  // around it (the tour's column, the engine's own part) hold "cognee" too,
  // so it is found by its place in the tree, not by its text.
  type Node = { type?: string; props?: Record<string, unknown>; children?: unknown[] }
  const isWordmark = (n: Node) => n.type === 'Text' && (n.children ?? []).join('') === 'cognee '
  const rowOf = (node: unknown): Node | undefined => {
    if (!node || typeof node !== 'object') return undefined
    const n = node as Node
    const kids = (n.children ?? []) as Node[]
    if (kids.some(k => k && typeof k === 'object' && ((k.children ?? []) as Node[]).some(g => g && typeof g === 'object' && isWordmark(g)))) return n
    for (const k of kids) {
      const found = rowOf(k)
      if (found) return found
    }
    return undefined
  }
  const row = rowOf(await ui.drawn())
  expect(row).toBeDefined()
  expect(row!.type).toBe('Box')
  expect(row!.props?.flexWrap).toBe('wrap')
  const children = row!.children as { type: string; props: { flexShrink?: number } }[]
  expect(children.length).toBeGreaterThanOrEqual(3)
  for (const child of children) {
    expect(child.type).toBe('Box')
    expect(child.props.flexShrink).toBe(0)
  }
  await ui.unmount()
})

test('the tour hint wraps as a paragraph beside buttons that keep their cells', async ($, on) => {
  const calls: Call[] = []
  wire(on, calls)
  fakeStore(on)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', ...BAND, props: { ...BAND.props, bodyColumns: 60 } })
  const hint = await ui.find({ type: 'Text', text: /The dataset name ↑ is a picker/ })
  expect(hint).toBeDefined()
  expect(hint!.props.wrap).toBe('wrap')
  expect(await ui.find({ type: 'Button', key: 'tourNext' })).toBeDefined()
  expect(await ui.find({ type: 'Button', key: 'tourSkip' })).toBeDefined()
  await ui.unmount()
})
