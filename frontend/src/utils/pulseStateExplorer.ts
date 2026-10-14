/*
 * Pulse の応答は、モデルごとに軌跡の点の形が違う。
 * 2準位は open/closed の占有数を別々に持ち、qutrit は準位ごと、
 * 結合ペアと多トランズモンは基底ラベル付きの同時占有数を持つ。
 * 状態エクスプローラーは同じ読み方（占有数・純度・リーケージ・参照値）で
 * 全モデルを並べたいので、ここで1つの形へ正規化する。
 */

import {
  isCoupledTransmonNetworkResponse,
  isQutritPulseResponse,
  type PulseComplexValue,
  type PulseLabForm,
  type PulseReadout,
  type PulseResponse,
  type PulseRunRecord,
} from '../types/pulse'
import type { PulseCircuitState } from '../types/pulseCircuit'
import { qutritTargetOverlap } from './pulseLab'

export type PulseExplorerSegment = 'pulse' | 'idle' | 'virtual_z'

/*
 * 見比べるために取っておいた実行と、比較表示の on/off。
 *
 * 状態エクスプローラーのページではなく App が持つ。ページ側で useState すると、
 * Pulseラボへ戻って再実行しようとしただけで保持が捨てられ、
 * 「保存したのに比べられない」ことになる。
 *
 * comparability は保持した時点の「環境以外」の指紋。
 * pulseComparabilitySignature を参照。
 */
export type PulseComparisonState = {
  /* heldAt は「実行した時刻」ではなく「保存した時刻」。帯の表示に使う。 */
  heldRun: { record: PulseRunRecord; comparability: string; heldAt: string } | null
  comparing: boolean
}

export const initialPulseComparisonState: PulseComparisonState = {
  heldRun: null,
  comparing: false,
}

export type PulseExplorerPoint = {
  timeUs: number
  segment: PulseExplorerSegment
  stepLabel: string | null
  /* 基底ラベル -> 占有確率。密度行列の対角成分にあたる。 */
  populations: Record<string, number>
  observedPopulations: Record<string, number>
  /* 散逸のない閉じた系の占有確率。2準位モデルだけが持つ。 */
  idealPopulations: Record<string, number> | null
  purity: number
  leakage: number | null
  /* 忠実度または目標状態との重なり。モデルによってどちらかになる。 */
  reference: number | null
  densityMatrix: PulseComplexValue[][] | null
}

export type PulseExplorerView = {
  modelLabel: string
  basisLabels: string[]
  computationalLabels: string[]
  dimension: number
  transmonCount: number
  levelCount: number
  points: PulseExplorerPoint[]
  finalDensityMatrix: PulseComplexValue[][] | null
  /* 参照値の呼び名。無い場合は null（結合モデルには対応する参照が無い）。 */
  referenceLabel: string | null
  idealLabel: string | null
  leakageLabel: string | null
  hasPerPointDensityMatrix: boolean
  pulseEndTimeUs: number
  totalTimeUs: number
  sequenceLength: number
  readoutEnabled: boolean
}

