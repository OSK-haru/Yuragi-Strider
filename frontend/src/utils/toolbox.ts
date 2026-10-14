/*
 * Gate-awareのツール箱。
 *
 * 実行前に「状態エクスプローラーで何を見るか」を決める。既存の8項目は
 * 表示の出し入れだけで、計算は常に行われている。追加の指標は選んだものだけを
 * 計算する。表示中の値の言い換えになる指標（線形エントロピー、ビュレス距離、
 * 1量子ビットのエントロピーなど）は最初から並べない。
 *
 * ここが状態エクスプローラーの「表示項目」の唯一の持ち主でもある。
 * エクスプローラー側で別に持つと、2か所の on/off が食い違う。
 */

import type { SimulationResponse } from '../types/simulation'
import { apiUrl } from './apiBase'
import type { CircuitConfig } from './circuitConfig'

export const EXPLORER_PANEL_KEYS = [
  'playback',
  'physical',
  'metrics',
  'probabilities',
  'output',
  'bloch',
  'density',
  'rates',
] as const
export type ExplorerPanelKey = typeof EXPLORER_PANEL_KEYS[number]

export const EXPLORER_PANEL_LABELS: Record<ExplorerPanelKey, string> = {
  playback: '再生バー',
  physical: '物理時間',
  metrics: '指標タイムライン',
  probabilities: '確率比較',
  output: '出力確率',
  bloch: 'Bloch球',
  density: '密度行列',
  rates: '環境レート',
}

export const TOOLBOX_METRIC_KEYS = [
  'entropy',
  'concurrence',
  'negativity',
  'mutualInformation',
  'traceDistance',
  'observables',
  'averageGateFidelity',
  'processMatrix',
  't1Sweep',
  'ramseySweep',
] as const
export type ToolboxMetricKey = typeof TOOLBOX_METRIC_KEYS[number]

export const TOOLBOX_METRIC_LABELS: Record<ToolboxMetricKey, string> = {
  entropy: 'フォン・ノイマン・エントロピー',
  concurrence: 'コンカレンス',
  negativity: 'ネガティビティ',
  mutualInformation: '相互情報量',
  traceDistance: 'トレース距離（理想状態に対して）',
  observables: '多量子ビット演算子の期待値',
  averageGateFidelity: '平均ゲート忠実度',
  processMatrix: 'χ 行列（プロセストモグラフィー）',
  t1Sweep: 'T1 スイープ',
  ramseySweep: 'Ramsey（T2*）スイープ',
}

/* 1回の通常実行の中で、スナップショットから計算するもの。 */
export const SNAPSHOT_METRIC_KEYS: ToolboxMetricKey[] = [
  'entropy',
  'concurrence',
  'negativity',
  'mutualInformation',
  'traceDistance',
  'observables',
]

/* 通常実行のあとに、別の実行を重ねて求めるもの。 */
export type ToolboxJobKey = 'process' | 't1Sweep' | 'ramseySweep'

export const EXACT_TOMOGRAPHY_MAX_QUBITS = 3
export const MONTE_CARLO_MAX_QUBITS = 6
export const MONTE_CARLO_SAMPLE_RANGE = { min: 16, max: 512 }
export const MAX_TOOLBOX_OBSERVABLES = 8

export type ToolboxSettings = {
  panels: Record<ExplorerPanelKey, boolean>
  metrics: Record<ToolboxMetricKey, boolean>
  /* 空はすべての量子ビット。 */
  entropyQubits: number[]
  concurrenceQubits: [number, number]
  negativitySubsystem: number[]
  mutualInformationA: number[]
  mutualInformationB: number[]
  /* カンマ区切りのパウリ文字列。先頭の文字が q0 に作用する。 */
  observablesInput: string
  monteCarloSamples: number
  /* null は T1 max の3倍を使う。 */
  t1MaximumDelayUs: number | null
  t1PointCount: number
  /* null は Tφ max の3倍を使う。 */
  ramseyMaximumDelayUs: number | null
  ramseyPointCount: number
  ramseyDetuningMhz: number
}

