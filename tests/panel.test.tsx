import { expect, test } from 'claude-code/testing'

// A run in its last stage: ticket 01 built (a red test turned green, one real failure), blind acceptance running,
// the orchestrator asking the person a question.
const B = '/p/.software-architector'
const L = `${B}/live/run1`
const at = (m: number) => `2026-10-28T12:${String(m).padStart(2, '0')}:00+03:00`
const stage = (id: string, label: string, s: number, f: number | null) =>
  ({ id, label, status: f === null ? 'active' : 'done', startedAt: at(s), finishedAt: f === null ? null : at(f), note: '' })
const STATE = {
  title: 'Plugin', dir: '.software-architector/runs/run1--wip', mode: 'semi', depth: 'normal', tier: 'T0', startedAt: at(0),
  updatedAt: at(40), finishedAt: null,
  stages: [stage('spec', 'Specification', 0, 10), stage('build', 'Build', 10, 30), stage('final', 'Acceptance', 30, null)],
  tickets: [{ id: '01', title: 'Whole plugin', status: 'done', wave: 1, model: 'strong', commit: 'a1b2c3d4e5',
    tests: { passed: 14, failed: 0 }, repairs: 1, retries: 0, startedAt: at(11), finishedAt: at(29) }],
  gates: { G1: { ok: true, at: at(5) }, G2: { ok: true, at: at(9) }, G3: { ok: true, at: at(10) }, G4: null },
}
const lines = (...evs: object[]) => evs.map(e => JSON.stringify(e)).join('\n') + '\n'
const tool = (m: number, t: string, s: string, ok: boolean, out: string) => ({ t: at(m), k: 'tool', tool: t, s, ok, out })
const FILES: Record<string, string> = {
  [`${B}/state.json`]: JSON.stringify(STATE),
  [`${L}/agents/main.jsonl`]: lines(
    { ...tool(11, 'Agent', 'software-architector:bv-executor: Ticket 01: whole plugin', true, 'm1'), spawned: 'ag1', tokens: 120000, ms: 780000 },
    { ...tool(31, 'Agent', 'software-architector:bv-blind: Blind acceptance by the brief', true, 'm2'), spawned: 'ag2' },
    tool(32, 'Bash', 'git status', false, 'm3'),
    { t: at(33), k: 'wait', last: 'Which bot are we building?' }),
  [`${L}/agents/ag1.jsonl`]: lines(
    { t: at(11), k: 'agent-start', type: 'bv-executor' },
    tool(12, 'Write', 'tests/test_a.py', true, 'a1'),
    tool(13, 'Bash', 'python -m pytest -q', false, 'a2'),
    tool(14, 'Edit', 'src/a.py', true, 'a3'),
    tool(15, 'Bash', 'python -m pytest -q', true, 'a4'),
    tool(16, 'Bash', 'cat nope', false, 'a5'),
    { t: at(17), k: 'agent-stop', last: 'done\nsecond\nthird\nfourth' }),
  [`${L}/agents/ag2.jsonl`]: lines({ t: at(31), k: 'agent-start', type: 'bv-blind' }, tool(32, 'Read', 'brief.md', true, 'b1')),
  [`${L}/out/a5`]: '── input ──\ncat nope\n\n── output ──\nNo such file',
}

// the engine hands paths in the platform's form: C:\p\… on Windows
const norm = (p: string) => p.replace(/\\/g, '/').replace(/^[A-Za-z]:/, '')

const PANE = {
  plugin: 'software-architector', component: 'Pane', requestId: 'software-architector',
  props: { title: 'Software-Architector', isFocused: true, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 80 }, view: {} },
} as const

