// Page decisions through the real client with in-memory HTTP/WebSocket doubles.
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
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
  if (!filename || !filename.startsWith(sourceRoot + path.sep)) throw new Error('Invalid local module')
  if (cache.has(filename)) return cache.get(filename).exports
  const module = { exports: {} }
  cache.set(filename, module)
  const output = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    fileName: filename,
  }).outputText
  const localRequire = (specifier) => {
    if (specifier.endsWith('.css')) return {}
    if (specifier.startsWith('.')) return load(path.relative(sourceRoot, path.resolve(path.dirname(filename), specifier)))
    if (specifier === 'react' || specifier === 'react/jsx-runtime') return externalRequire(specifier)
    throw new Error(`Unexpected external module: ${specifier}`)
  }
  new Function('require', 'module', 'exports', output)(localRequire, module, module.exports)
  return module.exports
}
const settings = load('utils/attackLiveSettings.ts')
const topology = load('utils/attackLiveTopology.ts')
const programValidation = load('utils/attackProgramValidation.ts')
const livePageSource = readFileSync(path.join(sourceRoot, 'pages', 'AttackLivePage.tsx'), 'utf8')
const checks = []

check('program preflight reports missing final measurement, gates after measurement, and missing classical targets', () => {
  const circuit = (columns, classical_bits = 2) => ({ version: 1, kind: 'quantscope_circuit_config',
    circuit_config: { logical_qubits: 2, initial_states: [0, 0], classical_bits, columns } })
  const gate = (type, targets = [0], classical_targets) => ({ type, targets,
    ...(classical_targets === undefined ? {} : { classical_targets }) })
  const noMeasurement = programValidation.validateAttackProgramShape(circuit([
    { step: 0, gates: [gate('H')] },
  ]))
  assert.deepEqual(noMeasurement.map(issue => issue.code), ['measurement_missing'])
  assert.match(noMeasurement[0].message, /最後に測定/)

  const afterMeasurement = programValidation.validateAttackProgramShape(circuit([
    { step: 0, gates: [gate('MEASURE', [0], [0])] },
    { step: 1, gates: [gate('X', [1])] },
  ]))
  assert.deepEqual(afterMeasurement.map(issue => issue.code), ['gate_after_measurement'])
  assert.match(afterMeasurement[0].message, /step 1.*X.*測定の後/)

  const missingTarget = programValidation.validateAttackProgramShape(circuit([
    { step: 0, gates: [gate('MEASURE')] },
  ]))
  assert.deepEqual(missingTarget.map(issue => issue.code), ['measurement_classical_target_missing'])
  assert.match(missingTarget[0].message, /古典bitが指定されていません/)
})

check('program preflight follows compiler step ordering and validates multiple final measurement blocks', () => {
  const bundle = { version: 1, kind: 'quantscope_circuit_config', circuit_config: {
    logical_qubits: 2, initial_states: [0, 0], classical_bits: 2,
    columns: [
      { step: 4, gates: [{ type: 'MEASURE', targets: [1], classical_targets: [1] }] },
      { step: 0, gates: [{ type: 'H', targets: [0] }] },
      { step: 5, gates: [{ type: 'MEASURE', targets: [0], classical_targets: [0] }] },
    ],
  } }
  assert.deepEqual(programValidation.validateAttackProgramShape(bundle), [])
})

check('program validation output is presented in the attacker terminal area', () => {
  assert.match(livePageSource, /aria-label="攻撃者ターミナル"/)
  assert.match(livePageSource, /aria-label="コマンドの実行結果とreply"/)
  assert.doesNotMatch(livePageSource, /届いたreply/)
  assert.match(livePageSource, /if \(programIssues\.length\) return/)
  assert.doesNotMatch(livePageSource, /回路の送信前チェック/)
})

check('circuit submission is a terminal command, not a button', () => {
  const terminal = load('utils/attackTerminal.ts')
  assert.deepEqual(terminal.parseTerminalCommand('submit'), { kind: 'submit' })
  assert.deepEqual(terminal.parseTerminalCommand('  submit --shots 64 --init inherit '),
    { kind: 'submit', shots: 64, initialization: 'inherit' })
  for (const bad of ['submit --shots 0', 'submit --shots 1.5', 'submit --shots', 'submit --init later', 'submit --fast']) {
    assert.equal(terminal.parseTerminalCommand(bad).kind, 'error', bad)
  }
  assert.deepEqual(terminal.parseTerminalCommand('submit --shots 32 --interleave other'),
    { kind: 'submit', shots: 32, interleave: 'victim', initialization: 'inherit' })
  assert.deepEqual(terminal.parseTerminalCommand('submit --interleave ref:1 --init inherit'),
    { kind: 'submit', interleave: 'training:1', initialization: 'inherit' })
  for (const bad of ['submit --interleave', 'submit --interleave attacker', 'submit --interleave other --init prepare']) {
    assert.equal(terminal.parseTerminalCommand(bad).kind, 'error', bad)
  }
  assert.deepEqual(terminal.parseTerminalCommand('answer --threshold 0.05 --direction ge'),
    { kind: 'answer', submission: { threshold: 0.05, direction: 'ge' } })
  assert.deepEqual(terminal.parseTerminalCommand('answer --direction le --threshold 1'),
    { kind: 'answer', submission: { threshold: 1, direction: 'le' } })
  assert.deepEqual(terminal.parseTerminalCommand('answer --message WhatIdo'),
    { kind: 'answer', submission: { message: 'WhatIdo' } })
  assert.deepEqual(terminal.parseTerminalCommand('answer --message two  words'),
    { kind: 'answer', submission: { message: 'two words' } })
  assert.equal(terminal.parseTerminalCommand('answer --message').kind, 'error')
  for (const bad of ['answer', 'answer --threshold 0.1', 'answer --threshold 1.5 --direction ge',
    'answer --threshold -1 --direction ge', 'answer --threshold abc --direction ge',
    'answer --threshold 0.1 --direction up', 'answer --threshold 0.1 --threshold 0.2 --direction ge']) {
    assert.equal(terminal.parseTerminalCommand(bad).kind, 'error', bad)
  }
  assert.deepEqual(terminal.parseTerminalCommand('status job-1'), { kind: 'job', action: 'status', jobId: 'job-1' })
  assert.deepEqual(terminal.parseTerminalCommand('cancel'), { kind: 'job', action: 'cancel' })
  assert.equal(terminal.parseTerminalCommand('result a b').kind, 'error')
  assert.deepEqual(terminal.parseTerminalCommand('wait 250us'), { kind: 'wait', target: 'duration', durationUs: 250 })
  assert.deepEqual(terminal.parseTerminalCommand('wait any'), { kind: 'wait', target: 'any' })
  assert.equal(terminal.parseTerminalCommand('wait soon').kind, 'error')
  assert.equal(terminal.parseTerminalCommand('rm -rf /').kind, 'error')
  assert.equal(terminal.parseTerminalCommand('   '), null)
  assert.deepEqual(terminal.parseTerminalCommand('connect'), { kind: 'connect' })
  assert.deepEqual(terminal.parseTerminalCommand('pending'), { kind: 'pending' })
  assert.equal(terminal.parseTerminalCommand('connect now').kind, 'error')
  const requestId = '21fc46f1bc89446aa88aa3ff28e90fcd'
  assert.deepEqual(terminal.parseTerminalCommand(`wait ${requestId}`), { kind: 'wait', target: 'request', requestId })
  assert.match(livePageSource, /aria-label="コマンドの実行結果とreply"/)
  assert.deepEqual(terminal.parseTerminalCommand('submit --measure q0=c1,q1=c0'), { kind: 'submit', measure: { 0: 1, 1: 0 } })
  assert.deepEqual(terminal.parseTerminalCommand('submit --measure 2:3 --shots 8'), { kind: 'submit', measure: { 2: 3 }, shots: 8 })
  for (const bad of ['submit --measure', 'submit --measure q0', 'submit --measure q0=c1,q0=c2', 'submit --measure x=y']) {
    assert.equal(terminal.parseTerminalCommand(bad).kind, 'error', bad)
  }
})

