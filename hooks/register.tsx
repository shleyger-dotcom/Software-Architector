// Software-Architector run panel: stages, tickets and every agent's actions beside the chat, from the journal the
// command hooks write (.software-architector/live/, see scripts/events.py). The band above the prompt reopens it.
import { atom, read, update } from 'claude-code'
import type { EngineInterface as Engine, Register } from 'claude-code'

import type { PanelView } from '../types'

const PANE = 'software-architector'
const COMMAND = 'bv-panel'
const WEB_COMMAND = 'bv-web'
const OUT_SHOWN = 60_000 // a tree draws 100 000 characters at most; the rest stays in the file
const ACTIONS_SHOWN = 200
const SILENCE_S = 300 // a running agent quiet this long is flagged: a long test run, or a stuck agent
const FILES_SHOWN = 15
const SUMMARY_LINES = 3

const isOpen = atom({ plugin: 'software-architector', key: 'isOpen' } as const, false)
const isPlaced = atom({ plugin: 'software-architector', key: 'isPlaced' } as const, true)
const view = atom({ plugin: 'software-architector', key: 'view' } as const, {} as PanelView)

type Stage = { id: string; label: string; status: string; note?: string; startedAt?: string | null
  finishedAt?: string | null }
type Ticket = { id: string; title: string; status: string; commit?: string; wave?: number | null; model?: string
  tests?: { passed: number; failed: number } | null; repairs?: number; retries?: number
  startedAt?: string | null; finishedAt?: string | null }
type Gate = { ok: boolean; at?: string } | null
type RunState = { title: string; dir: string; finishedAt: string | null; result?: string | null; accepted?: boolean | null
  unproven?: string[]; mode?: string
  depth?: string; tier?: string | null; startedAt?: string; updatedAt?: string
  stages: Stage[]; tickets: Ticket[]; gates: Record<string, Gate> }
type Ev = { t: string; k: string; tool?: string; s?: string; ok?: boolean; out?: string; chars?: number
  why?: string; type?: string; last?: string; spawned?: string; tokens?: number; ms?: number; tools?: number }
type Agent = { id: string; type: string; title: string; events: Ev[]; acts: Ev[]; running: boolean
  started?: string; stopped?: string; tokens?: number; ms?: number; ticket?: string
  hard: number; soft: Set<Ev>; denies: number }
type Run = { st: RunState; base: string; live: string; agents: Agent[]; url?: string; wait?: string }

const STAGE_MARK: Record<string, string> = { done: '✓', active: '●', pending: '·', skipped: '–', failed: '✗' }
const TICKET_STATUS: Record<string, string> = { pending: 'pending', 'in-progress': 'in progress', review: 'review',
  repair: 'repair', done: 'done', failed: 'failed' }
const AGENT_NAMES: Record<string, string> = { 'bv-scout': 'Scout', 'bv-research': 'Research', 'bv-coverage': 'Coverage G2',
  'bv-executor': 'Executor', 'bv-reviewer': 'Reviewer', 'bv-blind': 'Blind acceptance G4', 'bv-adr': 'ADR',
  'bv-memory': 'Memory' }
const MODE: Record<string, string> = { full: 'full auto', semi: 'semi-auto', interview: 'interview' }
const GATE_NAMES: Record<string, string> = { G1: 'Requirements', G2: 'Coverage', G3: 'Plan', G4: 'Blind acceptance', G5: 'Clean clone' }
const GATE_AGENT: Record<string, string> = { G2: 'bv-coverage', G4: 'bv-blind' }
const EDITS = new Set(['Write', 'Edit', 'MultiEdit', 'NotebookEdit'])
const TEST_RUN = /\b(pytest|unittest|jest|vitest|mocha|phpunit|rspec|go test|cargo test|dotnet test|(npm|pnpm|yarn|bun)( run)? test)\b/

// Parsed agent files by name, re-read only when their size changes; a module variable, rebuilt after a reload.
const cache = new Map<string, { size: number; events: Ev[] }>()

function runName(dir: string) {
  return (dir.split('/').pop() ?? dir).replace(/--wip$/, '')
}

function parseLines(text: string): Ev[] {
  const out: Ev[] = []
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    try {
      out.push(JSON.parse(line))
    } catch {
      // a line cut by a concurrent write: skip it, the next read has it whole
    }
  }
  return out
}

