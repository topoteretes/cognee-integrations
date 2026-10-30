// Tests for the live-activity module: what it shows while the memory hooks
// work and after they finish. Everything the module reaches (the activity
// marker file, the renderer process, the session id) is answered here, so the
// test runs with no file system, process or network.
import { expect, mock, test } from 'claude-code/testing'

const SESSION = 's1'
const HOME = '/home/tester'
const MARKER_PATH = `${HOME}/.cognee-plugin/claude-code/phase/${SESSION}.json`

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

const SPINNER = {
  component: 'Spinner' as const,
  props: { word: 'Thinking', message: null, suffix: '…', mode: 'thinking' },
}

function marker(nowMs: number, body: Record<string, unknown>): string {
  return JSON.stringify({ session_key: SESSION, ...body, _now: nowMs })
}

// The world beneath the plugin: a clock that moves only when a test moves it,
// the home directory, the session id, the marker file and the renderer.
function wire(on: Parameters<Parameters<typeof test>[1]>[1], state: { marker: string }, snapshotOf: () => object = () => SNAPSHOT) {
  const clock = mock.clock(on, { now: 1_700_000_000_000 })
  mock.env(on, { HOME })
  // The engine's own behaviour at the bottom of each chain the module joins.
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.complete', () => ({ text: '' }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  // What the engine draws itself: the spinner's word and suffix, nothing in the band.
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    if (e.component === 'Spinner') return Text({ children: [`${e.props.word}${e.props.suffix}`] })
    return Text({ children: [''] })
  })
  on('session.id', () => ({ value: SESSION }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('/assets/icon-lavender.svg')) return { value: '<svg viewBox="0 0 1 1"></svg>' }
    if (e.path !== MARKER_PATH) throw new Error(`unexpected read of ${e.path}`)
    return { value: state.marker }
  })
  on('fs.write', () => ({ value: undefined }))
  // Session start registers tools and commands; without these bottoms the
  // start hook would fail there and never reach its warm-up poll.
  on('tool.register', ($, e) => ({ value: { tool: `mcp__cognee-memory__${e.name}` } }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('process.run', () => ({
    value: { exitCode: 0, stdout: JSON.stringify(snapshotOf()), stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  return clock
}

test('the desktop band shows the row the terminal status line shows', async ($, on) => {
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  state.marker = marker(now, { active: null, results: { recalled: { count: 3, at: now / 1000, ok: true } } })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  // The mark (the official SVG on the desktop), the wordmark, the dataset,
  // the mode and the activity.
  expect(await ui.find({ type: 'Svg' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^cognee / })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /agent_sessions/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /tokens saved/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /3 memories used/ })).toBeDefined()
  // Healthy leads with the green dot, as on the text line.
  expect(await ui.find({ type: 'Text', text: /●/ })).toBeDefined()
  await ui.unmount()
})

test('in a terminal the band draws the same row, so the status line below can step aside', async ($, on) => {
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  state.marker = marker(now, { active: null, results: { recalled: { count: 3, at: now / 1000, ok: true } } })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'terminal', ...BAND })
  // A terminal gets the wordmark alone: no mark, and nothing standing in for it.
  expect(await ui.find({ type: 'Svg' })).toBeUndefined()
  expect(await ui.find({ type: 'Image' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /[∿▂▆█🧠]/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^cognee / })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /3 memories used/ })).toBeDefined()
  await ui.unmount()
})

test('a prompt shows recalling while the recall hook runs, then what it recalled', async ($, on) => {
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  state.marker = marker(now, { active: null, results: {} })
  let seenWhileRecalling: string | undefined
  const engine = $
  // The recall settings hook's place in the chain: beneath prompt.submit, it
  // writes the marker and answers with the recall header as context. While it
  // runs, the spinner is drawn through the engine to see what the module adds.
  on('prompt.submit', async (_, e) => {
    const ui = await engine.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...SPINNER })
    seenWhileRecalling = JSON.stringify(await ui.drawn())
    await ui.unmount()
    state.marker = marker(now, { active: null, results: { recalled: { count: 2, at: now / 1000, ok: true } } })
    return { text: e.text, context: ['Cognee memory: recall 2 memory; saved last turn 1 prompt / 0 trace / 1 answer'] }
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'what did we decide about the cache?' })

  expect(seenWhileRecalling).toContain('recalling…')

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /2 memories used/ })).toBeDefined()
  await ui.unmount()

  // The answer's text is the agent's own (its credit line comes from the recall
  // hook's instruction); the module adds nothing under it.
  const done = await $.turn.complete({ turnId: 't1', answer: 'We kept the cache.', durationMs: 1200, isAborted: false, reason: 'answer' })
  expect(done.text ?? '').toBe('')
})

