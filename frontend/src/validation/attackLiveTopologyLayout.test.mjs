// The fixed Kingston display layout must stay readable: every qubit placed
// once, every coupler a short line, and no two couplers crossing.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.resolve(here, '..', '..', '..')
const layoutSource = readFileSync(path.join(here, '..', 'data', 'ibmKingstonGraphLayout.ts'), 'utf8')
const conf = JSON.parse(readFileSync(
  path.join(repo, 'data', 'device_snapshots', 'ibm_kingston_2026-04-15', 'conf_kingston.json'), 'utf8'))

const positions = new Map()
for (const match of layoutSource.matchAll(/(\d+): \{ x: (\d+), y: (\d+) \}/g)) {
  positions.set(Number(match[1]), [Number(match[2]), Number(match[3])])
}
const edges = [...new Set(conf.coupling_map.map(([a, b]) => (a < b ? `${a}:${b}` : `${b}:${a}`)))]
  .map((key) => key.split(':').map(Number))

assert.equal(positions.size, conf.n_qubits, 'every qubit has one display position')
assert.equal(new Set([...positions.values()].map((p) => p.join(','))).size, positions.size, 'no two qubits share a position')

const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1])
const spacing = Math.min(...edges.map(([a, b]) => distance(positions.get(a), positions.get(b))))
for (const [a, b] of edges) {
  assert.ok(distance(positions.get(a), positions.get(b)) <= spacing * 1.5, `coupler ${a}-${b} is drawn as a short line`)
}

const orient = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
let crossings = 0
for (let i = 0; i < edges.length; i += 1) {
  for (let j = i + 1; j < edges.length; j += 1) {
    const [a, b] = edges[i], [c, d] = edges[j]
    if (new Set([a, b, c, d]).size < 4) continue
    const [pa, pb, pc, pd] = [a, b, c, d].map((q) => positions.get(q))
    if (orient(pa, pb, pc) * orient(pa, pb, pd) < 0 && orient(pc, pd, pa) * orient(pc, pd, pb) < 0) crossings += 1
  }
}
assert.equal(crossings, 0, 'no two couplers cross')
console.log(`ok - Kingston layout: ${positions.size} qubits, ${edges.length} couplers, 0 crossings`)