async function readRun($: Engine): Promise<Run | null> {
  const base = `${await $.session.root()}/.software-architector`
  let st: RunState
  try {
    st = JSON.parse(await $.fs.read(`${base}/state.json`))
  } catch {
    return null
  }
  const live = `${base}/live/${runName(st.dir)}`
  const files: Record<string, Ev[]> = {}
  try {
    for (const f of await $.fs.list(`${live}/agents`)) {
      if (!f.name.endsWith('.jsonl')) continue
      const key = `${live}/${f.name}`
      let hit = cache.get(key)
      if (!hit || hit.size !== f.size) {
        hit = { size: f.size, events: parseLines(await $.fs.read(`${live}/agents/${f.name}`)) }
        cache.set(key, hit)
      }
      files[f.name.slice(0, -'.jsonl'.length)] = hit.events
    }
  } catch {
    // no journal yet: the run has not called a tool since the plugin was updated
  }
  let url: string | undefined
  try {
    url = (await $.fs.read(`${base}/live/dashboard.url`)).trim() || undefined
  } catch {
    url = undefined
  }
  return { st, base, live, agents: toAgents(files, st.tickets), url, wait: waiting(st, files) }
}

// A failed test run the same agent later sees pass: red, then green, as test-first means it to be.
function redThenGreen(acts: Ev[]) {
  const out = new Set<Ev>()
  let red: Ev[] = []
  for (const e of acts) {
    if (e.k !== 'tool' || (e.tool !== 'Bash' && e.tool !== 'PowerShell') || !TEST_RUN.test(e.s ?? '')) continue
    if (e.ok === false) red.push(e)
    else {
      for (const x of red) out.add(x)
      red = []
    }
  }
  return out
}

function toAgents(files: Record<string, Ev[]>, tickets: Ticket[]): Agent[] {
  const spawns = new Map<string, Ev>()
  for (const evs of Object.values(files)) for (const e of evs) if (e.spawned) spawns.set(e.spawned, e)
  const agents = Object.entries(files).map(([id, events]): Agent => {
    const start = events.find(e => e.k === 'agent-start')
    const stop = [...events].reverse().find(e => e.k === 'agent-stop')
    const spawn = spawns.get(id)
    const type = id === 'main' ? 'main' : start?.type || ''
    const desc = spawn?.s?.replace(/^\S*:\s+/, '') ?? '' // "software-architector:bv-executor: Ticket 01: …" → "Ticket 01: …"
    const title = id === 'main' ? 'Orchestrator' : `${AGENT_NAMES[type] ?? (type || 'Agent')}${desc ? ` · ${desc}` : ''}`
    const acts = events.filter(e => e.k === 'tool' || e.k === 'deny')
    const soft = redThenGreen(acts)
    // shortcut: the ticket is found by its id in the spawn description ("Ticket 01: …", "T01 …"); record it on spawn if ids collide
    const ticket = desc ? tickets.find(t => new RegExp(`\\bT?${t.id}\\b`, 'i').test(desc))?.id : undefined
    return { id, type, title, events, acts, running: id !== 'main' && !!start && !stop, started: start?.t ?? events[0]?.t,
      stopped: stop?.t, tokens: spawn?.tokens, ms: spawn?.ms, ticket, soft,
      hard: acts.filter(e => e.k === 'tool' && e.ok === false).length - soft.size,
      denies: acts.filter(e => e.k === 'deny').length }
  })
  return agents.sort((a, b) => (a.started ?? '').localeCompare(b.started ?? ''))
}

// The orchestrator's last say: a question or an ended turn not answered yet means the run waits for the person.
function waiting(st: RunState, files: Record<string, Ev[]>) {
  if (st.finishedAt) return undefined
  const e = [...(files.main ?? [])].reverse().find(x => ['wait', 'resume', 'tool', 'deny'].includes(x.k))
  return e?.k === 'wait' ? e.last ?? '' : undefined
}