export const initialToolboxSettings: ToolboxSettings = {
  panels: {
    playback: true,
    physical: true,
    metrics: true,
    probabilities: true,
    output: true,
    bloch: true,
    density: true,
    rates: true,
  },
  metrics: {
    entropy: false,
    concurrence: false,
    negativity: false,
    mutualInformation: false,
    traceDistance: false,
    observables: false,
    averageGateFidelity: false,
    processMatrix: false,
    t1Sweep: false,
    ramseySweep: false,
  },
  entropyQubits: [],
  concurrenceQubits: [0, 1],
  negativitySubsystem: [0],
  mutualInformationA: [0],
  mutualInformationB: [1],
  observablesInput: '',
  monteCarloSamples: 64,
  t1MaximumDelayUs: null,
  t1PointCount: 31,
  ramseyMaximumDelayUs: null,
  ramseyPointCount: 61,
  ramseyDetuningMhz: 0.01,
}

export type ToolboxCheck = {
  /* 入力の誤り。実行を止める。 */
  errors: Partial<Record<ToolboxMetricKey, string>>
  /* この回路では定義できない。止めずに飛ばし、理由を見せる。 */
  unavailable: Partial<Record<ToolboxMetricKey, string>>
}

function hasMeasurement(circuit: CircuitConfig) {
  return circuit.columns.some((column) => column.gates.some((gate) => gate.type === 'MEASURE'))
}

function hasClassicalCondition(circuit: CircuitConfig) {
  return circuit.columns.some((column) => column.gates.some((gate) => (gate.conditions ?? []).length > 0))
}

export function parseObservables(input: string): string[] {
  return input
    .split(/[\s,、]+/)
    .map((part) => part.trim().toUpperCase())
    .filter(Boolean)
}

function subsetError(qubits: number[], qubitCount: number, name: string): string | null {
  if (qubits.length === 0) return `${name}の量子ビットを1つ以上選んでください。`
  if (qubits.some((qubit) => qubit < 0 || qubit >= qubitCount)) {
    return `${name}に回路にない量子ビットが含まれています。`
  }
  return null
}

export function resolvedEntropyQubits(settings: ToolboxSettings, qubitCount: number): number[] {
  return settings.entropyQubits.length === 0
    ? Array.from({ length: qubitCount }, (_, index) => index)
    : [...settings.entropyQubits].sort((left, right) => left - right)
}

export function processSimulationCount(settings: ToolboxSettings, qubitCount: number): number {
  return qubitCount <= EXACT_TOMOGRAPHY_MAX_QUBITS ? 4 ** qubitCount : settings.monteCarloSamples
}

export function resolvedSweepDelays(
  settings: ToolboxSettings,
  t1MaxUs: number,
  tphiMaxUs: number,
): { t1: number; ramsey: number } {
  const auto = (base: number) => Math.min(10_000, Math.max(10, base * 3))
  return {
    t1: settings.t1MaximumDelayUs ?? auto(t1MaxUs),
    ramsey: settings.ramseyMaximumDelayUs ?? auto(tphiMaxUs),
  }
}

