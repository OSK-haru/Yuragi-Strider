/** UI port for the D60–D64 attack environment flow.
 *
 * The UI layer builds screens from these types only. The transport client
 * (HTTP catalog/create/discard and the WebSocket session) implements the two
 * interfaces at the bottom. Secrets never cross this boundary: the owner cookie
 * is HttpOnly, and the attacker's virtual credential returned at creation stays
 * inside the client implementation. The victim's credential never leaves the
 * server. Field names of the catalog mirror the server JSON (D64).
 */

export type SettingValue = string | number | boolean

// ---- Settings catalog: GET /api/attack/settings/catalog (attack-settings-catalog-v1)

export type SettingFieldType = 'number' | 'integer' | 'boolean' | 'string' | 'choice'

export type SettingFieldDescriptor = {
  /** Canonical manifest path, used as the key in a create request. */
  path: string
  label: string
  type: SettingFieldType
  /** Why this value: shown next to the field. */
  basis: string
  description: string
  unit?: string
  minimum?: number
  maximum?: number
  choices?: string[]
  default?: SettingValue
  /** Must be explicitly supplied, even when a catalog default is shown. */
  required?: boolean
}

export type IdleNoiseModeId = 'calibration_values' | 'physical_parameters'

export type IdleNoiseMode = {
  id: IdleNoiseModeId
  available: boolean
  label: string
  description: string
  fields: SettingFieldDescriptor[]
  region_selection?: RegionSelectionCatalog
}

export type RegionSelectionCatalog = {
  max_qubits: number
  eligible_qubit_ids: number[]
  active_region_initial: number[]
  alternate_region_initial: number[]
  display_layout_notice: string
  region_notice: string
  /** Descriptor templates use `{qubit_id}`; values come from the snapshot. */
  qubit_noise_fields?: {
    primary: SettingFieldDescriptor[]
    dedicated: SettingFieldDescriptor[]
  }
}

export type StandardValue = { value: number; basis: string }

export type HistoricalSingleQubitInitialValues = {
  source: string
  device: string
  physical_qubit_id: number
  calibrated_at: string
  job_id: string
  /** Must be shown: historical one-qubit values, not current device-wide values. */
  scope_notice: string
  values: Record<string, StandardValue>
}

/** D66 historical snapshot; this coexists with the original D62 catalog. */
export type DeviceSnapshotInitialValues = {
  source: string
  device: string
  snapshot_date: string
  qubit_count: number
  directed_coupling_count: number
  undirected_edge_count: number
  active_region_limit: number
  active_region_initial: number[]
  alternate_region_initial: number[]
  eligibility_policy: {
    readout_error_exclude_above: number
    cz_error_exclude_at_or_above: number
  }
  qubits: Record<string, {
    usable_for_placement: boolean
    placement_status: 'usable' | 'unusable'
    unusable_reasons: string[]
    T1_us: number | null
    T2_us: number | null
    readout_error: number
    'P(1|0)': number
    'P(0|1)': number
    measurement_duration_us: number | null
    reset_duration_us: number | null
    basis: string
  }>
  cz_calibrations: Record<string, { error: number; duration_us: number; basis: string }>
}

export type StandardInitialValues = HistoricalSingleQubitInitialValues | DeviceSnapshotInitialValues

export type ChallengeEntry = {
  id: string
  title: string
  description: string
  /** Unavailable entries are listed for orientation but cannot be created. */
  available: boolean
  /** Only fields the preset lets the user edit. Hidden values never arrive. */
  fields: SettingFieldDescriptor[]
  /** Free-mode settings that reproduce this preset's public parameters (no hidden values). */
  free_equivalent?: {
    idle_noise_mode: IdleNoiseModeId
    settings: Record<string, SettingValue>
    /** Parts that exist only in challenge mode, e.g. budgets and grading. */
    not_reproduced: string[]
  }
  /** Public immutable allocation, when the preset exposes the selected region. */
  region_selection?: {
    locked: true
    placement_policy?: string
    noise_mode?: IdleNoiseModeId
    primary_physical_qubit_ids?: number[]
    dedicated_physical_qubit_ids?: number[]
  }
}

export type EnvironmentMode = 'free' | 'challenge'

export type SettingsCatalog = {
  schema_version: 'attack-settings-catalog-v1'
  modes: Array<{ id: EnvironmentMode; label: string }>
  free_mode: {
    idle_noise_modes: IdleNoiseMode[]
    standard_initial_values: StandardInitialValues
  }
  challenges: ChallengeEntry[]
}

// ---- Environment lifecycle: POST /api/attack/environments (D64 body), discard

export type CreateEnvironmentRequest =
  | { mode: 'free'; idle_noise_mode: IdleNoiseModeId; settings: Record<string, SettingValue>; regions?: RegionSelectionRequest }
  | { mode: 'challenge'; preset_id: string; settings: Record<string, SettingValue> }

export type RegionSelectionRequest =
  | { shared_physical_qubit_ids: number[] }
  | { victim_physical_qubit_ids: number[]; attacker_physical_qubit_ids: number[] }

