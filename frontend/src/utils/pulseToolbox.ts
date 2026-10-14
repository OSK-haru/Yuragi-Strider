/*
 * Pulse-levelのツール箱。
 *
 * 状態エクスプローラーで何を見るかを決める。既存の6項目は表示の出し入れだけで、
 * 追加の指標は保存済みの軌跡の密度行列から計算する。シミュレーションは
 * やり直さないので、実行後に選び直しても、読み込んだ結果に対しても使える。
 *
 * 表示中の値の言い換えになる指標は並べない。2準位1台のエントロピー（純度で
 * 決まる）、占有数だけで決まる単一トランズモンの Z がそれにあたる。
 *
 * ここが状態エクスプローラーの「表示項目」の唯一の持ち主でもある。
 * エクスプローラー側で別に持つと、2か所の on/off が食い違う。
 */

import { apiUrl } from './apiBase'
import type { PulseComplexValue } from '../types/pulse'

export const PULSE_PANEL_KEYS = [
  'physical',
  'metrics',
  'populations',
  'probabilities',
  'output',
  'density',
] as const
export type PulsePanelKey = typeof PULSE_PANEL_KEYS[number]

export const PULSE_PANEL_LABELS: Record<PulsePanelKey, string> = {
  physical: '物理時間',
  metrics: '指標タイムライン',
  populations: '占有数タイムライン',
  probabilities: '確率比較',
  output: '占有確率',
  density: '密度行列',
}

export const PULSE_TOOLBOX_METRIC_KEYS = [
  'entropy',
  'concurrence',
  'negativity',
  'mutualInformation',
  'observables',
] as const
export type PulseToolboxMetricKey = typeof PULSE_TOOLBOX_METRIC_KEYS[number]

export const PULSE_TOOLBOX_METRIC_LABELS: Record<PulseToolboxMetricKey, string> = {
  entropy: 'フォン・ノイマン・エントロピー',
  concurrence: 'コンカレンス',
  negativity: 'ネガティビティ',
  mutualInformation: '相互情報量',
  observables: 'パウリ演算子の期待値',
}

export const MAX_PULSE_TOOLBOX_OBSERVABLES = 8
/* バックエンドの上限と同じ。超える結果は送らず、理由を見せる。 */
export const PULSE_TOOLBOX_MAX_POINTS = 2000
export const PULSE_TOOLBOX_MAX_MATRIX_ELEMENTS = 250_000

export type PulseToolboxSettings = {
  panels: Record<PulsePanelKey, boolean>
  metrics: Record<PulseToolboxMetricKey, boolean>
  /* 空はすべてのトランズモン。 */
  entropyTransmons: number[]
  concurrenceTransmons: [number, number]
  negativitySubsystem: number[]
  mutualInformationA: number[]
  mutualInformationB: number[]
  /* カンマ区切りのパウリ文字列。先頭の文字が q0 に作用する。 */
  observablesInput: string
}

export const initialPulseToolboxSettings: PulseToolboxSettings = {
  panels: {
    physical: false,
    metrics: true,
    populations: false,
    probabilities: true,
    output: false,
    density: false,
  },
  metrics: {
    entropy: false,
    concurrence: false,
    negativity: false,
    mutualInformation: false,
    observables: false,
  },
  entropyTransmons: [],
  concurrenceTransmons: [0, 1],
  negativitySubsystem: [0],
  mutualInformationA: [0],
  mutualInformationB: [1],
  observablesInput: '',
}

/* 状態空間の形。実行前は設定から、実行後は結果から決まる。 */
export type PulseToolboxGeometry = {
  levels: number
  count: number
  /* 軌跡の各点に密度行列があるか。2準位1台のモデルは占有数しか返さない。 */
  hasDensityMatrices: boolean
  pointCount: number
}

export type PulseToolboxCheck = {
  /* 入力の誤り。その指標だけを止める。 */
  errors: Partial<Record<PulseToolboxMetricKey, string>>
  /* この状態空間では定義できない。理由を見せて飛ばす。 */
  unavailable: Partial<Record<PulseToolboxMetricKey, string>>
}

export function parsePulseObservables(input: string): string[] {
  return input
    .split(/[\s,、]+/)
    .map((part) => part.trim().toUpperCase())
    .filter(Boolean)
}

export function resolvedPulseEntropyTransmons(
  settings: PulseToolboxSettings,
  count: number,
): number[] {
  return settings.entropyTransmons.length === 0
    ? Array.from({ length: count }, (_, index) => index)
    : [...settings.entropyTransmons].sort((left, right) => left - right)
}

function subsetError(transmons: number[], count: number, name: string): string | null {
  if (transmons.length === 0) return `${name}のトランズモンを1つ以上選んでください。`
  if (transmons.some((transmon) => transmon < 0 || transmon >= count)) {
    return `${name}に回路にないトランズモンが含まれています。`
  }
  return null
}