export function checkToolbox(
  settings: ToolboxSettings,
  circuit: CircuitConfig,
  sweepDelays: { t1: number; ramsey: number },
): ToolboxCheck {
  const qubitCount = circuit.logical_qubits
  const errors: ToolboxCheck['errors'] = {}
  const unavailable: ToolboxCheck['unavailable'] = {}
  const enabled = settings.metrics

  if (qubitCount < 2) {
    for (const key of ['entropy', 'concurrence', 'negativity', 'mutualInformation', 'observables'] as const) {
      unavailable[key] = '2量子ビット以上の回路で使えます。1量子ビットでは、Bloch球と純度で表せる値と同じになります。'
    }
  } else {
    if (enabled.entropy) {
      const qubits = resolvedEntropyQubits(settings, qubitCount)
      errors.entropy = subsetError(qubits, qubitCount, 'エントロピー')
        ?? (qubits.length < 2 ? '2量子ビット以上を選んでください。1量子ビットのエントロピーはBloch球のベクトル長で決まります。' : undefined)
    }
    if (enabled.concurrence) {
      const [left, right] = settings.concurrenceQubits
      errors.concurrence = left === right
        ? '異なる2つの量子ビットを選んでください。'
        : subsetError([left, right], qubitCount, 'コンカレンス') ?? undefined
    }
    if (enabled.negativity) {
      errors.negativity = subsetError(settings.negativitySubsystem, qubitCount, 'ネガティビティ')
        ?? (settings.negativitySubsystem.length >= qubitCount ? '分割の片側に入れない量子ビットを1つ以上残してください。' : undefined)
    }
    if (enabled.mutualInformation) {
      const { mutualInformationA: a, mutualInformationB: b } = settings
      errors.mutualInformation = subsetError(a, qubitCount, '相互情報量の A')
        ?? subsetError(b, qubitCount, '相互情報量の B')
        ?? (a.some((qubit) => b.includes(qubit)) ? 'A と B に同じ量子ビットを入れないでください。' : undefined)
    }
    if (enabled.observables) {
      const observables = parseObservables(settings.observablesInput)
      if (observables.length === 0) {
        errors.observables = '演算子を1つ以上入力してください（例: ZZ, XXI）。'
      } else if (observables.length > MAX_TOOLBOX_OBSERVABLES) {
        errors.observables = `演算子は${MAX_TOOLBOX_OBSERVABLES}個までです。`
      } else {
        const bad = observables.find((pauli) => pauli.length !== qubitCount || /[^IXYZ]/.test(pauli))
        const light = observables.find((pauli) => pauli.replace(/I/g, '').length < 2)
        if (bad !== undefined) {
          errors.observables = `${bad} は I, X, Y, Z だけを使い、${qubitCount}文字にしてください。`
        } else if (light !== undefined) {
          errors.observables = `${light} は1量子ビット以下に作用します。その値はBloch球の成分と同じなので、2量子ビット以上に作用する演算子を入れてください。`
        }
      }
    }
  }

  const processReason = hasMeasurement(circuit)
    ? '測定を含む回路は理想の過程がユニタリにならないため、定義できません。'
    : hasClassicalCondition(circuit)
      ? '古典条件付きゲートを含む回路には、単一の理想ユニタリがありません。'
      : null
  if (processReason !== null) {
    unavailable.averageGateFidelity = processReason
    unavailable.processMatrix = processReason
  } else {
    if (qubitCount > MONTE_CARLO_MAX_QUBITS) {
      unavailable.averageGateFidelity = `${MONTE_CARLO_MAX_QUBITS}量子ビットまでです。`
    }
    if (qubitCount > EXACT_TOMOGRAPHY_MAX_QUBITS) {
      unavailable.processMatrix = `χ 行列は厳密なトモグラフィーが必要で、${EXACT_TOMOGRAPHY_MAX_QUBITS}量子ビットまでです。`
    }
  }
  if (
    enabled.averageGateFidelity
    && qubitCount > EXACT_TOMOGRAPHY_MAX_QUBITS
    && (
      !Number.isInteger(settings.monteCarloSamples)
      || settings.monteCarloSamples < MONTE_CARLO_SAMPLE_RANGE.min
      || settings.monteCarloSamples > MONTE_CARLO_SAMPLE_RANGE.max
    )
  ) {
    errors.averageGateFidelity = `サンプル数は${MONTE_CARLO_SAMPLE_RANGE.min}から${MONTE_CARLO_SAMPLE_RANGE.max}の整数にしてください。`
  }

  if (enabled.t1Sweep) {
    if (!Number.isFinite(sweepDelays.t1) || sweepDelays.t1 <= 0 || sweepDelays.t1 > 10_000) {
      errors.t1Sweep = 'T1 スイープの最大遅延は0より大きく10,000 μs以下にしてください。'
    } else if (!Number.isInteger(settings.t1PointCount) || settings.t1PointCount < 5 || settings.t1PointCount > 101) {
      errors.t1Sweep = 'T1 スイープの測定点数は5から101の整数にしてください。'
    }
  }
  if (enabled.ramseySweep) {
    const { ramseyPointCount: points, ramseyDetuningMhz: detuning } = settings
    if (!Number.isFinite(sweepDelays.ramsey) || sweepDelays.ramsey <= 0 || sweepDelays.ramsey > 10_000) {
      errors.ramseySweep = 'Ramsey スイープの最大遅延は0より大きく10,000 μs以下にしてください。'
    } else if (!Number.isInteger(points) || points < 9 || points > 101) {
      errors.ramseySweep = 'Ramsey スイープの測定点数は9から101の整数にしてください。'
    } else if (!Number.isFinite(detuning) || detuning <= 0 || detuning > 1) {
      errors.ramseySweep = '離調は0より大きく1 MHz以下にしてください。'
    } else if (detuning >= 0.5 * (points - 1) / sweepDelays.ramsey) {
      errors.ramseySweep = 'この離調では測定点が粗すぎます。測定点数を増やすか最大遅延を短くしてください。'
    }
  }

  for (const key of TOOLBOX_METRIC_KEYS) {
    if (errors[key] === undefined || errors[key] === null) delete errors[key]
    if (!enabled[key]) delete errors[key]
  }
  return { errors, unavailable }
}

