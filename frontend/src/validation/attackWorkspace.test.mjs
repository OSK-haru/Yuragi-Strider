// Focused local checks. No server, browser, temporary files or network access.
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'
import path from 'node:path'
import ts from 'typescript'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const externalRequire = createRequire(import.meta.url)
const cache = new Map()
function load(relative) {
  const requested = path.resolve(sourceRoot, relative)
  const filename = [requested, `${requested}.ts`, `${requested}.tsx`].find(existsSync)
  if (!filename || !filename.startsWith(sourceRoot + path.sep)) throw new Error(`Invalid local module: ${relative}`)
  if (cache.has(filename)) return cache.get(filename).exports
  const module = { exports: {} }
  cache.set(filename, module)
  const source = readFileSync(filename, 'utf8')
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    fileName: filename,
  }).outputText
  const localRequire = (specifier) => {
    if (specifier.endsWith('.css')) return {}
    if (specifier.startsWith('.')) return load(path.relative(sourceRoot, path.resolve(path.dirname(filename), specifier)))
    if (specifier === 'react' || specifier === 'react/jsx-runtime') return externalRequire(specifier)
    throw new Error(`Unexpected external module: ${specifier}`)
  }
  // Only transpiles this repository's trusted source, never user code/commands.
  new Function('require', 'module', 'exports', output)(localRequire, module, module.exports)
  return module.exports
}