export function buildPulseExplorerView(
  response: PulseResponse,
  formAtRun: PulseLabForm,
): PulseExplorerView {
  if (isQutritPulseResponse(response)) {
    const isSequence = Number(response.input.sequence_length ?? 1) > 1
    const points: PulseExplorerPoint[] = response.trajectory.map((point) => ({
      timeUs: point.time_us,
      segment: point.segment,
      stepLabel: point.sequence_step_label ?? null,
      populations: {
        '0': point.population_0,
        '1': point.population_1,
        '2': point.population_2,
      },
      observedPopulations: applyPulseReadoutError({
        '0': point.population_0,
        '1': point.population_1,
        '2': point.population_2,
      }, response.readout),
      idealPopulations: null,
      purity: point.purity,
      leakage: point.leakage_probability,
      reference: isSequence ? null : qutritTargetOverlap(point, formAtRun),
      densityMatrix: point.density_matrix,
    }))
    return {
      modelLabel: '3準位トランズモン qutrit',
      basisLabels: ['0', '1', '2'],
      computationalLabels: ['0', '1'],
      dimension: 3,
      transmonCount: 1,
      levelCount: 3,
      points,
      finalDensityMatrix: response.final.density_matrix,
      referenceLabel: isSequence ? null : '目標状態との重なり',
      idealLabel: null,
      leakageLabel: 'リーケージ P2',
      hasPerPointDensityMatrix: true,
      pulseEndTimeUs: response.pulse_end.time_us,
      totalTimeUs: lastTimeUs(points),
      sequenceLength: Number(response.input.sequence_length ?? 1),
      readoutEnabled: response.readout?.enabled ?? false,
    }
  }

  if (isCoupledTransmonNetworkResponse(response)) {
    const basisLabels = response.model.basis_order
    const transmonCount = response.model.logical_qubits
    const points: PulseExplorerPoint[] = response.trajectory.map((point) => ({
      timeUs: point.time_us,
      segment: point.segment,
      stepLabel: null,
      populations: { ...point.joint_populations },
      observedPopulations: applyPulseReadoutError(point.joint_populations, response.readout),
      idealPopulations: null,
      purity: point.purity,
      leakage: point.leakage_probability,
      reference: null,
      densityMatrix: point.density_matrix,
    }))
    /* 局所準位数は 次元 = levels^台数 から戻す（2準位ネットワークにも対応）。 */
    const levelCount = Math.round(basisLabels.length ** (1 / Math.max(1, transmonCount)))
    return {
      modelLabel: `${transmonCount}トランズモン・ネットワーク / ${levelCount}^${transmonCount}準位`,
      basisLabels,
      computationalLabels: basisLabels.filter((label) => /^[01]+$/.test(label)),
      dimension: basisLabels.length,
      transmonCount,
      levelCount,
      points,
      finalDensityMatrix: response.final.density_matrix,
      referenceLabel: null,
      idealLabel: null,
      leakageLabel: '計算空間外の確率',
      hasPerPointDensityMatrix: true,
      pulseEndTimeUs: response.pulse_end.time_us,
      totalTimeUs: lastTimeUs(points),
      sequenceLength: 1,
      readoutEnabled: response.readout?.enabled ?? false,
    }
  }

  const points: PulseExplorerPoint[] = response.trajectory.map((point) => ({
    timeUs: point.time_us,
    segment: point.segment,
    stepLabel: null,
    populations: {
      '0': point.open_population_0,
      '1': point.open_population_1,
    },
    observedPopulations: applyPulseReadoutError({
      '0': point.open_population_0,
      '1': point.open_population_1,
    }, response.readout),
    idealPopulations: {
      '0': point.closed_population_0,
      '1': point.closed_population_1,
    },
    purity: point.purity,
    leakage: null,
    reference: point.fidelity_to_closed,
    /* 2準位の軌跡は占有数だけを返す。密度行列は pulse_end と final にしかない。 */
    densityMatrix: null,
  }))
  return {
    modelLabel: '2準位ベースライン',
    basisLabels: ['0', '1'],
    computationalLabels: ['0', '1'],
    dimension: 2,
    transmonCount: 1,
    levelCount: 2,
    points,
    finalDensityMatrix: response.final.open_density_matrix,
    referenceLabel: '閉じた系への忠実度',
    idealLabel: '閉じた系（散逸なし）',
    leakageLabel: null,
    hasPerPointDensityMatrix: false,
    pulseEndTimeUs: response.pulse_end.time_us,
    totalTimeUs: lastTimeUs(points),
    sequenceLength: 1,
    readoutEnabled: response.readout?.enabled ?? false,
  }
}

export function applyPulseReadoutError(
  populations: Record<string, number>,
  readout: PulseReadout | undefined,
): Record<string, number> {
  if (!readout) {
    return { ...populations }
  }
  let observed = { ...populations }
  readout.assignment_errors.forEach(({ p10, p01 }, subsystem) => {
    const updated = Object.fromEntries(Object.keys(observed).map((label) => [label, 0]))
    Object.entries(observed).forEach(([label, probability]) => {
      const level = label[subsystem]
      if (level === '0') {
        updated[label] += (1 - p10) * probability
        const flipped = `${label.slice(0, subsystem)}1${label.slice(subsystem + 1)}`
        updated[flipped] += p10 * probability
      } else if (level === '1') {
        updated[label] += (1 - p01) * probability
        const flipped = `${label.slice(0, subsystem)}0${label.slice(subsystem + 1)}`
        updated[flipped] += p01 * probability
      } else {
        updated[label] += probability
      }
    })
    observed = updated
  })
  return observed
}

function lastTimeUs(points: PulseExplorerPoint[]): number {
  return points.at(-1)?.timeUs ?? 0
}

/* カーソル時刻にいちばん近い軌跡の点。無ければ -1。 */
export function nearestPulsePointIndex(
  points: PulseExplorerPoint[],
  timeUs: number,
): number {
  if (points.length === 0) {
    return -1
  }
  let bestIndex = 0
  let bestDistance = Number.POSITIVE_INFINITY
  points.forEach((point, index) => {
    const distance = Math.abs(point.timeUs - timeUs)
    if (distance < bestDistance) {
      bestDistance = distance
      bestIndex = index
    }
  })
  return bestIndex
}

export type PulseStateChangePoint = {
  timeUs: number
  value: number
}

/*
 * 隣り合う軌跡の点のあいだで、密度行列がどれだけ動いたか。
 * Gate-aware の「区間状態変化」と同じ Hilbert-Schmidt 距離を使い、
 * 密度行列の最大距離 sqrt(2) で割って 0..1 に収める。
 */