function observableError(pauli: string, count: number): string | null {
  if (pauli.length !== count || /[^IXYZ]/.test(pauli)) {
    return `${pauli} は I, X, Y, Z だけを使い、${count}文字にしてください。`
  }
  const weight = pauli.replace(/I/g, '').length
  if (weight === 0) return `${pauli} は恒等演算子で、値は常に1です。`
  if (!/[XY]/.test(pauli) && weight < 2) {
    return `${pauli} は1台の Z で、占有数の差としてすでに表示されています。X か Y を含む、または2台以上に作用する演算子を入れてください。`
  }
  return null
}

export function checkPulseToolbox(
  settings: PulseToolboxSettings,
  geometry: PulseToolboxGeometry,
): PulseToolboxCheck {
  const { levels, count } = geometry
  const errors: PulseToolboxCheck['errors'] = {}
  const unavailable: PulseToolboxCheck['unavailable'] = {}
  const enabled = settings.metrics

  if (!geometry.hasDensityMatrices) {
    const reason = '2準位1台のモデルの軌跡は占有数だけで、各時刻の密度行列がありません。3準位にするか、トランズモンを2台以上にしてください。'
    for (const key of PULSE_TOOLBOX_METRIC_KEYS) unavailable[key] = reason
    return { errors, unavailable }
  }
  const dimension = levels ** count
  if (geometry.pointCount > PULSE_TOOLBOX_MAX_POINTS || geometry.pointCount * dimension * dimension > PULSE_TOOLBOX_MAX_MATRIX_ELEMENTS) {
    const reason = '密度行列の数が多すぎて一度に計算できません。観測点数を減らして実行し直してください。'
    for (const key of PULSE_TOOLBOX_METRIC_KEYS) unavailable[key] = reason
    return { errors, unavailable }
  }

  if (count < 2) {
    for (const key of ['concurrence', 'negativity', 'mutualInformation'] as const) {
      unavailable[key] = '2台以上のトランズモンの間の量なので、1台では定義できません。'
    }
  }
  if (levels !== 2 && unavailable.concurrence === undefined) {
    unavailable.concurrence = 'コンカレンスは2準位のペアに対する量です。3準位ではネガティビティを使ってください。'
  }

  if (enabled.entropy) {
    const transmons = resolvedPulseEntropyTransmons(settings, count)
    errors.entropy = subsetError(transmons, count, 'エントロピー') ?? undefined
  }
  if (enabled.concurrence && unavailable.concurrence === undefined) {
    const [left, right] = settings.concurrenceTransmons
    errors.concurrence = left === right
      ? '異なる2台のトランズモンを選んでください。'
      : subsetError([left, right], count, 'コンカレンス') ?? undefined
  }
  if (enabled.negativity && unavailable.negativity === undefined) {
    errors.negativity = subsetError(settings.negativitySubsystem, count, 'ネガティビティ')
      ?? (settings.negativitySubsystem.length >= count ? '分割の片側に入れないトランズモンを1台以上残してください。' : undefined)
  }
  if (enabled.mutualInformation && unavailable.mutualInformation === undefined) {
    const { mutualInformationA: a, mutualInformationB: b } = settings
    errors.mutualInformation = subsetError(a, count, '相互情報量の A')
      ?? subsetError(b, count, '相互情報量の B')
      ?? (a.some((transmon) => b.includes(transmon)) ? 'A と B に同じトランズモンを入れないでください。' : undefined)
  }
  if (enabled.observables) {
    const observables = parsePulseObservables(settings.observablesInput)
    if (observables.length === 0) {
      errors.observables = count === 1
        ? '演算子を1つ以上入力してください（例: X, Y）。'
        : '演算子を1つ以上入力してください（例: XX, ZZ）。'
    } else if (observables.length > MAX_PULSE_TOOLBOX_OBSERVABLES) {
      errors.observables = `演算子は${MAX_PULSE_TOOLBOX_OBSERVABLES}個までです。`
    } else {
      for (const pauli of observables) {
        const message = observableError(pauli, count)
        if (message !== null) {
          errors.observables = message
          break
        }
      }
    }
  }

  for (const key of PULSE_TOOLBOX_METRIC_KEYS) {
    if (errors[key] === undefined || errors[key] === null || !enabled[key]) delete errors[key]
  }
  return { errors, unavailable }
}

export function isPulseMetricRequested(
  key: PulseToolboxMetricKey,
  settings: PulseToolboxSettings,
  check: PulseToolboxCheck,
): boolean {
  return settings.metrics[key] && check.unavailable[key] === undefined && check.errors[key] === undefined
}

export type PulseToolboxResponse = {
  schema_version: 'pulse-toolbox-v1'
  local_levels: number
  transmon_count: number
  times_us: number[]
  entropy?: { transmons: number[]; values: number[] }
  concurrence?: { transmons: number[]; values: number[] }
  negativity?: { subsystem: number[]; values: number[] }
  mutual_information?: { a: number[]; b: number[]; values: number[] }
  observables?: Array<{ pauli: string; values: number[] }>
}