const {
  validateWorkspaceCircuit, validateCommandLine, validateSetupOverrides,
  programSubmitLine, disconnectedAttackClient, boundedClientCall, AttackClientFailure,
  CommandRequestStore, sendCommandIntent,
} = load('utils/attackWorkspace.ts')
const { exportCircuitConfigBundleJson, parseCircuitConfigJson } = load('utils/circuitConfigTransfer.ts')
const { resizeCircuitEditorState } = load('utils/circuitEditing.ts')
const checks = []
function check(label, operation) { operation(); checks.push(label) }
const circuit = {
  logical_qubits: 2, initial_states: [0, 0], classical_bits: 2,
  columns: [{ step: 0, gates: [{ id: 'h', type: 'H', targets: [0] }] },
    { step: 1, gates: [{ id: 'cx', type: 'CNOT', controls: [0], targets: [1] }] },
    { step: 2, gates: [{ id: 'm0', type: 'MEASURE', targets: [0], classical_targets: [0] },
      { id: 'm1', type: 'MEASURE', targets: [1], classical_targets: [1] }] }],
}
const json = exportCircuitConfigBundleJson(circuit)
check('existing Circuit Studio JSON roundtrip', () => {
  assert.equal(parseCircuitConfigJson(json).logical_qubits, 2)
  validateWorkspaceCircuit('bell circuit.json', json, [])
})
check('8-qubit editor compatibility, not a Reset-only input', () => {
  const eight = { logical_qubits: 8, initial_states: Array(8).fill(0), columns: [{ step: 0, gates: [] }] }
  validateWorkspaceCircuit('eight.json', exportCircuitConfigBundleJson(eight), [])
})
check('one-qubit common input and resize; Gate-aware keeps its two-qubit minimum', () => {
  const one = { logical_qubits: 1, initial_states: [0], classical_bits: 1,
    columns: [{ step: 0, gates: [{ id: 'm', type: 'MEASURE', targets: [0], classical_targets: [0] }] }] }
  const file = exportCircuitConfigBundleJson(one)
  validateWorkspaceCircuit('one.json', file, [])
  assert.equal(parseCircuitConfigJson(file, 1).logical_qubits, 1)
  assert.throws(() => parseCircuitConfigJson(file))
  assert.equal(resizeCircuitEditorState(circuit, 1, 1).logical_qubits, 1)
  assert.equal(resizeCircuitEditorState(circuit, 1).logical_qubits, 2)
})
check('symbolic folders accepted, OS paths / traversal rejected', () => {
  validateWorkspaceCircuit('circuits/bell.json', json, [])
  for (const name of ['', '..', '../x', 'a/../b', '/x', 'x/', 'x//y', 'C:\\a', 'x\0', 'x\n']) {
    assert.throws(() => validateWorkspaceCircuit(name, json, []))
  }
})
check('UTF-8 name bound', () => {
  validateWorkspaceCircuit('a'.repeat(256), json, [])
  assert.throws(() => validateWorkspaceCircuit('あ'.repeat(86), json, []))
})
check('16 files, replacing an existing name does not consume another slot', () => {
  const names = Array.from({ length: 16 }, (_, index) => `c${index}.json`)
  validateWorkspaceCircuit(names[0], json, names)
  assert.throws(() => validateWorkspaceCircuit('extra.json', json, names))
})
check('64 KiB / malformed circuit rejection', () => {
  assert.throws(() => validateWorkspaceCircuit('x', ' '.repeat(65537), []))
  assert.throws(() => validateWorkspaceCircuit('x', '{', []))
})
check('single command line bound; grammar not reinvented in UI', () => {
  validateCommandLine('program   submit "bell circuit.json" --shots 32 --device qpu')
  for (const line of ['', ' ', 'help\njob status x', 'help\0', 'a'.repeat(4097)]) {
    assert.throws(() => validateCommandLine(line))
  }
})
check('editor-generated command uses Python shlex quoting', () => {
  const filename = "circuits/Huew's bell.json"
  const line = programSubmitLine(filename, 'device-1', 32)
  const python = path.resolve(sourceRoot, '../../.venv/Scripts/python.exe')
  const parsed = JSON.parse(execFileSync(python, ['-c',
    'import json,sys,shlex; print(json.dumps(shlex.split(sys.stdin.read())))'],
  { input: line, encoding: 'utf8', timeout: 10000 }))
  assert.deepEqual(parsed, ['program', 'submit', filename, '--device', 'device-1', '--shots', '32'])
  for (const shots of [0, -1, 1.5, NaN, Infinity]) assert.throws(() => programSubmitLine('x', 'qpu', shots))
})
const scenario = {
  id: 'public-only', title: 'Public', description: '', mode: 'free', settings: [
    { id: 'delivery', label: 'Delivery', group: 'attacker', value: 'preconfigured_profile', editable: true,
      kind: 'choice', choices: [{ value: 'preconfigured_profile', label: 'Profile' }] },
    { id: 'width', label: 'Width', group: 'qpu_service', value: 2, editable: true, kind: 'number', min: 1, max: 8, step: 1 },
    { id: 'flag', label: 'Flag', group: 'attacker', value: false, editable: false, kind: 'boolean' },
    { id: 'path', label: 'Shared path', group: 'relation', value: 'declared', editable: true,
      kind: 'choice', choices: [{ value: 'declared', label: 'Declared' }] },
    { id: 'repeats', label: 'Repeats', group: 'experiment', value: 4, editable: true,
      kind: 'number', min: 1, max: 8, step: 1 },
  ],
}
check('free setup accepts only declared editable fields and ranges', () => {
  validateSetupOverrides(scenario, { width: 8 })
  for (const overrides of [{ hidden_label: 1 }, { flag: true }, { width: NaN }, { width: 9 }, { width: 2.5 }, { delivery: 'invented' }]) {
    assert.throws(() => validateSetupOverrides(scenario, overrides))
  }
})
check('graded mode prohibits overrides', () => {
  validateSetupOverrides({ ...scenario, mode: 'graded' }, {})
  assert.throws(() => validateSetupOverrides({ ...scenario, mode: 'graded' }, { width: 8 }))
})
check('relation and experiment use the same declared-field validation', () => {
  validateSetupOverrides(scenario, { path: 'declared', repeats: 8 })
  assert.throws(() => validateSetupOverrides(scenario, { path: 'invented' }))
  assert.throws(() => validateSetupOverrides(scenario, { repeats: 9 }))
  assert.throws(() => validateSetupOverrides({ ...scenario, mode: 'graded' }, { repeats: 4 }))
})
const { CircuitProvider } = load('context/CircuitContext.tsx')
const { AdminModeContext } = load('context/AdminModeContextCore.ts')
const { AttackSimulatorPage } = load('pages/AttackSimulatorPage.tsx')
const html = renderToStaticMarkup(React.createElement(AdminModeContext.Provider,
  { value: { adminModeEnabled: false, internalInfoVisible: false, setAdminModeEnabled() {} } },
  React.createElement(CircuitProvider, { gateDurationDefaults: {}, keyboardEnabled: false, minimumLogicalQubits: 1 },
    React.createElement(AttackSimulatorPage, { gateDurationDefaults: {} }))))