export function isRequested(
  key: ToolboxMetricKey,
  settings: ToolboxSettings,
  check: ToolboxCheck,
): boolean {
  return settings.metrics[key] && check.unavailable[key] === undefined && check.errors[key] === undefined
}

/* /api/simulate に載せる toolbox ブロック。何も選ばれていなければ null。 */
export function toolboxRequestBlock(
  settings: ToolboxSettings,
  qubitCount: number,
  check: ToolboxCheck,
): Record<string, unknown> | null {
  const block: Record<string, unknown> = {}
  if (isRequested('entropy', settings, check)) block.entropy_qubits = resolvedEntropyQubits(settings, qubitCount)
  if (isRequested('concurrence', settings, check)) block.concurrence_qubits = [...settings.concurrenceQubits]
  if (isRequested('negativity', settings, check)) block.negativity_subsystem = [...settings.negativitySubsystem]
  if (isRequested('mutualInformation', settings, check)) {
    block.mutual_information_a = [...settings.mutualInformationA]
    block.mutual_information_b = [...settings.mutualInformationB]
  }
  if (isRequested('traceDistance', settings, check)) block.trace_distance = true
  if (isRequested('observables', settings, check)) block.observables = parseObservables(settings.observablesInput)
  return Object.keys(block).length === 0 ? null : block
}

export function toolboxErrorMessage(check: ToolboxCheck): string | null {
  for (const key of TOOLBOX_METRIC_KEYS) {
    const message = check.errors[key]
    if (message) return `ツール箱（${TOOLBOX_METRIC_LABELS[key]}）: ${message}`
  }
  return null
}

/* 通常実行のあとに走らせる追加の実行。送る本文は実行時点の設定で固める。 */
export type ToolboxJobRequest = {
  key: ToolboxJobKey
  path: string
  body: Record<string, unknown>
}