/** Public handle only; the attacker credential is kept inside the client. */
export type EnvironmentHandle = { environmentId: string; mode: EnvironmentMode }

/** Server-derived display data; never includes a manifest or credential. */
export type OwnedEnvironment = EnvironmentHandle & { createdAt: string }

export type CreateEnvironmentFailure =
  /** Browser limit reached (D63: 429). The UI offers "discard and recreate". */
  | 'browser_limit'
  | 'server_full'
  /** D64 422 INVALID_SETTINGS: no per-field reason by design. */
  | 'invalid_settings'
  /** D64 413 SETTINGS_TOO_LARGE */
  | 'too_large'
  | 'unavailable'

export type CreateEnvironmentResult =
  | { ok: true; environment: EnvironmentHandle }
  | { ok: false; reason: CreateEnvironmentFailure }

// ---- Live session over WebSocket (product host_clock; explicit_wait tests)

export type ProgramSubmission = {
  deviceId: string
  shots: number
  /** Circuit Studio editor JSON, sent as-is; parsed at declared arrival. */
  circuitConfig: unknown
  initialization?: 'prepare' | 'inherit'
  /** Interleaved observation run: a victim run precedes every shot. */
  interleave?: InterleaveReference
}

export type InterleaveReference = 'victim' | 'training:0' | 'training:1'

export type LiveRequestKind = 'submit' | 'cancel' | 'status' | 'result'

/** An outstanding attacker request, as the UI lists it. */
export type LiveRequest = {
  /** Opaque handle; not a host time or receipt sequence. */
  requestId: string
  kind: LiveRequestKind
  jobId?: string
  /** World time when the request was issued (the attacker's own clock). */
  issuedAtWorldUs: number
}

export type LiveReply = {
  requestId: string
  /** Public projection only (job status / counts). */
  value: unknown
  errorCode: string | null
}

export type WaitTarget =
  | { kind: 'duration'; durationUs: number }
  | { kind: 'request'; requestId: string }
  | { kind: 'any' }

export type WaitResult = {
  outcome: 'reached' | 'limited' | 'nothing_pending'
  worldTimeUs: number
}

export type ConnectionState = 'connecting' | 'open' | 'closed' | 'failed'

export interface LiveAttackConnection {
  readonly state: ConnectionState
  readonly clockMode: 'explicit_wait' | 'host_clock'
  readonly pauseWhileCatchingUp: boolean
  /** Qubits a program may use, from the attacker's device information; null if not published. */
  readonly maxProgramQubits: number | null
  /** Attacker-visible world time in the frozen scenario clock domain. */
  readonly worldTimeUs: number
  /** Operations never advance world time. Each resolves once the server accepted it. */
  submit(program: ProgramSubmission): Promise<LiveRequest>
  cancel(jobId: string): Promise<LiveRequest>
  status(jobId: string): Promise<LiveRequest>
  result(jobId: string): Promise<LiveRequest>
  /** Rebuild the server-retained, undelivered requests for this principal. */
  pendingRequests(): Promise<LiveRequest[]>
  /** Available only for internal explicit_wait scenarios; product UI hides it. */
  wait(target: WaitTarget): Promise<WaitResult>
  onClockChange(listener: (worldTimeUs: number) => void): () => void
  onReply(listener: (reply: LiveReply) => void): () => void
  onStateChange(listener: (state: ConnectionState) => void): () => void
  close(): void
}

/** A decision rule submitted for hidden-case grading (challenge mode only). */
export type SolutionEstimator = {
  /** Fraction of 1 outcomes in a run, 0..1. */
  threshold: number
  /** ge: predict 1 when the fraction is at or above the threshold. le: predict 1 when it is below. */
  direction: 'ge' | 'le'
}

/** The whole message, for a challenge graded by exact match. */
export type SolutionMessage = { message: string }

export type SolutionSubmission = SolutionEstimator | SolutionMessage

export type SolutionResult = {
  attempt: number
  attemptsRemaining: number
  /** Hidden-case aggregates; absent for exact-match grading, which gives no partial hint. */
  correct?: number
  total?: number
  required?: number
  passed: boolean
}

export type SolutionFailure = 'invalid' | 'exhausted' | 'unavailable'

export interface AttackEnvironmentClient {
  /** Grade a rule on hidden cases. Only aggregates come back. */
  submitSolution(environmentId: string, submission: SolutionSubmission, signal: AbortSignal):
    Promise<{ ok: true; result: SolutionResult } | { ok: false; reason: SolutionFailure }>
  getCatalog(signal: AbortSignal): Promise<SettingsCatalog>
  listOwnedEnvironments(signal: AbortSignal): Promise<OwnedEnvironment[]>
  /** Fetches a credential into the client closure; false means absent or expired. */
  resumeEnvironment(environment: EnvironmentHandle, signal: AbortSignal): Promise<boolean>
  createEnvironment(request: CreateEnvironmentRequest, signal: AbortSignal): Promise<CreateEnvironmentResult>
  discardEnvironment(environmentId: string, signal: AbortSignal): Promise<void>
  connect(environment: EnvironmentHandle, signal: AbortSignal): Promise<LiveAttackConnection>
}