check('actual React initial render: disconnected, no fake counts / environment', () => {
  assert.ok(html.includes('HTTP未接続'))
  assert.ok(html.includes('未成立：被害者、QPU、キューは動作していません。'))
  assert.ok(html.includes('回路編集ワークスペース'))
  assert.ok(html.includes('共通コマンドターミナル'))
  assert.ok(!html.includes('環境成立・設定固定'))
  assert.ok(!html.includes('被害者秘密の値'))
  assert.ok(html.includes('min="1" max="8"'))
})
check('common setup renders relation and experiment groups without Reset controls', () => {
  assert.ok(html.includes('<legend>共有資源・経路</legend>'))
  assert.ok(html.includes('<legend>実験条件</legend>'))
  assert.ok(!html.includes('reset_success_probability'))
  assert.ok(!html.includes('hidden_seed'))
})
assert.equal(disconnectedAttackClient.connected, false)
await assert.rejects(() => disconnectedAttackClient.establish({}, new AbortController().signal))
checks.push('disconnected adapter rejects instead of simulating success')
assert.equal(await boundedClientCall(async () => 'ok'), 'ok')
await assert.rejects(() => boundedClientCall(async () => { throw new Error('private-secret-marker') }),
  (error) => error instanceof AttackClientFailure && !error.message.includes('private-secret-marker'))
checks.push('unexpected adapter errors are sanitized')
let created = false
await assert.rejects(() => boundedClientCall(async () => {
  created = true // Server commits but its response is lost; no real network.
  throw new TypeError('Failed to fetch')
}, 1000, true), (error) => error instanceof AttackClientFailure && error.uncertain)
assert.equal(created, true)
checks.push('lost lifecycle response is uncertain, not permission to recreate')
await assert.rejects(() => boundedClientCall(async () => {
  throw new AttackClientFailure('設定は拒否されました。', false)
}, 1000, true), (error) => error instanceof AttackClientFailure && !error.uncertain)
checks.push('explicit classified rejection remains certain')
let aborted = false
await assert.rejects(() => boundedClientCall((signal) => {
  signal.addEventListener('abort', () => { aborted = true })
  return new Promise(() => {})
}, 5), (error) => error instanceof AttackClientFailure && error.uncertain)
assert.equal(aborted, true)
checks.push('timeout aborts transport without claiming cancellation / establishment')

const registered = [{ name: 'bell circuit.json', json }]
const submit = programSubmitLine(registered[0].name, 'device-1', 32)
const requests = new CommandRequestStore()
const accepted = new Map()
let calls = 0
let loseReply = true
const localClient = {
  async executeCommand(sessionId, intent) {
    calls += 1
    assert.equal(sessionId, 'session-1')
    assert.equal(intent.line, submit)
    if (!accepted.has(intent.requestId)) accepted.set(intent.requestId, `job-${accepted.size + 1}`)
    if (loseReply) throw new TypeError('response lost after acceptance')
    return { outcome: 'confirmed', lines: [accepted.get(intent.requestId)] }
  },
}
const first = requests.begin('session-1', submit, registered)
await assert.rejects(() => sendCommandIntent(localClient, 'session-1', first, requests),
  (error) => error instanceof AttackClientFailure && error.uncertain)
assert.equal(calls, 1)
assert.equal(accepted.size, 1)
assert.strictEqual(requests.unresolved('session-1'), first)
loseReply = false
const retry = requests.retry('session-1', submit, registered)
assert.strictEqual(retry, first)
assert.deepEqual(await sendCommandIntent(localClient, 'session-1', retry, requests), ['job-1'])
assert.equal(accepted.size, 1)
assert.equal(requests.unresolved('session-1'), null)
checks.push('acceptance plus lost reply: no automatic retry; manual retry retains ID and one job')
const separate = requests.begin('session-1', submit, registered)
assert.notEqual(separate.requestId, first.requestId)
assert.deepEqual(await sendCommandIntent(localClient, 'session-1', separate, requests), ['job-2'])
assert.equal(accepted.size, 2)
checks.push('same circuit in a normal new execution receives a distinct request ID')

