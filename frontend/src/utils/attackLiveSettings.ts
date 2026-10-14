/** Pure helpers for the D60–D64 attack environment screens.
 *
 * The server (D64 resolver) is the authority and re-validates everything.
 * These checks mirror the catalog descriptors only so the user sees which
 * field is wrong before sending, because the server deliberately answers
 * with a single INVALID_SETTINGS code.
 */
import type {
  ChallengeEntry, CreateEnvironmentRequest, IdleNoiseModeId, LiveReply, LiveRequest,
  SettingFieldDescriptor, SettingsCatalog, SettingValue,
  RegionSelectionRequest,
} from '../types/attackLive'
import type { CircuitConfigBundle } from './circuitConfigTransfer'

export function placementChoiceLabel(field: SettingFieldDescriptor, choice: string): string {
  if (field.path !== 'qpu_service.runtime_execution.placement_policy') return choice
  if (choice === 'guaranteed_same_physical_qubit') return '同じ物理領域を使う'
  if (choice === 'tenant_dedicated_region') return '利用者ごとに別の物理領域を使う'
  return choice
}

/** Gate timings belong to the declared service, not the Circuit Studio's
 * local-simulation duration controls. Keep all other parameters intact. */
export function applyServiceGateDurations(bundle: CircuitConfigBundle): CircuitConfigBundle {
  const result = structuredClone(bundle)
  for (const column of result.circuit_config.columns) for (const gate of column.gates) {
    if (gate.params) delete gate.params.duration_us
  }
  return result
}

/** Explicit authoring option for the common sampling Program. Never mutate
 * the editor or replace existing classical targets. */
export function bindUnspecifiedMeasurements(bundle: CircuitConfigBundle): CircuitConfigBundle {
  const result = structuredClone(bundle)
  const circuit = result.circuit_config
  let classicalBits = circuit.classical_bits ?? 0
  for (const column of circuit.columns) for (const gate of column.gates) {
    if (gate.type !== 'MEASURE') continue
    if (!gate.classical_targets?.length) gate.classical_targets = [...gate.targets]
    classicalBits = Math.max(classicalBits, ...gate.classical_targets.map((bit) => bit + 1))
  }
  circuit.classical_bits = classicalBits
  return result
}

/**
 * 送信する回路の測定先を決める。map で指定した qubit はその古典bitへ割り当て
 * （エディターでの指定より優先）、残りはエディターの指定を使い、それも無ければ
 * 同番号の古典bit（qN → cN）へ自動で割り当てる。エディターの回路は変えない。
 */
export function assignMeasurementTargets(
  bundle: CircuitConfigBundle, map: Readonly<Record<number, number>> = {},
): CircuitConfigBundle {
  const result = structuredClone(bundle)
  for (const column of result.circuit_config.columns) for (const gate of column.gates) {
    if (gate.type !== 'MEASURE') continue
    const current = gate.classical_targets?.length === gate.targets.length ? gate.classical_targets : undefined
    if (!gate.targets.some((qubit) => qubit in map) && current) continue
    gate.classical_targets = gate.targets.map((qubit, index) => map[qubit] ?? current?.[index] ?? qubit)
  }
  return bindUnspecifiedMeasurements(result)
}

export const MAX_SETTING_FIELDS = 128
export const MAX_SETTING_STRING_BYTES = 256
export const SUPPORTED_CATALOG_SCHEMA = 'attack-settings-catalog-v1'

export type SetupSelection =
  | { mode: 'free'; noiseMode: IdleNoiseModeId }
  | { mode: 'challenge'; challengeId: string }

export type SetupCategoryId = 'mode' | 'qpu' | 'noise' | 'service' | 'review'

export function setupCategoryForPath(path: string): Exclude<SetupCategoryId, 'mode' | 'review'> {
  if (path === 'qpu_service.runtime_execution.placement_policy') return 'qpu'
  if (path.includes('.physical_inputs.') || /^qpu_service\.(?:dedicated_region\.)?idle_dynamics\.qubits\./.test(path)) return 'noise'
  return 'service'
}

export function countChangedSettings(
  fields: SettingFieldDescriptor[], values: Record<string, SettingValue>,
): Record<Exclude<SetupCategoryId, 'mode' | 'review'>, number> {
  const counts = { qpu: 0, noise: 0, service: 0 }
  for (const field of fields) {
    const value = values[field.path] ?? field.default
    if (value !== field.default) counts[setupCategoryForPath(field.path)] += 1
  }
  return counts
}