check('measurement targets: --measure overrides, editor targets stay, the rest map qN to cN', () => {
  const bundle = { circuit_config: { logical_qubits: 3, classical_bits: 0, columns: [{ step: 0, gates: [
    { type: 'MEASURE', targets: [0] },
    { type: 'MEASURE', targets: [1], classical_targets: [2] },
    { type: 'MEASURE', targets: [2] },
  ] }] } }
  const auto = settings.assignMeasurementTargets(bundle)
  assert.deepEqual(auto.circuit_config.columns[0].gates.map((gate) => gate.classical_targets), [[0], [2], [2]])
  const mapped = settings.assignMeasurementTargets(bundle, { 0: 1, 2: 0 })
  assert.deepEqual(mapped.circuit_config.columns[0].gates.map((gate) => gate.classical_targets), [[1], [2], [0]])
  assert.equal(mapped.circuit_config.classical_bits, 3)
  assert.equal(bundle.circuit_config.columns[0].gates[0].classical_targets, undefined, 'editor circuit is untouched')
  assert.doesNotMatch(livePageSource, /未指定の測定先を同番号の古典bitに設定/)
  assert.doesNotMatch(livePageSource, /<label>shots<input/)
})

check('every attacker service operation is a terminal command; only environment management stays a button', () => {
  for (const label of ['編集した回路を送信', '最も早いreplyを待つ', '手動で接続', '時間を指定して待機', 'この要求を待つ', 'このジョブIDを使用']) {
    assert.ok(!livePageSource.includes(`${label}</button>`), label)
  }
  assert.doesNotMatch(livePageSource, /onClick=\{\(\) => jobAction\(/)
  assert.doesNotMatch(livePageSource, /onClick=\{\(\) => void workspace\.(submit|job|wait|connect)\(/)
})

check('service timing projection drops local duration only, preserving angles and editor', () => {
  const original = { version: 1, kind: 'quantscope_circuit_config', circuit_config: {
    logical_qubits: 1, initial_states: [0], columns: [{ step: 0,
      gates: [{ type: 'RX', targets: [0], params: { theta_rad: 0.5, duration_us: 999 } }] }],
  } }
  assert.deepEqual(settings.applyServiceGateDurations(original).circuit_config.columns[0].gates[0].params,
    { theta_rad: 0.5 })
  assert.equal(original.circuit_config.columns[0].gates[0].params.duration_us, 999)
})

check('explicit measurement binding preserves the editor and existing classical targets', () => {
  const original = { version: 1, kind: 'quantscope_circuit_config', circuit_config: {
    logical_qubits: 2, initial_states: [0, 0], classical_bits: 0,
    columns: [{ step: 0, gates: [{ type: 'MEASURE', targets: [0] },
      { type: 'MEASURE', targets: [1], classical_targets: [3] }] }],
  } }
  const bound = settings.bindUnspecifiedMeasurements(original)
  assert.equal(bound.circuit_config.classical_bits, 4)
  assert.deepEqual(bound.circuit_config.columns[0].gates.map(gate => gate.classical_targets), [[0], [3]])
  assert.equal(original.circuit_config.classical_bits, 0)
  assert.equal(original.circuit_config.columns[0].gates[0].classical_targets, undefined)
})
const { createLiveWorkspace } = load('utils/attackLiveWorkspace.ts')
const { createAttackEnvironmentClient } = load('clients/attackLiveClient.ts')
function check(label, operation) { operation(); checks.push(label) }
const field = { path: 'qpu_service.delay', label: '遅延', type: 'number', minimum: 0,
  maximum: 10, default: 2, basis: 'Declared', description: 'Declared' }
const calibration = { ...field, path: 'qpu_service.idle_dynamics.qubits.0.t1_us', default: 3 }
const frequency = { ...field, path: 'qpu_service.idle_dynamics.physical_inputs.qubit_frequency_ghz',
  label: '周波数', required: true, default: 5 }
const legacyStandard = { source: 'historical-source', device: 'ibm_kingston', physical_qubit_id: 150,
  calibrated_at: '2026-08-03', job_id: 'historical-job', scope_notice: 'Historical one-qubit value', values: {} }
const catalog = {
  schema_version: 'attack-settings-catalog-v1', modes: [{ id: 'free' }, { id: 'challenge' }],
  free_mode: { idle_noise_modes: [
    { id: 'calibration_values', available: true, fields: [field, calibration] },
    { id: 'physical_parameters', available: true, fields: [field, frequency] },
  ], standard_initial_values: legacyStandard },
  challenges: [
    { id: 'reset-leakage', title: 'Reset漏洩', available: false, fields: [] },
    { id: 'public-demo', title: '課題', available: true, fields: [field] },
  ],
}
check('mode selection excludes inactive fields and unavailable challenges', () => {
  assert.deepEqual(settings.fieldsFor(catalog, { mode: 'free', noiseMode: 'physical_parameters' }), [field, frequency])
  assert.deepEqual(settings.fieldsFor(catalog, { mode: 'challenge', challengeId: 'reset-leakage' }), [])
  assert.deepEqual(settings.fieldsFor(catalog, { mode: 'challenge', challengeId: 'public-demo' }), [field])
  assert.throws(() => settings.assertSupportedCatalog({ ...catalog, schema_version: 'future' }))
})
check('required physical frequency never silently uses a catalog initial value', () => {
  const values = settings.initialValues([field, frequency])
  assert.equal(values[field.path], 2)
  assert.equal(values[frequency.path], undefined)
  assert.ok(settings.fieldErrors([field, frequency], values)[frequency.path])
  assert.throws(() => settings.buildCreateRequest({ mode: 'free', noiseMode: 'physical_parameters' }, [field, frequency], values))
})
check('per-field type/range/finite/integer/UTF-8/choice checks', () => {
  for (const value of ['2', true, NaN, Infinity, -1, 11]) assert.ok(settings.validateValue(field, value))
  assert.ok(settings.validateValue({ ...field, type: 'integer' }, 1.5))
  assert.ok(settings.validateValue({ ...field, type: 'boolean' }, 'false'))
  assert.ok(settings.validateValue({ ...field, type: 'choice', choices: ['yes'] }, 'no'))
  assert.ok(settings.validateValue({ ...field, type: 'string' }, 'あ'.repeat(86)))
  assert.equal(settings.validateValue(field, undefined), null)
})
check('request contains visible edited settings only, never basis/hidden/inactive values', () => {
  assert.deepEqual(settings.buildCreateRequest({ mode: 'free', noiseMode: 'calibration_values' },
    [field], { [field.path]: field.default }).settings, {})
  const request = settings.buildCreateRequest({ mode: 'free', noiseMode: 'calibration_values' }, [field],
    { [field.path]: 4, [frequency.path]: 9, credential: 'not-input', basis: 'fabricated' })
  assert.deepEqual(request, { mode: 'free', idle_noise_mode: 'calibration_values', settings: { [field.path]: 4 } })
  assert.deepEqual(settings.buildCreateRequest({ mode: 'challenge', challengeId: 'public-demo' }, [], {}),
    { mode: 'challenge', preset_id: 'public-demo', settings: {} })
  assert.throws(() => settings.buildCreateRequest({ mode: 'free', noiseMode: 'calibration_values' }, Array(129).fill(field), {}))
})
check('duration grammar, finite bounds and clock formatting', () => {
  assert.equal(settings.parseWaitDuration(' 1.5ms '), 1500)
  assert.equal(settings.parseWaitDuration('250us'), 250)
  assert.equal(settings.parseWaitDuration('2s'), 2_000_000)
  for (const input of ['0us', '-2s', 'Infinitys', '1', '1e3us']) assert.throws(() => settings.parseWaitDuration(input))
  assert.equal(settings.formatWorldTime(250), '250.000 µs')
  assert.equal(settings.formatWorldTime(1500), '1.500 ms')
  assert.equal(settings.formatWorldTime(NaN), '—')
  assert.ok(!settings.describeReplyError('private-secret-marker').includes('private-secret-marker'))
})

const SECRET = 'virtual-credential-must-never-enter-page-state'
const ENVIRONMENT = '0123456789abcdef0123456789abcdef'
const JOB = 'job-01234567-89ab-cdef-0123-456789abcdef'
function fixture(clockMode = 'host_clock') {
  const calls = [], sockets = []
  let creationFailure = null, discardFailure = false, limited = false
  let worldClock = 0, requestSequence = 0
  const serverPending = []
  class FakeSocket {
    readyState = 0
    listeners = new Map()
    sent = []
    addEventListener(type, callback) {
      if (!this.listeners.has(type)) this.listeners.set(type, new Set())
      this.listeners.get(type).add(callback)
    }
    removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback) }
    emit(type, event = {}) { for (const callback of this.listeners.get(type) ?? []) callback(event) }
    message(value) { this.emit('message', { data: JSON.stringify(value) }) }
    send(raw) {
      const frame = JSON.parse(raw)
      this.sent.push(frame)
      if (frame.type === 'authenticate') queueMicrotask(() => this.message({ type: 'ready', world_time_us: worldClock,
        clock_mode: clockMode, pause_while_catching_up: false }))
      else if (frame.operation === 'request.pending.list') queueMicrotask(() => this.message({
        type: 'pending.list', command_id: frame.command_id, requests: serverPending.map(item => ({ ...item })),
      }))
      else if (frame.operation.startsWith('wait.')) queueMicrotask(() => {
        worldClock += frame.payload.duration_us ?? 1
        this.message({ type: 'wait.completed', command_id: frame.command_id,
          outcome: limited ? 'limited' : 'reached', world_time_us: worldClock })
        if (!limited) for (const item of serverPending.splice(0)) this.message({
          type: 'reply', request_id: item.request_id, error_code: null,
          value: { job_id: JOB, status: 'succeeded', result: { shots: 32, counts: { '0': 17, '1': 15 } } },
        })
      })
      else queueMicrotask(() => {
        const requestId = `request-${++requestSequence}`
        serverPending.push({ request_id: requestId,
          kind: { 'program.submit': 'submit', 'job.status.read': 'status',
            'job.result.read': 'result', 'job.cancel': 'cancel',
            'job.victim_activity.read': 'victim_activity' }[frame.operation],
          job_id: frame.payload.job_id ?? null, issued_at_world_us: worldClock })
        this.message({ type: 'accepted', command_id: frame.command_id, request_id: requestId,
          issued_at_world_us: worldClock })
        if (frame.operation === 'job.victim_activity.read') setTimeout(() => {
          for (const item of serverPending.splice(0)) for (const socket of sockets) if (socket.readyState === 1) socket.message({
            type: 'reply', request_id: item.request_id, error_code: null,
            value: { job_id: JOB, reference: 'training:1', victim_prepared_bit: 1, shots_total: 1, shots_shown: 1,
              shots: [{ index: 0, steps: [{ actor: 'victim', kind: 'victim_run', start_us: 0, end_us: 2, tenant: 'private-tenant', density: [[1]] }] }],
              attacker_credential: SECRET, host_time: 9123 },
          })
        }, 5)
        if (frame.operation === 'program.submit') setTimeout(() => {
          worldClock += 1
          for (const socket of sockets) if (socket.readyState === 1) socket.message({ type: 'clock', world_time_us: worldClock })
          for (const item of serverPending.splice(0)) {
            const reply = {
              type: 'reply', request_id: item.request_id, error_code: null,
              value: { job_id: JOB, status: 'succeeded', result: { shots: 32, counts: { '0': 17, '1': 15 }, credential: SECRET },
                attacker_credential: SECRET, tenant: 'private-tenant', host_time: 9123 },
            }
            for (const socket of sockets) if (socket.readyState === 1) socket.message(reply)
          }
        }, 25)
      })
    }
    close() { this.readyState = 3; this.emit('close') }
  }
  const transport = {
    locationHref: 'http://127.0.0.1:8765/',
    fetcher: async (url, init) => {
      calls.push(init.method)
      if (url.endsWith('/settings/catalog')) return response(200, catalog)
      if (url.endsWith('/environments/owned')) return response(200, { environments: [
        { environment_id: ENVIRONMENT, mode: 'free', created_at: '2026-10-07T00:00:00+00:00' },
      ] })
      if (url.endsWith('/attacker-credential')) return response(200, { attacker_credential: SECRET })
      if (init.method === 'DELETE') return response(discardFailure ? 503 : 204, null)
      if (creationFailure) { const reason = creationFailure; creationFailure = null; return response(reason, { detail: { code: 'ENVIRONMENT_LIMIT' } }) }
      return response(201, { environment_id: ENVIRONMENT, attacker_credential: SECRET })
    },
    socketFactory() {
      const socket = new FakeSocket()
      sockets.push(socket)
      queueMicrotask(() => { socket.readyState = 1; socket.emit('open') })
      return socket
    },
  }
  const client = createAttackEnvironmentClient(transport)
  return { workspace: createLiveWorkspace(client), calls, sockets,
    reload: () => createLiveWorkspace(createAttackEnvironmentClient(transport)),
    failCreate: (status) => { creationFailure = status }, failDiscard: () => { discardFailure = true },
    limitWait: () => { limited = true } }
}
function response(status, body) { return { ok: status >= 200 && status < 300, status, json: async () => body } }
const request = { mode: 'free', idle_noise_mode: 'calibration_values', settings: {} }
const brokenConnection = createLiveWorkspace({
  createEnvironment: async () => ({ ok: true, environment: { environmentId: ENVIRONMENT, mode: 'free' } }),
  connect: async () => { throw new Error('transport failure') },
  discardEnvironment: async () => {},
})
await brokenConnection.establish(request)
assert.equal(brokenConnection.getSnapshot().connectionState, 'failed')
assert.equal(brokenConnection.getSnapshot().environment.environmentId, ENVIRONMENT)
assert.equal(brokenConnection.getSnapshot().busy, null)
await brokenConnection.discard()
assert.equal(brokenConnection.getSnapshot().environment, null)
checks.push('connection failure preserves the environment for explicit disposal; never claims connecting forever')
{
  const f = fixture()
  await f.workspace.loadCatalog()
  await f.workspace.establish(request)
  await f.workspace.job('victim_activity', JOB)
  await new Promise(resolve => setTimeout(resolve, 20))
  const reply = f.workspace.getSnapshot().replies.at(-1)
  assert.equal(reply.value.reference, 'training:1')
  assert.deepEqual(Object.keys(reply.value.shots[0].steps[0]).sort(), ['actor', 'after', 'end_us', 'kind', 'start_us'])
  for (const secret of [SECRET, 'private-tenant', 'density', 'host_time', 'attacker_credential']) {
    assert.ok(!JSON.stringify(f.workspace.getSnapshot()).includes(secret), secret)
  }
  await f.workspace.discard()
  f.workspace.disconnect()
  checks.push('a victim-activity reply keeps only its validated fields; extra server fields never reach page state')
}
for (const mode of ['free', 'challenge']) {
  const f = fixture()
  const selected = mode === 'free' ? request : { mode, preset_id: 'public-demo', settings: {} }
  await f.workspace.loadCatalog()
  await f.workspace.establish(selected)
  await f.workspace.submit({ deviceId: 'ibm_kingston', shots: 32, circuitConfig: {} })
  assert.equal(f.workspace.getSnapshot().worldTimeUs, 0)
  assert.equal(f.workspace.getSnapshot().pending.length, 1)
  await new Promise(resolve => setTimeout(resolve, 35))
  assert.equal(f.workspace.getSnapshot().worldTimeUs, 1)
  assert.equal(f.workspace.getSnapshot().pending.length, 0)
  assert.deepEqual(f.workspace.getSnapshot().replies[0].value, { job_id: JOB, status: 'succeeded', shots: 32, counts: { '0': 17, '1': 15 } })
  assert.ok(!JSON.stringify(f.workspace.getSnapshot()).includes(SECRET))
  assert.ok(!JSON.stringify(f.workspace.getSnapshot()).includes('private-tenant'))
  await f.workspace.discard()
  assert.equal(f.workspace.getSnapshot().environment, null)
  f.workspace.disconnect()
  checks.push(`${mode}: catalog/create/connect/submit/server-clock-and-reply-push/discard; public state only`)
}
const reload = fixture()
await reload.workspace.establish(request)
await reload.workspace.submit({ deviceId: 'ibm_kingston', shots: 32, circuitConfig: {} })
const oldPendingId = reload.workspace.getSnapshot().pending[0].requestId
reload.workspace.disconnect()
const continued = reload.reload()
await continued.loadOwnedEnvironments()
assert.equal(continued.getSnapshot().ownedEnvironments.length, 1)
await continued.resume(continued.getSnapshot().ownedEnvironments[0])
assert.equal(continued.getSnapshot().worldTimeUs, 0)
assert.equal(continued.getSnapshot().pendingUnknown, false)
assert.equal(continued.getSnapshot().pending[0].requestId, oldPendingId)
assert.equal(continued.getSnapshot().pending[0].issuedAtWorldUs, 0)
assert.ok(continued.getSnapshot().notice.includes('復元しました'))
await continued.submit({ deviceId: 'ibm_kingston', shots: 32, circuitConfig: {} })
assert.equal(continued.getSnapshot().worldTimeUs, 0)
await new Promise(resolve => setTimeout(resolve, 35))
assert.equal(continued.getSnapshot().replies.length, 2)
assert.ok(!JSON.stringify(continued.getSnapshot()).includes(SECRET))
continued.disconnect()
checks.push('reload choice restores public world state without storing credentials or advancing time')
const replace = fixture()
await replace.workspace.establish(request)
await replace.workspace.establish(request)
assert.ok(replace.workspace.getSnapshot().replacement)
assert.deepEqual(replace.calls, ['POST'])
replace.workspace.cancelReplacement()
assert.equal(replace.workspace.getSnapshot().replacement, null)
await replace.workspace.establish(request)
await replace.workspace.replace()
assert.deepEqual(replace.calls, ['POST', 'DELETE', 'POST'])
checks.push('known environment recreation requires confirmation; delete completes before create')
replace.workspace.disconnect()

const quota = fixture()
quota.failCreate(429)
await quota.workspace.establish(request)
assert.ok(quota.workspace.getSnapshot().replacement)
await quota.workspace.replace()
assert.deepEqual(quota.calls, ['POST'])
await quota.workspace.replace(ENVIRONMENT)
assert.deepEqual(quota.calls, ['POST', 'DELETE', 'POST'])
quota.workspace.disconnect()
checks.push('browser limit keeps world intact until confirmation and a known environment ID')

const failedDiscard = fixture()
await failedDiscard.workspace.establish(request)
await failedDiscard.workspace.establish(request)
failedDiscard.failDiscard()
await failedDiscard.workspace.replace()
assert.deepEqual(failedDiscard.calls, ['POST', 'DELETE'])
assert.equal(failedDiscard.workspace.getSnapshot().environment.environmentId, ENVIRONMENT)
failedDiscard.workspace.disconnect()
checks.push('failed discard preserves handle and never starts replacement create')

const waiting = fixture('explicit_wait')
await waiting.workspace.establish(request)
await waiting.workspace.submit({ deviceId: 'ibm_kingston', shots: 32, circuitConfig: {} })
const framesBefore = waiting.sockets[0].sent.length
await waiting.workspace.wait({ kind: 'request', requestId: 'other-person' })
assert.equal(waiting.sockets[0].sent.length, framesBefore)
waiting.limitWait()
await waiting.workspace.wait({ kind: 'duration', durationUs: 25 })
assert.equal(waiting.workspace.getSnapshot().worldTimeUs, 25)
assert.equal(waiting.workspace.getSnapshot().pending.length, 1)
waiting.sockets[0].close()
assert.equal(waiting.workspace.getSnapshot().connectionState, 'closed')
assert.equal(waiting.sockets.length, 1)
waiting.workspace.disconnect()
checks.push('own-request wait only; limited wait keeps pending; disconnect never auto-reconnects')

const operations = fixture('explicit_wait')
await Promise.all([operations.workspace.establish(request), operations.workspace.establish(request)])
assert.deepEqual(operations.calls, ['POST'])
for (const kind of ['status', 'result', 'cancel']) await operations.workspace.job(kind, JOB)
assert.equal(operations.workspace.getSnapshot().worldTimeUs, 0)
assert.deepEqual(operations.workspace.getSnapshot().pending.map((item) => item.kind), ['status', 'result', 'cancel'])
await operations.workspace.wait({ kind: 'any' })
await new Promise(resolve => setTimeout(resolve, 35))
assert.equal(operations.workspace.getSnapshot().pending.length, 0)
operations.workspace.disconnect()
checks.push('overlapping creation never duplicates; status/result/cancel do not advance time; any wait settles replies')

const stalled = createLiveWorkspace({ getCatalog: () => new Promise(() => {}) }, 5)
await stalled.loadCatalog()
assert.equal(stalled.getSnapshot().busy, null)
assert.ok(stalled.getSnapshot().notice.includes('自動再送は行いません'))
stalled.disconnect()
checks.push('host timeout leaves virtual time unchanged, releases UI busy state and never auto-retries')

const { CircuitProvider } = load('context/CircuitContext.tsx')
const { AdminModeContext } = load('context/AdminModeContextCore.ts')
const { TutorialProvider } = load('context/TutorialContext.tsx')
const { AttackLivePage, AttackSnapshotInfo, AttackUnusableQubits, AttackQpuTopology, AttackModeNotice, AttackCredentialInfo } = load('pages/AttackLivePage.tsx')
check('mode note renders beside mode selection and matches the docs opening', () => {
  const modeNote = '※この画面の量子コンピューター・被害者・サービスは、すべてこのPCの中の仮想環境です。実機やクラウドサービスには一切接続しません。'
  const noteHtml = renderToStaticMarkup(React.createElement(AttackModeNotice))
  const docs = readFileSync(path.resolve(sourceRoot, '../../formalweb/website/docs/limitations/attack-simulator.md'), 'utf8')
  assert.ok(noteHtml.includes(modeNote))
  assert.match(livePageSource, /setupCategory === 'mode'[\s\S]*?<h2>モード<\/h2><AttackModeNotice \/>/)
  assert.match(livePageSource, /aria-label="環境設定の分類"/)
  assert.ok(docs.slice(0, 400).includes(modeNote))
})
check('setup categories expose change/error counts, show one category body, and review lists changes', () => {
  const fields = [
    { path: 'qpu_service.runtime_execution.placement_policy', default: 'same' },
    { path: 'qpu_service.idle_dynamics.qubits.2.t1_us', default: 31 },
    { path: 'qpu_service.idle_dynamics.physical_inputs.temperature_mk', default: 20 },
    { path: 'qpu_service.runtime_execution.queue_delay_us', default: 1 },
  ]
  assert.deepEqual(settings.countChangedSettings(fields, {
    [fields[0].path]: 'dedicated', [fields[1].path]: 30, [fields[2].path]: 20,
  }), { qpu: 1, noise: 1, service: 0 })
  assert.equal(settings.firstErrorCategory(fields, { [fields[1].path]: '範囲外' }), 'noise')
  assert.equal(settings.firstErrorCategory(fields, {}), null)
  assert.match(livePageSource, /SETUP_CATEGORIES\.map\(\(\{ id, label \}\)/)
  assert.match(livePageSource, /setupCategory === 'review'[\s\S]*初期値から変更した項目/)
  assert.match(livePageSource, /setSetupCategory\(firstErrorCategory\(visibleFields, invalid\) \?\? 'review'\)/)
  // The rationale of a setting belongs to the official documentation, never to the live screen.
  assert.doesNotMatch(livePageSource, /attack-live__basis|<summary>根拠<\/summary>/)
})
check('virtual credential warning renders beside workspace environment details', () => {
  const noteHtml = renderToStaticMarkup(React.createElement(AttackCredentialInfo))
  assert.ok(noteHtml.includes('仮想資格情報'))
  assert.ok(noteHtml.includes('シミュレーター内でだけ通用する仮の資格情報です。実在のAPIキーや秘密は入力しないでください。'))
  assert.match(livePageSource, /<\/details>\s*<AttackCredentialInfo \/>/)
})
check('placement choices describe the two regional policies without claiming cloud placement', () => {
  const placement = { path: 'qpu_service.runtime_execution.placement_policy' }
  assert.equal(settings.placementChoiceLabel(placement, 'guaranteed_same_physical_qubit'), '同じ物理領域を使う')
  assert.equal(settings.placementChoiceLabel(placement, 'tenant_dedicated_region'), '利用者ごとに別の物理領域を使う')
  assert.equal(settings.placementChoiceLabel({ path: 'other' }, 'value'), 'value')
})
check('topology replacement preserves connected regions and rejects disconnected or unusable qubits', () => {
  const edges = [[0, 1], [1, 2], [2, 3], [4, 5]]
  const eligible = new Set([0, 1, 2, 3, 4, 5])
  assert.deepEqual(topology.replaceRegionQubit([0, 1, 2], 3, 0, edges, eligible),
    { ok: true, ids: [1, 2, 3] })
  const disconnected = topology.replaceRegionQubit([0, 1, 2], 4, 0, edges, eligible)
  assert.equal(disconnected.ok, false)
  assert.match(disconnected.reason, /連結しなくなる/)
  assert.equal(topology.replaceRegionQubit([0, 1, 2], 9, 0, edges, eligible).ok, false)
})
check('only selected yellow qubits are editable in calibration mode; physical mode is read-only', () => {
  const regions = { shared: [2, 3], victim: [2, 3], attacker: [7, 8] }
  assert.equal(topology.canEditQubitNoise('calibration_values', 2, 'shared', regions), true)
  assert.equal(topology.canEditQubitNoise('calibration_values', 5, 'shared', regions), false)
  assert.equal(topology.canEditQubitNoise('calibration_values', 7, 'attacker', regions), true)
  assert.equal(topology.canEditQubitNoise('physical_parameters', 2, 'shared', regions), false)
  assert.deepEqual(topology.regionSelectionRequest('tenant_dedicated_region', regions), {
    victim_physical_qubit_ids: [2, 3], attacker_physical_qubit_ids: [7, 8],
  })
})
check('noise templates expand only for selected regions and bind snapshot defaults/basis', () => {
  const snapshot = { qubits: {
    '2': { usable_for_placement: true, T1_us: 31, T2_us: 22, basis: 'snapshot basis' },
    '3': { usable_for_placement: true, T1_us: 30, T2_us: 21, basis: 'snapshot basis' },
    '7': { usable_for_placement: true, T1_us: 19, T2_us: 14, basis: 'snapshot basis' },
  } }
  const template = (prefix) => [
    { path: `${prefix}.{qubit_id}.t1_us`, label: 'Qubit {qubit_id} T1', type: 'number', basis: 'source', minimum: .001, maximum: 1e6, unit: 'us' },
    { path: `${prefix}.{qubit_id}.equilibrium_excited_population`, label: 'Qubit {qubit_id} population', type: 'number', basis: 'editable assumption', minimum: 0, maximum: 1 },
  ]
  const regionCatalog = { qubit_noise_fields: {
    primary: template('qpu_service.idle_dynamics.qubits'),
    dedicated: template('qpu_service.dedicated_region.idle_dynamics.qubits'),
  } }
  const generated = settings.regionNoiseFields([], regionCatalog, snapshot,
    { shared: [2, 3], victim: [2, 3], attacker: [7] }, 'tenant_dedicated_region', 'calibration_values')
  assert.equal(generated.length, 6)
  assert.equal(generated.find(field => field.path.endsWith('.7.t1_us')).default, 19)
  assert.equal(generated.find(field => field.path.endsWith('.7.t1_us')).basis, 'snapshot basis')
  assert.ok(generated.some(field => field.path === 'qpu_service.dedicated_region.idle_dynamics.qubits.7.t1_us'))
  assert.deepEqual(settings.regionNoiseFields([], regionCatalog, snapshot,
    { shared: [2], victim: [2], attacker: [7] }, 'guaranteed_same_physical_qubit', 'physical_parameters'), [])
})
check('Kingston graph renders all fixed-position qubits and snapshot edges with non-color status marks', () => {
  const qubits = Object.fromEntries(Array.from({ length: 156 }, (_, qid) => [String(qid), {
    usable_for_placement: qid !== 146, unusable_reasons: qid === 146 ? ['snapshot exclusion'] : [],
    T1_us: 30, T2_us: 20, basis: 'historical basis',
  }]))
  const cz_calibrations = {}
  for (let qid = 0; qid < 155; qid++) cz_calibrations[`${qid}:${qid + 1}`] = {}
  for (let qid = 0; qid < 21; qid++) cz_calibrations[`${qid}:${qid + 2}`] = {}
  const graphStandard = { source: 'snapshot', device: 'ibm_kingston', snapshot_date: '2026-04-15',
    qubit_count: 156, undirected_edge_count: 176, active_region_limit: 6, active_region_initial: [0, 1, 2, 3, 4, 5],
    alternate_region_initial: [10, 11, 12, 13, 14, 15], eligibility_policy: {}, qubits, cz_calibrations }
  const regionCatalog = { max_qubits: 6, eligible_qubit_ids: Array.from({ length: 156 }, (_, qid) => qid).filter(qid => qid !== 146),
    active_region_initial: graphStandard.active_region_initial, alternate_region_initial: graphStandard.alternate_region_initial,
    display_layout_notice: '表示用の配置であり、実機の物理的な位置ではありません。', region_notice: 'serverで再検査' }
  const graphHtml = renderToStaticMarkup(React.createElement(AttackQpuTopology, {
    standard: graphStandard, regionCatalog, placement: 'guaranteed_same_physical_qubit',
    noiseMode: 'physical_parameters', owner: 'shared', setOwner() {}, selectedQubit: null, setSelectedQubit() {},
    onNoiseQubitSelect() {},
    regions: { shared: graphStandard.active_region_initial, victim: graphStandard.active_region_initial,
      attacker: graphStandard.alternate_region_initial }, setRegions() {}, editable: true, onNotice() {},
  }))
  assert.equal((graphHtml.match(/class="attack-live__node /g) ?? []).length, 156)
  assert.equal((graphHtml.match(/class="attack-live__edge"/g) ?? []).length, 176)
  assert.match(graphHtml, /is-active/)
  assert.match(graphHtml, /is-unusable/)
  assert.match(graphHtml, /class="attack-live__cross"/)
  assert.match(graphHtml, /qubit 146 · 使用不可/)
  assert.match(graphHtml, /実機の物理的な位置ではありません/)
})
const html = renderToStaticMarkup(React.createElement(AdminModeContext.Provider,
  { value: { adminModeEnabled: false, internalInfoVisible: false, setAdminModeEnabled() {} } },
  React.createElement(TutorialProvider, { currentRoute: 'attack-simulator', onNavigate() {} },
    React.createElement(CircuitProvider, { gateDurationDefaults: {}, keyboardEnabled: false, minimumLogicalQubits: 1 },
      React.createElement(AttackLivePage, { gateDurationDefaults: {}, active: false })))))
check('the attack page offers the Reset tutorial and marks the parts the course points at', () => {
  assert.ok(html.includes('Reset課題のチュートリアル'))
  assert.ok(html.includes('data-tutorial-anchor="attack-create-button"'))
  /* モードのカードは設定目録を取得してから出るので、最初の描画ではなくソースで確かめる。 */
  for (const anchor of ['attack-mode-cards', 'attack-circuit-editor', 'attack-terminal']) {
    assert.ok(livePageSource.includes(`data-tutorial-anchor="${anchor}"`), anchor)
  }
  assert.ok(livePageSource.includes('data-tutorial-anchor={`attack-challenge-${challenge.id}`}'))
})
check('real React initial render: no fabricated environment, clock, counts, or credential input', () => {
  assert.ok(html.includes('モードを選んでください'))
  assert.ok(!html.includes('世界時刻'))
  assert.ok(!html.includes(SECRET))
  assert.ok(!html.includes('attacker_credential'))
})
const props = JSON.parse(readFileSync(path.resolve(sourceRoot, '../../data/device_snapshots/ibm_kingston_2026-04-15/props_kingston.json'), 'utf8'))
const snapshot = {
  source: 'data/device_snapshots/ibm_kingston_2026-04-15/SOURCE.md', device: 'ibm_kingston', snapshot_date: props.last_update_date,
  qubit_count: 156, undirected_edge_count: 176, active_region_initial: [0], active_region_limit: 6,
  eligibility_policy: { readout_error_exclude_above: .1, cz_error_exclude_at_or_above: .5 },
  qubits: { '0': { usable_for_placement: true, 'P(1|0)': .01, 'P(0|1)': .02, measurement_duration_us: 2, reset_duration_us: 3 },
    '146': { usable_for_placement: false, unusable_reasons: ['missing_t1_or_t2', 'attached_to_cz_error_at_or_above_threshold:146-147'] } },
  attacker_credential: SECRET,
}
const snapshotHtml = renderToStaticMarkup(React.createElement(AttackSnapshotInfo, { standard: snapshot }))
  + renderToStaticMarkup(React.createElement(AttackUnusableQubits, { standard: snapshot }))
check('snapshot help links to the official assumptions and limitations page', () => {
  assert.ok(snapshotHtml.includes('href="https://yuragi-strider.23sam55781.workers.dev/docs/limitations/attack-simulator"'))
  assert.ok(snapshotHtml.includes('攻撃シミュレーターの前提と限界'))
  assert.ok(!snapshotHtml.includes('/help#attack-settings'))
})
check('snapshot date/provenance/unusable qubit render, per-region readout table and extra secret never render', () => {
  for (const text of ['現在の実機の代表値ではありません', 'IBMの公開データを値として使っているだけで、IBMの装置には接続しません', '2026-04-15', '156', '176', 'qubit 146', 'T1/T2較正値なし', '結合146-147']) assert.ok(snapshotHtml.includes(text), text)
  /* 保持領域は利用者が変えられるので、その領域の読み出し・Reset較正値は一般的な情報として出さない。 */
  for (const text of ['読み出し・Resetの較正値', '導出近似', 'P(1|0)']) assert.ok(!snapshotHtml.includes(text), text)
  assert.ok(!snapshotHtml.includes(SECRET))
  const oldHtml = renderToStaticMarkup(React.createElement(AttackSnapshotInfo, { standard: legacyStandard }))
  assert.ok(oldHtml.includes('2026-08-03'))
})
check('an INVALID_PROGRAM reply names the unmet service condition', () => {
  assert.equal(settings.describeRejectionReason({ reason: 'shots exceed the service limit: 2000 > 1024' }),
    'shots が上限を超えています（2000 > 上限 1024）')
  assert.match(settings.describeRejectionReason({ reason: 'program exceeds the service instruction-work limit: 1024 shots x 40 instructions > 32768' }), /1024 × 40 = 40960 > 上限 32768/)
  assert.match(settings.describeRejectionReason({ reason: 'something new' }), /something new/)
  assert.equal(settings.describeRejectionReason(null), null)
  assert.equal(settings.describeRejectionReason({ job_id: 'x' }), null)
})
const activityUtil = load('utils/victimActivity.ts')
const terminalUtil = load('utils/attackTerminal.ts')
const { VictimActivityPanel } = load('components/VictimActivityPanel.tsx')
const step = (actor, kind, start, end, after) => ({ actor, kind, start_us: start, end_us: end, ...(after ? { after } : {}) })
const serviceActivity = {
  job_id: 'job-1', reference: 'victim', victim_prepared_bit: 1, shots_total: 32, shots_shown: 2,
  shots: [
    { index: 0, steps: [step('victim', 'victim_run', 0, 2.02), step('service', 'service_reset', 2.02, 3.02, 'victim'), step('attacker', 'attacker_measure', 3.02, 4.02)] },
    { index: 1, steps: [step('service', 'service_reset', 4.02, 5.02, 'attacker'), step('victim', 'victim_run', 5.02, 7.04), step('service', 'service_reset', 7.04, 8.04, 'victim'), step('attacker', 'attacker_measure', 8.04, 9.04)] },
  ],
}

check('the victim activity reply is read strictly: the server shape passes, anything else is not shown', () => {
  const parsed = activityUtil.parseVictimActivity(serviceActivity)
  assert.equal(parsed.shots.length, 2)
  assert.equal(parsed.shots[1].steps[0].after, 'attacker')
  const bad = [
    null, {}, { ...serviceActivity, shots: 'x' }, { ...serviceActivity, victim_prepared_bit: 7 },
    { ...serviceActivity, shots: [{ index: 0, steps: [step('stranger', 'victim_run', 0, 1)] }] },
    { ...serviceActivity, shots: [{ index: 0, steps: [step('victim', 'victim_run', 5, 1)] }] },
    { ...serviceActivity, shots: [{ index: 0, steps: [step('victim', 'victim_run', -1, 1)] }] },
    { ...serviceActivity, shots: Array.from({ length: 65 }, (_, index) => ({ index, steps: [] })) },
  ]
  for (const value of bad) assert.equal(activityUtil.parseVictimActivity(value), null, JSON.stringify(value)?.slice(0, 60))
  // A job result is not an activity reply.
  assert.equal(activityUtil.parseVictimActivity({ job_id: 'job-1', status: 'succeeded', shots: 4, counts: { 0: 1, 1: 3 } }), null)
})

check('the terminal text names each step, tells whose state a reset cleared, and counts the shots it leaves out', () => {
  const lines = activityUtil.describeVictimActivity(activityUtil.parseVictimActivity(serviceActivity), 1)
  assert.match(lines[0], /本物の被害者.*被害者が準備する bit: 1/)
  assert.match(lines[1], /被害者の実行.*→ Reset（被害者の後）.*→ 攻撃者の測定/)
  assert.match(lines[2], /ほか 31 shot/)
  const plain = activityUtil.describeVictimActivity({ ...serviceActivity, reference: null, victim_prepared_bit: null })
  assert.match(plain[1], /挟まっていません/)
})

check('the activity command parses with and without a job id, and the help lists it', () => {
  assert.deepEqual(terminalUtil.parseTerminalCommand('activity'), { kind: 'job', action: 'activity' })
  assert.deepEqual(terminalUtil.parseTerminalCommand('activity job-9'), { kind: 'job', action: 'activity', jobId: 'job-9' })
  assert.ok(terminalUtil.TERMINAL_HELP.some((line) => line.startsWith('activity ')))
})

check('the victim activity panel lays out three lanes, labels every block, and states what it cannot show', () => {
  const panel = (activity, jobId = 'job-1') => renderToStaticMarkup(React.createElement(VictimActivityPanel,
    { activity, jobId, busy: false, onRead() {} }))
  const html = panel(activityUtil.parseVictimActivity(serviceActivity))
  for (const text of ['被害者の動き', 'shot 0', 'shot 1', '被害者', 'サービス', '攻撃者', 'Reset（被害者の後）', '攻撃者の測定', '先頭2shot', 'このジョブの動きを読む']) {
    assert.ok(html.includes(text), text)
  }
  assert.ok(html.includes('role="img"') && html.includes('aria-label="被害者の動き。'))
  assert.ok(html.includes('抽象コマンド'))
  assert.ok(!html.includes('density') && !html.includes('tenant'))
  const none = panel(null, '')
  assert.ok(none.includes('ジョブを送ると読めます') && !none.includes('<svg'))
  const ordinary = panel({ ...serviceActivity, reference: null, victim_prepared_bit: null, shots: [] })
  assert.ok(ordinary.includes('挟まっていません') && !ordinary.includes('<svg'))
})

check('the page shows the panel only when the service says the command is on', () => {
  const source = readFileSync(path.join(sourceRoot, 'pages', 'AttackLivePage.tsx'), 'utf8')
  assert.ok(source.includes("messagesView === 'activity' && <VictimActivityPanel"))
  assert.ok(source.includes("{currentTab === 'workspace' && state.victimActivityAvailable && <button"))
  assert.ok(source.includes("command.action === 'activity' && !state.victimActivityAvailable"))
})

console.log(`PASS ${checks.length} attack live page checks`)
for (const label of checks) console.log(`  ${label}`)
