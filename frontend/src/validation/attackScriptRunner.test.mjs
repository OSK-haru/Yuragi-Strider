// Focused checks for the in-browser script runner: the synchronous channel (with a
// real worker thread and SharedArrayBuffer), the page-side services, the runner
// lifecycle with a fake Worker, the file store, and the terminal command.
// No browser, server, Pyodide or network.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { Worker } from 'node:worker_threads'
import ts from 'typescript'

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
function transpile(relative) {
  const filename = path.join(sourceRoot, relative)
  return ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename,
  }).outputText
}
function load(relative) {
  const module = { exports: {} }
  new Function('require', 'module', 'exports', transpile(relative))((specifier) => {
    if (!specifier.startsWith('.')) throw new Error(`unexpected runtime import ${specifier}`)
    return load(`${path.relative(sourceRoot, path.resolve(path.dirname(path.join(sourceRoot, relative)), specifier))}.ts`)
  }, module, module.exports)
  return module.exports
}

const channel = load('scripting/syncChannel.ts')
const { createScriptServices } = load('scripting/scriptServices.ts')
const { createScriptRunner, EXIT_KILLED, scriptRuntimeSupport } = load('scripting/scriptRunner.ts')
const files = load('scripting/scriptFiles.ts')
const terminal = load('utils/attackTerminal.ts')
const checks = []
async function check(label, operation) { await operation(); checks.push(label) }

/* --- sync channel: a real blocked worker thread ------------------------------ */
const workerSource = `
const { parentPort, workerData } = require('node:worker_threads')
const module = { exports: {} }
new Function('require', 'module', 'exports', workerData.source)(() => { throw new Error('import') }, module, module.exports)
const { waitForResponse } = module.exports
parentPort.on('message', ({ buffer, callId, timeoutMs }) => {
  let slices = 0
  // The first slice runs just before the first block: only then may the main thread write.
  const outcome = waitForResponse(buffer, callId, () => {
    slices += 1
    if (slices === 1) parentPort.postMessage({ started: true })
  }, timeoutMs)
  parentPort.postMessage({ outcome, slices })
})
`
const thread = new Worker(workerSource, { eval: true, workerData: { source: transpile('scripting/syncChannel.ts') } })
/* Resolves with the worker's result; onStarted runs once the worker is about to block. */
const ask = (message, onStarted = () => {}) => new Promise((resolve) => {
  const listener = (reply) => {
    if (reply.started) { onStarted(); return }
    thread.off('message', listener)
    resolve(reply)
  }
  thread.on('message', listener)
  thread.postMessage(message)
})

await check('a blocked worker wakes on its own call id and ignores an abandoned call', async () => {
  const buffer = channel.createChannelBuffer(1024)
  const responder = channel.createResponder(buffer)
  const waiting = ask({ buffer, callId: 2, timeoutMs: 5000 }, () => {
    responder.received(1)
    setTimeout(() => responder.write(1, { value: 'stale' }), 30)
    setTimeout(() => { responder.received(2); responder.write(2, { value: { ok: 'fresh' } }) }, 120)
  })
  const { outcome, slices } = await waiting
  assert.deepEqual(outcome, { value: { ok: 'fresh' } })
  assert.ok(slices >= 1, 'the worker checks for interruption while it waits')
})
await check('a response for a superseded call is never written', () => {
  const buffer = channel.createChannelBuffer(1024)
  const responder = channel.createResponder(buffer)
  responder.received(3)
  assert.equal(responder.write(2, { value: 'late' }), false)
  assert.equal(responder.write(3, { value: 'now' }), true)
})
await check('an oversized response becomes an error instead of a truncated JSON', async () => {
  const buffer = channel.createChannelBuffer(64)
  const responder = channel.createResponder(buffer)
  responder.received(1)
  responder.write(1, { value: 'x'.repeat(5000) })
  const { outcome } = await ask({ buffer, callId: 1, timeoutMs: 1000 })
  assert.equal(outcome.error.code, 'RESPONSE_TOO_LARGE')
})
await check('a wait without a response times out with a code', async () => {
  const { outcome } = await ask({ buffer: channel.createChannelBuffer(64), callId: 1, timeoutMs: 120 })
  assert.equal(outcome.error.code, 'TIMEOUT')
})
await thread.terminate()

