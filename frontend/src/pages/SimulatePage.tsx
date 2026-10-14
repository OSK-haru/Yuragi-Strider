import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import './SimulatePage.css'
import '../components/IdeLayout.css'
import { IdeViewToggles, IdeWorkbench } from '../components/IdeWorkbench'
import { useIdeLayout } from '../hooks/useIdeLayout'
import { DensityMatrixSummaryCard } from '../components/DensityMatrixSummaryCard'
import { DiagnosticsCard, type SimulationDiagnostics } from '../components/DiagnosticsCard'
import { ParameterPanel } from '../components/ParameterPanel'
import { RunPanel } from '../components/RunPanel'
import { SimulationCompletionPopup } from '../components/SimulationCompletionPopup'
import { ToolboxPanel } from '../components/ToolboxPanel'
import { uiResponseExample } from '../mock/uiResponseExample'
import { algorithmPresets } from '../utils/algorithmPresets'
import { apiUrl } from '../utils/apiBase'
import type { SimulateSettings } from '../utils/simulateSettings'
import { circuitEditorStateToConfig, type CircuitConfig } from '../utils/circuitConfig'
import { validateCircuitConfigForRun } from '../utils/circuitValidation'
import {
  checkToolbox,
  resolvedSweepDelays,
  toolboxErrorMessage,
  toolboxJobRequests,
  toolboxRequestBlock,
  type ToolboxJobRequest,
  type ToolboxSettings,
} from '../utils/toolbox'
import { estimateSimulationCost } from '../utils/simulationCost'
import { useCircuitContext } from '../context/useCircuitContext'
import { useTutorial } from '../context/useTutorial'
import { hasExtendedDuration, hasShortT1 } from '../utils/tutorialProgress'
import { useInternalInfoVisible } from '../context/useAdminMode'
import type {
  CircuitCompilation,
  GateDurationDefaultErrors,
  GateDurationDefaults,
  GateAwareEvolutionMethod,
  GateCompilationMode,
  MeasurementOptions,
  PerQubitCalibrationSettings,
  ReadoutErrorSettings,
  ReadoutErrorSettingsErrors,
  SimulationBackend,
  SimulateRequestParameterErrors,
  SimulateRequestParameters,
  SnapshotOptions,
  SimulationLoadStatus,
  SimulationResponse,
  SimulationSummaryData,
} from '../types/simulation'

type SimulatePageProps = {
  diagnostics: SimulationDiagnostics
  result: SimulationSummaryData
  gateDurationDefaults: GateDurationDefaults
  onGateDurationDefaultsChange: (gateDurationDefaults: GateDurationDefaults) => void
  previousResponse: SimulationResponse | null
  /* toolboxJobs は通常実行に続けて App が走らせる追加の実行。 */
  onSuccessfulResponse: (
    response: SimulationResponse,
    circuitConfig: CircuitConfig,
    toolboxJobs: ToolboxJobRequest[],
  ) => void
  settings: SimulateSettings
  onSettingsChange: (settings: SimulateSettings) => void
  /*
   * IDE の作業領域に置く回路エディターと状態エクスプローラー。App が用意する。
   * 通常モードはこの IDE 画面に一本化している。
   */
  ide: {
    renderEditor: () => ReactNode
    renderExplorer: (showCircuit: () => void) => ReactNode
  }
  /* 最初に開く表示。旧 /state-explorer から来たときは 'explorer'。 */
  initialIdeView?: IdeView
}

type IdeView = 'circuit' | 'explorer'
type IdeMessagesTab = 'run' | 'alerts' | 'diagnostics'

type SimulateRequestPayload = {
  simulation_backend: SimulationBackend
  evolution_method: GateAwareEvolutionMethod
  compilation_mode: GateCompilationMode
  input_mode: 'physical'
  circuit_config: ReturnType<typeof circuitEditorStateToConfig>
  gate_duration_defaults: GateDurationDefaults
  measurement_options: MeasurementOptions
  readout_error?: {
    p10: number
    p01: number
    per_qubit?: Array<{ p10: number; p01: number }>
  }
  snapshot_options: SnapshotOptions
  parameters: SimulateRequestParameters & {
    per_qubit?: Array<{ t1_max_us: number; tphi_max_us: number }>
  }
  toolbox?: Record<string, unknown>
}

type RequestErrorKind = 'none' | 'api' | 'validation'
/** summary は誰にでも見せる説明、detail は詳細モード専用の技術情報。 */
type RequestFailure = {
  summary: string
  detail: string | null
}
type CompletionNotice = {
  title: string
  detail: string
}
const RUN_REQUEST_MIN_TIMEOUT_MS = 15000
const RUN_REQUEST_TIMEOUT_PER_STEP_MS = 25
/* ドラッグ中の1操作ごとにリクエストが飛ばない程度の、体感では気づかない待ち。 */
const COMPILE_PREVIEW_DEBOUNCE_MS = 250
const COMPILE_PREVIEW_TIMEOUT_MS = 8000

const defaultMeasurementOptions: MeasurementOptions = {
  shots: 1024,
  seed: 0,
}

const requiredResponseKeys: Array<keyof SimulationResponse> = [
  'circuit',
  'parameters',
  'rates',
  'diagnostics',
  'summary',
  'timeline',
  'output_probabilities',
  'measurement',
  'run',
  'warnings',
  'issues',
]

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isResponseArray(value: unknown) {
  return Array.isArray(value)
}

function hasRequiredResponseKeys(value: unknown): value is SimulationResponse {
  if (typeof value !== 'object' || value === null) {
    return false
  }

  const candidate = value as Record<string, unknown>
  return (
    requiredResponseKeys.every((key) => Object.prototype.hasOwnProperty.call(candidate, key)) &&
    isRecord(candidate.circuit) &&
    isRecord(candidate.parameters) &&
    isRecord(candidate.rates) &&
    isRecord(candidate.diagnostics) &&
    isRecord(candidate.summary) &&
    isResponseArray(candidate.timeline) &&
    isRecord(candidate.output_probabilities) &&
    isRecord(candidate.measurement) &&
    isRecord(candidate.run) &&
    isResponseArray(candidate.warnings) &&
    isResponseArray(candidate.issues)
  )
}