function dur(s: number) {
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h ${Math.floor(s / 60) % 60}m`
}

function secs(from: string, to?: string | null) {
  return Math.max(0, Math.round(((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000))
}

function since(from?: string | null, to?: string | null) {
  return from ? dur(secs(from, to)) : ''
}

function num(n: number) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n)
}

function took(a: Agent) {
  return a.running ? since(a.started) : a.ms ? dur(Math.round(a.ms / 1000)) : since(a.started, a.stopped)
}

function activeStage(st: RunState) {
  const i = st.stages.findIndex(s => s.status === 'active')
  const stage = st.stages[i]
  return stage ? { stage, n: i + 1 } : null
}

// The stage an agent belongs to: the last one that had started when the agent did.
function stageOf(a: Agent, stages: Stage[]) {
  const t = a.started ?? ''
  return [...stages].reverse().find(s => s.startedAt && s.startedAt <= t)?.id
}

function ticketInfo(t: Ticket) {
  return [t.wave != null && `wave ${t.wave}`, t.model, t.tests && `tests ${t.tests.passed}/${t.tests.failed}`,
    (t.repairs || t.retries) && `repairs ${t.repairs ?? 0} · retries ${t.retries ?? 0}`, t.commit?.slice(0, 7),
    t.startedAt && since(t.startedAt, t.finishedAt)].filter(Boolean).join(' · ')
}

async function open($: Engine, focus: boolean) {
  const r = await $.ui.open({ id: PANE, title: 'Software-Architector', ...(focus ? { focus: true as const } : {}) })
  await update($, isOpen, () => true)
  await update($, isPlaced, () => r.isPlaced)
  return r
}

function show($: Engine, next: PanelView) {
  void update($, view, () => next)
}

async function copy($: Engine, text: string, surface: Parameters<Engine['ui']['copy']>[0]['surface']) {
  const r = await $.ui.copy({ text, surface })
  $.ui.toast(r.isCopied ? `Copied: ${text}` : `Not copied (${r.reason})`)
}

// The web dashboard in the browser, its server started first when none serves the project: `bv web`.
async function openWeb($: Engine) {
  const root = $.plugin.root
  const script = `${root}/scripts/bv.py`
  // as bin/bv.cmd does on Windows: the py launcher, else python; elsewhere bin/bv finds a Python itself
  const tries = /^[A-Za-z]:/.test(root) ? [['py', '-3', script, 'web'], ['python', script, 'web']] : [[`${root}/bin/bv`, 'web']]
  let why = 'Python 3.9+ not found'
  for (const argv of tries) {
    try {
      const r = await $.process.run(argv, { cwd: await $.session.root(), timeoutMs: 20_000 })
      const text = r.exitCode === 0 ? `Web dashboard: ${r.stdout.trim()}` : `Web dashboard did not open: ${(r.stderr || r.stdout).trim()}`
      $.ui.toast(text)
      return text
    } catch (err) {
      why = String(err) // this launcher is missing: try the next one
    }
  }
  $.ui.toast(`Web dashboard did not open: ${why}`)
  return `Web dashboard did not open: ${why}`
}

// What could not be proven stays next to the word "delivered", never in a footnote.
const except = (st: RunState) => (st.unproven?.length ? `, except: ${st.unproven.join(', ')}` : '')

// Things worth a toast while the panel is closed: a failed gate, stage or check, a guard block, the end of the run.
function alerts(run: Run): Map<string, string> {
  const out = new Map<string, string>()
  for (const [g, v] of Object.entries(run.st.gates)) if (v && !v.ok) out.set(`gate:${g}:${v.at}`, `Gate ${g} failed`)
  for (const s of run.st.stages) if (s.status === 'failed') out.set(`stage:${s.id}`, `Stage "${s.label}" failed`)
  for (const a of run.agents) for (const e of a.events) {
    if (e.k === 'deny') out.set(`deny:${a.id}:${e.t}:${e.s}`, `Blocked: ${e.why ?? e.s}`)
  }
  if (run.st.finishedAt) out.set('finish', `${run.st.accepted === false ? 'Build NOT accepted' : `Build delivered${except(run.st)}`}: ${run.st.result ?? ''}`)
  return out
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: COMMAND, description: 'Software-Architector: open the run panel' })
    await $.command.register({ name: WEB_COMMAND, description: 'Software-Architector: open the web dashboard in the browser' })
    let sig = ''
    let seen: Set<string> | null = null
    $.clock.every(2000, () => {
      void (async () => {
        const base = `${await $.session.root()}/.software-architector`
        if (!(await $.fs.exists(`${base}/state.json`))) return
        const st = (await $.fs.list(base)).find(f => f.name === 'state.json')
        let now = `${st?.mtimeMs}:${st?.size}`
        const run = await readRun($)
        if (!run) return
        if (await $.fs.exists(`${run.live}/agents`)) {
          now += (await $.fs.list(`${run.live}/agents`)).map(f => `${f.name}:${f.size}`).join(',')
        }
        if (!run.st.finishedAt) now += `:${Math.floor(Date.now() / 10_000)}` // keeps the clocks and silences fresh
        if (now === sig) return
        sig = now
        $.ui.invalidate('ui.render')
        const found = alerts(run)
        if (seen && !(await read($, isOpen))) {
          const fresh = [...found].filter(([k]) => !seen?.has(k))
          const last = fresh.at(-1)
          if (last) $.ui.toast(`${last[1]} — /${COMMAND}`)
        }
        seen = new Set(found.keys())
      })().catch(() => undefined) // the next tick tries again
    })
    return next(e)
  })

  on('skill.prompt', async ($, e, next) => {
    if (/^(software-architector:)?build$/.test(e.skill) && e.text.includes('Software-Architector')) await open($, false)
    return next(e)
  })

  on('command.run', { command: COMMAND }, async $ => {
    try {
      const r = await open($, true)
      if (r.isPlaced) return { text: 'Software-Architector panel opened.' }
      const run = await readRun($)
      return { text: `The panel does not fit here (${r.reason}).` +
        (run?.url ? ` Web dashboard: ${run.url}` : ' Web dashboard: `bv serve --detach`.') }
    } catch (err) {
      return { text: `Panel did not open: ${String(err)}` }
    }
  })

  on('command.run', { command: WEB_COMMAND }, async $ => ({ text: await openWeb($) }))

  on('ui.close', { id: PANE }, async ($, e, next) => {
    await update($, isOpen, () => false).catch(() => undefined) // closing must never be held up by our state
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const run = await readRun($)
    if (!run) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    // plain: one row without chrome, so the band never spills onto the rows around it
    // a row Box, not <>…</>: a fragment is a column Box and stacks the buttons
    const buttons = <Box flexDirection="row">
      <Button key="open" label="Panel" hotkey="b" plain onPress={() => void open($, true)} />
      <Text> </Text>
      <Button key="web" label="Web" hotkey="w" plain onPress={() => void openWeb($)} />
    </Box>
    const shown = (await read($, isOpen)) && (await $.ui.panes()).some(p => p.id === PANE)
    // both buttons stay reachable while the project has a run: finished, or with the pane already open
    if (shown || run.st.finishedAt) {
      return (
        <Box>
          <Text dimColor>Software-Architector{run.st.finishedAt ? (run.st.accepted === false ? ' · not accepted' : ` · delivered${except(run.st)}`) : ''} </Text>
          {buttons}
        </Box>
      )
    }
    const at = activeStage(run.st)
    const tk = run.st.tickets
    const busy = run.agents.filter(a => a.running)
    const doing = busy.at(-1)?.acts.at(-1)
    const hard = run.agents.reduce((n, a) => n + a.hard, 0)
    const denies = run.agents.reduce((n, a) => n + a.denies, 0)
    const web = !(await read($, isPlaced)) && run.url
    return (
      <Box>
        {run.wait !== undefined && <Text color="warning" bold>⏸ waiting for you · </Text>}
        <Text dimColor wrap="truncate">
          Software-Architector · {at ? `${at.n}/${run.st.stages.length} ${at.stage.label}` : 'stage —'}
          {tk.length ? ` · tickets ${tk.filter(t => t.status === 'done').length}/${tk.length}` : ''}
          {busy.length ? ` · active ${busy.length}` : ''}{doing ? `: ${doing.tool} ${(doing.s ?? '').slice(0, 40)}` : ''}
        </Text>
        {hard > 0 && <Text color="error"> ✗{hard}</Text>}
        {denies > 0 && <Text color="error"> ⛔{denies}</Text>}
        <Text dimColor>{web ? ` · ${web} ` : ' '}</Text>
        {buttons}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text, Code } = $.ui.resolve(e)
    const run = await readRun($)
    if (!run) {
      return <Text dimColor>No run yet. Start: /software-architector:build &lt;idea&gt;</Text>
    }
    const { st } = run
    const v = await read($, view)
    const agent = v.agent ? run.agents.find(a => a.id === v.agent) : undefined

    if (agent && v.call) {
      const calls = agent.acts.filter(x => x.out)
      const i = calls.findIndex(x => x.out === v.call)
      const ev = calls[i]
      let source: string
      try {
        source = await $.fs.read(`${run.live}/out/${v.call}`)
      } catch {
        source = 'Output file not found.'
      }
      const cut = source.length > OUT_SHOWN
      const outPath = `${run.live}/out/${v.call}`
      const file = ev && (EDITS.has(ev.tool ?? '') || ev.tool === 'Read') ? ev.s : undefined
      const mark = ev?.ok !== false ? '' : agent.soft.has(ev) ? '✗ (red test, later green) ' : '✗ '
      return (
        <Box flexDirection="column">
          <Box flexWrap="wrap" gap={1}>
            <Button key="back" label="← To agent" onPress={() => show($, { ...v, call: undefined })} />
            {i > 0 && <Button key="prev" label="Earlier" hotkey="p" onPress={() => show($, { ...v, call: calls[i - 1]?.out })} />}
            {i >= 0 && i < calls.length - 1 &&
              <Button key="next" label="Later" hotkey="n" onPress={() => show($, { ...v, call: calls[i + 1]?.out })} />}
            <Button key="copy-out" label="Output path" hotkey="c" onPress={p => void copy($, outPath, p.surface)} />
            {file && <Button key="copy-file" label="File path" hotkey="f" onPress={p => void copy($, file, p.surface)} />}
          </Box>
          <Text bold wrap="wrap">{mark}{ev?.tool} · {ev?.s}</Text>
          <Text dimColor>{ev?.t?.slice(11, 19)} · {ev?.chars ?? source.length} chars{i >= 0 ? ` · call ${i + 1} of ${calls.length}` : ''}</Text>
          <Code source={cut ? source.slice(0, OUT_SHOWN) : source} />
          {cut && <Text dimColor wrap="wrap">Shown {OUT_SHOWN} characters. Full output: {outPath}</Text>}
        </Box>
      )
    }

    if (agent) {
      const list = agent.acts
      const bad = list.filter(x => x.k === 'deny' || (x.ok === false && !agent.soft.has(x)))
      const shownList = (v.errors ? bad : list).slice(-ACTIONS_SHOWN).reverse()
      const byTool = new Map<string, number>()
      for (const x of list) if (x.k === 'tool') byTool.set(x.tool ?? '?', (byTool.get(x.tool ?? '?') ?? 0) + 1)
      const tools = [...byTool].sort((a, b) => b[1] - a[1]).map(([t, n]) => `${t} ${n}`).join(' · ')
      const files = [...new Set(list.filter(x => x.k === 'tool' && x.ok !== false && EDITS.has(x.tool ?? '')).map(x => x.s ?? ''))]
      const lastBad = [...list].reverse().find(x => x.k === 'tool' && x.ok === false && !agent.soft.has(x))
      const last = [...agent.events].reverse().find(x => x.k === 'agent-stop')?.last
      const lines = last?.split('\n') ?? []
      const long = lines.length > SUMMARY_LINES
      return (
        <Box flexDirection="column">
          <Button key="back" label="← Overview" onPress={() => show($, { done: v.done })} />
          <Text bold wrap="wrap">{agent.title}</Text>
          <Text dimColor wrap="wrap">
            {agent.id === 'main' ? 'main thread' : agent.running ? `running ${took(agent)}` : `done in ${took(agent)}`}
            {' · '}actions {list.length}{agent.tokens ? ` · tokens ${num(agent.tokens)}` : ''}
          </Text>
          {(agent.hard > 0 || agent.soft.size > 0 || agent.denies > 0) && (
            <Text wrap="wrap">
              {agent.hard > 0 && <Text color="error">errors {agent.hard} </Text>}
              {agent.soft.size > 0 && <Text dimColor>red tests, later green: {agent.soft.size} </Text>}
              {agent.denies > 0 && <Text color="error">blocks {agent.denies}</Text>}
            </Text>
          )}
          {tools && <Text dimColor wrap="wrap">{tools}</Text>}
          {lastBad && (
            <Button key="last-bad" plain onPress={() => show($, { ...v, call: lastBad.out })}>
              <Text color="error">Last error: </Text>{lastBad.tool} <Text dimColor>{lastBad.s}</Text>
            </Button>
          )}
          {files.length > 0 && <Text> </Text>}
          {files.length > 0 && <Text bold>Files changed: {files.length}</Text>}
          {files.slice(0, FILES_SHOWN).map(f => (
            <Button key={`file:${f}`} plain dimColor onPress={p => void copy($, f, p.surface)}>{f}</Button>
          ))}
          {files.length > FILES_SHOWN && <Text dimColor>…{files.length - FILES_SHOWN} more</Text>}
          {last && <Text> </Text>}
          {last && <Text wrap="wrap">Result: {v.full || !long ? last : lines.slice(0, SUMMARY_LINES).join('\n') + ' …'}</Text>}
          {long && <Button key="full" label={v.full ? 'Collapse result' : 'Full result'} hotkey="f"
            onPress={() => show($, { ...v, full: !v.full })} />}
          <Text> </Text>
          <Box flexWrap="wrap" gap={1}>
            <Text dimColor>{v.errors ? 'Errors only, newest first:' : 'Newest first:'}</Text>
            {bad.length > 0 && <Button key="errors" label={v.errors ? 'All actions' : `Errors only (${bad.length})`}
              hotkey="e" onPress={() => show($, { ...v, errors: !v.errors })} />}
          </Box>
          {shownList.map((x, i) =>
            x.k === 'deny'
              ? <Text key={`d${i}`} color="error" wrap="wrap">⛔ {x.tool} {x.s} — {x.why}</Text>
              : <Button key={x.out ?? `a${i}`} plain onPress={() => show($, { ...v, call: x.out })}>
                  {x.ok === false
                    ? <Text color={agent.soft.has(x) ? 'inactive' : 'error'}>✗</Text>
                    : <Text color="success">✓</Text>}
                  {` ${x.tool} `}<Text dimColor>{x.s}</Text>
                </Button>)}
          {(v.errors ? bad : list).length > ACTIONS_SHOWN &&
            <Text dimColor>…{(v.errors ? bad : list).length - ACTIONS_SHOWN} earlier</Text>}
        </Box>
      )
    }

    const at = activeStage(st)
    const tk = st.tickets
    const subs = run.agents.filter(a => a.id !== 'main')
    const main = run.agents.find(a => a.id === 'main')
    const tokens = subs.reduce((n, a) => n + (a.tokens ?? 0), 0)
    const lastT = [st.updatedAt ?? '', ...run.agents.map(a => a.events.at(-1)?.t ?? '')].sort().at(-1)
    const quietRun = !st.finishedAt && run.wait === undefined && !!lastT && secs(lastT) > SILENCE_S
    const visible = (a: Agent) => v.done || a.running || a.hard > 0 || a.denies > 0
    const hiddenAll = subs.filter(a => !visible(a)).length

    const agentRow = (a: Agent) => {
      const lastEv = a.events.at(-1)
      const quiet = a.running && !!lastEv && secs(lastEv.t) > SILENCE_S
      const doing = a.acts.at(-1)
      return (
        <Box key={`ag:${a.id}`} flexDirection="column">
          <Button key={a.id} plain onPress={() => show($, { done: v.done, agent: a.id })}>
            <Text color={a.running ? 'claude' : 'success'}>{a.running ? '●' : '✓'}</Text>
            {` ${a.title} `}
            {a.hard > 0 && <Text color="error">✗{a.hard} </Text>}
            {a.soft.size > 0 && <Text color="inactive">✗{a.soft.size} </Text>}
            {a.denies > 0 && <Text color="error">⛔{a.denies} </Text>}
            <Text dimColor>· {a.acts.length} · {took(a)}</Text>
          </Button>
          {a.running && (doing || quiet) && (
            <Text color={quiet ? 'warning' : 'inactive'} wrap="truncate">
              {'  ↳ '}{doing ? `${doing.tool} ${doing.s ?? ''}` : ''}
              {quiet ? ` · silent ${since(lastEv?.t)}` : doing ? ` · ${since(doing.t)} ago` : ''}
            </Text>
          )}
        </Box>
      )
    }
    const group = (key: string, list: Agent[]) => {
      const hidden = list.filter(a => !visible(a)).length
      return (
        <Box key={key} flexDirection="column" paddingLeft={2}>
          {list.filter(visible).map(agentRow)}
          {hidden > 0 && <Text dimColor>hidden finished without errors: {hidden}</Text>}
        </Box>
      )
    }
    const byTicket = (id: string) => subs.filter(a => a.ticket === id)
    const byStage = (id: string) => subs.filter(a => !a.ticket && stageOf(a, st.stages) === id)
    const loose = subs.filter(a => !a.ticket && !stageOf(a, st.stages))

    return (
      <Box flexDirection="column">
        <Text bold wrap="wrap">{st.title}</Text>
        <Text dimColor wrap="wrap">
          {[st.mode && (MODE[st.mode] ?? st.mode), st.depth, st.tier,
            st.startedAt && (st.finishedAt ? `${st.accepted === false ? 'closed' : 'delivered'} in ${since(st.startedAt, st.finishedAt)}` : `running ${since(st.startedAt)}`),
            tokens > 0 && `tokens ${num(tokens)}`].filter(Boolean).join(' · ')}
        </Text>
        {lastT && !st.finishedAt && (
          <Text color={quietRun ? 'warning' : 'inactive'}>
            {quietRun ? `Silent for ${since(lastT)}: no events from any agent` : `updated ${since(lastT)} ago`}
          </Text>
        )}
        {st.finishedAt && (st.accepted === false
          ? <Text color="error" wrap="wrap">NOT accepted: {st.result ?? ''}</Text>
          : st.unproven?.length
            ? <Text color="warning" wrap="wrap">Delivered{except(st)} — {st.result ?? ''}</Text>
            : <Text color="success" wrap="wrap">Delivered: {st.result ?? ''}</Text>)}
        {run.wait !== undefined && (
          <Box flexDirection="column" borderStyle="round" borderColor="warning" paddingX={1}>
            <Text color="warning" bold>⏸ Waiting for you</Text>
            {run.wait && <Text wrap="wrap">{run.wait.length > 400 ? run.wait.slice(0, 400) + ' …' : run.wait}</Text>}
          </Box>
        )}

        <Text> </Text>
        <Text bold>Gates</Text>
        {Object.entries(st.gates).map(([g, x]) => {
          const busy = !x && run.agents.some(a => a.running && a.type === GATE_AGENT[g])
          return (
            <Text key={g} color={x ? (x.ok ? 'success' : 'error') : busy ? 'claude' : 'inactive'} wrap="truncate">
              {x ? (x.ok ? '✓' : '✗') : busy ? '●' : '·'} {g} {GATE_NAMES[g] ?? ''}
              {x?.at ? ` · ${x.at.slice(11, 16)}` : busy ? ' · running' : ''}
            </Text>
          )
        })}

        <Text> </Text>
        <Text bold>Stages{tk.length ? ` · tickets ${tk.filter(t => t.status === 'done').length}/${tk.length}` : ''}</Text>
        {st.stages.map(s => (
          <Box key={`st:${s.id}`} flexDirection="column">
            <Text color={s.status === 'failed' ? 'error' : s.status === 'done' ? 'success' : s.status === 'active' ? 'claude' : 'inactive'} wrap="truncate">
              {STAGE_MARK[s.status] ?? '·'} {s.label}
              {s.startedAt && s.status !== 'skipped' ? ` · ${since(s.startedAt, s.finishedAt)}` : ''}
              {s.note ? ` — ${s.note}` : ''}
            </Text>
            {s.id === 'build' && tk.map(t => (
              <Box key={`tk:${t.id}`} flexDirection="column" paddingLeft={2}>
                <Text wrap="truncate" color={t.status === 'failed' ? 'error' : t.status === 'done' ? 'success' : t.status === 'pending' ? undefined : 'claude'}>
                  T{t.id} {TICKET_STATUS[t.status] ?? t.status} · {t.title}
                </Text>
                {ticketInfo(t) && <Text dimColor wrap="truncate">{'  '}{ticketInfo(t)}</Text>}
                {group(`tka:${t.id}`, byTicket(t.id))}
              </Box>
            ))}
            {group(`sta:${s.id}`, byStage(s.id))}
          </Box>
        ))}
        {loose.length > 0 && <Text bold>Other agents</Text>}
        {loose.length > 0 && group('loose', loose)}

        <Text> </Text>
        {main && agentRow(main)}
        {(hiddenAll > 0 || v.done) && (
          <Button key="done" label={v.done ? 'Hide finished without errors' : `Show all agents (+${hiddenAll})`} hotkey="a"
            onPress={() => show($, { done: !v.done })} />
        )}
        {run.agents.length === 0 && <Text dimColor>Log is empty — actions appear with the first tool call.</Text>}
        <Text> </Text>
        <Box flexWrap="wrap" gap={1}>
          <Button key="web" label="Open in browser" hotkey="w" onPress={() => void openWeb($)} />
          {run.url && <Text dimColor wrap="wrap">{run.url}</Text>}
        </Box>
      </Box>
    )
  })
}