/* --- page-side services against a fake live connection ----------------------- */
const SECRET = 'virtual-attacker-credential-never-leave-the-client'
function fakeConnection({ replyFirst = false } = {}) {
  const listeners = new Set()
  const sent = []
  let closed = false
  let sequence = 0
  const reply = (requestId, value, errorCode = null) => { for (const listener of listeners) listener({ requestId, value, errorCode }) }
  const accept = (kind, detail) => {
    const requestId = `r${(sequence += 1)}`
    sent.push({ kind, ...detail })
    if (replyFirst) reply(requestId, { status: 'succeeded', kind })
    else setTimeout(() => reply(requestId, { status: 'succeeded', kind }), 5)
    return Promise.resolve({ requestId, kind, issuedAtWorldUs: 0 })
  }
  return {
    sent, reply, get closed() { return closed },
    connection: {
      state: 'open', worldTimeUs: 1234, clockMode: 'host_clock', pauseWhileCatchingUp: false,
      submit: (program) => accept('submit', { program }),
      status: (jobId) => accept('status', { jobId }), result: (jobId) => accept('result', { jobId }),
      cancel: (jobId) => accept('cancel', { jobId }),
      onReply(listener) { listeners.add(listener); return () => listeners.delete(listener) },
      onClockChange() { return () => {} }, onStateChange() { return () => {} },
      close() { closed = true },
    },
  }
}
function portFor(fake, solution) {
  const opened = []
  return { opened, port: {
    environmentId: '0123456789abcdef0123456789abcdef',
    async connect() { opened.push(1); return fake.connection },
    async submitSolution(estimator) { return solution(estimator) },
  } }
}

await check('a script request maps the wire payload onto the existing client call', async () => {
  const fake = fakeConnection()
  const { port, opened } = portFor(fake)
  const services = createScriptServices(port, async () => null)
  const open = await services.call('open', {})
  assert.deepEqual(open, { value: { environment_id: port.environmentId, world_time_us: 1234 } })
  const outcome = await services.call('request', { operation: 'program.submit', payload: {
    device_id: 'ibm_kingston', shots: 8, circuit_config: { columns: [] }, initialization: 'inherit', interleave: 'victim' } })
  assert.equal(outcome.value.value.kind, 'submit')
  assert.deepEqual(fake.sent[0].program, { deviceId: 'ibm_kingston', shots: 8, circuitConfig: { columns: [] },
    initialization: 'inherit', interleave: 'victim' })
  await services.call('request', { operation: 'job.result.read', payload: { job_id: 'job-1' } })
  assert.equal(opened.length, 1, 'one connection per run')
  assert.ok(!JSON.stringify(outcome).includes(SECRET))
})
await check('issue sends without waiting and reply collects that request only', async () => {
  const fake = fakeConnection()
  const services = createScriptServices(portFor(fake).port, async () => null)
  const payload = { device_id: 'ibm_kingston', shots: 8, circuit_config: { columns: [] } }
  const first = await services.call('issue', { operation: 'program.submit', payload })
  const second = await services.call('issue', { operation: 'program.submit', payload })
  assert.equal(fake.sent.length, 2)
  assert.notEqual(first.value.request_id, second.value.request_id)
  const later = await services.call('reply', { request_id: second.value.request_id })
  const earlier = await services.call('reply', { request_id: first.value.request_id })
  assert.equal(later.value.value.kind, 'submit')
  assert.equal(earlier.value.value.kind, 'submit')
  assert.equal((await services.call('reply', { request_id: first.value.request_id })).error.code, 'UNKNOWN_REQUEST')
  assert.equal((await services.call('reply', { request_id: 'someone-else' })).error.code, 'UNKNOWN_REQUEST')
})
await check('a reply delivered before the accepted promise settles is not lost', async () => {
  const fake = fakeConnection({ replyFirst: true })
  const services = createScriptServices(portFor(fake).port, async () => null)
  const outcome = await services.call('request', { operation: 'job.status.read', payload: { job_id: 'job-2' } })
  assert.equal(outcome.value.value.kind, 'status')
})
await check('only the public job operations pass; malformed input is refused before sending', async () => {
  const fake = fakeConnection()
  const services = createScriptServices(portFor(fake).port, async () => null)
  for (const [args, code] of [
    [{ operation: 'wait.any', payload: {} }, 'INVALID'],
    [{ operation: 'request.pending.list', payload: { job_id: 'x' } }, 'OPERATION_DENIED'],
    [{ operation: 'job.cancel', payload: { job_id: 'has space' } }, 'INVALID'],
    [{ operation: 'program.submit', payload: { device_id: 'd', shots: 1.5 } }, 'INVALID'],
    [{ operation: 'program.submit', payload: { device_id: 'd', shots: 1, interleave: 'admin' } }, 'INVALID'],
  ]) assert.equal((await services.call('request', args)).error.code, code, JSON.stringify(args))
  assert.equal(fake.sent.length, 0)
  assert.equal((await services.call('format_disk', {})).error.code, 'UNKNOWN_METHOD')
})
await check('answer returns the same snake_case aggregates as the HTTP API and maps refusals', async () => {
  const fake = fakeConnection()
  let reason = null
  const services = createScriptServices(portFor(fake, (estimator) => reason
    ? { ok: false, reason }
    : { ok: true, result: { attempt: 1, attemptsRemaining: 2, correct: 5, total: 6, required: 5, passed: estimator.direction === 'ge' } }).port,
  async () => null)
  assert.deepEqual((await services.call('answer', { threshold: 0.2, direction: 'ge' })).value,
    { attempt: 1, attempts_remaining: 2, correct: 5, total: 6, required: 5, passed: true })
  assert.equal((await services.call('answer', { threshold: 'x', direction: 'ge' })).error.code, 'INVALID')
  reason = 'exhausted'
  assert.equal((await services.call('answer', { threshold: 0.2, direction: 'le' })).error.code, 'EXHAUSTED')
})
await check('stdin passes the input() prompt and the typed line', async () => {
  const prompts = []
  const services = createScriptServices(portFor(fakeConnection()).port, async (prompt) => { prompts.push(prompt); return 'Huew' })
  assert.deepEqual((await services.call('stdin', { prompt: 'name? ' })).value, { line: 'Huew' })
  assert.deepEqual(prompts, ['name? '])
})
await check('dispose closes the script connection and fails a waiting request', async () => {
  const fake = fakeConnection()
  fake.connection.result = () => Promise.resolve({ requestId: 'never', kind: 'result', issuedAtWorldUs: 0 })
  const services = createScriptServices(portFor(fake).port, async () => null)
  const waiting = services.call('request', { operation: 'job.result.read', payload: { job_id: 'job-3' }, timeout_ms: 60_000 })
  await new Promise((r) => setTimeout(r, 10))
  services.dispose()
  assert.equal((await waiting).error.code, 'CLOSED')
  assert.equal(fake.closed, true)
})
await check('a missing reply times out with a code', async () => {
  const fake = fakeConnection()
  fake.connection.status = () => Promise.resolve({ requestId: 'silent', kind: 'status', issuedAtWorldUs: 0 })
  const services = createScriptServices(portFor(fake).port, async () => null)
  assert.equal((await services.call('request', { operation: 'job.status.read', payload: { job_id: 'j' }, timeout_ms: 20 })).error.code, 'TIMEOUT')
})