function validateSimulationParameters(parameters: SimulateRequestParameters): {
  errors: SimulateRequestParameterErrors
  firstMessage: string | null
} {
  const errors: SimulateRequestParameterErrors = {}

  const validateZeroToOne = (
    name: keyof Pick<
      SimulateRequestParameters,
      | 'device_quality'
      | 'fidelity_threshold'
    >,
    label: string,
  ) => {
    const value = parameters[name]
    if (!Number.isFinite(value) || value < 0 || value > 1) {
      errors[name] = `${label} は 0 以上 1 以下で入力してください。`
    }
  }

  validateZeroToOne('device_quality', 'デバイス品質')
  validateZeroToOne('fidelity_threshold', '忠実度のしきい値')

  if (!Number.isFinite(parameters.temperature_mk)) {
    errors.temperature_mk = '温度は有限の数値で入力してください。'
  } else if (parameters.temperature_mk < 0) {
    errors.temperature_mk = '温度は 0 mK 以上で入力してください。'
  }

  if (!Number.isFinite(parameters.flux_noise_phi0)) {
    errors.flux_noise_phi0 = '磁束ノイズは有限の数値で入力してください。'
  } else if (parameters.flux_noise_phi0 < 0) {
    errors.flux_noise_phi0 = '磁束ノイズは 0 以上で入力してください。'
  }

  if (!Number.isFinite(parameters.qubit_frequency_ghz)) {
    errors.qubit_frequency_ghz = '量子ビット周波数は有限の数値で入力してください。'
  } else if (parameters.qubit_frequency_ghz <= 0) {
    errors.qubit_frequency_ghz = '量子ビット周波数は 0 GHz より大きい値で入力してください。'
  }

  if (!Number.isFinite(parameters.t1_max_us)) {
    errors.t1_max_us = '最大 T1 は有限の数値で入力してください。'
  } else if (parameters.t1_max_us <= 0) {
    errors.t1_max_us = '最大 T1 は 0 us より大きい値で入力してください。'
  }

  if (!Number.isFinite(parameters.tphi_max_us)) {
    errors.tphi_max_us = '最大 Tφ は有限の数値で入力してください。'
  } else if (parameters.tphi_max_us <= 0) {
    errors.tphi_max_us = '最大 Tφ は 0 us より大きい値で入力してください。'
  }

  if (!Number.isFinite(parameters.duration_us)) {
    errors.duration_us = 'シミュレーション時間は有限の数値で入力してください。'
  } else if (parameters.duration_us <= 0) {
    errors.duration_us = 'シミュレーション時間は 0 より大きい値で入力してください。'
  }

  if (!Number.isFinite(parameters.time_steps)) {
    errors.time_steps = '時間ステップ数は有限の数値で入力してください。'
  } else if (!Number.isInteger(parameters.time_steps) || parameters.time_steps < 2) {
    errors.time_steps = '時間ステップ数は 2 以上の整数で入力してください。'
  }

  const firstMessage = Object.values(errors)[0] ?? null
  return { errors, firstMessage }
}

function validateReadoutError(settings: ReadoutErrorSettings): {
  errors: ReadoutErrorSettingsErrors
  firstMessage: string | null
} {
  const errors: ReadoutErrorSettingsErrors = {}
  if (!settings.enabled) {
    return { errors, firstMessage: null }
  }
  if (!Number.isFinite(settings.p10) || settings.p10 < 0 || settings.p10 > 1) {
    errors.p10 = 'p10は0以上1以下の有限値にしてください。'
  }
  if (!Number.isFinite(settings.p01) || settings.p01 < 0 || settings.p01 > 1) {
    errors.p01 = 'p01は0以上1以下の有限値にしてください。'
  }
  if (!errors.p10 && !errors.p01 && settings.p10 + settings.p01 >= 1) {
    errors.span = 'p10 + p01は1未満にしてください。'
  }
  return { errors, firstMessage: errors.p10 ?? errors.p01 ?? errors.span ?? null }
}

function normalizePerQubitCalibration(
  settings: PerQubitCalibrationSettings,
  qubitCount: number,
  parameters: SimulateRequestParameters,
  readoutError: ReadoutErrorSettings,
): PerQubitCalibrationSettings {
  return {
    ...settings,
    values: Array.from({ length: qubitCount }, (_, index) => settings.values[index] ?? ({
      t1_max_us: parameters.t1_max_us,
      tphi_max_us: parameters.tphi_max_us,
      p10: readoutError.p10,
      p01: readoutError.p01,
    })),
  }
}

function validatePerQubitCalibration(
  settings: PerQubitCalibrationSettings,
  readoutEnabled: boolean,
): string | null {
  if (!settings.enabled) return null
  for (let index = 0; index < settings.values.length; index += 1) {
    const entry = settings.values[index]
    if (!Number.isFinite(entry.t1_max_us) || entry.t1_max_us <= 0) {
      return `q${index} の T1 max は0より大きい有限値にしてください。`
    }
    if (!Number.isFinite(entry.tphi_max_us) || entry.tphi_max_us <= 0) {
      return `q${index} の Tφ max は0より大きい有限値にしてください。`
    }
    if (readoutEnabled && (
      !Number.isFinite(entry.p10) || !Number.isFinite(entry.p01)
      || entry.p10 < 0 || entry.p10 > 1 || entry.p01 < 0 || entry.p01 > 1
      || entry.p10 + entry.p01 >= 1
    )) {
      return `q${index} の読み出し誤差は0以上1以下で、p10 + p01 < 1にしてください。`
    }
  }
  return null
}

function validateGateDurationDefaults(gateDurations: GateDurationDefaults): {
  errors: GateDurationDefaultErrors
  firstMessage: string | null
} {
  const errors: GateDurationDefaultErrors = {}

  const validateNonNegative = (name: keyof GateDurationDefaults, label: string) => {
    const value = gateDurations[name]
    if (!Number.isFinite(value) || value < 0) {
      errors[name] = `${label} は 0 us 以上で入力してください。`
    }
  }

  const validatePositive = (name: keyof GateDurationDefaults, label: string) => {
    const value = gateDurations[name]
    if (!Number.isFinite(value) || value <= 0) {
      errors[name] = `${label} は 0 us より大きい値で入力してください。`
    }
  }

  validatePositive('H', 'H の操作時間')
  validatePositive('X', 'X の操作時間')
  validateNonNegative('Z', 'Z の操作時間')
  validatePositive('CNOT', 'CNOT の操作時間')
  validateNonNegative('MEASURE', '測定の操作時間')

  validatePositive('Y', 'Y gate duration')
  validateNonNegative('S', 'S gate duration')
  validateNonNegative('T', 'T gate duration')
  validatePositive('RX', 'RX gate duration')
  validatePositive('RY', 'RY gate duration')
  validatePositive('CZ', 'CZ gate duration')
  validatePositive('SWAP', 'SWAP gate duration')
  validatePositive('RZ', 'RZ gate duration')
  validatePositive('CP', 'CP gate duration')
  validatePositive('CCX', 'CCX gate duration')
  validatePositive('QFT', 'QFT gate duration per qubit')
  validatePositive('ORACLE', 'ORACLE gate duration per qubit')
  validatePositive('MESSAGE', 'MESSAGE gate duration')

  const firstMessage = Object.values(errors)[0] ?? null
  return { errors, firstMessage }
}