test('nothing recalled: the band says so plainly', async ($, on) => {
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  state.marker = marker(now, { active: null, results: {} })
  on('prompt.submit', ($, e) => {
    state.marker = marker(now, { active: null, results: { recalled: { count: 0, at: now / 1000, ok: true } } })
    return { text: e.text }
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'hello there' })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /nothing relevant/ })).toBeDefined()
  await ui.unmount()
})

test('after the turn the band shows saving until the marker says what was saved', async ($, on) => {
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  state.marker = marker(now, { active: null, results: {} })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  await $.turn.complete({ turnId: 't1', answer: 'Done.', durationMs: 900, isAborted: false, reason: 'answer' })

  let ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /saving…/ })).toBeDefined()
  await ui.unmount()

  // The Stop hook lands its save; the next poll tick picks it up.
  state.marker = marker(now, { active: null, results: { saved: { count: 3, at: now / 1000, ok: true } } })
  await clock.advance(1100)
  ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /saving…/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /✓ memory saved/ })).toBeDefined()
  await ui.unmount()
})

test('failed work is never ticked: a queued write, a failed sync, an anchor that failed', async ($, on) => {
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const cases: [Record<string, unknown>, RegExp][] = [
    [{ saved: { count: 0, at: now / 1000, ok: false, buffered: true } }, /save queued/],
    [{ saved: { count: 0, at: now / 1000, ok: false } }, /save failed/],
    [{ saved: { count: 1, at: now / 1000, ok: false } }, /✓ memory saved/],
    [{ synced: { at: now / 1000, ok: false } }, /session sync failed/],
    [{ anchored: { at: now / 1000, ok: false } }, /context not kept/],
    [{ anchored: { at: now / 1000, ok: true } }, /✓ context kept/],
  ]
  for (const [results, text] of cases) {
    state.marker = marker(now, { active: null, results })
    await clock.advance(1100)
    const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
    expect(await ui.find({ type: 'Text', text })).toBeDefined()
    await ui.unmount()
  }
})

test('a save nothing ever reports stops spinning after the stale window', async ($, on) => {
  // Cloud mode: the Stop hook only spools the capture and no marker exists
  // until the worker delivers it. The band must not say saving… for the rest
  // of the session.
  const state = { marker: '' }
  const clock = wire(on, state)
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  await $.turn.complete({ turnId: 't1', answer: 'Done.', durationMs: 900, isAborted: false, reason: 'answer' })

  let ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /saving…/ })).toBeDefined()
  await ui.unmount()

  await clock.advance(60_000)
  ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /saving…/ })).toBeDefined()
  await ui.unmount()

  await clock.advance(31_000)
  ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /saving…/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^cognee / })).toBeDefined()
  await ui.unmount()
})

test('a stale active phase is ignored rather than shown forever', async ($, on) => {
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  const stale = (now - 5 * 60_000) / 1000
  const recent = (now - 2 * 60_000) / 1000
  state.marker = marker(now, { active: { phase: 'recalling', detail: '', started_at: stale, updated_at: stale }, results: { saved: { count: 4, at: recent, ok: true } } })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /recalling/ })).toBeUndefined()
  // Two minutes old: still shown, faint.
  const saved = await ui.find({ type: 'Text', text: /✓ memory saved/ })
  expect(saved).toBeDefined()
  expect(saved!.props.dimColor).toBe(true)
  await ui.unmount()
})

test('a failed ready verdict is not shown next to a green dot', async ($, on) => {
  // The hook recorded "not ready" while the server was still booting, then
  // the boot finished and the connection marker confirmed it: the marker
  // wins, and the stale setting-up phase is not shown either.
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  const stale = (now - 3 * 60_000) / 1000
  const verdict = (now - 2 * 60_000) / 1000
  state.marker = marker(now, {
    active: { phase: 'setting_up', detail: 'starting memory server', started_at: stale, updated_at: stale },
    results: { ready: { at: verdict, ok: false } },
  })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /●/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /memory unavailable/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /setting up memory/ })).toBeUndefined()
  await ui.unmount()
})

