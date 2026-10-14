export const TWO_LEVEL_PULSE_MODEL = 'driven_two_level_rwa_experimental_v1' as const
export const QUTRIT_PULSE_MODEL = 'driven_transmon_qutrit_rwa_experimental_v1' as const
export const COUPLED_TRANSMON_NETWORK_PULSE_MODEL = 'driven_coupled_transmon_network_rwa_experimental_v1' as const

export type PulseModelId =
  | typeof TWO_LEVEL_PULSE_MODEL
  | typeof QUTRIT_PULSE_MODEL
  | typeof COUPLED_TRANSMON_NETWORK_PULSE_MODEL
export type PulseShape = 'square' | 'gaussian'
export type PulseAmplitudeMode = 'target_rotation_angle' | 'peak_amplitude'
export type PulseEnvironmentMode = 'physical' | 'direct_rates'
export type PulseEvolutionMethod = 'fixed_step_rk4' | 'explicit_cptp'
export type QuasiStaticQuadratureOrder = 3 | 5 | 7 | 9
export type PulseBackend = 'python' | 'rust' | 'auto'

export type PulseLocalLevels = 2 | 3

export type PulseLabForm = {
  modelId: PulseModelId
  /*
   * 準位数トグル。1台なら 2→2準位モデル / 3→qutrit、2台以上なら
   * ネットワークモデルへ local_levels として渡る。modelId はこの値と
   * 回路の台数から導出する。
   */
  localLevels: PulseLocalLevels
  evolutionMethod: PulseEvolutionMethod
  backend: PulseBackend
  shape: PulseShape
  amplitudeMode: PulseAmplitudeMode
  targetRotationAngleRad: number
  peakAmplitudeRadPerUs: number
  pulseDurationUs: number
  sigmaUs: number
  truncationSigma: number
  totalSimulationTimeUs: number
  phaseRad: number
  detuningRadPerUs: number
  anharmonicityMhz: number
  /*
   * ネットワークモデル（台数2以上）の構成。q2以降の基準離調は現在0固定で、
   * 交換結合は隣接ペアすべてに同じ値を適用する。
   */
  networkDetuningQ0RadPerUs: number
  networkDetuningQ1RadPerUs: number
  networkExchangeCouplingRadPerUs: number
  dragBetaUs: number
  environmentMode: PulseEnvironmentMode
  deviceQuality: number
  temperatureMk: number
  fluxNoisePhi0: number
  qubitFrequencyGhz: number
  t1MaxUs: number
  tphiMaxUs: number
  gammaDownPerUs: number
  gammaUpPerUs: number
  gammaPhiPerUs: number
  gamma10DownPerUs: number
  gamma01UpPerUs: number
  gamma21DownPerUs: number
  gamma12UpPerUs: number
  gammaPhiAdjacentPerUs: number
  quasiStaticNoiseEnabled: boolean
  quasiStaticDetuningSigmaRadPerUs: number
  quasiStaticQuadratureOrder: QuasiStaticQuadratureOrder
  /*
   * ネットワークモデルの準静的離調ノイズ。全トランズモンに共通の σ を与え、
   * 隣接ペアに共通の相関係数 r を掛ける（バックエンドの chain モデルに対応）。
   * σ=0 のときはアンサンブル平均を行わない。
   */
  networkQuasiStaticSigmaRadPerUs: number
  networkQuasiStaticAdjacentCorrelation: number
  networkQuasiStaticQuadratureOrder: 3 | 5
  networkPerQubitCalibrationEnabled: boolean
  networkT1MaxUs: number[]
  networkTphiMaxUs: number[]
  networkReadoutP10: number[]
  networkReadoutP01: number[]
  readoutErrorEnabled: boolean
  readoutP10: number
  readoutP01: number
  snapshotCount: number
}

export type PulseLabField = keyof PulseLabForm
export type PulseLabErrors = Partial<Record<PulseLabField, string>>