function parseCustomSnapshotTimes(value: string): {
  times: number[]
  error: string | null
} {
  const trimmed = value.trim()
  if (trimmed.length === 0) {
    return { times: [], error: null }
  }

  const parts = trimmed.split(',').map((part) => part.trim()).filter(Boolean)
  if (parts.length > 100) {
    return { times: [], error: 'スナップショット時刻は最大 100 個まで指定できます。' }
  }
  const times = parts.map(Number)
  if (times.some((time) => !Number.isFinite(time) || time < 0)) {
    return { times: [], error: 'カスタム時刻は 0 以上の有限な数値で入力してください。' }
  }
  return { times, error: null }
}

function validateSnapshotOptions(
  options: SnapshotOptions,
  durationUs: number,
): string | null {
  if (!Number.isInteger(options.uniform_count) || options.uniform_count < 0 || options.uniform_count > 100) {
    return '均等サンプル数は 0〜100 の整数で入力してください。'
  }
  if (options.uniform_count === 1) {
    return '均等サンプル数 1 は指定できません。0 または 2 以上を使用してください。'
  }
  if (options.custom_times_us.some((time) => time > durationUs)) {
    return 'カスタムスナップショット時刻は総シミュレーション時間を超えられません。'
  }
  return null
}

/* 完了通知・問題一覧で内部識別子の代わりに出す日本語の呼称。 */
const evolutionMethodNoticeLabels: Record<GateAwareEvolutionMethod, string> = {
  fixed_step_rk4: '固定ステップ RK4',
  explicit_cptp: '明示的 CPTP 写像',
}

const issueLevelLabels: Record<string, string> = {
  info: '情報',
  warning: '警告',
  error: 'エラー',
  critical: '重大',
}

/* データソース表示に使う日本語の呼称。内部の実装名は出さない。 */
const SOURCE_LABEL_FIXTURE = '内蔵サンプル'
const SOURCE_LABEL_FIXTURE_FALLBACK = '内蔵サンプル（代替表示）'
const SOURCE_LABEL_SERVER_RUN = 'サーバーでの実行結果'

/*
 * 利用者に見せる文と、詳細モードでのみ添える技術詳細を分ける。
 * HTTP ステータスやサーバ側の生の detail は summary へ混ぜない。
 */
function formatApiFailureMessage(status: number, detail: string | null): RequestFailure {
  const technicalDetail = detail ? `HTTP ${status}: ${detail}` : `HTTP ${status}`

  if (status === 422) {
    return {
      summary: '入力内容をサーバー側で確認できませんでした。前回の結果を表示しています。',
      detail: technicalDetail,
    }
  }

  return {
    summary: 'シミュレーションの実行に失敗しました。前回の結果を表示しています。',
    detail: technicalDetail,
  }
}

/* 利用者向けの文と技術詳細を、throw を跨いでも分けたまま運ぶ。 */
class RequestFailureError extends Error {
  readonly failure: RequestFailure

  constructor(failure: RequestFailure) {
    super(failure.summary)
    this.name = 'RequestFailureError'
    this.failure = failure
  }
}

function toRequestFailure(error: unknown, timeoutMs: number): RequestFailure {
  if (error instanceof RequestFailureError) {
    return error.failure
  }

  if (error instanceof Error && error.name === 'AbortError') {
    return {
      summary: '待ち時間の上限に達したため、実行を打ち切りました。前回の結果を表示しています。',
      detail: `request aborted after ${timeoutMs} ms`,
    }
  }

  return {
    summary: '実行リクエストに失敗しました。前回の結果を表示しています。',
    detail: error instanceof Error ? error.message : null,
  }
}

function normalizeApiDetail(detail: unknown): string | null {
  if (typeof detail === 'string') {
    const trimmed = detail.trim()
    return trimmed.length > 0 ? trimmed.replace(/\s+/g, ' ').slice(0, 180) : null
  }

  if (Array.isArray(detail)) {
    const joined = detail
      .map((entry) => normalizeApiDetail(entry))
      .filter((entry): entry is string => entry !== null)
      .join('; ')
    return joined.length > 0 ? joined.slice(0, 260) : null
  }

  if (typeof detail === 'object' && detail !== null) {
    const candidate = detail as Record<string, unknown>
    const message = normalizeApiDetail(candidate.message)
    const pydanticMessage = normalizeApiDetail(candidate.msg)
    const location = Array.isArray(candidate.loc)
      ? candidate.loc.map((entry) => String(entry)).join('.')
      : normalizeApiDetail(candidate.loc)
    const errorType = normalizeApiDetail(candidate.error_type)
      ?? normalizeApiDetail(candidate.type)
    const error = normalizeApiDetail(candidate.error)
    if (message || pydanticMessage || location || errorType || error) {
      return [
        location,
        message ?? pydanticMessage,
        errorType ? `(${errorType})` : null,
        error,
      ]
        .filter((entry): entry is string => entry !== null)
        .join(': ')
        .slice(0, 260)
    }
    if ('detail' in candidate) {
      return normalizeApiDetail(candidate.detail)
    }
  }

  return null
}

