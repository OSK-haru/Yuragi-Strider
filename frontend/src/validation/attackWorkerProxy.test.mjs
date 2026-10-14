// The Worker forwards only /api/attack/* to the API server and keeps the checks intact.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import ts from 'typescript'

const source = readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'worker.ts'), 'utf8')
const output = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const module = { exports: {} }
new Function('module', 'exports', output)(module, module.exports)
const worker = module.exports.default
const { attackProxyRequest } = module.exports
const API = 'https://api.example.test'
const checks = []
const check = (label, operation) => { operation(); checks.push(label) }

check('only /api/attack/ paths are proxied', () => {
  for (const url of ['https://app.test/', 'https://app.test/api/health', 'https://app.test/api/attacker', 'https://app.test/x/api/attack/y']) {
    assert.equal(attackProxyRequest(new Request(url), API), null, url)
  }
  const proxied = attackProxyRequest(new Request('https://app.test/api/attack/environments/owned?x=1', { method: 'POST', body: '{}' }), API)
  assert.equal(proxied.url, `${API}/api/attack/environments/owned?x=1`)
  assert.equal(proxied.method, 'POST')
})

check('client-asserted proxy identity is stripped and Origin/cookies are forwarded untouched', () => {
  const proxied = attackProxyRequest(new Request('https://app.test/api/attack/environments', {
    method: 'POST', body: '{}',
    headers: { origin: 'https://app.test', cookie: 'a=b', 'x-attack-simulator-request': '1',
      'x-forwarded-for': '1.2.3.4', 'x-forwarded-host': 'evil.test', forwarded: 'for=1.2.3.4', 'x-real-ip': '1.2.3.4' },
  }), API)
  for (const name of ['x-forwarded-for', 'x-forwarded-host', 'forwarded', 'x-real-ip']) assert.equal(proxied.headers.get(name), null, name)
  assert.equal(proxied.headers.get('origin'), 'https://app.test')
  assert.equal(proxied.headers.get('cookie'), 'a=b')
  assert.equal(proxied.headers.get('x-attack-simulator-request'), '1')
})

check('Origin is added only for a browser same-origin GET, never for writes or cross-site reads', () => {
  const get = (headers, method = 'GET') => attackProxyRequest(new Request('https://app.test/api/attack/settings/catalog', { method, headers }), API)
  assert.equal(get({ 'sec-fetch-site': 'same-origin' }).headers.get('origin'), 'https://app.test')
  assert.equal(get({ 'sec-fetch-site': 'cross-site' }).headers.get('origin'), null)
  assert.equal(get({}).headers.get('origin'), null)
  assert.equal(get({ 'sec-fetch-site': 'same-origin' }, 'DELETE').headers.get('origin'), null)
})

let assetCalls = 0
const env = { ASSETS: { fetch: async () => { assetCalls += 1; return new Response('asset') } } }
const originalFetch = globalThis.fetch
const forwarded = []
globalThis.fetch = async (request) => {
  forwarded.push(request)
  if (request.url.endsWith('/upgrade')) return { status: 101, headers: new Headers(), upgrade: true } // Node cannot construct a real 101 Response.
  return new Response('{"ok":true}', { status: 200, headers: { 'set-cookie': 'id=1; HttpOnly', 'content-type': 'application/json' } })
}
const asyncChecks = async () => {
  const proxied = await worker.fetch(new Request('https://app.test/api/attack/settings/catalog'), env)
  assert.equal(assetCalls, 0)
  assert.equal(proxied.headers.get('set-cookie'), 'id=1; HttpOnly')
  assert.equal(proxied.headers.get('cache-control'), 'no-store')
  assert.equal(proxied.headers.get('x-content-type-options'), 'nosniff')
  assert.equal(forwarded[0].url, 'https://quantsim-vjul.onrender.com/api/attack/settings/catalog')
  checks.push('proxied responses keep Set-Cookie, are never cached and get the security headers')

  const page = await worker.fetch(new Request('https://app.test/some/page'), env)
  assert.equal(await page.text(), 'asset')
  assert.equal(assetCalls, 1)
  checks.push('everything else is still served from static assets')

  assert.equal(page.headers.get('cross-origin-opener-policy'), 'same-origin')
  assert.equal(page.headers.get('cross-origin-embedder-policy'), 'require-corp')
  assert.doesNotMatch(page.headers.get('content-security-policy'), /unsafe-eval/)
  const runtime = await worker.fetch(new Request('https://app.test/assets/python-runtime/pyodide.asm.wasm'), env)
  assert.equal(runtime.headers.get('cross-origin-embedder-policy'), 'require-corp')
  assert.match(runtime.headers.get('content-security-policy'), /script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'/)
  assert.match(runtime.headers.get('content-security-policy'), /connect-src 'self' https:\/\/cdn\.jsdelivr\.net/)
  checks.push('pages are cross-origin isolated; only the Python runtime path allows wasm and eval')

  const huge = await worker.fetch(new Request('https://app.test/api/attack/environments', {
    method: 'POST', body: 'x'.repeat(80 * 1024), headers: { 'content-length': String(80 * 1024) } }), env)
  assert.equal(huge.status, 413)
  assert.equal(forwarded.length, 1)
  checks.push('oversized bodies are rejected before reaching the API')

  const overridden = await worker.fetch(new Request('https://app.test/api/attack/live/upgrade'), { ...env, ATTACK_API_ORIGIN: 'https://other.example.test' })
  assert.equal(overridden.status, 101)
  assert.equal(overridden.upgrade, true)
  assert.equal(forwarded.at(-1).url, 'https://other.example.test/api/attack/live/upgrade')
  checks.push('a WebSocket upgrade response is returned untouched and the API origin is configurable')
}
try { await asyncChecks() } finally { globalThis.fetch = originalFetch }
console.log(`attackWorkerProxy: ${checks.length} checks passed`)
