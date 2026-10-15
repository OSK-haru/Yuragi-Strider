import type { VictimActivity, VictimActivityStep } from '../types/attackLive'

/*
 * 自由モードの抽象コマンド「被害者の動き」。サービスが返す記録（誰が・いつ・何をしたか）を、
 * 画面とターミナルで同じ言葉に直す。状態や被害者の測定値はサービスが返さないので、ここにも無い。
 */

const ACTORS = new Set(['victim', 'service', 'attacker', 'unknown'])
const MAX_SHOTS = 64
const MAX_STEPS = 16

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isTime(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function parseStep(value: unknown): VictimActivityStep | null {
  if (!isRecord(value) || typeof value.actor !== 'string' || !ACTORS.has(value.actor)
      || typeof value.kind !== 'string' || value.kind.length > 40
      || !isTime(value.start_us) || !isTime(value.end_us) || value.end_us < value.start_us) return null
  const after = value.after === 'victim' || value.after === 'attacker' ? value.after : null
  return { actor: value.actor as VictimActivityStep['actor'], kind: value.kind, after,
    start_us: value.start_us, end_us: value.end_us }
}

/** サービスの返事が「被害者の動き」の形なら取り出す。形が違えば null（捏造しない）。 */
export function parseVictimActivity(value: unknown): VictimActivity | null {
  if (!isRecord(value) || typeof value.job_id !== 'string' || !Array.isArray(value.shots)
      || value.shots.length > MAX_SHOTS || !Number.isSafeInteger(value.shots_total)
      || !Number.isSafeInteger(value.shots_shown)) return null
  const reference = value.reference === null || typeof value.reference === 'string' ? value.reference : undefined
  const bit = value.victim_prepared_bit
  if (reference === undefined || !(bit === null || bit === 0 || bit === 1)) return null
  const shots: VictimActivity['shots'] = []
  for (const shot of value.shots) {
    if (!isRecord(shot) || !Number.isSafeInteger(shot.index) || !Array.isArray(shot.steps) || shot.steps.length > MAX_STEPS) return null
    const steps = shot.steps.map(parseStep)
    if (steps.some((step) => step === null)) return null
    shots.push({ index: shot.index as number, steps: steps as VictimActivityStep[] })
  }
  return { job_id: value.job_id, reference, victim_prepared_bit: bit, shots_total: value.shots_total as number,
    shots_shown: value.shots_shown as number, shots }
}

/** 1 手順の呼び名。Reset は「誰の状態を戻したか」で分ける（漏れの経路はここにある）。 */
export function stepLabel(step: VictimActivityStep): string {
  if (step.kind === 'victim_run') return '被害者の実行'
  if (step.kind === 'attacker_measure') return '攻撃者の測定'
  if (step.kind === 'service_reset') {
    return step.after === 'victim' ? 'Reset（被害者の後）' : step.after === 'attacker' ? 'Reset（攻撃者の後）' : 'Reset'
  }
  if (step.kind === 'idle') return '待機'
  return step.kind
}

export function referenceLabel(reference: string | null): string {
  if (reference === 'victim') return '本物の被害者'
  if (reference === 'training:0') return '練習相手 ref:0（bit 0）'
  if (reference === 'training:1') return '練習相手 ref:1（bit 1）'
  return '被害者なし（通常のジョブ）'
}

function microseconds(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(3)}ms` : `${value.toFixed(2)}µs`
}

/** ターミナル用の文章。長いジョブは先頭の数 shot だけで、残りは件数で示す。 */
export function describeVictimActivity(activity: VictimActivity, linesPerShot = 4): string[] {
  const head = `被害者の動き（${activity.job_id}）: ${referenceLabel(activity.reference)}`
    + (activity.victim_prepared_bit === null ? '' : ` · 被害者が準備する bit: ${activity.victim_prepared_bit}`)
  if (activity.reference === null) return [head, '  このジョブには被害者の実行が挟まっていません（--interleave を付けた観測で挟まります）。']
  const lines = [head]
  for (const shot of activity.shots.slice(0, linesPerShot)) {
    lines.push(`  shot ${shot.index}: ${shot.steps.map((step) => `${stepLabel(step)} ${microseconds(step.start_us)}–${microseconds(step.end_us)}`).join(' → ')}`)
  }
  const hidden = activity.shots_total - Math.min(activity.shots.length, linesPerShot)
  if (hidden > 0) lines.push(`  …ほか ${hidden} shot も同じ順番です（画面の「被害者の動き」には先頭 ${activity.shots_shown} shot を出しています）。`)
  return lines
}
