import type { DeviceSnapshotInitialValues, RegionSelectionRequest } from '../types/attackLive'

export type RegionOwner = 'shared' | 'victim' | 'attacker'

export function isConnectedRegion(ids: readonly number[], edges: readonly (readonly [number, number])[]): boolean {
  if (!ids.length || new Set(ids).size !== ids.length) return false
  const selected = new Set(ids)
  const adjacency = new Map(ids.map((id) => [id, new Set<number>()]))
  for (const [left, right] of edges) if (selected.has(left) && selected.has(right)) {
    adjacency.get(left)?.add(right)
    adjacency.get(right)?.add(left)
  }
  const reached = new Set<number>()
  const stack = [ids[0]]
  while (stack.length) {
    const id = stack.pop()!
    if (reached.has(id)) continue
    reached.add(id)
    for (const next of adjacency.get(id) ?? []) if (!reached.has(next)) stack.push(next)
  }
  return reached.size === selected.size
}

export function canEditQubitNoise(
  mode: string, qid: number, owner: RegionOwner,
  regions: { shared: readonly number[]; victim: readonly number[]; attacker: readonly number[] },
): boolean {
  if (mode !== 'calibration_values') return false
  const active = owner === 'shared' ? regions.shared : regions[owner]
  return active.includes(qid)
}

export function replaceRegionQubit(
  ids: readonly number[], add: number, remove: number,
  edges: readonly (readonly [number, number])[], eligible: ReadonlySet<number>,
): { ok: true; ids: number[] } | { ok: false; reason: string } {
  if (!eligible.has(add)) return { ok: false, reason: `qubit ${add}は配置対象外です。` }
  if (!ids.includes(remove)) return { ok: false, reason: `qubit ${remove}は選択領域にありません。` }
  if (ids.includes(add)) return { ok: false, reason: `qubit ${add}はすでに選択されています。` }
  const next = ids.filter((id) => id !== remove).concat(add).sort((a, b) => a - b)
  return isConnectedRegion(next, edges)
    ? { ok: true, ids: next }
    : { ok: false, reason: '入れ替えるとCZ結合で連結しなくなるため、変更できません。' }
}

export function graphEdges(snapshot: DeviceSnapshotInitialValues): [number, number][] {
  return Object.keys(snapshot.cz_calibrations).map((key) => key.split(':').map(Number) as [number, number])
}

export function regionSelectionRequest(
  placement: string,
  regions: { shared: number[]; victim: number[]; attacker: number[] },
): RegionSelectionRequest {
  return placement === 'tenant_dedicated_region'
    ? { victim_physical_qubit_ids: [...regions.victim], attacker_physical_qubit_ids: [...regions.attacker] }
    : { shared_physical_qubit_ids: [...regions.shared] }
}

export function regionDefaults(snapshot: DeviceSnapshotInitialValues) {
  return {
    shared: [...snapshot.active_region_initial],
    victim: [...snapshot.active_region_initial],
    attacker: [...snapshot.alternate_region_initial],
  }
}