test('the pane shows the run, an agent and one of its calls', async ($, on) => {
  const copied: string[] = []
  on('session.root', () => ({ value: '/p' }))
  on('fs.read', (_, e) => {
    const f = FILES[norm(e.path)]
    if (f === undefined) throw new Error(`ENOENT ${e.path}`)
    return { value: f }
  })
  on('fs.exists', (_, e) => ({ value: Object.keys(FILES).some(f => f.startsWith(norm(e.path))) }))
  on('fs.list', (_, e) => {
    const dir = norm(e.path)
    return { value: Object.keys(FILES).filter(f => f.startsWith(`${dir}/`)).map(f => (
      { name: f.slice(dir.length + 1), kind: 'file' as const, size: FILES[f]?.length ?? 0, mtimeMs: 0, isLink: false })) }
  })
  on('ui.panes', () => ({ value: [] }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.copy', (_, e) => {
    copied.push(e.text)
    return { value: { isCopied: true as const } }
  })

  for (const surface of ['vscode', 'terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /Waiting for you/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Which bot are we building\?/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /G4 Blind acceptance · running/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /wave 1 · strong · tests 14\/0 · repairs 1 · retries 0 · a1b2c3d/ })).toBeDefined()
    expect((await ui.find({ key: 'ag1' }))?.text).toMatch(/Executor · Ticket 01: whole plugin ✗1 ✗1 · 5 · 13m/)
    expect(await ui.find({ type: 'Text', text: /↳ Read brief\.md/ })).toBeDefined()

    await ui.press({ key: 'ag1' })
    expect(await ui.find({ type: 'Text', text: /errors 1/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /red tests, later green: 1/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Bash 3 · Write 1 · Edit 1/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /fourth/ })).toBeUndefined()
    await ui.press({ key: 'full' })
    expect(await ui.find({ type: 'Text', text: /fourth/ })).toBeDefined()
    await ui.press({ key: 'errors' })
    expect(await ui.findAll({ type: 'Button', text: /pytest/ })).toHaveLength(0)
    await ui.press({ key: 'file:src/a.py' })

    await ui.press({ key: 'last-bad' })
    expect(await ui.find({ type: 'Text', text: /✗ Bash · cat nope/ })).toBeDefined()
    expect(await ui.find({ key: 'next' })).toBeUndefined()
    await ui.press({ key: 'copy-out' })
    await ui.press({ key: 'prev' })
    expect(await ui.find({ type: 'Text', text: /Bash · python -m pytest -q/ })).toBeDefined()

    await ui.press({ key: 'back' })
    await ui.press({ key: 'back' })
    await ui.unmount()
  }
  expect(copied).toEqual(['src/a.py', `${L}/out/a5`, 'src/a.py', `${L}/out/a5`, 'src/a.py', `${L}/out/a5`])
})

// A finished run: the files of the run above, with this state on disk.
const serveState = (on: any, state: object) => {
  on('session.root', () => ({ value: '/p' }))
  on('fs.read', (_: unknown, e: { path: string }) => {
    const f = norm(e.path) === `${B}/state.json` ? JSON.stringify(state) : FILES[norm(e.path)]
    if (f === undefined) throw new Error(`ENOENT ${e.path}`)
    return { value: f }
  })
  on('fs.exists', (_: unknown, e: { path: string }) => ({ value: Object.keys(FILES).some(f => f.startsWith(norm(e.path))) }))
  on('fs.list', (_: unknown, e: { path: string }) => {
    const dir = norm(e.path)
    return { value: Object.keys(FILES).filter(f => f.startsWith(`${dir}/`)).map(f => (
      { name: f.slice(dir.length + 1), kind: 'file' as const, size: FILES[f]?.length ?? 0, mtimeMs: 0, isLink: false })) }
  })
  on('ui.panes', () => ({ value: [] }))
  on('ui.toast', () => ({ value: undefined }))
}

test('a run closed without acceptance never looks delivered', async ($, on) => {
  serveState(on, { ...STATE, finishedAt: at(50), result: 'half works', accepted: false,
    gates: { ...STATE.gates, G5: { ok: false, at: at(45) } } })
  const ui = await $.ui.mount({ ...PANE, surface: 'vscode' })
  expect(await ui.find({ type: 'Text', text: /NOT accepted: half works/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /G5 Clean clone/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Delivered/ })).toBeUndefined()
  await ui.unmount()
})

test('what could not be proven is named next to "delivered"', async ($, on) => {
  serveState(on, { ...STATE, finishedAt: at(50), result: 'works', accepted: true, unproven: ['S03', 'R10'] })
  const ui = await $.ui.mount({ ...PANE, surface: 'vscode' })
  expect(await ui.find({ type: 'Text', text: /Delivered, except: S03, R10 — works/ })).toBeDefined()
  await ui.unmount()
})

test('the band above the prompt says the run waits and what fails', async ($, on) => {
  on('session.root', () => ({ value: '/p' }))
  on('fs.read', (_, e) => {
    const f = FILES[norm(e.path)]
    if (f === undefined) throw new Error(`ENOENT ${e.path}`)
    return { value: f }
  })
  on('fs.list', (_, e) => {
    const dir = norm(e.path)
    return { value: Object.keys(FILES).filter(f => f.startsWith(`${dir}/`)).map(f => (
      { name: f.slice(dir.length + 1), kind: 'file' as const, size: FILES[f]?.length ?? 0, mtimeMs: 0, isLink: false })) }
  })
  on('ui.panes', () => ({ value: [] }))
  const runs: (readonly string[])[] = []
  const toasts: string[] = []
  on('process.run', (_, e) => {
    runs.push(e.argv)
    return { value: { exitCode: 0, stdout: 'http://localhost:8702\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.toast', (_, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })

  for (const surface of ['vscode', 'terminal'] as const) {
    const ui = await $.ui.mount({ plugin: 'software-architector', component: 'AbovePrompt', surface,
      props: { hasSurvey: false, isCollapsed: false, isWorking: true, maxRows: 3, bodyColumns: 120, scroll: { offset: 0, bodyRows: 3 }, view: {} } })
    expect(await ui.find({ type: 'Text', text: /waiting for you/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /3\/3 Acceptance · tickets 1\/1 · active 1: Read brief\.md/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /✗2/ })).toBeDefined()
    await ui.press({ key: 'web' })
    await ui.unmount()
  }
  expect(runs.map(r => r.at(-1))).toEqual(['web', 'web'])
  expect(toasts).toEqual(['Web dashboard: http://localhost:8702', 'Web dashboard: http://localhost:8702'])
})
