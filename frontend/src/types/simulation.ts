import type { CircuitPreviewData } from './circuit'

export type OutputProbabilities = Record<string, number>

export type SimulationLoadStatus = 'fixture' | 'loading' | 'api' | 'error'
export type GateAwareEvolutionMethod = 'fixed_step_rk4' | 'explicit_cptp'
export type GateCompilationMode = 'logical_direct' | 'auto_decompose'
export type SimulationBackend = 'python_dense' | 'rust_dense_preview' | 'rust_dense_v1'

export type SimulationSummaryData = {
  final_fidelity: number | null
  final_purity: number | null
  completion_fidelity: number | null
  completion_purity: number | null
  effective_time_us: number | null
}

export type MetricPoint = {
  time_us: number
  fidelity: number | null
  purity: number | null
}

export type SerializableComplexMatrix = {
  real: number[][]
  imag: number[][]
}

export type StateSnapshotKind =
  | 'initial'
  | 'uniform_time'
  | 'custom_time'
  | 'column_boundary'
  | 'measurement'
  | 'after_circuit'
  | 'idle_sample'
  | 'final'

export type StateSnapshot = {
  index: number
  requested_time_us?: number | null
  time_us?: number
  progress?: number
  kind?: StateSnapshotKind | string
  capture_method?: string | null
  event_kind?: string | null
  column_index?: number | null
  density_matrix: SerializableComplexMatrix
}

export type SnapshotOptions = {
  enabled: boolean
  uniform_count: number
  custom_times_us: number[]
  include_initial: boolean
  include_final: boolean
  include_column_boundaries: boolean
  include_after_circuit: boolean
}

export type PhysicalTimelineOperation = {
  gate: string
  qubits: number[]
  targets: number[]
  controls: number[]
  declared_duration_us: number
  effective_column_duration_us: number
}

export type PhysicalTimelineEvent = {
  id: string
  kind: 'circuit_column' | 'instantaneous_column' | 'idle' | string
  execution_column_index: number | null
  circuit_step: number | null
  source_circuit_columns: number[]
  start_us: number
  duration_us: number
  end_us: number
  operations: PhysicalTimelineOperation[]
}

export type PhysicalTimeline = {
  schema_version: string
  time_unit: 'us' | string
  column_timing_model: string
  total_duration_us: number
  circuit_completion_time_us: number
  sampled_times_us: number[]
  events: PhysicalTimelineEvent[]
}

export type CircuitProbe = {
  id: string
  circuit_position: {
    column_index: number | null
    boundary: 'before' | 'after' | 'completion' | 'final' | string
  }
  noisy_snapshot_index: number
  ideal_snapshot_index?: number | null
  time_us: number
}

export type StateTransferCheckpoint = {
  role: 'message' | 'receive' | string
  column_index: number
  qubit: number | null
  noisy_snapshot_index: number | null
  ideal_snapshot_index: number | null
  time_us: number | null
  available: boolean
}

export type StateTransferMetadata = {
  schema_version: string
  available: boolean
  reason: string | null
  message: { column_index: number; gate_index: number; qubit: number; operation: 'MESSAGE' | string } | null
  receive: { annotation_id: string | null; source_id: string | null; column_index: number; qubit: number | null } | null
  checkpoints: StateTransferCheckpoint[]
  metrics?: {
    noisy_message_to_receive_frobenius: number | null
    ideal_message_to_receive_frobenius: number | null
  }
}

export type MeasurementOptions = {
  shots: number
  seed: number
}

export type ReadoutErrorSettings = {
  enabled: boolean
  p10: number
  p01: number
}

export type QubitCalibration = {
  t1_max_us: number
  tphi_max_us: number
  p10: number
  p01: number
}

export type PerQubitCalibrationSettings = {
  enabled: boolean
  values: QubitCalibration[]
}

export type ReadoutErrorSettingsErrors = Partial<Record<'p10' | 'p01' | 'span', string>>