export function SimulatePage({
  gateDurationDefaults,
  onGateDurationDefaultsChange,
  previousResponse,
  onSuccessfulResponse,
  settings,
  onSettingsChange,
  ide,
  initialIdeView = 'circuit',
}: SimulatePageProps) {
  const { circuitState, handleLoadCircuitPreset } = useCircuitContext()
  const [ideView, setIdeView] = useState<IdeView>(initialIdeView)
  const [ideMessagesTab, setIdeMessagesTab] = useState<IdeMessagesTab>('run')
  const ideLayout = useIdeLayout('gate-aware', { inspectorWidth: 340 }, { navigator: 'プリセット' })
  const internalInfoVisible = useInternalInfoVisible()
  /* チュートリアルは、回路・設定・実行完了の3つをここから受け取る。 */
  const tutorial = useTutorial()
  /*
   * チュートリアル中は、台本が指す表示を優先する。利用者が選んだ表示や
   * 保存したペイン配置は書き換えず、案内のあいだだけ上から被せる。
   */
  const tutorialBeat = tutorial.beat
  const tutorialIdeView = tutorialBeat?.route === 'gate-aware-ide' ? tutorialBeat.ideView : undefined
  const activeIdeView: IdeView = tutorialIdeView ?? ideView
  const tutorialAnchors = tutorialBeat?.anchors ?? []
  const tutorialPanes = {
    navigator: tutorialAnchors.includes('circuit-summary'),
    inspector: tutorialBeat?.opensPanel === 'advanced-settings'
      || tutorialAnchors.some((anchor) => anchor === 'parameter-panel' || anchor.startsWith('param-')),
  }
  const { reportCondition, recordRun } = tutorial
  const [response, setResponse] = useState<SimulationResponse>(
    () => previousResponse ?? uiResponseExample,
  )
  const [loadStatus, setLoadStatus] = useState<SimulationLoadStatus>(
    previousResponse === null ? 'fixture' : 'api',
  )
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [errorDetail, setErrorDetail] = useState<string | null>(null)
  const [lastFetchUrl, setLastFetchUrl] = useState<string>('')
  const [lastFetchStartedAt, setLastFetchStartedAt] = useState<string>('')
  const [frontendRunStartedAt, setFrontendRunStartedAt] = useState<string>('')
  const [frontendRunFinishedAt, setFrontendRunFinishedAt] = useState<string>('')
  const [frontendRunElapsedMs, setFrontendRunElapsedMs] = useState<number | null>(null)
  const [frontendRunTimeoutMs, setFrontendRunTimeoutMs] = useState<number | null>(null)
  const [lastFetchResult, setLastFetchResult] = useState<string>(
    previousResponse === null ? 'idle' : 'success',
  )
  const [latestSourceLabel, setLatestSourceLabel] = useState<string>(
    previousResponse === null ? SOURCE_LABEL_FIXTURE : '前回のシミュレーション',
  )
  const [activeRequestLabel, setActiveRequestLabel] = useState<string>('')
  /*
   * 設定は App が持っている。ここでは読み出しと、1項目だけ差し替える
   * 書き込みを用意する。useState を戻すとページ遷移で初期値へ戻る。
   */
  const {
    parameters: simulationParameters,
    evolutionMethod,
    compilationMode,
    simulationBackend,
    snapshotOptions,
    readoutError,
    perQubitCalibration,
    customSnapshotTimesInput,
    toolbox: toolboxSettings,
  } = settings
  const updateSettings = useCallback(
    <Key extends keyof SimulateSettings>(key: Key, value: SimulateSettings[Key]) => {
      onSettingsChange({ ...settings, [key]: value })
    },
    [settings, onSettingsChange],
  )
  const setSimulationParameters = useCallback(
    (update: SimulateRequestParameters | ((current: SimulateRequestParameters) => SimulateRequestParameters)) => {
      onSettingsChange({
        ...settings,
        parameters: typeof update === 'function' ? update(settings.parameters) : update,
      })
    },
    [settings, onSettingsChange],
  )
  const setEvolutionMethod = useCallback(
    (value: GateAwareEvolutionMethod) => updateSettings('evolutionMethod', value),
    [updateSettings],
  )
  const setCompilationMode = useCallback(
    (value: GateCompilationMode) => updateSettings('compilationMode', value),
    [updateSettings],
  )
  const setSimulationBackend = useCallback(
    (value: SimulationBackend) => updateSettings('simulationBackend', value),
    [updateSettings],
  )
  /* 直近に取得できたプレビューと、その取得元の回路。null は取得失敗を表す。 */
  const [compilationPreviewResult, setCompilationPreviewResult] = useState<{
    body: string
    compilation: CircuitCompilation | null
  } | null>(null)
  const [parameterErrors, setParameterErrors] =
    useState<SimulateRequestParameterErrors>({})
  const [gateDurationErrors, setGateDurationErrors] =
    useState<GateDurationDefaultErrors>({})
  const [readoutErrorErrors, setReadoutErrorErrors] =
    useState<ReadoutErrorSettingsErrors>({})
  const [perQubitCalibrationError, setPerQubitCalibrationError] = useState<string | null>(null)
  const resolvedPerQubitCalibration = useMemo(
    () => normalizePerQubitCalibration(
      perQubitCalibration,
      circuitState.logical_qubits,
      simulationParameters,
      readoutError,
    ),
    [perQubitCalibration, circuitState.logical_qubits, simulationParameters, readoutError],
  )
  const setSnapshotOptions = useCallback(
    (update: SnapshotOptions | ((current: SnapshotOptions) => SnapshotOptions)) => {
      onSettingsChange({
        ...settings,
        snapshotOptions: typeof update === 'function' ? update(settings.snapshotOptions) : update,
      })
    },
    [settings, onSettingsChange],
  )
  const setCustomSnapshotTimesInput = useCallback(
    (value: string) => updateSettings('customSnapshotTimesInput', value),
    [updateSettings],
  )
  const setToolboxSettings = useCallback(
    (value: ToolboxSettings) => updateSettings('toolbox', value),
    [updateSettings],
  )
  const toolboxSweepDelays = useMemo(
    () => resolvedSweepDelays(toolboxSettings, simulationParameters.t1_max_us, simulationParameters.tphi_max_us),
    [toolboxSettings, simulationParameters.t1_max_us, simulationParameters.tphi_max_us],
  )
  const toolboxCheck = useMemo(
    () => checkToolbox(toolboxSettings, circuitEditorStateToConfig(circuitState), toolboxSweepDelays),
    [toolboxSettings, circuitState, toolboxSweepDelays],
  )
  const [snapshotOptionsError, setSnapshotOptionsError] = useState<string | null>(null)
  const [requestErrorKind, setRequestErrorKind] = useState<RequestErrorKind>('none')
  const [completionNotice, setCompletionNotice] = useState<CompletionNotice | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)
  const requestIdRef = useRef(0)
  const mountedRef = useRef(true)

  /*
   * チュートリアルの達成条件のうち、いまの状態から読めるもの。
   * 実行の完了だけは出来事なので、成功時に別途報告する。
   */
  const tutorialShortT1 = hasShortT1(simulationParameters)
  const tutorialLongDuration = hasExtendedDuration(simulationParameters)
  useEffect(() => {
    reportCondition('t1-lowered', tutorialShortT1)
    reportCondition('duration-extended', tutorialLongDuration)
  }, [reportCondition, tutorialShortT1, tutorialLongDuration])

  /*
   * 実行前プレビュー。/api/circuit/compile は時間発展をまったく走らせず、
   * ゲート分解だけを返すので、編集のたびに呼んでも実行枠も計算時間も食わない。
   * 送る中身を文字列に畳んでおくと、実体が同じ編集状態で再実行しなくて済む。
   */
  const compilationPreviewBody = useMemo(() => {
    const circuitConfig = circuitEditorStateToConfig(circuitState)
    if (!validateCircuitConfigForRun(circuitConfig).valid) {
      return null
    }

    return JSON.stringify({
      compilation_mode: compilationMode,
      circuit_config: circuitConfig,
      gate_duration_defaults: gateDurationDefaults,
    })
  }, [circuitState, compilationMode, gateDurationDefaults])

  /*
   * 新しいプレビューを取りに行っている間も、直前のプレビューは出したままにする。
   * 編集のたびに図が消えて実行済みの図に戻る、というちらつきを避けるため。
   */
  const compilationPreview =
    compilationPreviewBody === null ? null : compilationPreviewResult?.compilation ?? null
  const compilationPreviewPending =
    compilationPreviewBody !== null && compilationPreviewResult?.body !== compilationPreviewBody

  useEffect(() => {
    if (compilationPreviewBody === null) {
      return
    }

    const controller = new AbortController()
    let timeoutId: number | undefined

    const debounceId = window.setTimeout(() => {
      timeoutId = window.setTimeout(() => controller.abort(), COMPILE_PREVIEW_TIMEOUT_MS)
      void (async () => {
        try {
          const apiResponse = await fetch(apiUrl('/api/circuit/compile'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: compilationPreviewBody,
            cache: 'no-store',
            signal: controller.signal,
          })
          if (!apiResponse.ok) {
            throw new Error(`HTTP ${apiResponse.status}`)
          }

          const parsed = (await apiResponse.json()) as { compilation?: CircuitCompilation }
          if (controller.signal.aborted) {
            return
          }
          setCompilationPreviewResult({
            body: compilationPreviewBody,
            compilation: parsed.compilation ?? null,
          })
        } catch {
          /* プレビューは補助情報なので、失敗しても実行の邪魔はしない。 */
          if (!controller.signal.aborted) {
            setCompilationPreviewResult({ body: compilationPreviewBody, compilation: null })
          }
        } finally {
          window.clearTimeout(timeoutId)
        }
      })()
    }, COMPILE_PREVIEW_DEBOUNCE_MS)

    return () => {
      window.clearTimeout(debounceId)
      window.clearTimeout(timeoutId)
      controller.abort()
    }
  }, [compilationPreviewBody])

  function handleSimulationParametersChange(nextParameters: SimulateRequestParameters) {
    const validation = validateSimulationParameters(nextParameters)
    const gateDurationValidation = validateGateDurationDefaults(gateDurationDefaults)
    setSimulationParameters(nextParameters)
    setParameterErrors(validation.errors)

    if (
      requestErrorKind === 'validation' &&
      validation.firstMessage === null &&
      gateDurationValidation.firstMessage === null &&
      validateReadoutError(readoutError).firstMessage === null
    ) {
      setRequestErrorKind('none')
      setErrorMessage(null)
      setErrorDetail(null)
    }
  }

  function handleGateDurationDefaultsChange(nextGateDurations: GateDurationDefaults) {
    const validation = validateGateDurationDefaults(nextGateDurations)
    onGateDurationDefaultsChange(nextGateDurations)
    setGateDurationErrors(validation.errors)

    const parameterValidation = validateSimulationParameters(simulationParameters)
    if (
      requestErrorKind === 'validation' &&
      validation.firstMessage === null &&
      parameterValidation.firstMessage === null
    ) {
      setRequestErrorKind('none')
      setErrorMessage(null)
      setErrorDetail(null)
    }
  }

  function handleReadoutErrorChange(nextReadoutError: ReadoutErrorSettings) {
    const validation = validateReadoutError(nextReadoutError)
    updateSettings('readoutError', nextReadoutError)
    setReadoutErrorErrors(validation.errors)
    if (
      requestErrorKind === 'validation' &&
      validation.firstMessage === null &&
      validateSimulationParameters(simulationParameters).firstMessage === null &&
      validateGateDurationDefaults(gateDurationDefaults).firstMessage === null
    ) {
      setRequestErrorKind('none')
      setErrorMessage(null)
      setErrorDetail(null)
    }
  }

  function handlePerQubitCalibrationChange(next: PerQubitCalibrationSettings) {
    const normalized = normalizePerQubitCalibration(
      next,
      circuitState.logical_qubits,
      simulationParameters,
      readoutError,
    )
    updateSettings('perQubitCalibration', normalized)
    setPerQubitCalibrationError(
      validatePerQubitCalibration(normalized, readoutError.enabled),
    )
  }

  async function runSimulationFromApi() {
    const frontendStartedAtMs = performance.now()
    const frontendStartedAtIso = new Date().toISOString()
    const timeoutMs = getRunRequestTimeoutMs(
      simulationParameters.time_steps,
      circuitState.logical_qubits,
      evolutionMethod,
    )
    setFrontendRunStartedAt(frontendStartedAtIso)
    setFrontendRunFinishedAt('')
    setFrontendRunElapsedMs(null)
    setFrontendRunTimeoutMs(timeoutMs)

    const validation = validateSimulationParameters(simulationParameters)
    const gateDurationValidation = validateGateDurationDefaults(gateDurationDefaults)
    const readoutErrorValidation = validateReadoutError(readoutError)
    const perQubitValidationMessage = validatePerQubitCalibration(
      resolvedPerQubitCalibration,
      readoutError.enabled,
    )
    const parsedSnapshotTimes = parseCustomSnapshotTimes(customSnapshotTimesInput)
    const requestedSnapshotOptions = {
      ...snapshotOptions,
      custom_times_us: parsedSnapshotTimes.times,
    }
    const snapshotValidationMessage =
      parsedSnapshotTimes.error ??
      validateSnapshotOptions(requestedSnapshotOptions, simulationParameters.duration_us)
    setSnapshotOptionsError(snapshotValidationMessage)
    setParameterErrors(validation.errors)
    setGateDurationErrors(gateDurationValidation.errors)
    setReadoutErrorErrors(readoutErrorValidation.errors)
    setPerQubitCalibrationError(perQubitValidationMessage)
    const firstValidationMessage =
      validation.firstMessage ??
      gateDurationValidation.firstMessage ??
      readoutErrorValidation.firstMessage ??
      perQubitValidationMessage ??
      snapshotValidationMessage ??
      toolboxErrorMessage(toolboxCheck)
    if (firstValidationMessage !== null) {
      setLoadStatus('error')
      setActiveRequestLabel(SOURCE_LABEL_SERVER_RUN)
      setLastFetchUrl('not requested - validation failed')
      setLastFetchStartedAt(new Date().toISOString())
      setLastFetchResult('validation failed')
      setRequestErrorKind('validation')
      setErrorMessage(firstValidationMessage)
      setErrorDetail(null)
      setFrontendRunFinishedAt(new Date().toISOString())
      setFrontendRunElapsedMs(Number((performance.now() - frontendStartedAtMs).toFixed(1)))
      return
    }

    const circuitConfig = circuitEditorStateToConfig(circuitState)
    const circuitValidation = validateCircuitConfigForRun(circuitConfig)
    if (!circuitValidation.valid) {
      setLoadStatus('error')
      setActiveRequestLabel(SOURCE_LABEL_SERVER_RUN)
      setLastFetchUrl('not requested - validation failed')
      setLastFetchStartedAt(new Date().toISOString())
      setLastFetchResult('validation failed')
      setRequestErrorKind('validation')
      setErrorMessage(circuitValidation.message)
      setErrorDetail(null)
      setFrontendRunFinishedAt(new Date().toISOString())
      setFrontendRunElapsedMs(Number((performance.now() - frontendStartedAtMs).toFixed(1)))
      return
    }

    abortControllerRef.current?.abort()

    const requestId = requestIdRef.current + 1
    requestIdRef.current = requestId

    const controller = new AbortController()
    abortControllerRef.current = controller
    const url = apiUrl(`/api/simulate?ts=${Date.now()}`)
    const startedAt = new Date().toISOString()
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs)

    setLoadStatus('loading')
    setActiveRequestLabel(SOURCE_LABEL_SERVER_RUN)
    setLastFetchUrl(url)
    setLastFetchStartedAt(startedAt)
    setLastFetchResult('pending')
    setErrorMessage(null)
    setErrorDetail(null)
    setRequestErrorKind('none')

    const toolboxBlock = toolboxRequestBlock(toolboxSettings, circuitConfig.logical_qubits, toolboxCheck)
    try {
      const payload: SimulateRequestPayload = {
        simulation_backend: simulationBackend,
        evolution_method: evolutionMethod,
        compilation_mode: compilationMode,
        input_mode: 'physical',
        circuit_config: circuitConfig,
        gate_duration_defaults: gateDurationDefaults,
        measurement_options: defaultMeasurementOptions,
        ...(readoutError.enabled ? {
          readout_error: {
            p10: readoutError.p10,
            p01: readoutError.p01,
            ...(resolvedPerQubitCalibration.enabled ? {
              per_qubit: resolvedPerQubitCalibration.values.map(({ p10, p01 }) => ({ p10, p01 })),
            } : {}),
          },
        } : {}),
        snapshot_options: requestedSnapshotOptions,
        parameters: {
          ...simulationParameters,
          ...(resolvedPerQubitCalibration.enabled ? {
            per_qubit: resolvedPerQubitCalibration.values.map(({ t1_max_us, tphi_max_us }) => ({
              t1_max_us,
              tphi_max_us,
            })),
          } : {}),
        },
        ...(toolboxBlock === null ? {} : { toolbox: toolboxBlock }),
      }
      /* 追加の実行は、この実行と同じ設定で固めておく。 */
      const toolboxJobs = toolboxJobRequests(toolboxSettings, toolboxCheck, payload, {
        parameters: simulationParameters,
        readoutError: readoutError.enabled ? { p10: readoutError.p10, p01: readoutError.p01 } : null,
        delays: toolboxSweepDelays,
      })
      const apiResponse = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
        cache: 'no-store',
        signal: controller.signal,
      })
      if (!apiResponse.ok) {
        const rawError = await apiResponse.text()
        let responseDetail: string | null = null
        if (rawError.trim().length > 0) {
          try {
            const parsedError = JSON.parse(rawError) as unknown
            responseDetail = normalizeApiDetail(parsedError)
          } catch {
            responseDetail = rawError.trim().replace(/\s+/g, ' ').slice(0, 180)
          }
        }

        throw new RequestFailureError(
          formatApiFailureMessage(apiResponse.status, responseDetail),
        )
      }

      const parsed = (await apiResponse.json()) as unknown
      if (!hasRequiredResponseKeys(parsed)) {
        throw new RequestFailureError({
          summary: 'サーバーの応答を解釈できませんでした。前回の結果を表示しています。',
          detail: 'response payload is missing required keys',
        })
      }

      if (!mountedRef.current || requestId !== requestIdRef.current) {
        return
      }

      setResponse(parsed as SimulationResponse)
      onSuccessfulResponse(parsed as SimulationResponse, circuitConfig, toolboxJobs)
      setLoadStatus('api')
      setLatestSourceLabel(SOURCE_LABEL_SERVER_RUN)
      setLastFetchResult('success')
      setErrorMessage(null)
      setErrorDetail(null)
      setRequestErrorKind('none')
      setFrontendRunFinishedAt(new Date().toISOString())
      setFrontendRunElapsedMs(Number((performance.now() - frontendStartedAtMs).toFixed(1)))
      setCompletionNotice({
        title: 'シミュレーションが完了しました',
        /* 完了通知は誰の目にも入るので、内部の実行基盤名は出さない。 */
        detail: internalInfoVisible
          ? `${parsed.run.selected_backend} / ${evolutionMethod}`
          : `${evolutionMethodNoticeLabels[evolutionMethod]}で計算しました`,
      })

      /*
       * チュートリアルへの報告。判定は「この実行に使った設定」で行う。
       * あとで設定を戻しても、この実行が済んだ事実は変わらないようにする。
       */
      reportCondition('simulation-finished', true)
      if (hasShortT1(simulationParameters)) {
        reportCondition('short-t1-run-finished', true)
      }
      if (hasExtendedDuration(simulationParameters)) {
        reportCondition('long-run-finished', true)
      }
      recordRun({
        fidelity: parsed.summary.final_fidelity,
        purity: parsed.summary.final_purity,
        t1Us: simulationParameters.t1_max_us,
        durationUs: simulationParameters.duration_us,
      })
    } catch (error) {
      if (!mountedRef.current || requestId !== requestIdRef.current) {
        return
      }

      const failure = toRequestFailure(error, timeoutMs)

      setLoadStatus('error')
      setLastFetchResult('failed')
      setErrorMessage(failure.summary)
      setErrorDetail(failure.detail)
      setRequestErrorKind('api')
      setFrontendRunFinishedAt(new Date().toISOString())
      setFrontendRunElapsedMs(Number((performance.now() - frontendStartedAtMs).toFixed(1)))
    } finally {
      window.clearTimeout(timeoutId)
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null
      }
    }
  }

  useEffect(() => {
    mountedRef.current = true

    return () => {
      mountedRef.current = false
      abortControllerRef.current?.abort()
    }
  }, [])

  const connectionLabel =
    loadStatus === 'loading'
      ? '確認中...'
      : loadStatus === 'api'
        ? '接続済み'
        : loadStatus === 'error'
          ? requestErrorKind === 'validation'
            ? '未送信'
            : 'サーバーに接続できません'
          : '未接続'

  const dataSourceLabel =
    loadStatus === 'loading'
      ? `${activeRequestLabel || SOURCE_LABEL_SERVER_RUN} を読み込み中...`
      : loadStatus === 'api'
        ? latestSourceLabel
        : loadStatus === 'error'
          ? requestErrorKind === 'validation'
            ? `前回の結果を保持中（${latestSourceLabel}）`
          : latestSourceLabel === SOURCE_LABEL_FIXTURE
            ? SOURCE_LABEL_FIXTURE_FALLBACK
            : `前回の結果を保持中（${latestSourceLabel}）`
          : SOURCE_LABEL_FIXTURE
  const circuitGateCount = circuitState.columns.reduce(
    (count, column) => count + column.gates.length,
    0,
  )
  const circuitColumnCount = circuitState.columns.filter((column) => column.gates.length > 0).length
  const costEstimate = estimateSimulationCost({
    logicalQubits: circuitState.logical_qubits,
    timeSteps: simulationParameters.time_steps,
    durationUs: simulationParameters.duration_us,
    circuitGateCount,
    circuitColumnCount,
    evolutionMethod,
  })
  const parameterControls = (
    <>
            <ParameterPanel
              editableParameters={simulationParameters}
              gateDurationDefaults={gateDurationDefaults}
              validationMessages={parameterErrors}
              gateDurationValidationMessages={gateDurationErrors}
              readoutError={readoutError}
              readoutErrorValidationMessages={readoutErrorErrors}
              logicalQubits={circuitState.logical_qubits}
              perQubitCalibration={resolvedPerQubitCalibration}
              perQubitCalibrationError={perQubitCalibrationError}
              onEditableParametersChange={handleSimulationParametersChange}
              onGateDurationDefaultsChange={handleGateDurationDefaultsChange}
              onReadoutErrorChange={handleReadoutErrorChange}
              onPerQubitCalibrationChange={handlePerQubitCalibrationChange}
            />
            <section className="simulate-page__snapshot-controls" aria-labelledby="snapshot-controls-title">
              <div className="simulate-page__snapshot-heading">
                <div>
                  <span className="simulate-page__section-eyebrow">サンプリング</span>
                  <h2 id="snapshot-controls-title">スナップショットのサンプリング</h2>
                </div>
                <label className="simulate-page__snapshot-toggle">
                  <input
                    type="checkbox"
                    checked={snapshotOptions.enabled}
                    onChange={(event) =>
                      setSnapshotOptions((current) => ({
                        ...current,
                        enabled: event.target.checked,
                      }))
                    }
                  />
                  有効
                </label>
              </div>
              <p className="simulate-page__snapshot-help">
                初期状態・列の境界・最終状態などのイベントスナップショットを保持しながら、時刻サンプルを指定します。
              </p>
              <div className="simulate-page__snapshot-grid">
                <label>
                  均等サンプル数
                  <input
                    type="number"
                    min={0}
                    max={100}
                    step={1}
                    value={snapshotOptions.uniform_count}
                    onChange={(event) =>
                      setSnapshotOptions((current) => ({
                        ...current,
                        uniform_count: Number(event.target.value),
                      }))
                    }
                  />
                </label>
                <label>
                  カスタム時刻 [us]
                  <input
                    type="text"
                    value={customSnapshotTimesInput}
                    placeholder="0.5, 1.25"
                    onChange={(event) => setCustomSnapshotTimesInput(event.target.value)}
                  />
                </label>
              </div>
              <div className="simulate-page__snapshot-checks">
                {([
                  ['include_initial', '初期状態'],
                  ['include_final', '最終状態'],
                  ['include_column_boundaries', '列の境界'],
                  ['include_after_circuit', '回路の後'],
                ] as const).map(([key, label]) => (
                  <label key={key}>
                    <input
                      type="checkbox"
                      checked={snapshotOptions[key]}
                      onChange={(event) =>
                        setSnapshotOptions((current) => ({
                          ...current,
                          [key]: event.target.checked,
                        }))
                      }
                    />
                    {label}
                  </label>
                ))}
              </div>
              {snapshotOptionsError ? (
                <p className="simulate-page__snapshot-error" role="alert">
                  {snapshotOptionsError}
                </p>
              ) : null}
            </section>
    </>
  )

  const renderRunPanel = (showRunButton: boolean) => (
        <RunPanel
          run={response.run}
          compilationPreview={compilationPreview}
          compilationPreviewPending={compilationPreviewPending}
          costEstimate={costEstimate}
          connectionLabel={connectionLabel}
          dataSourceLabel={dataSourceLabel}
          loadStatus={loadStatus}
          errorMessage={errorMessage}
          errorDetail={errorDetail}
          lastFetchResult={lastFetchResult}
          lastFetchUrl={lastFetchUrl}
          lastFetchStartedAt={lastFetchStartedAt}
          frontendRunStartedAt={frontendRunStartedAt}
          frontendRunFinishedAt={frontendRunFinishedAt}
          frontendRunElapsedMs={frontendRunElapsedMs}
          frontendRunTimeoutMs={frontendRunTimeoutMs}
          evolutionMethod={evolutionMethod}
          compilationMode={compilationMode}
          simulationBackend={simulationBackend}
          onEvolutionMethodChange={setEvolutionMethod}
          onCompilationModeChange={setCompilationMode}
          onSimulationBackendChange={setSimulationBackend}
          onRunSimulation={() => {
            void runSimulationFromApi()
          }}
          showRunButton={showRunButton}
        />
  )

  const alertsContent = (
    <>
            {response.warnings.length > 0 ? (
              <div className="simulate-page__alert-block">
                <div className="simulate-page__alert-heading">警告</div>
                <ul className="simulate-page__warning-list">
                  {response.warnings.map((warning, index) => (
                    <li className="simulate-page__warning-item" key={`${warning}-${index}`}>
                      {warning}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {response.issues.length > 0 ? (
              <div className="simulate-page__alert-block">
                <div className="simulate-page__alert-heading">問題</div>
                <div className="simulate-page__issue-list">
                  {response.issues.map((issue, index) => (
                    <article className="simulate-page__issue-card" key={`${issue.code}-${index}`}>
                      <div className="simulate-page__issue-header">
                        <strong className="simulate-page__issue-code">
                          {issueLevelLabels[issue.level] ?? issue.level}
                          {internalInfoVisible ? ` / ${issue.code}` : ''}
                        </strong>
                        <span className="simulate-page__issue-message">{issue.message}</span>
                      </div>
                      {issue.detail ? (
                        <p className="simulate-page__issue-detail">{issue.detail}</p>
                      ) : null}
                      {issue.suggestion ? (
                        <p className="simulate-page__issue-suggestion">{issue.suggestion}</p>
                      ) : null}
                    </article>
                  ))}
                </div>
              </div>
            ) : null}
    </>
  )

  const completionPopup = completionNotice ? (
        <SimulationCompletionPopup
          mode="gate-aware"
          title={completionNotice.title}
          detail={completionNotice.detail}
          onDismiss={() => setCompletionNotice(null)}
        />
  ) : null

  const canRun = response.run.can_run ?? true
  const alertCount = response.warnings.length + response.issues.length
  const fidelity = response.summary.final_fidelity
  const messageTabs: Array<[IdeMessagesTab, string, number | null]> = [
    ['run', '実行', null],
    ['alerts', '警告と問題', alertCount],
    ['diagnostics', '診断', null],
  ]
  return (
    <main className="simulate-page simulate-ide ide">
      <header className="ide__titlebar">
        <h1>IDEワークスペース</h1>
        <nav className="ide__tabs" aria-label="作業領域の表示">
          <button type="button" aria-pressed={activeIdeView === 'circuit'} onClick={() => setIdeView('circuit')}>回路</button>
          <button type="button" aria-pressed={activeIdeView === 'explorer'} onClick={() => setIdeView('explorer')}>状態エクスプローラー</button>
        </nav>
        <IdeViewToggles controls={ideLayout} />
        <span className="ide__badge" role="status">{connectionLabel}</span>
      </header>
      <div className="ide__toolbar" role="toolbar" aria-label="操作">
        <button type="button" data-tutorial-anchor="run-button" onClick={() => { void runSimulationFromApi() }} disabled={!canRun || loadStatus === 'loading'}>
          {loadStatus === 'loading' ? '実行中…' : 'シミュレーションを実行'}
        </button>
        <span className="ide__toolbar-note">{dataSourceLabel}</span>
      </div>
      {errorMessage ? <p className="ide__notice" role="status">{errorMessage}</p> : null}
      <IdeWorkbench
        controls={ideLayout}
        forceVisible={tutorialPanes}
        navigator={(
          <aside className="ide__pane ide__navigator" aria-label="プリセット">
            <h2 className="ide__pane-title">プリセット</h2>
            <div className="ide__pane-body">
              <dl className="ide__facts" data-tutorial-anchor="circuit-summary">
                <div><dt>量子ビット</dt><dd>{circuitState.logical_qubits}</dd></div>
                <div><dt>列</dt><dd>{circuitState.columns.length}</dd></div>
                <div><dt>ゲート</dt><dd>{circuitGateCount}</dd></div>
              </dl>
              <h3>アルゴリズム</h3>
              <ul className="ide__tree" aria-label="アルゴリズムのプリセット">
                {algorithmPresets.map((preset) => (
                  <li key={preset.key}>
                    <button type="button" onClick={() => { handleLoadCircuitPreset(preset.key); setIdeView('circuit') }}>
                      <span>{preset.title}</span>
                      <small>{preset.qubits} 量子ビット · {preset.category}</small>
                    </button>
                  </li>
                ))}
              </ul>
              <small>読み込むと、編集中の回路を置き換えます。</small>
            </div>
          </aside>
        )}
        editor={(
          <section className="ide__pane ide__editor" aria-label="作業領域">
            <div className="ide__doc-tabs">
              <span className="ide__doc-tab">{activeIdeView === 'circuit' ? '回路エディター' : '状態エクスプローラー'}</span>
            </div>
            <div className="ide__pane-body">
              {activeIdeView === 'circuit' ? ide.renderEditor() : ide.renderExplorer(() => setIdeView('circuit'))}
            </div>
          </section>
        )}
        toolbox={(
          <aside className="ide__pane ide__toolbox" aria-label="分析ツール箱">
            <h2 className="ide__pane-title">分析ツール箱</h2>
            <div className="ide__pane-body">
              <ToolboxPanel
                settings={toolboxSettings}
                check={toolboxCheck}
                qubitCount={circuitState.logical_qubits}
                sweepDelays={toolboxSweepDelays}
                onChange={setToolboxSettings}
              />
            </div>
          </aside>
        )}
        inspector={(
          <aside className="ide__pane ide__inspector" aria-label="プロパティ" data-tutorial-anchor="advanced-settings">
            <h2 className="ide__pane-title">計算パラメーター</h2>
            <div className="ide__pane-body">
              {parameterControls}
            </div>
          </aside>
        )}
        messages={(
          <section className="ide__pane ide__messages" aria-label="メッセージ">
            <div className="ide__doc-tabs" role="group" aria-label="メッセージの種類">
              {messageTabs.map(([key, label, count]) => (
                <button key={key} type="button" aria-pressed={ideMessagesTab === key} onClick={() => setIdeMessagesTab(key)}>
                  {label}{count === null ? null : <span>{count}</span>}
                </button>
              ))}
            </div>
            <div className="ide__pane-body simulate-ide__messages-body">
              {ideMessagesTab === 'run' ? renderRunPanel(false) : null}
              {ideMessagesTab === 'alerts'
                ? alertCount > 0 ? alertsContent : <p className="simulate-ide__empty">警告と問題はありません。</p>
                : null}
              {ideMessagesTab === 'diagnostics' ? (
                <>
                  <DensityMatrixSummaryCard response={response} onOpenStateExplorer={() => setIdeView('explorer')} />
                  <DiagnosticsCard diagnostics={response.diagnostics} rates={response.rates} />
                </>
              ) : null}
            </div>
          </section>
        )}
      />
      <footer className="ide__statusbar">
        <span>{loadStatus === 'loading' ? '実行中…' : '準備完了'}</span>
        <span>接続: {connectionLabel}</span>
        <span>最終忠実度 {fidelity === null ? '—' : `${(fidelity * 100).toFixed(2)}%`}</span>
        {frontendRunElapsedMs === null ? null : <span>前回の実行 {frontendRunElapsedMs} ms</span>}
      </footer>
      {completionPopup}
    </main>
  )
}

function getRunRequestTimeoutMs(
  timeSteps: number,
  logicalQubits: number,
  evolutionMethod: GateAwareEvolutionMethod,
): number {
  const stepBudget = Math.max(
    RUN_REQUEST_MIN_TIMEOUT_MS,
    timeSteps * RUN_REQUEST_TIMEOUT_PER_STEP_MS,
  )
  // Noisy dense cost grows as 4**n. Preserve enough time for bounded 6-8Q
  // runs while the cost notice steers users toward small step counts.
  const qubitBudget = logicalQubits >= 8
    ? Math.max(900000, Math.min(1800000, timeSteps * 20000))
    : logicalQubits === 7
      ? Math.max(300000, timeSteps * 6000)
      : logicalQubits === 6
        ? Math.max(120000, timeSteps * 2500)
        : logicalQubits >= 4
          ? 60000
          : logicalQubits === 3
            ? 30000
            : RUN_REQUEST_MIN_TIMEOUT_MS
  const evolutionBudget = evolutionMethod === 'explicit_cptp' ? 120000 : 0
  return Math.max(stepBudget, qubitBudget, evolutionBudget)
}