/* 指標の選択だけを送る部分。選ばれていなければ null。 */
export function pulseToolboxSelection(
  settings: PulseToolboxSettings,
  geometry: PulseToolboxGeometry,
  check: PulseToolboxCheck,
): Record<string, unknown> | null {
  const selection: Record<string, unknown> = {}
  if (isPulseMetricRequested('entropy', settings, check)) {
    selection.entropy_transmons = resolvedPulseEntropyTransmons(settings, geometry.count)
  }
  if (isPulseMetricRequested('concurrence', settings, check)) {
    selection.concurrence_transmons = [...settings.concurrenceTransmons]
  }
  if (isPulseMetricRequested('negativity', settings, check)) {
    selection.negativity_subsystem = [...settings.negativitySubsystem]
  }
  if (isPulseMetricRequested('mutualInformation', settings, check)) {
    selection.mutual_information_a = [...settings.mutualInformationA]
    selection.mutual_information_b = [...settings.mutualInformationB]
  }
  if (isPulseMetricRequested('observables', settings, check)) {
    selection.observables = parsePulseObservables(settings.observablesInput)
  }
  return Object.keys(selection).length === 0 ? null : selection
}

export function pulseToolboxErrorMessage(check: PulseToolboxCheck): string | null {
  for (const key of PULSE_TOOLBOX_METRIC_KEYS) {
    const message = check.errors[key]
    if (message) return `${PULSE_TOOLBOX_METRIC_LABELS[key]}: ${message}`
  }
  return null
}

export type PulseToolboxPoint = {
  timeUs: number
  densityMatrix: PulseComplexValue[][] | null
}

export type PulseToolboxState =
  | { status: 'done'; data: PulseToolboxResponse }
  | { status: 'error'; message: string }

/*
 * 密度行列はエルミートなので、上三角（対角を含む）だけを行順に送る。
 * 値は有効数字10桁に丸める。誤差は1e-10程度で、表示する桁には影響しない。
 */
function matrixPayload(matrix: PulseComplexValue[][]) {
  const round = (value: number) => Number(value.toPrecision(10))
  const upperReal: number[] = []
  const upperImag: number[] = []
  matrix.forEach((row, rowIndex) => {
    for (let column = rowIndex; column < row.length; column += 1) {
      upperReal.push(round(row[column].real))
      upperImag.push(round(row[column].imag))
    }
  })
  return { upper_real: upperReal, upper_imag: upperImag }
}

/* 計算は小さな固有値問題の繰り返しなので、サーバーの打ち切りよりずっと短く待つ。 */
const PULSE_TOOLBOX_TIMEOUT_MS = 30_000

export async function runPulseToolbox(
  selection: Record<string, unknown>,
  geometry: PulseToolboxGeometry,
  points: PulseToolboxPoint[],
  signal: AbortSignal,
): Promise<PulseToolboxState> {
  const matrices = points.map((point) => point.densityMatrix)
  if (matrices.some((matrix) => matrix === null)) {
    return { status: 'error', message: 'この結果には密度行列を持たない時刻があるため計算できません。' }
  }
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort)
  const timeoutId = window.setTimeout(abort, PULSE_TOOLBOX_TIMEOUT_MS)
  try {
    const response = await fetch(apiUrl('/api/analysis/pulse-toolbox'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        local_levels: geometry.levels,
        transmon_count: geometry.count,
        times_us: points.map((point) => point.timeUs),
        density_matrices: (matrices as PulseComplexValue[][][]).map(matrixPayload),
        ...selection,
      }),
      cache: 'no-store',
      signal: controller.signal,
    })
    if (response.status === 503) {
      return { status: 'error', message: 'ツール箱: サーバーが混み合っているため計算できませんでした。少し待って選び直してください。' }
    }
    if (!response.ok) {
      return { status: 'error', message: `ツール箱: サーバーで計算できませんでした（HTTP ${response.status}）。` }
    }
    const data = await response.json() as { schema_version?: unknown }
    if (data.schema_version !== 'pulse-toolbox-v1') {
      return { status: 'error', message: 'ツール箱: 応答の形式を解釈できませんでした。' }
    }
    return { status: 'done', data: data as PulseToolboxResponse }
  } catch (error) {
    if (signal.aborted) return { status: 'error', message: 'ツール箱: 設定が変わったため中止しました。' }
    if (error instanceof DOMException && error.name === 'AbortError') {
      return { status: 'error', message: 'ツール箱: 待ち時間の上限に達したため打ち切りました。' }
    }
    return { status: 'error', message: 'ツール箱: サーバーに接続できませんでした。' }
  } finally {
    window.clearTimeout(timeoutId)
    signal.removeEventListener('abort', abort)
  }
}

/*
 * 同じ結果・同じ選択に対する計算結果の控え。エクスプローラーを離れて戻るたびに
 * 計算し直さないための小さなキャッシュで、軌跡の配列が捨てられれば一緒に消える。
 */
const resultCache = new WeakMap<object, Map<string, PulseToolboxResponse>>()

export function cachedPulseToolbox(points: object, key: string): PulseToolboxResponse | undefined {
  return resultCache.get(points)?.get(key)
}

export function storePulseToolbox(points: object, key: string, data: PulseToolboxResponse) {
  let entries = resultCache.get(points)
  if (entries === undefined) {
    entries = new Map()
    resultCache.set(points, entries)
  }
  entries.set(key, data)
}