export type PulseComplexValue = {
  real: number
  imag: number
}

export type PulsePhysicality = {
  trace_error: number
  hermiticity_error: number
  minimum_eigenvalue: number
}

export type PulseEvolutionDiagnostics = {
  internal_step_count: number
  rhs_evaluation_count: number
  hamiltonian_evaluation_count: number
  minimum_internal_step_us: number
  maximum_internal_step_us: number
  raw_trace_error: number
  raw_hermiticity_error: number
  raw_minimum_eigenvalue: number
  cleanup_correction_norm: number
  actual_duration_us: number
}

export type PulseReadout = {
  enabled: boolean
  model_id: 'independent_affine_binary_readout_v1'
  assignment_errors: Array<{ p10: number; p01: number }>
  true_probabilities: Record<string, number>
  observed_probabilities: Record<string, number>
  preserves_state_metrics: true
  noncomputational_level_policy: 'preserve'
}

export type TwoLevelPulsePoint = {
  time_us: number
  segment: 'pulse' | 'idle'
  open_population_0: number
  open_population_1: number
  closed_population_0: number
  closed_population_1: number
  fidelity_to_closed: number
  purity: number
  raw_physicality: PulsePhysicality
  cleaned_physicality: PulsePhysicality
  cleanup_correction_norm: number
}

export type TwoLevelPulseState = TwoLevelPulsePoint & {
  open_density_matrix: PulseComplexValue[][]
  closed_density_matrix: PulseComplexValue[][]
}

export type TwoLevelPulseResponse = {
  contract_version: 'pulse-baseline-a-v1'
  model: {
    model_id: typeof TWO_LEVEL_PULSE_MODEL
    description: string
    frame: 'rotating'
    approximation: 'RWA'
    logical_qubits: 1
    state_levels: 2
    experimental: true
    hardware_calibrated: false
    internal_units: PulseUnits
  }
  input: Record<string, string | number | null>
  rates: Record<string, string | number | null>
  step_policy: Record<string, string | number | boolean | null>
  sample_times_us: number[]
  trajectory: TwoLevelPulsePoint[]
  pulse_end: TwoLevelPulseState
  final: TwoLevelPulseState
  readout: PulseReadout
  diagnostics: PulseDiagnostics
  warnings: string[]
  limitations: string[]
}

export type QutritPulsePoint = {
  time_us: number
  segment: 'pulse' | 'idle' | 'virtual_z'
  population_0: number
  population_1: number
  population_2: number
  computational_population: number
  leakage_probability: number
  population_sum_error: number
  purity: number
  density_matrix: PulseComplexValue[][]
  raw_physicality: PulsePhysicality
  cleaned_physicality: PulsePhysicality
  cleanup_correction_norm: number
  sequence_step_index?: number
  sequence_step_label?: string
}

export type QutritPulseResponse = {
  contract_version: 'pulse-extension-b-v1'
  model: {
    model_id: typeof QUTRIT_PULSE_MODEL
    description: string
    frame: 'rotating'
    approximation: 'RWA'
    logical_qubits: 1
    state_levels: 3
    basis_order: ['0', '1', '2']
    subsystem_dimensions: [3]
    experimental: true
    hardware_calibrated: false
    internal_units: PulseUnits
  }
  input: Record<string, string | number | null>
  rates: Record<string, string | number | null>
  step_policy: Record<string, string | number | boolean | null>
  sample_times_us: number[]
  trajectory: QutritPulsePoint[]
  leakage: {
    maximum_recorded_leakage_probability: number
    leakage_at_pulse_end: number
    leakage_at_final_time: number
  }
  pulse_end: QutritPulsePoint
  final: QutritPulsePoint
  readout: PulseReadout
  diagnostics: PulseDiagnostics
  warnings: string[]
  limitations: string[]
}