export function firstErrorCategory(
  fields: SettingFieldDescriptor[], errors: Record<string, string>,
): Exclude<SetupCategoryId, 'mode' | 'review'> | null {
  const firstPath = Object.keys(errors)[0]
  if (!firstPath) return null
  const field = fields.find((candidate) => candidate.path === firstPath)
  return field ? setupCategoryForPath(field.path) : 'service'
}

export function assertSupportedCatalog(catalog: SettingsCatalog): void {
  if (catalog?.schema_version !== SUPPORTED_CATALOG_SCHEMA) {
    throw new Error('設定目録の形式に対応していません。画面とサーバーの版を確認してください。')
  }
}

export function availableNoiseModes(catalog: SettingsCatalog) {
  return catalog.free_mode.idle_noise_modes.filter((mode) => mode.available)
}

export function findChallenge(catalog: SettingsCatalog, id: string): ChallengeEntry | undefined {
  return catalog.challenges.find((challenge) => challenge.id === id)
}

/** Fields for the current selection. Inactive noise-mode fields never appear (D62 addendum). */
export function fieldsFor(catalog: SettingsCatalog, selection: SetupSelection): SettingFieldDescriptor[] {
  if (selection.mode === 'free') {
    const mode = catalog.free_mode.idle_noise_modes.find((candidate) => candidate.id === selection.noiseMode)
    return mode?.available ? mode.fields : []
  }
  const challenge = findChallenge(catalog, selection.challengeId)
  return challenge?.available ? challenge.fields : []
}

/** Expand server-declared per-qubit noise templates only for the selected
 * region. This keeps the public catalog compact and create requests bounded. */
export function regionNoiseFields(
  catalogFields: SettingFieldDescriptor[], regionCatalog: import('../types/attackLive').RegionSelectionCatalog | undefined,
  standard: import('../types/attackLive').StandardInitialValues | undefined,
  regions: { shared: number[]; victim: number[]; attacker: number[] }, placement: string,
  noiseMode: string,
): SettingFieldDescriptor[] {
  if (noiseMode !== 'calibration_values' || !regionCatalog?.qubit_noise_fields
      || !standard || !('qubits' in standard)) return []
  const selected = placement === 'tenant_dedicated_region'
    ? [
        ...regions.victim.map((qid) => ({ qid, scope: 'primary' as const })),
        ...regions.attacker.map((qid) => ({ qid, scope: 'dedicated' as const })),
      ]
    : regions.shared.map((qid) => ({ qid, scope: 'primary' as const }))
  const generated = selected.flatMap(({ qid, scope }) => {
    const calibration = standard.qubits[String(qid)]
    if (!calibration?.usable_for_placement) return []
    return (regionCatalog.qubit_noise_fields?.[scope] ?? []).map((template) => {
      const path = template.path.replaceAll('{qubit_id}', String(qid))
      const label = template.label.replaceAll('{qubit_id}', String(qid))
      const suffix = path.split('.').at(-1)
      const defaultValue = suffix === 't1_us' ? calibration.T1_us
        : suffix === 't2_us' ? calibration.T2_us : 0
      return { ...template, path, label, default: defaultValue ?? undefined,
        basis: suffix === 'equilibrium_excited_population' ? template.basis : calibration.basis }
    })
  })
  const qubitPath = /^qpu_service\.(?:dedicated_region\.)?idle_dynamics\.qubits\.\d+\./
  return [...catalogFields.filter((field) => !qubitPath.test(field.path)), ...generated]
}

export function initialValues(fields: SettingFieldDescriptor[]): Record<string, SettingValue> {
  const values: Record<string, SettingValue> = {}
  for (const field of fields) {
    if (!field.required && field.default !== undefined) values[field.path] = field.default
  }
  return values
}

function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length
}