export type MeasurementResult = {
  mode: string
  shots: number
  seed: number
  counts: Record<string, number>
  frequencies: OutputProbabilities
  explicit_measurement_mode: string
  explicit_measurement_count: number
  explicit_measurement_targets: number[]
  explicit_measurement_bindings: Array<{ qubit: number; classical_bit: number }>
  classical_register_bits: number
  classical_register_mode: string
  classical_conditioning_supported: boolean
  classical_branch_count: number
  classical_branching_noise_applied: boolean
  classical_branches: Array<{
    probability: number
    classical_bits: number[]
    measurements: Array<Record<string, unknown>>
  }>
  classical_shot_preview: Array<{
    shot_index: number
    branch_index: number
    classical_bits: number[]
    measurements: Array<Record<string, unknown>>
  }>
  branch_probability_sum?: number | null
  branch_probability_normalized?: boolean | null
  branch_state_representation?: string
  conditional_operations?: Array<{
    gate: string
    targets: number[]
    conditions: Array<{ bit: number; value: number }>
    column_index: number
  }>
}

export type SimulationDiagnosticsValue =
  | string
  | number
  | boolean
  | null
  | undefined

export type SimulationDiagnostics = {
  simulation_model: string
  evolution_mode: string
  simulation_backend: string
  backend_name: string
  rust_kernel_mode: string
  rust_kernel_call_count: number
  rust_kernel_sampled_batch_count: number
  backend_fallback_used: boolean
  rust_kernel_fallback_used: boolean
} & Record<string, SimulationDiagnosticsValue>

export type SimulationParameters = {
  environment_model: string
  input_mode: string
  temperature_k: number | null
  temperature_mk: number | null
  normalized_temperature: number | null
  qubit_frequency_ghz: number | null
  device_quality: number | null
  flux_noise_phi0: number | null
  duration_us: number
  time_steps: number
  fidelity_threshold: number
  simulation_backend: string
}

export type SimulationRates = {
  gamma0_per_us: number | null
  gamma_down_per_us: number | null
  gamma_up_per_us: number | null
  gamma_population_relaxation_per_us: number | null
  gamma_phi_per_us: number | null
  t1_base_us: number | null
  t1_effective_us: number | null
  tphi_base_us: number | null
  t2_effective_us: number | null
  gamma1_per_us: number | null
  gamma1_per_us_deprecation: string
  per_qubit?: Array<Record<string, string | number | boolean | null>>
  per_qubit_environment_enabled?: boolean
}

export type PhysicalSimulationParameters = {
  device_quality: number
  temperature_mk: number
  flux_noise_phi0: number
  qubit_frequency_ghz: number
  t1_max_us: number
  tphi_max_us: number
  duration_us: number
  time_steps: number
  fidelity_threshold: number
}

export type SimulateRequestParameters = PhysicalSimulationParameters

export type SimulateRequestParameterErrors = Partial<
  Record<keyof SimulateRequestParameters, string>
>

export type GateDurationDefaults = {
  H: number
  X: number
  Y: number
  Z: number
  S: number
  T: number
  RX: number
  RY: number
  RZ: number
  CNOT: number
  CZ: number
  SWAP: number
  CP: number
  CCX: number
  /** QFT spans a variable register, so this is the duration per spanned qubit. */
  QFT: number
  /** ORACLE spans a variable register too; this is per spanned qubit. */
  ORACLE: number
  MEASURE: number
  MESSAGE: number
  RECEIVED: number
}

export type GateDurationDefaultErrors = Partial<
  Record<keyof GateDurationDefaults, string>
>

export type CompiledCircuitGate = {
  type: string
  targets: number[]
  controls?: number[]
  params?: Record<string, number>
}

export type CompiledCircuitColumn = {
  step: number
  gates: CompiledCircuitGate[]
}

/** `CircuitConfig.to_dict()` from the backend, narrowed to what the UI draws. */
export type CompiledCircuitData = {
  logical_qubits?: number
  initial_states?: string[]
  columns?: CompiledCircuitColumn[]
}

export type CompiledSourceMapEntry = {
  logical_column: number
  source_gate: string
  rule_id: string | null
  compiled_operations: Array<{
    compiled_column: number
    gate: string
    targets: number[]
    controls: number[]
    params: Record<string, number>
  }>
}

/**
 * The gate compiler's report. `/api/simulate` returns it for the run that just
 * finished; `/api/circuit/compile` returns the same shape for the circuit being
 * edited, before any run.
 */
export type CircuitCompilation = {
  mode: GateCompilationMode
  native_gate_set_id: string
  logical_gate_count: number
  compiled_gate_count: number
  logical_depth: number
  compiled_depth: number
  logical_duration_us: number | null
  compiled_duration_us: number | null
  decomposition_rules_used: string[]
  source_map: CompiledSourceMapEntry[]
  compiled_circuit: CompiledCircuitData
}