export type TransmonNetworkPoint = {
  time_us: number
  segment: 'pulse' | 'idle'
  joint_populations: Record<string, number>
  computational_population: number
  leakage_probability: number
  population_sum_error: number
  purity: number
  density_matrix: PulseComplexValue[][]
  raw_physicality: PulsePhysicality
  cleaned_physicality: PulsePhysicality
  cleanup_correction_norm: number
}

export type CoupledTransmonNetworkResponse = {
  contract_version: 'pulse-transmon-network-v1'
  model: Record<string, unknown> & {
    model_id: typeof COUPLED_TRANSMON_NETWORK_PULSE_MODEL
    description: string
    logical_qubits: number
    hilbert_dimension: number
    basis_order: string[]
  }
  input: Record<string, unknown>
  rates: Array<Record<string, unknown>>
  step_policy: Record<string, unknown>
  sample_times_us: number[]
  trajectory: TransmonNetworkPoint[]
  leakage: {
    maximum_recorded_leakage_probability: number
    leakage_at_pulse_end: number
    leakage_at_final_time: number
  }
  pulse_end: TransmonNetworkPoint
  final: TransmonNetworkPoint
  readout: PulseReadout
  diagnostics: Record<string, unknown> & PulseDiagnostics
  warnings: string[]
  limitations: string[]
}

type PulseUnits = {
  time: 'us'
  angular_frequency: 'rad/us'
  rate: '1/us'
}

type PulseDiagnostics = {
  api_runtime_ms: number
  evolution: {
    requested: PulseEvolutionMethod
    resolved: PulseEvolutionMethod
    method_id:
      | 'fixed_step_rk4_v1'
      | 'explicit_cptp_midpoint_gksl_v1'
    cptp_guaranteed_by_construction: boolean
    cleanup_applied: boolean
    open_pulse_audit: Record<string, unknown> | null
    open_idle_audit: Record<string, unknown> | null
    closed_pulse_audit: Record<string, unknown> | null
    closed_idle_audit: Record<string, unknown> | null
  }
  open_pulse: PulseEvolutionDiagnostics
  open_idle: PulseEvolutionDiagnostics | null
  maximum_cleaned_trace_error: number
  maximum_cleaned_hermiticity_error: number
  minimum_cleaned_eigenvalue: number
} & Record<string, unknown>

export type PulseResponse =
  | TwoLevelPulseResponse
  | QutritPulseResponse
  | CoupledTransmonNetworkResponse

/*
 * 実行済みPulseの記録。Pulseラボと状態エクスプローラーが同じ結果を見るため、
 * 応答だけでなく、そのとき使われた設定と実行条件の指紋も一緒に持ち歩く。
 */
export type PulseRunRecord = {
  response: PulseResponse
  formAtRun: PulseLabForm
  completedAt: string
  signature: string
  /*
   * この結果がどこから来たか。'computed' はこのアプリで実行したもの、
   * 'imported' は結果ファイルから復元したもの。
   *
   * 省略時は 'computed' として扱う（既存の実行経路を変えないため）。
   * 画面はこの値を見て「読み込んだ結果」であることを明示する。計算し直した
   * ものと見分けが付かないまま図が出ていると、その数字がいまの設定で
   * 得られたものだと誤解されるため。
   */
  origin?: 'computed' | 'imported'
}

export type PulseWaveformPoint = {
  timeUs: number
  omegaX: number
  omegaY: number
}

export type PulseCostEstimate = {
  estimatedInternalSteps: number
  maximumInternalSteps: number
  overBudget: boolean
  level: 'normal' | 'elevated' | 'blocked'
  message: string
}

export function isQutritPulseResponse(
  response: PulseResponse,
): response is QutritPulseResponse {
  return response.model.model_id === QUTRIT_PULSE_MODEL
}

export function isCoupledTransmonNetworkResponse(
  response: PulseResponse,
): response is CoupledTransmonNetworkResponse {
  return response.model.model_id === COUPLED_TRANSMON_NETWORK_PULSE_MODEL
}