/** Japanese message for the user, or null when the value fits the descriptor. */
export function validateValue(field: SettingFieldDescriptor, value: SettingValue | undefined): string | null {
  if (value === undefined) return field.required ? `${field.label}を入力してください。` : null
  switch (field.type) {
    case 'number':
    case 'integer': {
      if (typeof value !== 'number' || !Number.isFinite(value)) return `${field.label}には数値を入力してください。`
      if (field.type === 'integer' && !Number.isInteger(value)) return `${field.label}には整数を入力してください。`
      if (field.minimum !== undefined && value < field.minimum) return `${field.label}は${field.minimum}以上にしてください。`
      if (field.maximum !== undefined && value > field.maximum) return `${field.label}は${field.maximum}以下にしてください。`
      return null
    }
    case 'boolean':
      return typeof value === 'boolean' ? null : `${field.label}の値が不正です。`
    case 'string':
      if (typeof value !== 'string') return `${field.label}の値が不正です。`
      return utf8Bytes(value) <= MAX_SETTING_STRING_BYTES ? null : `${field.label}が長すぎます。`
    case 'choice':
      return typeof value === 'string' && (field.choices ?? []).includes(value)
        ? null : `${field.label}は選択肢から選んでください。`
  }
}

export function fieldErrors(fields: SettingFieldDescriptor[], values: Record<string, SettingValue>): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const field of fields) {
    const message = validateValue(field, values[field.path])
    if (message) errors[field.path] = message
  }
  return errors
}

/** Build the D64 body from the visible fields only; unknown keys are dropped. */
export function buildCreateRequest(
  selection: SetupSelection, fields: SettingFieldDescriptor[], values: Record<string, SettingValue>,
  regions?: RegionSelectionRequest,
): CreateEnvironmentRequest {
  if (fields.length > MAX_SETTING_FIELDS) throw new Error('設定項目が多すぎます。')
  const errors = fieldErrors(fields, values)
  const first = Object.values(errors)[0]
  if (first) throw new Error(first)
  const settings: Record<string, SettingValue> = {}
  // Unchanged initial values already belong to the trusted server template.
  // Do not turn them into redundant overrides/provenance records.
  for (const field of fields) if (values[field.path] !== undefined
    && (field.required || values[field.path] !== field.default)) settings[field.path] = values[field.path]
  const request: CreateEnvironmentRequest = selection.mode === 'free'
    ? { mode: 'free', idle_noise_mode: selection.noiseMode, settings, ...(regions ? { regions } : {}) }
    : { mode: 'challenge', preset_id: selection.challengeId, settings }
  if (utf8Bytes(JSON.stringify(request)) > 64 * 1024) throw new Error('設定の送信内容が大きすぎます。')
  return request
}

/** Parse the same duration grammar as the terminal `wait` (D60): 250us, 1.5ms, 2s. */
export function parseWaitDuration(text: string): number {
  const match = /^\s*(\d+(?:\.\d+)?)\s*(us|µs|ms|s)\s*$/.exec(text)
  if (!match) throw new Error('待機時間は 250us、1.5ms、2s のように単位付きで入力してください。')
  const scale = match[2] === 's' ? 1_000_000 : match[2] === 'ms' ? 1_000 : 1
  const value = Number(match[1]) * scale
  if (!Number.isFinite(value) || value <= 0) throw new Error('待機時間は正の値にしてください。')
  return value
}

export function formatWorldTime(us: number): string {
  if (!Number.isFinite(us)) return '—'
  if (us >= 1_000_000) return `${(us / 1_000_000).toFixed(6)} s`
  if (us >= 1_000) return `${(us / 1_000).toFixed(3)} ms`
  return `${us.toFixed(3)} µs`
}

/** Remove a request once its reply has been delivered. Order is preserved. */
export function settleRequest(pending: LiveRequest[], reply: LiveReply): LiveRequest[] {
  return pending.filter((request) => request.requestId !== reply.requestId)
}

const ERROR_MESSAGES: Record<string, string> = {
  JOB_NOT_FOUND: 'ジョブが見つかりません。',
  OPERATION_DENIED: 'この操作は許可されていません。',
  RESULT_NOT_READY: '結果はまだ公開されていません。待機してから照会してください。',
  CANCELLATION_REJECTED: '実行が始まっていたため取消できませんでした。',
  INVALID_PROGRAM: '回路または送信条件がサービスの条件を満たしていません。',
  HOST_CAPACITY_REACHED: 'この環境で受け付けられるジョブ数の上限に達しました。待機中のジョブが減ってから送信してください。',
  JOB_EXPIRED: 'ジョブの記録は保持期間を過ぎたため破棄されました。新しいジョブとして送信し直してください。',
}