test('the row follows a boot the hook handed to its worker past the warm-up, until the phase ends', async ($, on) => {
  const state = { marker: '' }
  let ready = false
  let renders = 0
  const clock = wire(on, state, () => {
    renders += 1
    return ready ? SNAPSHOT : { ...SNAPSHOT, state: '' }
  })
  const began = clock.now() / 1000
  // What the worker's heartbeat does: the phase stays fresh while it boots.
  const touch = () => {
    state.marker = marker(clock.now(), {
      active: { phase: 'setting_up', detail: 'starting memory server', started_at: began, updated_at: clock.now() / 1000 },
      results: {},
    })
  }
  touch()
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  // Four minutes of boot, touched every fifteen seconds.
  for (let s = 1; s <= 240; s += 1) {
    await clock.advance(1000)
    if (s % 15 === 0) touch()
  }
  let ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /setting up memory · starting memory server…/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /memory unavailable/ })).toBeUndefined()
  await ui.unmount()
  // The snapshot was retaken through the warm-up (every 5 s for two
  // minutes) and, more slowly, past it (every 30 s), so the row can turn
  // green the moment the connection marker lands.
  expect(renders).toBeGreaterThanOrEqual(1 + 24 + 4)
  const during = renders

  // The worker registers the session: the phase ends ready, the marker is there.
  ready = true
  state.marker = marker(clock.now(), { active: null, results: { ready: { at: clock.now() / 1000, ok: true } } })
  for (let s = 0; s < 3; s += 1) await clock.advance(1000)
  ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'terminal', ...BAND })
  expect(await ui.find({ type: 'Text', text: /●/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /memory ready/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /setting up/ })).toBeUndefined()
  await ui.unmount()
  expect(renders).toBeGreaterThan(during)

  // And the poll stops with the phase: nothing runs while the session idles.
  const settled = renders
  for (let s = 0; s < 60; s += 1) await clock.advance(1000)
  expect(renders).toBe(settled)
})

test('a result older than five minutes leaves the row', async ($, on) => {
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  const old = (now - 6 * 60_000) / 1000
  state.marker = marker(now, { active: null, results: { recalled: { count: 3, at: old, ok: true } } })
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...BAND })
  expect(await ui.find({ type: 'Text', text: /memories used/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^cognee / })).toBeDefined()
  await ui.unmount()
})

test('the spinner only gains a suffix in a terminal for the async phases', async ($, on) => {
  const state = { marker: '' }
  const clock = wire(on, state)
  const now = clock.now()
  state.marker = marker(now, { active: { phase: 'saving', detail: '', started_at: now / 1000, updated_at: now / 1000 }, results: {} })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'terminal', ...SPINNER })
  expect(JSON.stringify(await ui.drawn())).toContain('saving…')
  await ui.unmount()
})

const CREDIT_ANSWER = {
  component: 'AssistantMessage' as const,
  props: {
    text: 'The lister asks the tenant first.\n\n> 🧠 Cognee memory [m4, m5]: the July notes on /users/me returning 404 on tenants.',
    isFirstOfReply: true,
  },
}

test('on the desktop the credit line under an answer carries the wave mark instead of the brain', async ($, on) => {
  const state = { marker: '' }
  wire(on, state)
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'desktop', ...CREDIT_ANSWER })
  expect(await ui.find({ type: 'Svg' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^The July notes on \/users\/me/ })).toBeDefined()
  // The label and the tags stay in the stored line only.
  expect(await ui.find({ type: 'Text', text: /Cognee memory|\[m4, m5\]/ })).toBeUndefined()
  // The brain is gone from what is drawn; the reply's own text is still the engine's.
  expect(await ui.find({ type: 'Text', text: /🧠/ })).toBeUndefined()
  await ui.unmount()
})

test('in a terminal the credit line under an answer carries the display logotype instead of the brain', async ($, on) => {
  const state = { marker: '' }
  wire(on, state)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'terminal', ...CREDIT_ANSWER })
  // Two rows of braille, the first opening the "c".
  expect(await ui.find({ type: 'Text', text: /^⡏⠁⡏⡇/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^⠉⠁⠉⠁/ })).toBeDefined()
  // Centred on a note longer than the mark.
  const drawn = JSON.stringify(await ui.drawn())
  expect(drawn).toContain('"alignSelf":"center"')
  expect(await ui.find({ type: 'Svg' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^The July notes on \/users\/me/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Cognee memory|\[m4, m5\]|🧠/ })).toBeUndefined()
  await ui.unmount()
})

test('an answer without a credit line is left to the engine', async ($, on) => {
  const state = { marker: '' }
  wire(on, state)
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({
    plugin: 'cognee-memory',
    surface: 'desktop',
    component: 'AssistantMessage',
    props: { text: 'Plain answer, no memory used.', isFirstOfReply: true },
  })
  expect(await ui.find({ type: 'Svg' })).toBeUndefined()
  await ui.unmount()
})

test('the mobile app keeps the credit line as the model wrote it', async ($, on) => {
  const state = { marker: '' }
  wire(on, state)
  await $.session.start({ cwd: '/work', surface: 'mobile', isInteractive: true })

  const ui = await $.ui.mount({ plugin: 'cognee-memory', surface: 'mobile', ...CREDIT_ANSWER })
  expect(await ui.find({ type: 'Svg' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /^the July notes/ })).toBeUndefined()
  await ui.unmount()
})