for (const reply of [{ outcome: 'unknown', lines: ['pretend success'] }, { lines: ['legacy ambiguous reply'] },
  { outcome: 'confirmed', lines: [42] }]) {
  const intent = requests.begin('session-1', submit, registered)
  await assert.rejects(() => sendCommandIntent({ executeCommand: async () => reply }, 'session-1', intent, requests),
    (error) => error instanceof AttackClientFailure && error.uncertain)
  assert.strictEqual(requests.unresolved('session-1'), intent)
}
checks.push('unknown and malformed replies remain unresolved and never produce successful output')

check('retry requires unchanged command and saved file content; new execution remains available', () => {
  const pending = requests.unresolved('session-1')
  assert.throws(() => requests.retry('session-1', `${submit} `, registered))
  const changed = [{ ...registered[0], json: `${json} ` }]
  assert.throws(() => requests.retry('session-1', submit, changed))
  const fresh = requests.begin('session-1', submit, changed)
  assert.notEqual(fresh.requestId, pending.requestId)
  requests.failed(fresh, true)
  requests.workspaceChanged() // Includes a PUT whose acknowledgement is lost.
  assert.throws(() => requests.retry('session-1', submit, changed))
})
check('common terminal grammar / explicit request IDs remain untouched in intent', () => {
  const original = 'program   submit "bell circuit.json" --shots 32 --request-id user-chosen --device device-1'
  const intent = requests.begin('session-1', original, registered)
  assert.equal(intent.line, original)
  requests.failed(intent, true)
  assert.equal(requests.retry('session-1', original, registered).line, original)
  // Actual priority resolution belongs to the backend integration, not a UI parser.
})
check('one retained operation; discarded / other sessions cannot revive it', () => {
  const old = requests.unresolved('session-1')
  const next = requests.begin('session-1', submit, registered)
  requests.failed(next, true)
  requests.failed(old, true) // Late completions cannot replace the current request.
  requests.confirmed(old)
  assert.strictEqual(requests.unresolved('session-1'), next)
  assert.throws(() => requests.retry('session-2', submit, registered))
  assert.equal(requests.unresolved('session-1'), null)
  const last = requests.begin('session-1', submit, registered)
  requests.failed(last, true)
  requests.clear()
  assert.throws(() => requests.retry('session-1', submit, registered))
  requests.failed(last, true)
  assert.equal(requests.unresolved('session-1'), null)
})
check('retained workspace snapshot has file-count and byte bounds', () => {
  assert.throws(() => requests.begin('session-1', submit, Array(17).fill(registered[0])))
  assert.throws(() => requests.begin('session-1', submit, [{ name: 'x', json: ' '.repeat(65537) }]))
})
const rejected = requests.begin('session-1', submit, registered)
const rejectClient = { executeCommand: async () => { throw new AttackClientFailure('受付前の拒否', false) } }
await assert.rejects(() => sendCommandIntent(rejectClient, 'session-1', rejected, requests))
assert.equal(requests.unresolved('session-1'), null)
const previouslyUnknown = requests.begin('session-1', submit, registered)
requests.failed(previouslyUnknown, true)
await assert.rejects(() => sendCommandIntent(rejectClient, 'session-1', previouslyUnknown, requests))
assert.strictEqual(requests.unresolved('session-1'), previouslyUnknown)
checks.push('certain initial rejection clears intent, but rejected retry cannot disprove prior acceptance')
let finishLate
const timed = requests.begin('session-1', submit, registered)
await assert.rejects(() => sendCommandIntent({ executeCommand: () => new Promise((resolve) => { finishLate = resolve }) },
  'session-1', timed, requests, 5), (error) => error instanceof AttackClientFailure && error.uncertain)
finishLate({ outcome: 'confirmed', lines: ['late acceptance'] })
await Promise.resolve()
assert.strictEqual(requests.unresolved('session-1'), timed)
checks.push('timeout retains identity and late transport success cannot silently settle the operation')
console.log(`PASS ${checks.length} focused UI boundary/render checks`)
checks.forEach((label) => console.log(`  ${label}`))