/* --- runner lifecycle with a fake Worker -------------------------------------- */
function fakeWorkerFactory() {
  const workers = []
  return { workers, create() {
    const listeners = { message: [], error: [] }
    const worker = { posted: [], terminated: false,
      postMessage(message) { this.posted.push(message) },
      terminate() { this.terminated = true },
      addEventListener(type, listener) { listeners[type].push(listener) },
      emit(message) { for (const listener of listeners.message) listener({ data: message }) } }
    workers.push(worker)
    return worker
  } }
}
function runnerWith(factory, killAfterMs = 40) {
  const events = { output: [], phases: [], input: [] }
  const runner = createScriptRunner({
    output: (stream, text) => events.output.push([stream, text]),
    phase: (phase) => events.phases.push(phase),
    inputRequested: (waiting, prompt) => events.input.push([waiting, prompt]),
  }, { createWorker: () => factory.create(), sdk: { 'attack_client/__init__.py': '' }, killAfterMs,
    createChannel: () => channel.createChannelBuffer(1024), createResponder: channel.createResponder })
  return { runner, events }
}
const services = (calls = []) => (readLine) => ({
  async call(method, args) { calls.push([method, args]); return method === 'stdin' ? { value: { line: await readLine('?') } } : { value: { ok: true } } },
  dispose() { calls.push(['dispose']) },
})