export function pulseStateChangeSeries(
  points: PulseExplorerPoint[],
): PulseStateChangePoint[] {
  const series: PulseStateChangePoint[] = []
  let previous: PulseComplexValue[][] | null = null
  for (const point of points) {
    const matrix = point.densityMatrix
    if (matrix === null) {
      return []
    }
    const distance = previous === null ? 0 : hilbertSchmidtDistance(previous, matrix)
    if (distance === null) {
      return []
    }
    series.push({ timeUs: point.timeUs, value: distance })
    previous = matrix
  }
  return series
}

function hilbertSchmidtDistance(
  left: PulseComplexValue[][],
  right: PulseComplexValue[][],
): number | null {
  if (left.length === 0 || left.length !== right.length) {
    return null
  }
  let squaredNorm = 0
  for (let row = 0; row < left.length; row += 1) {
    if (left[row]?.length !== right[row]?.length) {
      return null
    }
    for (let column = 0; column < left[row].length; column += 1) {
      const realDifference = left[row][column].real - right[row][column].real
      const imaginaryDifference = left[row][column].imag - right[row][column].imag
      if (!Number.isFinite(realDifference) || !Number.isFinite(imaginaryDifference)) {
        return null
      }
      squaredNorm += realDifference ** 2 + imaginaryDifference ** 2
    }
  }
  return Math.max(0, Math.min(1, Math.sqrt(squaredNorm / 2)))
}

/*
 * 実行条件の指紋。Gate-aware の状態エクスプローラーが回路の署名で
 * 結果の古さを判定するのと同じ役目を、Pulse では実行フォームと
 * レーン構成とハードウェア制約が担う。
 * ブロックのidは実行結果に影響しないので、署名から外す。
 */
export function pulseSetupSignature(
  form: PulseLabForm,
  circuit: PulseCircuitState,
): string {
  return JSON.stringify({
    form,
    executionConstraints: circuit.executionConstraints,
    transmons: circuit.transmons.map((transmon) => ({
      index: transmon.index,
      frequencyGhz: transmon.frequencyGhz,
      anharmonicityMhz: transmon.anharmonicityMhz,
    })),
    lanes: circuit.lanes.map((lane) => ({
      transmonIndex: lane.transmonIndex,
      steps: lane.steps.map((step) => (
        step.operation === 'drive'
          ? { operation: step.operation, primitive: step.primitive, pulse: step.pulse }
          : { operation: step.operation, angleRad: step.angleRad }
      )),
    })),
  })
}

/*
 * 「保持した実行と重ねてよいか」を決める指紋。
 *
 * 重ねて意味があるのは、駆動と観測条件が同じまま環境だけを変えた場合に限る。
 * そのとき2本の差はそのまま環境の寄与になる。パルスの形や観測時間まで
 * 変わってしまうと、差がどこから来たのか分けられず、重ねた図は誤読を生む。
 *
 * なのでここには環境・散逸まわり（environmentMode, deviceQuality, 温度,
 * T1/Tphi, 各 gamma, 準静的ノイズ）を入れない。それ以外は全部入れる。
 */
export function pulseComparabilitySignature(
  form: PulseLabForm,
  circuit: PulseCircuitState,
): string {
  return JSON.stringify({
    /* 状態空間の形。ここが変わると基底そのものが対応しない。 */
    modelId: form.modelId,
    localLevels: form.localLevels,
    /* 駆動の形。差を環境のせいにするには、パルスが同じでなければならない。 */
    shape: form.shape,
    amplitudeMode: form.amplitudeMode,
    targetRotationAngleRad: form.targetRotationAngleRad,
    peakAmplitudeRadPerUs: form.peakAmplitudeRadPerUs,
    pulseDurationUs: form.pulseDurationUs,
    sigmaUs: form.sigmaUs,
    truncationSigma: form.truncationSigma,
    phaseRad: form.phaseRad,
    detuningRadPerUs: form.detuningRadPerUs,
    dragBetaUs: form.dragBetaUs,
    anharmonicityMhz: form.anharmonicityMhz,
    networkDetuningQ0RadPerUs: form.networkDetuningQ0RadPerUs,
    networkDetuningQ1RadPerUs: form.networkDetuningQ1RadPerUs,
    networkExchangeCouplingRadPerUs: form.networkExchangeCouplingRadPerUs,
    /* 観測条件。時間軸が違うと同じ物理時刻で並べられない。 */
    totalSimulationTimeUs: form.totalSimulationTimeUs,
    snapshotCount: form.snapshotCount,
    /* 回路。台数・操作列が変われば軌跡の意味が変わる。 */
    transmonCount: circuit.transmons.length,
    lanes: circuit.lanes.map((lane) => ({
      transmonIndex: lane.transmonIndex,
      steps: lane.steps.map((step) => (
        step.operation === 'drive'
          ? { operation: step.operation, primitive: step.primitive, pulse: step.pulse }
          : { operation: step.operation, angleRad: step.angleRad }
      )),
    })),
  })
}

export function formatPulseProbability(value: number): string {
  return `${(Math.max(0, Math.min(1, value)) * 100).toFixed(2)}%`
}
