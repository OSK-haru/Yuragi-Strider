// Focused transport checks with fake fetch/WebSocket; no browser, server, or network.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import ts from 'typescript'

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const filename = path.join(sourceRoot, 'clients', 'attackLiveClient.ts')
const output = ts.transpileModule(readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  fileName: filename,
}).outputText
const module = { exports: {} }
new Function('require', 'module', 'exports', output)(() => {
  throw new Error('unexpected runtime import')
}, module, module.exports)
const { createAttackEnvironmentClient } = module.exports

const checks = []
function check(label, operation) { operation(); checks.push(label) }
const SECRET = 'attacker-virtual-credential-never-return-this'
const capturedLogs = []
const originalConsole = Object.fromEntries(['log', 'warn', 'error'].map((name) => [name, console[name]]))
for (const name of Object.keys(originalConsole)) console[name] = (...values) => capturedLogs.push(values.join(' '))
const environmentId = '0123456789abcdef0123456789abcdef'
const catalog = { schema_version: 'attack-settings-catalog-v1', modes: [], free_mode: {}, challenges: [] }
function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return body },
  }
}

const httpCalls = []
const fetcher = async (url, init) => {
  httpCalls.push({ url, init })
  if (url.endsWith('/settings/catalog')) return response(200, catalog)
  if (url.endsWith('/environments/owned')) return response(200, { environments: [
    { environment_id: environmentId, mode: 'free', created_at: '2026-10-07T00:00:00+00:00' },
  ] })
  if (url.endsWith('/attacker-credential')) return response(200, { attacker_credential: SECRET })
  if (url.endsWith('/submission')) {
    const sent = JSON.parse(init.body)
    if ('message' in sent) return response(200, { attempt: 1, attempts_remaining: 2, passed: sent.message === 'right' })
    if (sent.threshold > 1) return response(422, { detail: { code: 'INVALID_SUBMISSION' } })
    if (sent.threshold === 0.9) return response(409, { detail: { code: 'SUBMISSIONS_EXHAUSTED' } })
    return response(200, { attempt: 1, attempts_remaining: 2, correct: 17, total: 20, required: 16, passed: true })
  }
  if (init.method === 'POST') return response(201, {
    environment_id: environmentId,
    attacker_credential: SECRET,
  })
  if (init.method === 'DELETE') return response(204, null)
  return response(404, { detail: { code: 'NOT_FOUND' } })
}

class FakeSocket {
  readyState = 0
  listeners = new Map()
  sent = []
  addEventListener(type, listener) {
    const values = this.listeners.get(type) ?? new Set()
    values.add(listener)
    this.listeners.set(type, values)
  }
  removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener) }
  emit(type, event = {}) {
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }
  open() { this.readyState = 1; this.emit('open') }
  send(raw) {
    const message = JSON.parse(raw)
    this.sent.push(message)
    if (message.type === 'authenticate') {
      queueMicrotask(() => this.emit('message', { data: JSON.stringify({
        type: 'ready', protocol: 'quanyu.attack.v1', world_time_us: 0,
        clock_mode: 'host_clock', pause_while_catching_up: false, max_program_qubits: 1,
      }) }))
    } else if (message.operation === 'program.submit') {
      queueMicrotask(() => this.emit('message', { data: JSON.stringify({
        type: 'accepted', command_id: message.command_id, request_id: 'opaque-request-1',
        issued_at_world_us: 0,
      }) }))
      setTimeout(() => {
        this.emit('message', { data: JSON.stringify({ type: 'clock', world_time_us: 15 }) })
        this.emit('message', { data: JSON.stringify({
          type: 'reply', request_id: 'opaque-request-1', value: { counts: { '0': 2 } }, error_code: null,
        }) })
      }, 2)
    } else if (message.operation === 'request.pending.list') {
      queueMicrotask(() => this.emit('message', { data: JSON.stringify({
        type: 'pending.list', command_id: message.command_id, requests: [{
          request_id: 'opaque-request-1', kind: 'submit', job_id: null, issued_at_world_us: 0,
        }],
      }) }))
    }
  }
  close() { this.readyState = 3; this.emit('close') }
}