export type RunPanelData = {
  status: string
  selected_backend: string
  last_run_label: string
  can_run?: boolean
  compilation?: CircuitCompilation
  comparison?: {
    ideal_timeline: MetricPoint[]
    ideal_state_snapshots: StateSnapshot[]
  }
}

export type SimulationIssue = {
  level: string
  code: string
  message: string
  detail: string | null
  suggestion: string | null
}

export type SimulationResponse = {
  circuit: CircuitPreviewData
  parameters: SimulationParameters
  rates: SimulationRates
  diagnostics: SimulationDiagnostics
  summary: SimulationSummaryData
  timeline: MetricPoint[]
  physical_timeline?: PhysicalTimeline
  circuit_probes?: CircuitProbe[]
  state_transfer?: StateTransferMetadata
  output_probabilities: OutputProbabilities
  measurement: MeasurementResult
  state_snapshots: StateSnapshot[]
  run: RunPanelData
  /* 実行前にツール箱で選んだ状態指標。選ばなければ無い。 */
  toolbox?: ToolboxResponse
  warnings: string[]
  issues: SimulationIssue[]
}

/*
 * ツール箱の状態指標。各 values は state_snapshots と同じ並びで1つずつ。
 * unavailable は要求したが計算できなかった項目と、その理由コード。
 */
export type ToolboxResponse = {
  schema_version: 'toolbox-v1'
  times_us: number[]
  unavailable: Partial<Record<ToolboxSnapshotMetricKey, 'no_snapshots' | 'no_ideal_snapshots'>>
  entropy?: { qubits: number[]; values: number[] }
  concurrence?: { qubits: number[]; values: number[] }
  negativity?: { subsystem: number[]; values: number[] }
  mutual_information?: { a: number[]; b: number[]; values: number[] }
  trace_distance?: { values: Array<number | null> }
  observables?: Array<{ pauli: string; values: number[] }>
  elapsed_ms?: number
}

export type ToolboxSnapshotMetricKey =
  | 'entropy'
  | 'concurrence'
  | 'negativity'
  | 'mutual_information'
  | 'trace_distance'
  | 'observables'

export type ProcessCharacterizationResponse = {
  schema_version: 'process-characterization-v1'
  method: 'exact_tomography' | 'monte_carlo'
  average_gate_fidelity: number
  standard_error: number
  confidence_interval_95: [number, number] | null
  simulation_count: number
  monte_carlo_samples?: number
  monte_carlo_seed?: number
  qubit_count: number
  circuit_duration_us: number
  noise_free_environment: boolean
  elapsed_ms: number
  process_matrix?: {
    basis: string[]
    noisy: SerializableComplexMatrix
    ideal: SerializableComplexMatrix
    trace: number
    minimum_eigenvalue: number
  }
}

export type SweepPoint = {
  delay_us: number
  true_p1: number
  observed_p1: number
  fitted_observed_p1: number
  residual: number
}

export type SweepReadout = {
  enabled: boolean
  p10: number
  p01: number
}

export type T1SweepResponse = {
  schema_version: 't1-sweep-v1'
  model: 'single_exponential_with_observation_floor'
  points: SweepPoint[]
  fit: {
    amplitude: number
    observation_floor: number
    t1_us: number
    root_mean_square_error: number
    maximum_absolute_error: number
    r_squared: number | null
  }
  reference: {
    t1_effective_us: number | null
    gamma_population_relaxation_per_us: number | null
    temperature_mk: number
  }
  readout: SweepReadout
}

export type RamseyFit = {
  model: 'exponential_envelope' | 'gaussian_envelope'
  envelope_power: 1 | 2
  amplitude: number
  observation_floor: number
  t2_star_us: number
  frequency_mhz: number
  phase_rad: number
  root_mean_square_error: number
  maximum_absolute_error: number
  r_squared: number | null
  aicc: number
  delta_aicc: number
}

export type RamseySweepResponse = {
  schema_version: 'ramsey-sweep-v1'
  selected_model: RamseyFit['model']
  points: SweepPoint[]
  fit: RamseyFit
  candidate_ranking_by_aicc: RamseyFit[]
  reference: {
    t2_effective_us: number | null
    detuning_mhz: number
    temperature_mk: number
  }
  readout: SweepReadout
}

export type MockSimulationResult = SimulationSummaryData & {
  output_probabilities: OutputProbabilities
}