export function toolboxJobRequests(
  settings: ToolboxSettings,
  check: ToolboxCheck,
  simulatePayload: Record<string, unknown>,
  sweep: {
    parameters: Record<string, unknown>
    readoutError: { p10: number; p01: number } | null
    delays: { t1: number; ramsey: number }
  },
): ToolboxJobRequest[] {
  const jobs: ToolboxJobRequest[] = []
  const wantsFidelity = isRequested('averageGateFidelity', settings, check)
  const wantsChi = isRequested('processMatrix', settings, check)
  if (wantsFidelity || wantsChi) {
    const body: Record<string, unknown> = {
      ...simulatePayload,
      include_process_matrix: wantsChi,
      monte_carlo_samples: settings.monteCarloSamples,
    }
    delete body.toolbox
    jobs.push({ key: 'process', path: '/api/analysis/process-characterization', body })
  }
  const sweepBase = {
    simulation_backend: simulatePayload.simulation_backend,
    evolution_method: simulatePayload.evolution_method,
    parameters: sweep.parameters,
    ...(sweep.readoutError === null ? {} : { readout_error: sweep.readoutError }),
  }
  if (isRequested('t1Sweep', settings, check)) {
    jobs.push({
      key: 't1Sweep',
      path: '/api/analysis/t1-sweep',
      body: { ...sweepBase, maximum_delay_us: sweep.delays.t1, point_count: settings.t1PointCount },
    })
  }
  if (isRequested('ramseySweep', settings, check)) {
    jobs.push({
      key: 'ramseySweep',
      path: '/api/analysis/ramsey-sweep',
      body: {
        ...sweepBase,
        maximum_delay_us: sweep.delays.ramsey,
        point_count: settings.ramseyPointCount,
        detuning_mhz: settings.ramseyDetuningMhz,
      },
    })
  }
  return jobs
}

export type ToolboxJobState =
  | { status: 'pending' }
  | { status: 'done'; data: unknown }
  | { status: 'error'; message: string }

/* 通常実行に続く追加実行の進み具合。どの応答に対するものかを一緒に持つ。 */
export type ToolboxJobsState = {
  response: SimulationResponse
  jobs: Partial<Record<ToolboxJobKey, ToolboxJobState>>
}

/* サーバーの打ち切り（120秒）より少しだけ長く待つ。 */
const TOOLBOX_JOB_TIMEOUT_MS = 130_000

const JOB_SCHEMA: Record<ToolboxJobKey, string> = {
  process: 'process-characterization-v1',
  t1Sweep: 't1-sweep-v1',
  ramseySweep: 'ramsey-sweep-v1',
}

const JOB_LABELS: Record<ToolboxJobKey, string> = {
  process: 'プロセスの評価',
  t1Sweep: 'T1 スイープ',
  ramseySweep: 'Ramsey スイープ',
}

export async function runToolboxJob(
  request: ToolboxJobRequest,
  signal: AbortSignal,
): Promise<ToolboxJobState> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort)
  const timeoutId = window.setTimeout(abort, TOOLBOX_JOB_TIMEOUT_MS)
  const label = JOB_LABELS[request.key]
  try {
    const response = await fetch(apiUrl(request.path), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request.body),
      cache: 'no-store',
      signal: controller.signal,
    })
    if (response.status === 503) {
      return { status: 'error', message: `${label}: サーバーが混み合っているため実行できませんでした。少し待って再実行してください。` }
    }
    if (response.status === 504) {
      return { status: 'error', message: `${label}: 計算が時間の上限を超えました。量子ビット数・サンプル数・測定点数を減らしてください。` }
    }
    if (!response.ok) {
      return { status: 'error', message: `${label}: サーバーで計算できませんでした（HTTP ${response.status}）。` }
    }
    const data = await response.json() as { schema_version?: unknown }
    if (data.schema_version !== JOB_SCHEMA[request.key]) {
      return { status: 'error', message: `${label}: 応答の形式を解釈できませんでした。` }
    }
    return { status: 'done', data }
  } catch (error) {
    if (signal.aborted) return { status: 'error', message: `${label}: 新しい実行が始まったため中止しました。` }
    if (error instanceof DOMException && error.name === 'AbortError') {
      return { status: 'error', message: `${label}: 待ち時間の上限に達したため打ち切りました。` }
    }
    return { status: 'error', message: `${label}: サーバーに接続できませんでした。` }
  } finally {
    window.clearTimeout(timeoutId)
    signal.removeEventListener('abort', abort)
  }
}