await check('a run posts files and SDK once, relays output, answers calls and resolves the exit code', async () => {
  const factory = fakeWorkerFactory()
  const { runner, events } = runnerWith(factory)
  const calls = []
  const finished = runner.run({ files: { 'main.py': 'print(1)' }, entry: 'main.py', argv: ['a'] }, services(calls))
  const worker = factory.workers[0]
  const request = worker.posted[0]
  assert.equal(request.type, 'run')
  assert.equal(request.entry, 'main.py')
  assert.ok(request.channel instanceof SharedArrayBuffer && request.interrupt instanceof SharedArrayBuffer)
  assert.equal(runner.running, true)
  await assert.rejects(runner.run({ files: {}, entry: 'x.py', argv: [] }, services()), /実行中/)
  worker.emit({ type: 'output', runId: request.runId, stream: 'stdout', text: 'hello' })
  worker.emit({ type: 'output', runId: request.runId + 99, stream: 'stdout', text: 'stale run' })
  worker.emit({ type: 'call', runId: request.runId, callId: 1, method: 'request', args: { x: 1 } })
  await new Promise((r) => setTimeout(r, 5))
  const control = new Int32Array(request.channel, 0, 2)
  assert.equal(Atomics.load(control, 0), 1, 'the response for call 1 was written')
  worker.emit({ type: 'exit', runId: request.runId, code: 3 })
  assert.equal(await finished, 3)
  assert.deepEqual(events.output, [['stdout', 'hello']])
  assert.deepEqual(calls.map(([method]) => method), ['request', 'dispose'])
  assert.equal(runner.running, false)
  runner.run({ files: {}, entry: 'b.py', argv: [] }, services())
  assert.equal(factory.workers.length, 1, 'the loaded worker is reused for the next run')
  runner.dispose()
})
await check('input() waits for the terminal line and Ctrl+C abandons it without answering', async () => {
  const factory = fakeWorkerFactory()
  const { runner, events } = runnerWith(factory, 1000)
  const finished = runner.run({ files: {}, entry: 'main.py', argv: [] }, services())
  const worker = factory.workers[0]
  const { runId, channel: buffer, interrupt } = worker.posted[0]
  worker.emit({ type: 'call', runId, callId: 1, method: 'stdin', args: {} })
  await new Promise((r) => setTimeout(r, 5))
  assert.equal(runner.waitingForInput, true)
  assert.deepEqual(events.input.at(-1), [true, '?'])
  assert.equal(runner.provideInput('Huew'), true)
  await new Promise((r) => setTimeout(r, 5))
  assert.equal(Atomics.load(new Int32Array(buffer, 0, 2), 0), 1)
  worker.emit({ type: 'call', runId, callId: 2, method: 'stdin', args: {} })
  await new Promise((r) => setTimeout(r, 5))
  runner.interrupt()
  assert.equal(new Uint8Array(interrupt)[0], 2, 'KeyboardInterrupt is requested')
  assert.equal(runner.waitingForInput, false)
  await new Promise((r) => setTimeout(r, 5))
  assert.equal(Atomics.load(new Int32Array(buffer, 0, 2), 0), 1, 'the abandoned input is not answered')
  worker.emit({ type: 'exit', runId, code: 130 })
  assert.equal(await finished, 130)
  runner.dispose()
})
await check('a script that ignores Ctrl+C is stopped with its worker after the grace period', async () => {
  const factory = fakeWorkerFactory()
  const { runner, events } = runnerWith(factory, 30)
  const finished = runner.run({ files: {}, entry: 'spin.py', argv: [] }, services())
  runner.interrupt()
  assert.equal(await finished, EXIT_KILLED)
  assert.equal(factory.workers[0].terminated, true)
  assert.ok(events.output.some(([stream]) => stream === 'system'))
  runner.run({ files: {}, entry: 'again.py', argv: [] }, services())
  assert.equal(factory.workers.length, 2, 'the next run starts a fresh worker')
  runner.dispose()
})
await check('a second Ctrl+C stops at once', async () => {
  const factory = fakeWorkerFactory()
  const { runner } = runnerWith(factory, 10_000)
  const finished = runner.run({ files: {}, entry: 'spin.py', argv: [] }, services())
  runner.interrupt(); runner.interrupt()
  assert.equal(await finished, EXIT_KILLED)
})
await check('pages that are not cross-origin isolated do not offer the runner', () => {
  assert.equal(scriptRuntimeSupport({ crossOriginIsolated: false }).ok, false)
})

/* --- file store and terminal command ------------------------------------------ */
await check('file names are flat .py names; storage that is broken or too large is ignored', () => {
  assert.equal(files.fileNameProblem('attack.py', {}), null)
  for (const name of ['../x.py', 'dir/x.py', 'x.txt', '1x.py', '.py', 'a b.py']) assert.ok(files.fileNameProblem(name, {}), name)
  assert.ok(files.fileNameProblem('a.py', { 'a.py': '' }))
  const storage = (value) => ({ getItem: () => value })
  assert.equal(files.loadScriptFiles(storage(null)), null)
  assert.equal(files.loadScriptFiles(storage('{bad')), null)
  assert.equal(files.loadScriptFiles(storage(JSON.stringify({ files: { 'a.py': 'x'.repeat(files.MAX_TOTAL_CHARS + 1) } }))), null)
  assert.deepEqual(files.loadScriptFiles(storage(JSON.stringify({ files: { 'a.py': '1', '../b.py': '2' }, active: 'zz.py' }))),
    { files: { 'a.py': '1' }, active: 'a.py' })
  assert.equal(files.loadScriptFiles({ getItem() { throw new Error('denied') } }), null)
  assert.equal(files.saveScriptFiles({ setItem() { throw new Error('quota') } }, { files: {}, active: '' }), false)
})
await check('python <file> [args] is a terminal command', () => {
  assert.deepEqual(terminal.parseTerminalCommand('python main.py --answer 3'), { kind: 'python', file: 'main.py', argv: ['--answer', '3'] })
  assert.equal(terminal.parseTerminalCommand('python').kind, 'error')
})

console.log(`attackScriptRunner: ${checks.length} checks passed`)
for (const label of checks) console.log(`  ${label}`)
