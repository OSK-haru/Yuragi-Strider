/*
 * Gate-awareラボの設定と、その初期値。
 *
 * ページ側で useState して持つと、状態エクスプローラーへ寄り道して戻った
 * だけで画面が作り直され、指定した値が初期値へ戻ってしまう。そのため
 * App が持ち、ここには「何を覚えておくか」の形だけを置く。
 */

import type {
  GateAwareEvolutionMethod,
  GateCompilationMode,
  PerQubitCalibrationSettings,
  ReadoutErrorSettings,
  SimulateRequestParameters,
  SimulationBackend,
  SimulationResponse,
  SnapshotOptions,
} from '../types/simulation'
import type { CircuitConfig } from './circuitConfig'
import { initialToolboxSettings, type ToolboxSettings } from './toolbox'

/*
 * 状態エクスプローラーで見比べるために取っておいた実行と、比較表示の on/off。
 *
 * ページではなく App が持つ。ページ側で useState すると、シミュレーションへ
 * 戻って環境を変えようとしただけで保持が捨てられ、
 * 「保存したのに比べられない」ことになる。
 */
export type GateAwareComparisonState = {
  heldResult: {
    response: SimulationResponse
    circuitConfig: CircuitConfig
    heldAt: string
    /* 保持した時点の「環境以外」の指紋。 */
    comparability: string
  } | null
  comparing: boolean
}

export const initialGateAwareComparisonState: GateAwareComparisonState = {
  heldResult: null,
  comparing: false,
}

export type SimulateSettings = {
  parameters: SimulateRequestParameters
  evolutionMethod: GateAwareEvolutionMethod
  compilationMode: GateCompilationMode
  simulationBackend: SimulationBackend
  snapshotOptions: SnapshotOptions
  readoutError: ReadoutErrorSettings
  perQubitCalibration: PerQubitCalibrationSettings
  customSnapshotTimesInput: string
  toolbox: ToolboxSettings
}

export const initialSimulationParameters: SimulateRequestParameters = {
  device_quality: 0.8,
  temperature_mk: 15.0,
  flux_noise_phi0: 0.000001,
  qubit_frequency_ghz: 5.0,
  t1_max_us: 100.0,
  tphi_max_us: 100.0,
  duration_us: 2.0,
  time_steps: 101,
  fidelity_threshold: 0.9,
}

export const initialSnapshotOptions: SnapshotOptions = {
  enabled: true,
  uniform_count: 10,
  custom_times_us: [],
  include_initial: true,
  include_final: true,
  include_column_boundaries: true,
  include_after_circuit: true,
}

export const initialReadoutError: ReadoutErrorSettings = {
  enabled: false,
  p10: 0.005,
  p01: 0.013,
}

export const initialPerQubitCalibration: PerQubitCalibrationSettings = {
  enabled: false,
  values: [],
}

export const initialSimulateSettings: SimulateSettings = {
  parameters: initialSimulationParameters,
  evolutionMethod: 'fixed_step_rk4',
  compilationMode: 'logical_direct',
  simulationBackend: 'rust_dense_preview',
  snapshotOptions: initialSnapshotOptions,
  readoutError: initialReadoutError,
  perQubitCalibration: initialPerQubitCalibration,
  customSnapshotTimesInput: '',
  toolbox: initialToolboxSettings,
}