/* サービスが INVALID_PROGRAM で返す、満たされなかった条件（英語の固定文）を日本語にする。 */
const REJECTION_PATTERNS: [RegExp, (match: RegExpExecArray) => string][] = [
  [/^shots exceed the service limit: (\d+) > (\d+)$/, (m) => `shots が上限を超えています（${m[1]} > 上限 ${m[2]}）`],
  [/^program exceeds the service instruction-work limit: (\d+) shots x (\d+) instructions > (\d+)$/,
    (m) => `shots × 命令数が上限を超えています（${m[1]} × ${m[2]} = ${Number(m[1]) * Number(m[2])} > 上限 ${m[3]}）。shots を減らすか回路を短くしてください`],
  [/^circuit register exceeds service capabilities: (\d+) qubits > (\d+)$/, (m) => `回路の qubit 数が上限を超えています（${m[1]} > 上限 ${m[2]}）`],
  [/^circuit register exceeds service capabilities: no classical bit$/, () => '古典bitがありません。測定結果を書き込む古典bitが必要です'],
  [/^gate or classical condition is not supported by this service: (\S+)$/, (m) => `このサービスが対応していないゲート、または古典条件です（${m[1]}）`],
  [/^initialization is not supported by this service$/, () => '指定した初期化方式（--init）はこのサービスで使えません'],
  [/^interleaved runs require the shared same-region placement.*$/, () => '交互観測（--interleave）は、両者が同じ qubit を共有する配置でだけ使えます。配置の方針を「同じ領域」にしてください'],
  [/^observation budget: (shots|training_queries|victim_observations|retries) is outside the observation budget$/,
    (m) => ({
      shots: '1回の観測で繰り返せる回数の上限を超えています（設定の「1回の観測で攻撃者が繰り返せる回数」）',
      training_queries: '参照ラベル付きの観測回数の上限に達しました',
      victim_observations: '他の利用者の観測回数の上限に達しました',
      retries: '再試行回数の上限に達しました',
    }[m[1]] ?? '観測回数の上限に達しました')],
  [/^interleaved run needs up to (\d+) physical events; this environment allows (\d+)$/,
    (m) => `1回の観測が長すぎます（物理イベント ${m[1]} > 上限 ${m[2]}）。shots を減らしてください`],
  [/^this scenario does not provide that interleaved victim$/, () => '指定した観測対象（--interleave）はこの環境にありません'],
  [/^compiled unitary storage exceeds the service limit$/, () => '回路が大きすぎて、サービスが扱える行列サイズを超えています'],
  [/^gate has no declared service duration$/, () => 'サービスがゲート時間を宣言していないゲートです'],
  [/^CZ edge has no eligible snapshot calibration$/, () => 'CZ を掛けた2つの qubit が、較正済みの結合ではありません'],
  [/^CZ requires exactly two physical operands$/, () => 'CZ には qubit がちょうど2つ必要です'],
  [/^this service supports CNOT with one control and one target$/, () => 'CNOT は制御1つ・標的1つだけ対応しています'],
  [/^CNOT control and target must be different$/, () => 'CNOT の制御と標的は別の qubit にしてください'],
  [/^gate parameters are not supported by this service$/, () => 'このゲートに指定したパラメータはサービスが対応していません'],
  [/^sampling measurements need classical targets$/, () => '測定には書き込み先の古典bitが必要です'],
  [/^unknown circuit configuration field$/, () => '回路の形式が正しくありません（未知の項目があります）'],
  [/^invalid live program envelope$/, () => '送信内容の形式が正しくありません'],
]

/** reply の value に載った拒否理由。日本語化できないものは原文を括弧つきで示す。 */
export function describeRejectionReason(value: unknown): string | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const reason = (value as Record<string, unknown>).reason
  if (typeof reason !== 'string' || !reason) return null
  for (const [pattern, describe] of REJECTION_PATTERNS) {
    const match = pattern.exec(reason)
    if (match) return describe(match)
  }
  return `サービスの条件を満たしていません（${reason}）`
}

export function describeReplyError(code: string | null): string | null {
  if (code === null) return null
  return ERROR_MESSAGES[code] ?? 'サービスが要求を拒否しました。'
}