const sockets = []
const client = createAttackEnvironmentClient({
  fetcher,
  locationHref: 'http://127.0.0.1:8765/',
  socketFactory(url, protocols) {
    assert.match(url, /^ws:\/\/127\.0\.0\.1:8765\/api\/attack\/live\//)
    assert.deepEqual(protocols, ['quanyu.attack.v1'])
    const socket = new FakeSocket()
    sockets.push(socket)
    return socket
  },
})

const controller = new AbortController()
assert.strictEqual(await client.getCatalog(controller.signal), catalog)
const created = await client.createEnvironment({
  mode: 'free', idle_noise_mode: 'physical_parameters', settings: {},
}, controller.signal)
assert.deepEqual(created, { ok: true, environment: { environmentId, mode: 'free' } })
assert.ok(!JSON.stringify(created).includes(SECRET))
assert.equal(httpCalls[1].init.headers['X-Attack-Simulator-Request'], '1')
assert.equal(httpCalls[1].init.credentials, 'same-origin')
checks.push('catalog/create return public data only; create uses same-origin cookie and D61 header')

const opening = client.connect(created.environment, controller.signal)
const socket = sockets[0]
socket.open()
const connection = await opening
assert.equal(connection.maxProgramQubits, 1)
checks.push('the ready frame carries the program-qubit limit from device information')
assert.equal(connection.state, 'open')
assert.equal(connection.clockMode, 'host_clock')
assert.deepEqual(socket.sent[0], { type: 'authenticate', credential: SECRET })
const stateChanges = []
connection.onStateChange((state) => stateChanges.push(state))
const replies = []
connection.onReply((reply) => replies.push(reply))
const request = await connection.submit({
  deviceId: 'qpu-sim-100', shots: 2,
  circuitConfig: { logical_qubits: 2, columns: [] }, initialization: 'prepare',
})
assert.deepEqual(request, { requestId: 'opaque-request-1', kind: 'submit', issuedAtWorldUs: 0 })
assert.equal(connection.worldTimeUs, 0, 'submit must not advance explicit_wait time')
await new Promise((resolve) => setTimeout(resolve, 10))
assert.equal(connection.worldTimeUs, 15)
assert.deepEqual(replies, [{
  requestId: 'opaque-request-1', value: { counts: { '0': 2 } }, errorCode: null,
}])
assert.ok(!JSON.stringify([request, replies]).includes(SECRET))
connection.close()
assert.deepEqual(stateChanges, ['closed'])
assert.ok(!socket.sent.some((frame) => JSON.stringify(frame).includes(SECRET)
  && frame.type !== 'authenticate'))
checks.push('host-clock replies and clock updates arrive from server without a playback/wait operation')

const reconnecting = client.connect(created.environment, controller.signal)
const failedSocket = sockets[1]
failedSocket.open()
const failedConnection = await reconnecting
const failureStates = []
failedConnection.onStateChange((state) => failureStates.push(state))
failedSocket.emit('error')
failedSocket.close()
assert.equal(failedConnection.state, 'failed')
assert.deepEqual(failureStates, ['failed'])
assert.equal(sockets.length, 2, 'transport must not reconnect automatically')
checks.push('socket failure is observable and does not trigger automatic reconnect')

const reloaded = createAttackEnvironmentClient({
  fetcher, locationHref: 'http://127.0.0.1:8765/',
  socketFactory(url, protocols) {
    assert.match(url, /^ws:\/\/127\.0\.0\.1:8765\/api\/attack\/live\//)
    assert.deepEqual(protocols, ['quanyu.attack.v1'])
    const socket = new FakeSocket()
    sockets.push(socket)
    return socket
  },
})
const owned = await reloaded.listOwnedEnvironments(controller.signal)
assert.deepEqual(owned, [{ environmentId, mode: 'free', createdAt: '2026-10-07T00:00:00+00:00' }])
assert.ok(!JSON.stringify(owned).includes(SECRET))
assert.equal(await reloaded.resumeEnvironment(owned[0], controller.signal), true)
const resumedOpening = reloaded.connect(owned[0], controller.signal)
const resumedSocket = sockets.at(-1)
resumedSocket.open()
const resumedConnection = await resumedOpening
assert.deepEqual(resumedSocket.sent[0], { type: 'authenticate', credential: SECRET })
assert.equal(resumedConnection.worldTimeUs, 0)
await resumedConnection.submit({ deviceId: 'qpu-sim-100', shots: 2, circuitConfig: {} })
assert.equal(resumedConnection.worldTimeUs, 0)
const recoveredPending = await resumedConnection.pendingRequests()
assert.deepEqual(recoveredPending, [{ requestId: 'opaque-request-1', kind: 'submit', issuedAtWorldUs: 0 }])
assert.equal(resumedConnection.worldTimeUs, 0)
const resumedReplies = []
resumedConnection.onReply((reply) => resumedReplies.push(reply))
await new Promise((resolve) => setTimeout(resolve, 10))
assert.equal(resumedReplies.length, 1)
assert.equal(resumedConnection.worldTimeUs, 15)
resumedConnection.close()
assert.equal(httpCalls.at(-2).init.headers['X-Attack-Simulator-Request'], '1')
assert.equal(httpCalls.at(-1).init.headers['X-Attack-Simulator-Request'], '1')
assert.ok(!JSON.stringify([owned, recoveredPending, resumedReplies]).includes(SECRET))
checks.push('new client lists owned world, reissues credential internally, reconnects and receives pushed reply')

for (const [status, body, expected] of [
  [429, { detail: { code: 'ENVIRONMENT_LIMIT' } }, 'browser_limit'],
  [503, { detail: { code: 'ENVIRONMENT_CAPACITY' } }, 'server_full'],
  [422, { detail: { code: 'INVALID_SETTINGS' } }, 'invalid_settings'],
  [413, { detail: { code: 'SETTINGS_TOO_LARGE' } }, 'too_large'],
]) {
  const failingClient = createAttackEnvironmentClient({
    fetcher: async () => response(status, body), locationHref: 'http://127.0.0.1:1/',
    socketFactory: () => new FakeSocket(),
  })
  assert.deepEqual(await failingClient.createEnvironment({
    mode: 'free', idle_noise_mode: 'calibration_values', settings: {},
  }, controller.signal), { ok: false, reason: expected })
}
checks.push('create HTTP failure codes map to the declared UI union')

const graded = await client.submitSolution(environmentId, { threshold: 0.05, direction: 'ge' }, controller.signal)
assert.deepEqual(graded, { ok: true, result: { attempt: 1, attemptsRemaining: 2, correct: 17, total: 20, required: 16, passed: true } })
const gradedCall = httpCalls.at(-1)
assert.equal(gradedCall.init.headers['X-Attack-Simulator-Request'], '1')
assert.equal(gradedCall.init.credentials, 'same-origin')
assert.deepEqual(JSON.parse(gradedCall.init.body), { threshold: 0.05, direction: 'ge' })
assert.deepEqual(await client.submitSolution(environmentId, { threshold: 2, direction: 'ge' }, controller.signal), { ok: false, reason: 'invalid' })
assert.deepEqual(await client.submitSolution(environmentId, { threshold: 0.9, direction: 'le' }, controller.signal), { ok: false, reason: 'exhausted' })
const message = await client.submitSolution(environmentId, { message: 'right' }, controller.signal)
assert.deepEqual(message, { ok: true, result: { attempt: 1, attemptsRemaining: 2, passed: true } })
assert.deepEqual(JSON.parse(httpCalls.at(-1).init.body), { message: 'right' })
checks.push('solution submission uses the owner cookie and CSRF header and maps failures without leaking values')
checks.push('an exact-match message result carries no partial aggregates')

await client.discardEnvironment(environmentId, controller.signal)
const deletion = httpCalls.at(-1)
assert.equal(deletion.init.method, 'DELETE')
assert.equal(deletion.init.headers['X-Attack-Simulator-Request'], '1')
assert.equal(deletion.init.credentials, 'same-origin')
checks.push('discard uses D61 CSRF header and clears client-held credential only after success')

for (const [name, original] of Object.entries(originalConsole)) console[name] = original
assert.ok(!capturedLogs.join('\n').includes(SECRET))
assert.ok(checks.length >= 4)
console.log(`attackLiveClient: ${checks.length} checks passed`)
