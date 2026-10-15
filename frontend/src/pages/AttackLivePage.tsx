import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { AttackCircuitEditor } from '../components/AttackCircuitEditor'
import { IdeViewToggles, IdeWorkbench } from '../components/IdeWorkbench'
import { useIdeLayout } from '../hooks/useIdeLayout'
import { createAttackEnvironmentClient } from '../clients/attackLiveClient'
import { useCircuitContext } from '../context/useCircuitContext'
import { useTutorial } from '../context/useTutorial'
import type { GateDurationDefaults } from '../types/simulation'
import type { AttackEnvironmentClient, ChallengeEntry, DeviceSnapshotInitialValues, InterleaveReference, LiveReply, RegionSelectionCatalog, SettingFieldDescriptor, SettingValue, StandardInitialValues } from '../types/attackLive'
import {
  assignMeasurementTargets, availableNoiseModes, buildCreateRequest, describeReplyError, fieldErrors, fieldsFor,
  describeRejectionReason, formatWorldTime, initialValues, applyServiceGateDurations, placementChoiceLabel,
  regionNoiseFields, setupCategoryForPath, countChangedSettings, firstErrorCategory,
  type SetupCategoryId, type SetupSelection,
} from '../utils/attackLiveSettings'
import { createLiveWorkspace } from '../utils/attackLiveWorkspace'
import { validateAttackProgramShape } from '../utils/attackProgramValidation'
import { parseTerminalCommand, TERMINAL_HELP } from '../utils/attackTerminal'
import { canEditQubitNoise, graphEdges, regionDefaults, regionSelectionRequest, replaceRegionQubit, type RegionOwner } from '../utils/attackLiveTopology'
import { exportCircuitConfigBundleJson } from '../utils/circuitConfigTransfer'
import { AttackScriptEditor } from '../components/AttackScriptEditor'
import { VictimActivityPanel } from '../components/VictimActivityPanel'
import { describeVictimActivity, parseVictimActivity } from '../utils/victimActivity'
import { browserStorage, defaultScriptFiles, loadScriptFiles, saveScriptFiles, type ScriptFiles } from '../scripting/scriptFiles'
import { EXIT_KILLED, scriptRuntimeSupport, type RunnerPhase, type ScriptRunner } from '../scripting/scriptRunner'
import { createScriptServices } from '../scripting/scriptServices'
import { IBM_KINGSTON_DISPLAY_POSITIONS, IBM_KINGSTON_GRAPH_VIEWBOX, IBM_KINGSTON_LAYOUT_NOTICE } from '../data/ibmKingstonGraphLayout'
import '../components/IdeLayout.css'
import './AttackLivePage.css'

const CONNECTION_LABELS = {
  idle: '未成立', connecting: '接続中', open: '接続済み', closed: '切断', failed: '接続失敗',
}
const REQUEST_LABELS: Record<string, string> = {
  submit: '回路送信', status: '状態照会', result: '結果照会', cancel: '取消',
  victim_activity: '被害者の動きの照会', activity: '被害者の動きの照会',
}
const SCRIPT_PHASE_LABELS: Record<RunnerPhase, string> = {
  idle: '', loading: 'Python を読み込み中', packages: 'パッケージを準備中', running: 'スクリプト実行中', stopping: '停止中',
}
const PLACEMENT_PATH = 'qpu_service.runtime_execution.placement_policy'
const DEFAULT_SHOTS = 32
const MAX_SHOTS = 1024

/* 送信する回路の測定先を q0→c0, q1→c1 の形で並べる。 */
function describeMeasurementTargets(bundle: ReturnType<typeof assignMeasurementTargets>): string {
  const pairs = bundle.circuit_config.columns.flatMap((column) => column.gates
    .filter((gate) => gate.type === 'MEASURE')
    .flatMap((gate) => gate.targets.map((qubit, index) => `q${qubit}→c${gate.classical_targets?.[index] ?? '?'}`)))
  return pairs.length ? pairs.join(', ') : '測定なし'
}
const SETUP_CATEGORIES: { id: SetupCategoryId; label: string }[] = [
  { id: 'mode', label: 'モード' }, { id: 'qpu', label: '仮想QPU' },
  { id: 'noise', label: '待機ノイズ' }, { id: 'service', label: 'サービスと時間' },
  { id: 'review', label: '確認して作成' },
]

function SettingInput({ field, value, error, disabled, onChange }: {
  field: SettingFieldDescriptor; value: SettingValue | undefined; error?: string; disabled: boolean
  onChange: (value: SettingValue | undefined) => void
}) {
  const id = `attack-setting-${field.path}`
  return <div className="attack-live__field">
    <label htmlFor={id}>{field.label}{field.unit && <span> ({field.unit})</span>}{field.required && ' · 必須'}</label>
    {field.type === 'boolean'
      ? <input id={id} type="checkbox" checked={value === true} disabled={disabled}
        onChange={(event) => onChange(event.target.checked)} aria-describedby={`${id}-basis`} />
      : field.type === 'choice'
        ? <select id={id} value={value === undefined ? '' : String(value)} disabled={disabled}
          onChange={(event) => onChange(event.target.value)} aria-invalid={!!error} aria-describedby={`${id}-basis ${id}-error`}>
          <option value="">選択してください</option>
          {field.choices?.map((choice) => <option key={choice} value={choice}>{placementChoiceLabel(field, choice)}</option>)}
        </select>
        : <input id={id} type={field.type === 'string' ? 'text' : 'number'}
          value={value === undefined || (typeof value === 'number' && !Number.isFinite(value)) ? '' : String(value)}
          min={field.minimum} max={field.maximum} step={field.type === 'integer' ? 1 : 'any'}
          disabled={disabled} aria-invalid={!!error} aria-describedby={`${id}-basis ${id}-error`}
          onChange={(event) => onChange(event.target.value === '' ? undefined
            : field.type === 'string' ? event.target.value : event.target.valueAsNumber)} />}
    <details id={`${id}-basis`} className="attack-live__basis"><summary>根拠</summary>
      <small>{value !== undefined && value !== field.default && '変更値はユーザー指定。初期値の根拠: '}{field.basis}</small>
    </details>
    {field.path === PLACEMENT_PATH && <small>{field.description} 現実のクラウドにおける配置保証ではありません。</small>}
    {error && <span id={`${id}-error`} className="attack-live__error" role="alert">{error}</span>}
  </div>
}

export function AttackSnapshotInfo({ standard }: { standard: StandardInitialValues }) {
  return <aside className="attack-live__source"><strong>{standard.device}</strong>
    <p>ある時点の較正値です。現在の実機の代表値ではありません。IBMの公開データを値として使っているだけで、IBMの装置には接続しません。</p>
    <small>出所: {standard.source}</small>
    {'snapshot_date' in standard ? <>
      <small>時点: {standard.snapshot_date} · {standard.qubit_count} qubit / {standard.undirected_edge_count}結合</small>
      <small>保持領域: {standard.active_region_initial.join(', ')}（メモリ制約の上限{standard.active_region_limit} qubit）</small>
    </> : <small>時点: {standard.calibrated_at} · qubit {standard.physical_qubit_id} · {standard.scope_notice}</small>}
    <a href="https://yuragi-strider.23sam55781.workers.dev/docs/limitations/attack-simulator" target="_blank" rel="noopener noreferrer">攻撃シミュレーターの前提と限界</a>
  </aside>
}

/** 配置できないqubitとその理由。仮想QPUの設定欄に置く。 */
export function AttackUnusableQubits({ standard }: { standard: StandardInitialValues }) {
  if (!('snapshot_date' in standard)) return null
  return <details className="attack-live__unusable"><summary>使えないqubit · 配置対象外</summary>
    <small>読み出し誤差&gt;{standard.eligibility_policy.readout_error_exclude_above * 100}%、またはCZ誤差≥{standard.eligibility_policy.cz_error_exclude_at_or_above}。必須較正値の欠落も除外します。</small>
    <ul>{Object.entries(standard.qubits).filter(([, item]) => !item.usable_for_placement).map(([id, item]) => <li key={id}>
      <strong>qubit {id}</strong> — {item.unusable_reasons.map((reason) => placementReason(reason)).join('、')}
    </li>)}</ul>
  </details>
}

export function AttackModeNotice() {
  return <p className="attack-live__mode-note">※この画面の量子コンピューター・被害者・サービスは、すべてこのPCの中の仮想環境です。実機やクラウドサービスには一切接続しません。</p>
}

export function AttackCredentialInfo() {
  return <p className="attack-live__credential-note"><strong>仮想資格情報</strong>：シミュレーター内でだけ通用する仮の資格情報です。実在のAPIキーや秘密は入力しないでください。</p>
}

function placementReason(reason: string) {
  if (reason === 'readout_error_above_threshold') return '読み出し誤差が閾値超'
  if (reason === 'missing_t1_or_t2') return 'T1/T2較正値なし'
  if (reason === 'missing_measure_or_reset_duration') return '測定・Resetの所要時間なし'
  const pair = /^attached_to_cz_error_at_or_above_threshold:(\d+-\d+)$/.exec(reason)
  return pair ? `CZ誤差が閾値以上（結合${pair[1]}）` : reason
}

export function AttackQpuTopology({ standard, regionCatalog, placement, noiseMode, regions, setRegions,
  owner, setOwner, selectedQubit, setSelectedQubit, editable, onNotice, onNoiseQubitSelect }: {
  standard: DeviceSnapshotInitialValues; regionCatalog?: RegionSelectionCatalog
  placement: string; noiseMode: string
  regions: { shared: number[]; victim: number[]; attacker: number[] }
  setRegions: (change: (previous: { shared: number[]; victim: number[]; attacker: number[] }) => { shared: number[]; victim: number[]; attacker: number[] }) => void
  owner: RegionOwner; setOwner: (owner: RegionOwner) => void
  selectedQubit: number | null; setSelectedQubit: (qid: number | null) => void
  onNoiseQubitSelect: (qid: number) => void
  editable: boolean; onNotice: (message: string) => void
}) {
  const [interaction, setInteraction] = useState<'region' | 'noise'>('region')
  const [pendingAdd, setPendingAdd] = useState<number | null>(null)
  const dedicated = placement === 'tenant_dedicated_region'
  const activeOwner: RegionOwner = dedicated ? owner === 'attacker' ? 'attacker' : 'victim' : 'shared'
  const activeRegion = activeOwner === 'shared' ? regions.shared : regions[activeOwner]
  const otherRegion = dedicated ? regions[activeOwner === 'victim' ? 'attacker' : 'victim'] : []
  const edges = useMemo(() => graphEdges(standard), [standard])
  const eligible = useMemo(() => new Set(regionCatalog?.eligible_qubit_ids ?? []), [regionCatalog])
  const statusFor = (qid: number) => {
    if (!standard.qubits[String(qid)]?.usable_for_placement) return 'unusable'
    if (activeRegion.includes(qid)) return 'active'
    if (otherRegion.includes(qid)) return 'other'
    return 'available'
  }
  function swapWith(remove: number, add: number) {
    const result = replaceRegionQubit(activeRegion, add, remove, edges, eligible)
    if (!result.ok) { onNotice(result.reason); return }
    if (dedicated && result.ids.some((qid) => otherRegion.includes(qid))) {
      onNotice('利用者ごとの領域は重ねられません。'); return
    }
    setRegions((previous) => activeOwner === 'shared'
      ? { ...previous, shared: result.ids }
      : { ...previous, [activeOwner]: result.ids })
    setSelectedQubit(add); setPendingAdd(null)
    onNotice(`qubit ${add}を領域へ追加し、qubit ${remove}と入れ替えました。`)
  }
  function clickQubit(qid: number) {
    const qubit = standard.qubits[String(qid)]
    if (!qubit?.usable_for_placement) {
      onNotice(`qubit ${qid}は選択できません: ${qubit?.unusable_reasons.map(placementReason).join('、') || '較正情報がありません。'}`)
      setSelectedQubit(qid); return
    }
    if (interaction === 'noise') {
      setSelectedQubit(qid); setPendingAdd(null)
      if (statusFor(qid) === 'active') onNoiseQubitSelect(qid)
      else onNotice('選択中の黄色いqubitだけ編集できます。')
      return
    }
    if (!editable) { onNotice('課題presetが領域を固定しています。'); return }
    if (statusFor(qid) === 'active') {
      if (pendingAdd !== null) swapWith(qid, pendingAdd)
      else setSelectedQubit(qid)
      return
    }
    if (statusFor(qid) === 'other') { onNotice('別利用者の領域はこの操作から変更できません。'); return }
    setPendingAdd(qid); setSelectedQubit(qid)
    onNotice(`qubit ${qid}を加えるには、外す黄色のqubitを選んでください。`)
  }
  function regionRequestPreview() {
    return regionSelectionRequest(placement, regions)
  }
  return <section className="attack-live__panel attack-live__topology" aria-label="仮想QPU topology">
    <div className="attack-live__topology-heading"><div><h2>仮想QPU · ibm_kingston</h2>
      <p>{standard.qubit_count} qubit / {standard.undirected_edge_count} CZ結合 · 保持領域上限 {regionCatalog?.max_qubits ?? standard.active_region_limit} qubit</p></div>
      <span className="attack-live__layout-notice">{regionCatalog?.display_layout_notice ?? IBM_KINGSTON_LAYOUT_NOTICE}</span></div>
    <p className="attack-live__muted">{regionCatalog?.region_notice ?? '作成時にサーバーが領域を検査します。'}</p>
    <div className="attack-live__topology-toolbar">
      <div role="group" aria-label="操作">
        <button type="button" aria-pressed={interaction === 'region'} onClick={() => { setInteraction('region'); setPendingAdd(null) }}>領域の選択</button>
        <button type="button" aria-pressed={interaction === 'noise'} disabled={noiseMode !== 'calibration_values'} onClick={() => { setInteraction('noise'); setPendingAdd(null) }}>qubitノイズ</button>
      </div>
      {dedicated && <div role="group" aria-label="編集する利用者の領域">
        <button type="button" aria-pressed={activeOwner === 'victim'} onClick={() => { setOwner('victim'); setPendingAdd(null); setSelectedQubit(null) }}>被害者領域</button>
        <button type="button" aria-pressed={activeOwner === 'attacker'} onClick={() => { setOwner('attacker'); setPendingAdd(null); setSelectedQubit(null) }}>攻撃者領域</button>
      </div>}
    </div>
    <div className="attack-live__topology-legend" aria-label="状態の凡例">
      <span><i className="is-available" />使用可能</span><span><i className="is-unusable">×</i>使用不可</span>
      <span><i className="is-active" />選択中</span>{dedicated && <span><i className="is-other" />もう一方の利用者</span>}
    </div>
    <div className="attack-live__graph-scroll">
      <svg className="attack-live__graph" viewBox={IBM_KINGSTON_GRAPH_VIEWBOX} role="group"
        aria-label="156 qubitと176 CZ結合の仮想QPUグラフ">
        {edges.map(([left, right]) => {
          const a = IBM_KINGSTON_DISPLAY_POSITIONS[left], b = IBM_KINGSTON_DISPLAY_POSITIONS[right]
          return <line key={`${left}:${right}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className="attack-live__edge" />
        })}
        {Object.entries(standard.qubits).map(([rawId, calibration]) => {
          const qid = Number(rawId), point = IBM_KINGSTON_DISPLAY_POSITIONS[qid]
          if (!point) return null
          const status = statusFor(qid), selected = selectedQubit === qid
          const title = !calibration.usable_for_placement
            ? `qubit ${qid} · 使用不可 · ${calibration.unusable_reasons.map(placementReason).join('、')}`
            : `qubit ${qid} · ${status === 'active' ? '選択中の領域' : status === 'other' ? 'もう一方の利用者領域' : '使用可能'}`
          return <g key={qid} className={`attack-live__node is-${status}${selected ? ' is-selected' : ''}`}
            role="button" tabIndex={0} aria-label={title} onClick={() => clickQubit(qid)}
            onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); clickQubit(qid) } }}>
            <title>{title}</title>
            {status === 'other' && <circle cx={point.x} cy={point.y} r="25" className="attack-live__tenant-ring" />}
            <circle cx={point.x} cy={point.y} r={selected ? 22 : 19} />
            {status === 'unusable' && <path d={`M ${point.x-8} ${point.y-8} L ${point.x+8} ${point.y+8} M ${point.x+8} ${point.y-8} L ${point.x-8} ${point.y+8}`} className="attack-live__cross" />}
            <text x={point.x} y={point.y+4}>{qid}</text>
          </g>
        })}
      </svg>
    </div>
    {pendingAdd !== null && <div className="attack-live__region-swap" role="group" aria-label="入れ替える領域qubitを選択">
      <strong>qubit {pendingAdd}を追加するため、外す黄色のqubitを選ぶ</strong>
      {activeRegion.map((qid) => <button type="button" key={qid} onClick={() => swapWith(qid, pendingAdd)}>qubit {qid}を外す</button>)}
      <button type="button" onClick={() => setPendingAdd(null)}>取消</button>
    </div>}
    <AttackUnusableQubits standard={standard as StandardInitialValues} />
    <p className="attack-live__muted">選択領域: {activeRegion.join(', ') || '未選択'} · この一覧をcreate要求に含め、server resolverが再検査します。</p>
    {dedicated && <p className="attack-live__muted">被害者: {regions.victim.join(', ')} ／ 攻撃者: {regions.attacker.join(', ')} · 二領域は非重複です。</p>}
    <code className="attack-live__region-preview">送信する領域設定: {JSON.stringify(regionRequestPreview())}</code>
  </section>
}

export function AttackQubitNoiseEditor({ standard, regions, placement, fields, values, setValues,
  errors, setErrors, noiseMode, selectedQubit, setSelectedQubit, owner, setOwner, editable }: {
  standard: DeviceSnapshotInitialValues; regions: { shared: number[]; victim: number[]; attacker: number[] }
  placement: string; fields: SettingFieldDescriptor[]; values: Record<string, SettingValue>
  setValues: (change: (previous: Record<string, SettingValue>) => Record<string, SettingValue>) => void
  errors: Record<string, string>; setErrors: (change: (previous: Record<string, string>) => Record<string, string>) => void
  noiseMode: string; selectedQubit: number | null; setSelectedQubit: (qid: number | null) => void
  owner: RegionOwner; setOwner: (owner: RegionOwner) => void; editable: boolean
}) {
  if (noiseMode !== 'calibration_values' || !('qubits' in standard)) {
    return <p className="attack-live__muted">物理パラメタ方式ではqubit別のT1・T2編集はありません。</p>
  }
  const dedicated = placement === 'tenant_dedicated_region'
  const activeOwner: RegionOwner = dedicated ? owner === 'attacker' ? 'attacker' : 'victim' : 'shared'
  const activeRegion = activeOwner === 'shared' ? regions.shared : regions[activeOwner]
  const calibration = selectedQubit === null ? null : standard.qubits[String(selectedQubit)] ?? null
  const canEdit = selectedQubit !== null && canEditQubitNoise(noiseMode, selectedQubit, activeOwner, regions)
    && calibration?.usable_for_placement === true && editable
  const activeFields = fields.filter((field) => field.path.includes(`.qubits.${selectedQubit}.`))
  return <section className="attack-live__panel attack-live__noise-editor" aria-label="qubit別待機ノイズ">
    {dedicated && <div role="group" aria-label="編集する利用者のqubit領域">
      <button type="button" aria-pressed={activeOwner === 'victim'} onClick={() => { setOwner('victim'); setSelectedQubit(null) }}>被害者領域</button>
      <button type="button" aria-pressed={activeOwner === 'attacker'} onClick={() => { setOwner('attacker'); setSelectedQubit(null) }}>攻撃者領域</button>
    </div>}
    <label htmlFor="attack-noise-qubit">編集する領域qubit</label>
    <select id="attack-noise-qubit" value={selectedQubit ?? ''} disabled={!editable}
      onChange={(event) => setSelectedQubit(event.target.value === '' ? null : Number(event.target.value))}>
      <option value="">qubitを選択</option>
      {activeRegion.map((qid) => <option key={qid} value={qid}>qubit {qid}</option>)}
    </select>
    {selectedQubit !== null && calibration && <>
      <h3>qubit {selectedQubit} · 待機ノイズ</h3>
      <details className="attack-live__basis"><summary>根拠</summary>
        <small>{calibration.basis} · ある時点の値で、現在の実機の代表値ではありません。</small>
      </details>
      <dl className="attack-live__noise-values">
        <div><dt>T1</dt><dd>{typeof calibration.T1_us !== 'number' ? '較正値なし' : `${calibration.T1_us} µs`}</dd></div>
        <div><dt>T2</dt><dd>{typeof calibration.T2_us !== 'number' ? '較正値なし' : `${calibration.T2_us} µs`}</dd></div>
        <div><dt>平衡励起状態占有率</dt><dd>0（変更可能なモデル仮定）</dd></div>
      </dl>
      {!canEdit && <p className="attack-live__muted">選択中の黄色いqubitだけ編集できます。</p>}
      {activeFields.map((field) => <SettingInput key={field.path} field={field}
        value={values[field.path] ?? field.default} error={errors[field.path]} disabled={!canEdit}
        onChange={(value) => {
          setValues((previous) => { const next = { ...previous }; if (value === undefined) delete next[field.path]; else next[field.path] = value; return next })
          setErrors((previous) => { const next = { ...previous }; delete next[field.path]; return next })
        }} />)}
    </>}
  </section>
}

type TerminalLine = { id: number; kind: 'input' | 'output' | 'error'; text: string }
let terminalLineId = 0
const TERMINAL_LIMIT = 500
function terminalLines(kind: TerminalLine['kind'], texts: string[]): TerminalLine[] {
  return texts.map((text) => ({ id: (terminalLineId += 1), kind, text }))
}

function replyLines(reply: LiveReply): TerminalLine[] {
  const error = describeReplyError(reply.errorCode)
  const jobId = jobIdFor(reply)
  const reason = describeRejectionReason(reply.value)
  const activity = error ? null : parseVictimActivity(reply.value)
  if (activity) return terminalLines('output', [`reply ${reply.requestId}:`, ...describeVictimActivity(activity)])
  return [
    ...terminalLines(error ? 'error' : 'output', [`reply ${reply.requestId}: ${error ?? JSON.stringify(reply.value)}`]),
    ...(error && reason ? terminalLines('error', [`  満たされていない条件: ${reason}`]) : []),
    ...(jobId ? terminalLines('output', [`  ジョブID ${jobId}（status / result / cancel で操作）`]) : []),
  ]
}

function jobIdFor(reply: LiveReply): string | null {
  const value = reply.value
  return typeof value === 'object' && value !== null && 'job_id' in value
    && typeof value.job_id === 'string' ? value.job_id : null
}

/** D60–D67 browser workspace. All environment actions use the existing client. */
export function AttackLivePage({ gateDurationDefaults, client, active = true }: {
  gateDurationDefaults: GateDurationDefaults; client?: AttackEnvironmentClient; active?: boolean
}) {
  const editor = useCircuitContext()
  const [workspace] = useState(() => createLiveWorkspace(client ?? createAttackEnvironmentClient()))
  const state = useSyncExternalStore(workspace.subscribe, workspace.getSnapshot, workspace.getSnapshot)
  const [selection, setSelection] = useState<SetupSelection | null>(null)
  const [values, setValues] = useState<Record<string, SettingValue>>({})
  const [regions, setRegions] = useState(() => ({ shared: [] as number[], victim: [] as number[], attacker: [] as number[] }))
  const [regionOwner, setRegionOwner] = useState<RegionOwner>('shared')
  const [selectedNoiseQubit, setSelectedNoiseQubit] = useState<number | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [localNotice, setLocalNotice] = useState('')
  const [localWarning, setLocalWarning] = useState(false)
  const showNotice = (text: string, warning = false) => { setLocalNotice(text); setLocalWarning(warning) }
  const [tab, setTab] = useState<'setup' | 'workspace'>('setup')
  /* 今の環境をどの課題プリセットで作ったか。チュートリアルが「案内する課題の環境か」を見分けるのに使う。 */
  const [environmentPreset, setEnvironmentPreset] = useState<string | null>(null)
  const tutorial = useTutorial()
  const [messagesTab, setMessagesTab] = useState<'terminal' | 'pending' | 'activity'>('terminal')
  const ideLayout = useIdeLayout('attack')
  const [setupCategory, setSetupCategory] = useState<SetupCategoryId>('mode')
  const [jobId, setJobId] = useState('')
  const [replacementId, setReplacementId] = useState('')
  const [terminal, setTerminal] = useState<TerminalLine[]>([])
  const [commandInput, setCommandInput] = useState('')
  const [commandHistory, setCommandHistory] = useState<string[]>([])
  const [historyIndex, setHistoryIndex] = useState<number | null>(null)
  const terminalLogRef = useRef<HTMLDivElement>(null)
  const echoedReplies = useRef(new Set<string>())
  const [editorView, setEditorView] = useState<'circuit' | 'script'>('circuit')
  /* 保存が無ければ、実行環境の読み込み後に最初のファイル（SDKのサンプルを含む）を置く。 */
  const [storedScriptFiles, setScriptFiles] = useState<ScriptFiles | null>(() => loadScriptFiles(browserStorage()))
  const [scriptsSaved, setScriptsSaved] = useState(true)
  const [scriptPhase, setScriptPhase] = useState<RunnerPhase>('idle')
  /* input() の待ち。文字列は input() に渡された prompt（待っていなければ null）。 */
  const [scriptInputPrompt, setScriptInputPrompt] = useState<string | null>(null)
  const scriptWaitingInput = scriptInputPrompt !== null
  const [scriptSupport] = useState(() => scriptRuntimeSupport())
  const [scriptRuntime, setScriptRuntime] = useState<{ runner: ScriptRunner; starter: Record<string, string> } | null>(null)
  const runner = scriptRuntime?.runner ?? null
  const scriptFiles = storedScriptFiles ?? (scriptRuntime ? defaultScriptFiles(scriptRuntime.starter) : null)
  const scriptRunning = scriptPhase !== 'idle'
  const scriptUnavailable = scriptSupport.ok ? null : scriptSupport.reason
  const busy = state.busy !== null
  /* 最後に読んだ「被害者の動き」。いまのジョブのものを優先する。 */
  const activities = state.replies.map((reply) => (reply.errorCode ? null : parseVictimActivity(reply.value))).filter((item) => item !== null)
  const latestActivity = activities.findLast((item) => item.job_id === jobId) ?? activities.at(-1) ?? null
  const locked = busy || state.environment !== null
  const connected = state.connectionState === 'open'
  const catalog = state.catalog
  const fields = catalog && selection ? fieldsFor(catalog, selection) : []
  const standard = catalog?.free_mode.standard_initial_values
  const standardSnapshot = standard && 'qubits' in standard ? standard : undefined
  const selectedChallenge = selection?.mode === 'challenge'
    ? catalog?.challenges.find((item) => item.id === selection.challengeId) : undefined
  const challengeRegion = selectedChallenge?.region_selection
  const idleMode = selection?.mode === 'free'
    ? catalog?.free_mode.idle_noise_modes.find((item) => item.id === selection.noiseMode) : undefined
  const regionCatalog = idleMode?.region_selection ?? (selection?.mode === 'challenge'
    ? catalog?.free_mode.idle_noise_modes.find((item) => item.id === 'calibration_values')?.region_selection
    : undefined)
  const placementField = fields.find((field) => field.path === PLACEMENT_PATH)
  const placement = selection?.mode === 'challenge'
    ? challengeRegion?.placement_policy ?? 'guaranteed_same_physical_qubit'
    : String(values[PLACEMENT_PATH] ?? placementField?.default ?? 'guaranteed_same_physical_qubit')
  const qbitFieldPath = /^qpu_service\.(?:dedicated_region\.)?idle_dynamics\.qubits\.\d+\./
  const nonQubitFields = fields.filter((field) => !qbitFieldPath.test(field.path))
  const scopedNoiseFields = selection?.mode === 'free'
    ? regionNoiseFields(fields, regionCatalog, standard, regions, placement, selection.noiseMode) : []
  /*
   * regionNoiseFields は qubit 以外の項目も含めて返すので、そのまま連結すると
   * 較正値モードで同じ項目が二重に並ぶ。パスで一つにまとめる。
   */
  const visibleFields = selection?.mode === 'challenge' ? fields
    : [...new Map([...nonQubitFields, ...scopedNoiseFields].map((field) => [field.path, field])).values()]
  const placementFields = visibleFields.filter((field) => field.path === PLACEMENT_PATH)
  const noiseFields = visibleFields.filter((field) => field.path.includes('.physical_inputs.')
    || qbitFieldPath.test(field.path))
  const serviceFields = visibleFields.filter((field) => !placementFields.includes(field) && !noiseFields.includes(field))
  const deviceId = catalog?.free_mode.standard_initial_values.device ?? ''
  const prompt = `attacker@${deviceId || 'qpu'}:~$`
  const currentTab = state.environment ? tab : 'setup'
  const messagesView = currentTab === 'workspace'
    && (messagesTab === 'terminal' || (messagesTab === 'pending' && state.clockMode === 'explicit_wait')
      || (messagesTab === 'activity' && state.victimActivityAvailable))
    ? messagesTab : 'terminal'
  /* 送信前検査は、測定先を自動で割り当てた（--measure を付けない）submit の回路で行う。 */
  const serviceBundle = useMemo(
    () => applyServiceGateDurations(JSON.parse(exportCircuitConfigBundleJson(editor.circuitState))),
    [editor.circuitState],
  )
  const submissionBundle = useMemo(() => assignMeasurementTargets(serviceBundle), [serviceBundle])
  const preflightIssues = useMemo(() => validateAttackProgramShape(submissionBundle), [submissionBundle])

  /*
   * チュートリアル（Reset課題）への報告。いま満たされているかを毎回伝える。
   * コースの開始（isActive）でも伝え直すので、始める前から満たしていても取りこぼさない。
   */
  const { reportCondition: reportTutorial, isActive: tutorialActive } = tutorial
  const selectedChallengeId = selection?.mode === 'challenge' ? selection.challengeId : null
  useEffect(() => {
    reportTutorial('attack-mode-challenge', selection?.mode === 'challenge')
    reportTutorial('attack-challenge-selected', selectedChallengeId === 'reset-leakage')
    reportTutorial('attack-challenge-env', state.environment?.mode === 'challenge' && environmentPreset === 'reset-leakage')
    reportTutorial('attack-measure-ready', state.environment !== null && preflightIssues.length === 0)
  }, [reportTutorial, tutorialActive, selection?.mode, selectedChallengeId, state.environment, environmentPreset, preflightIssues.length])

  /* 結果の公開は出来事なので、コース開始時点の数を基準にして、増えたときだけ報告する。 */
  const resultReplies = state.replies.filter((reply) => typeof reply.value === 'object' && reply.value !== null && 'counts' in reply.value).length
  const resultBaseline = useRef<number | null>(null)
  useEffect(() => {
    if (!tutorialActive) { resultBaseline.current = null; return }
    if (resultBaseline.current === null) { resultBaseline.current = resultReplies; return }
    if (resultReplies > resultBaseline.current) reportTutorial('attack-result-seen', true)
  }, [tutorialActive, resultReplies, reportTutorial])

  /*
   * ビートが指す画面（環境設定／ワークスペース）へ、ビートが変わった瞬間だけ切り替える。
   * effect で setState すると連鎖して描画し直すので、前のビートを覚えて描画中に判定する。
   * 環境が無いのにワークスペースは開けない。
   */
  const tutorialBeat = tutorial.beat
  const tutorialBeatId = tutorialBeat?.id ?? ''
  const [seenTutorialBeat, setSeenTutorialBeat] = useState('')
  if (seenTutorialBeat !== tutorialBeatId) {
    setSeenTutorialBeat(tutorialBeatId)
    const wanted = tutorialBeat?.attackView
    if (wanted && !(wanted === 'workspace' && state.environment === null)) setTab(wanted)
  }
  const changedFieldCounts = countChangedSettings(visibleFields, values)
  const defaultRegions = standardSnapshot ? regionDefaults(standardSnapshot) : { shared: [], victim: [], attacker: [] }
  const changedItems: { category: SetupCategoryId; label: string; from: string; to: string }[] = []
  if (selection) changedItems.push({ category: 'mode', label: 'モード', from: '未選択',
    to: selection.mode === 'free' ? '自由モード' : `課題: ${(selectedChallenge?.title ?? selection.challengeId) || '未選択'}` })
  if (selection?.mode === 'free' && selection.noiseMode !== 'calibration_values') {
    changedItems.push({ category: 'noise', label: '待機ノイズの方式', from: '実機較正値', to: '物理パラメタ' })
  }
  const regionsChanged = selection?.mode === 'free' && JSON.stringify(regions) !== JSON.stringify(defaultRegions)
  if (regionsChanged) {
    changedItems.push({ category: 'qpu', label: '使用領域', from: '初期領域',
      to: placement === 'tenant_dedicated_region'
        ? `被害者 ${regions.victim.join(', ')} / 攻撃者 ${regions.attacker.join(', ')}`
        : regions.shared.join(', ') })
  }
  for (const field of visibleFields) {
    const value = values[field.path] ?? field.default
    if (value !== field.default) changedItems.push({ category: setupCategoryForPath(field.path),
      label: field.label, from: String(field.default ?? '未設定'), to: String(value ?? '未設定') })
  }
  const errorCounts: Record<SetupCategoryId, number> = { mode: 0, qpu: 0, noise: 0, service: 0, review: 0 }
  for (const path of Object.keys(errors)) errorCounts[setupCategoryForPath(path)] += 1
  errorCounts.review = Object.keys(errors).length
  const changeCounts: Record<SetupCategoryId, number> = { mode: 0, qpu: changedFieldCounts.qpu,
    noise: changedFieldCounts.noise, service: changedFieldCounts.service, review: changedItems.length }
  if (selection) changeCounts.mode = 1
  if (selection?.mode === 'free' && selection.noiseMode !== 'calibration_values') changeCounts.noise += 1
  if (regionsChanged) changeCounts.qpu += 1
  const placementPolicy = placement
  const challengeAvailable = selection?.mode !== 'challenge'
    || !!selectedChallenge?.available

  useEffect(() => {
    if (active) void (async () => {
      if (!workspace.getSnapshot().catalog) await workspace.loadCatalog()
      await workspace.loadOwnedEnvironments()
    })()
  }, [active, workspace])
  useEffect(() => () => workspace.disconnect(), [workspace])
  /* 実行環境（Worker と SDK の取り込み）は後から読む。Python 本体は最初の実行まで読まない。 */
  useEffect(() => {
    let alive = true
    let created: ScriptRunner | null = null
    void import('../scripting/browserScriptRunner').then((module) => {
      if (!alive) return
      created = module.createBrowserScriptRunner({
        output: (stream, text) => setTerminal((previous) => [...previous,
          ...terminalLines(stream === 'stdout' ? 'output' : 'error', [text])].slice(-TERMINAL_LIMIT)),
        phase: (phase) => setScriptPhase(phase),
        inputRequested: (waiting, prompt) => setScriptInputPrompt(waiting ? prompt ?? '' : null),
      })
      setScriptRuntime({ runner: created, starter: module.STARTER_FILES })
    })
    return () => { alive = false; created?.dispose() }
  }, [])
  /* 環境が替わったら、前の環境へつながっているスクリプトは止める。 */
  const environmentKey = state.environment?.environmentId ?? null
  useEffect(() => { if (runner?.running) runner.dispose() }, [environmentKey, runner])
  function changeScriptFiles(next: ScriptFiles) {
    setScriptFiles(next)
    setScriptsSaved(saveScriptFiles(browserStorage(), next))
  }
  useEffect(() => workspace.subscribe(() => {
    const fresh = workspace.getSnapshot().replies.filter((reply) => !echoedReplies.current.has(reply.requestId))
    if (!fresh.length) return
    for (const reply of fresh) echoedReplies.current.add(reply.requestId)
    setTerminal((previous) => [...previous, ...fresh.flatMap(replyLines)].slice(-TERMINAL_LIMIT))
    /* 新しく届いたジョブを操作中のジョブにする。送信直後の status / result がそのジョブを見るように。 */
    const latestJob = [...fresh].reverse().map(jobIdFor).find((value) => value !== null)
    if (latestJob) setJobId(latestJob)
  }), [workspace])
  useLayoutEffect(() => {
    const log = terminalLogRef.current
    if (log) log.scrollTop = log.scrollHeight
  }, [terminal])

  function choose(next: SetupSelection) {
    if (!catalog || locked) return
    setSelection(next)
    setValues(initialValues(fieldsFor(catalog, next)))
    const source = catalog.free_mode.standard_initial_values
    const defaults = 'qubits' in source ? regionDefaults(source) : { shared: [], victim: [], attacker: [] }
    if (next.mode === 'challenge') {
      const presetRegion = catalog.challenges.find((item) => item.id === next.challengeId)?.region_selection
      if (presetRegion?.primary_physical_qubit_ids) {
        const primary = [...presetRegion.primary_physical_qubit_ids]
        const attacker = [...(presetRegion.dedicated_physical_qubit_ids ?? [])]
        setRegions({ shared: primary, victim: primary, attacker })
      } else setRegions(defaults)
    } else setRegions(defaults)
    setErrors({}); showNotice('')
    setRegionOwner('shared'); setSelectedNoiseQubit(null)
  }
  /* 課題の公開パラメーターを自由モードの設定値として読み込む。隠れた値は含まれない。 */
  function reproduceInFree(equivalent: NonNullable<ChallengeEntry['free_equivalent']>) {
    if (!catalog || locked) return
    choose({ mode: 'free', noiseMode: equivalent.idle_noise_mode })
    setValues((previous) => ({ ...previous, ...equivalent.settings }))
    showNotice(`課題の設定を自由モードに読み込みました。課題モード専用の${equivalent.not_reproduced.join('・')}は再現されません。`)
  }
  async function establish() {
    if (!selection || !catalog) return
    const invalid = fieldErrors(visibleFields, values)
    setErrors(invalid); showNotice('')
    if (Object.keys(invalid).length) {
      setSetupCategory(firstErrorCategory(visibleFields, invalid) ?? 'review')
      return
    }
    try {
      const request = buildCreateRequest(selection, visibleFields, values,
        selection.mode === 'free' && regionCatalog ? regionSelectionRequest(placement, regions) : undefined)
      await workspace.establish(request)
      if (workspace.getSnapshot().environment && !workspace.getSnapshot().replacement) {
        setJobId(''); setTab('workspace')
        setEnvironmentPreset(selection.mode === 'challenge' ? selection.challengeId : null)
      }
    } catch (error) {
      showNotice(error instanceof Error ? error.message : '設定を確認してください。', true)
    }
  }
  function print(kind: TerminalLine['kind'], ...texts: string[]) {
    setTerminal((previous) => [...previous, ...terminalLines(kind, texts)].slice(-TERMINAL_LIMIT))
  }
  async function submit(options: { shots?: number; initialization?: 'prepare' | 'inherit'; interleave?: InterleaveReference; measure?: Record<number, number> } = {}) {
    const count = options.shots ?? DEFAULT_SHOTS
    if (count > MAX_SHOTS) { print('error', `ERROR: shots が上限を超えています（${count} > 上限 ${MAX_SHOTS}）。`); return }
    if (!deviceId) { print('error', '設定目録にデバイスがありません。'); return }
    const mode = options.initialization ?? 'prepare'
    /* --measure を付けたときは、その割り当てで回路を作り直して検査し直す。 */
    const bundle = options.measure ? assignMeasurementTargets(serviceBundle, options.measure) : submissionBundle
    const programIssues = options.measure ? validateAttackProgramShape(bundle) : preflightIssues
    if (programIssues.length) print('error', ...programIssues.map((issue) => `ERROR: ${issue.message}`))
    if (programIssues.length) return
    const interleave = options.interleave
    print('output', `回路を送信します（${deviceId} · shots ${count} · ${mode === 'inherit' ? '確定済み状態を継承' : '宣言した初期状態を準備'}）。`,
      `  測定先: ${describeMeasurementTargets(bundle)}`,
      ...(interleave ? [`  各 shot の前に ${interleave === 'victim' ? '他の利用者' : `参照ラベル ${interleave.slice(-1)} の利用者`}のジョブが実行され、サービスのResetを挟んで測定します。`] : []))
    await workspace.submit({ deviceId, shots: count, initialization: mode, circuitConfig: bundle, ...(interleave ? { interleave } : {}) })
    /* チュートリアルへ「受け付けられた観測」を知らせる（拒否されたときの警告は数えない）。 */
    if (interleave && !workspace.getSnapshot().noticeWarning) {
      tutorial.reportCondition(interleave === 'training:0' ? 'attack-ref0-submitted'
        : interleave === 'training:1' ? 'attack-ref1-submitted' : 'attack-victim-submitted', true)
    }
  }
  async function runScript(file: string, argv: string[]) {
    if (scriptUnavailable) { print('error', scriptUnavailable); return }
    if (!runner || !scriptFiles) { print('error', 'スクリプト実行環境を準備しています。少し待ってから実行してください。'); return }
    if (!(file in scriptFiles.files)) {
      print('error', `${file} というファイルはありません（${Object.keys(scriptFiles.files).join('、')}）。`); return
    }
    const port = workspace.scriptPort()
    if (!port) { print('error', '環境がありません。'); return }
    const code = await runner.run({ files: scriptFiles.files, entry: file, argv },
      (readLine) => createScriptServices(port, readLine))
    if (code === EXIT_KILLED) print('error', 'スクリプトを強制停止しました。')
    else if (code !== 0) print('error', `終了コード ${code}`)
  }
  async function runCommand(line: string) {
    /* 実行中のシェルと同じく、打った行はスクリプトの input() へ渡す。 */
    if (runner?.running) {
      if (runner.waitingForInput) { print('input', `${scriptInputPrompt ?? ''}${line}`); runner.provideInput(line) }
      else if (line.trim()) print('error', 'スクリプトの実行中です。止めるには Ctrl+C。')
      return
    }
    const command = parseTerminalCommand(line)
    if (!command) return
    print('input', `${prompt} ${line.trim()}`)
    setCommandHistory((previous) => [...previous, line.trim()].slice(-50))
    setHistoryIndex(null)
    if (command.kind === 'error') { print('error', command.message); return }
    if (command.kind === 'help') { print('output', ...TERMINAL_HELP); return }
    if (command.kind === 'clear') { setTerminal([]); return }
    if (command.kind === 'check') {
      if (preflightIssues.length) print('error', ...preflightIssues.map((issue) => `ERROR: ${issue.message}`))
      else print('output', '回路の測定位置と古典bit割当てを確認しました。')
      return
    }
    if (command.kind === 'pending') {
      if (state.pendingUnknown) print('output', '再読み込み前の未完了要求一覧は取得できません。件数はこの画面で受け付けた分だけです。')
      if (!state.pending.length) print('output', '未完了の要求はありません。')
      else print('output', ...state.pending.map((request) =>
        `${request.requestId}  ${REQUEST_LABELS[request.kind]}  送信時の世界時刻 ${formatWorldTime(request.issuedAtWorldUs)}`))
      return
    }
    if (command.kind === 'python') { await runScript(command.file, command.argv); return }
    if (busy) { print('error', '別の操作を処理中です。終わってから実行してください。'); return }
    if (command.kind === 'connect') {
      if (connected) { print('output', 'すでに接続しています。'); return }
      print('output', 'サービスへ接続します。')
      await workspace.connect()
      print('output', `接続: ${CONNECTION_LABELS[workspace.getSnapshot().connectionState]}`)
      return
    }
    if (command.kind === 'answer') {
      if (state.environment?.mode !== 'challenge') { print('error', 'answer は課題モードの環境でだけ使えます。'); return }
      print('output', 'message' in command.submission ? 'メッセージを提出します。'
        : '判定規則を提出します。見えないケースの観測に数秒かかることがあります。')
      await workspace.submitAnswer(command.submission)
      const answered = workspace.getSnapshot()
      if (!answered.noticeWarning && /^提出 \d+回目/.test(answered.notice)) tutorial.reportCondition('attack-answered', true)
    }
    if (!connected && command.kind !== 'answer') { print('error', '接続されていません。connect で接続してください。'); return }
    if (command.kind === 'submit') await submit(command)
    if (command.kind === 'job') {
      /* 省略時は操作中のジョブ（最後に届いたジョブ、または最後に指定したジョブ）を使う。 */
      if (command.action === 'activity' && !state.victimActivityAvailable) {
        print('error', 'この環境では「被害者の動き」は使えません。自由モードで「抽象コマンド: 被害者の動きの表示を有効にする」をオンにして、環境を作り直してください（課題モードでは使えません）。'); return
      }
      const id = command.jobId ?? jobId
      if (!id) { print('error', 'ジョブIDを指定してください（例: status job-…）。'); return }
      setJobId(id)
      print('output', `${REQUEST_LABELS[command.action]}を要求します（${id}）。`)
      await workspace.job(command.action === 'activity' ? 'victim_activity' : command.action, id)
    }
    if (command.kind === 'wait') {
      if (state.clockMode !== 'explicit_wait') { print('error', 'この環境では世界時刻が実時間で進むため、待機コマンドは使えません。'); return }
      await workspace.wait(command.target === 'any' ? { kind: 'any' }
        : command.target === 'request' ? { kind: 'request', requestId: command.requestId }
          : { kind: 'duration', durationUs: command.durationUs })
    }
    const notice = workspace.getSnapshot().notice
    if (notice) print('output', notice)
  }
  const renderFields = (items: SettingFieldDescriptor[]) => items.map((field) =>
    <SettingInput key={field.path} field={field} value={values[field.path] ?? field.default}
      error={errors[field.path]} disabled={locked} onChange={(value) => {
        setValues((previous) => { const next = { ...previous }; if (value === undefined) delete next[field.path]; else next[field.path] = value; return next })
        setErrors((previous) => { const next = { ...previous }; delete next[field.path]; return next })
      }} />)

  const categoryLabel = SETUP_CATEGORIES.find((category) => category.id === setupCategory)?.label ?? '環境設定'
  const errorEntries = Object.entries(errors)
  const showSnapshot = !!catalog && !!selection && (selection.mode === 'free' || !!selection.challengeId)

  /*
   * Quartus型のIDEレイアウト: 上にタイトルとツールバー、左にナビゲーター、
   * 中央に作業領域、右にプロパティ、下にメッセージ、最下部にステータスバー。
   * 各ペインは内部でスクロールし、画面全体は動かさない。
   */
  return <main className="attack-live ide" data-tab={currentTab}>
    {currentTab === 'workspace' && state.environment && messagesView === 'terminal'
      && <a href="#attack-terminal-input" className="ide__skip" onClick={(event) => {
        event.preventDefault()
        document.getElementById('attack-terminal-input')?.focus()
      }}>ターミナルの入力欄へ移動</a>}
    <header className="ide__titlebar">
      <h1>攻撃シミュレーター</h1>
      <nav className="ide__tabs" aria-label="攻撃シミュレーター画面">
        <button type="button" aria-pressed={currentTab === 'setup'} onClick={() => setTab('setup')}>環境設定</button>
        <button type="button" aria-pressed={currentTab === 'workspace'} disabled={!state.environment}
          title={state.environment ? undefined : '環境を作成すると開けます'} onClick={() => setTab('workspace')}>攻撃者ワークスペース</button>
      </nav>
      <IdeViewToggles controls={ideLayout} omit={['inspector', 'toolbox']} />
      <span className="ide__badge" role="status">{CONNECTION_LABELS[state.connectionState]}</span>
    </header>
    <div className="ide__toolbar" role="toolbar" aria-label="操作">
      {currentTab === 'setup'
        ? <button type="button" disabled={busy || !selection || !challengeAvailable} aria-describedby="attack-next-step" data-tutorial-anchor="attack-create-button" onClick={() => void establish()}>
          {state.environment ? '今の設定で作り直す' : '環境を作成'}</button>
        : null}
      {state.environment && <button type="button" className="ide__danger" disabled={busy} onClick={() => void workspace.discard()}>環境を破棄</button>}
      <button type="button" disabled={tutorial.isActive} title="1 bit課題を、判定ルールの提出まで案内します（約8分）"
        onClick={() => tutorial.start('reset-challenge')}>Reset課題のチュートリアル</button>
      <span id="attack-next-step" className={state.environment ? 'ide__toolbar-note' : 'ide__toolbar-note ide__toolbar-note--next'}>{
        state.environment ? '環境成立 · 前提設定は固定されています。'
          : !selection ? 'まず「モード」でモードを選んでください。選ぶと「環境を作成」が押せます。'
            : !challengeAvailable ? '課題を1つ選んでください。選ぶと「環境を作成」が押せます。'
              : '設定を確かめたら「環境を作成」で始められます。'}</span>
    </div>
    {(localNotice || state.notice) && (() => {
      const warning = localNotice ? localWarning : state.noticeWarning
      return <p className={warning ? 'ide__notice ide__notice--warning' : 'ide__notice'} role={warning ? 'alert' : 'status'}>{localNotice || state.notice}</p>
    })()}
    <IdeWorkbench
      controls={ideLayout}
      forceVisible={{ messages: (currentTab === 'setup' && errorEntries.length > 0)
        || (tutorial.course?.id === 'reset-challenge' && currentTab === 'workspace') }}
      navigator={(
        <aside className="ide__pane ide__navigator" aria-label="ナビゲーター">
          <h2 className="ide__pane-title">{currentTab === 'setup' ? '環境設定' : '環境'}</h2>
          <div className="ide__pane-body">
            {currentTab === 'setup' && <>
              {catalog && <nav className="attack-live__setup-categories" aria-label="環境設定の分類">
                {SETUP_CATEGORIES.map(({ id, label }) => <button key={id} type="button"
                  aria-pressed={setupCategory === id} onClick={() => setSetupCategory(id)}>
                  <span>{label}</span>
                  {(errorCounts[id] > 0 || changeCounts[id] > 0) && <small>{[
                    errorCounts[id] > 0 ? `${errorCounts[id]}件のエラー` : '',
                    changeCounts[id] > 0 ? `${changeCounts[id]}件の変更` : '',
                  ].filter(Boolean).join(' · ')}</small>}
                </button>)}
              </nav>}
              {!state.environment && state.ownedEnvironments.length > 0 && <section className="ide__group" aria-label="所有環境">
                <h3>続きから</h3><p className="attack-live__muted">同じブラウザで成立させた環境へ戻れます。復帰だけでは世界時刻は進みません。</p>
                {state.ownedEnvironments.map((environment) => <div key={environment.environmentId} className="attack-live__row">
                  <span>{environment.mode === 'free' ? '自由モード' : '課題モード'} · {new Date(environment.createdAt).toLocaleString('ja-JP')}</span>
                  <button type="button" disabled={busy} onClick={async () => {
                    await workspace.resume(environment)
                    if (workspace.getSnapshot().environment) setTab('workspace')
                  }}>続きから</button>
                </div>)}
              </section>}
            </>}
            {currentTab === 'workspace' && state.environment && <>
              <details className="attack-live__identity"><summary>環境ID</summary><code>{state.environment.environmentId}</code></details>
              <AttackCredentialInfo />
              <dl className="ide__facts">
                <div><dt>デバイス</dt><dd>{deviceId || '—'}</dd></div>
                <div><dt>モード</dt><dd>{state.environment.mode === 'free' ? '自由モード' : '課題モード'}</dd></div>
              </dl>
              <p className="attack-live__muted">{state.clockMode === 'host_clock'
                ? `世界時刻は現実の経過時間に合わせて進みます。${state.pauseWhileCatchingUp ? '数値計算が追いつくまでの間だけ停止します。' : ''}`
                : '送信・照会・取消では時間は進みません。待機だけで進みます。'}</p>
              <section className="ide__group" aria-label="回路の送信">
                <h3>回路の送信</h3>
                <small>送信はターミナルで <code className="ide__inline-code">submit</code> を実行します。省略した項目は次の値になります。</small>
                <dl className="ide__facts">
                  <div><dt>--shots</dt><dd>{DEFAULT_SHOTS}（上限 {MAX_SHOTS}）</dd></div>
                  <div><dt>--init</dt><dd>prepare</dd></div>
                  <div><dt>--measure</dt><dd>qN → cN</dd></div>
                </dl>
                <small>今の回路の測定先: <code className="ide__inline-code">{describeMeasurementTargets(submissionBundle)}</code></small>
                <small>--measure で指定した qubit はその古典bitへ割り当てます。指定しなかった測定は、エディターの指定、無ければ同番号の古典bitを使います。エディターの回路は変えません。</small>
                <small>ゲート時間はサービスが決めます。エディターのduration指定は送信しません。</small>
                <small>回路幅・命令・shots・状態継承は、サービス条件に従って検査されます。</small>
              </section>
              <section className="ide__group" aria-label="操作中のジョブ">
                <h3>操作中のジョブ</h3>
                <code>{jobId || '—'}</code>
                <small>status / result / cancel でジョブIDを省略すると、このジョブを使います。replyでジョブIDが届くと、そのジョブに切り替わります。</small>
              </section>
            </>}
          </div>
        </aside>
      )}
      editor={(
        <section className="ide__pane ide__editor" aria-label={currentTab === 'setup' ? '設定の編集' : '回路の編集'}>
          {currentTab === 'setup'
            ? <div className="ide__doc-tabs"><span className="ide__doc-tab">{categoryLabel}</span></div>
            : <div className="ide__doc-tabs" role="tablist" aria-label="編集する対象" onKeyDown={(event) => {
              const order = ['circuit', 'script'] as const
              const index = order.indexOf(editorView)
              const next = event.key === 'ArrowRight' ? order[(index + 1) % order.length]
                : event.key === 'ArrowLeft' ? order[(index + order.length - 1) % order.length]
                  : event.key === 'Home' ? order[0] : event.key === 'End' ? order[order.length - 1] : null
              if (!next) return
              event.preventDefault()
              setEditorView(next)
              requestAnimationFrame(() => document.getElementById(`attack-editor-tab-${next}`)?.focus())
            }}>
              {([['circuit', '回路エディター'], ['script', 'スクリプト']] as const).map(([view, label]) =>
                <button key={view} type="button" role="tab" id={`attack-editor-tab-${view}`} aria-selected={editorView === view}
                  aria-controls="attack-editor-panel" tabIndex={editorView === view ? 0 : -1}
                  onClick={() => setEditorView(view)}>{label}</button>)}
            </div>}
          <div className="ide__pane-body" {...(currentTab === 'workspace'
            ? { id: 'attack-editor-panel', role: 'tabpanel', 'aria-labelledby': `attack-editor-tab-${editorView}` } : {})}>
            {currentTab === 'setup' && <>
              {!catalog && <div className="attack-live__panel"><p>{busy ? '設定目録を取得しています…' : '設定目録を取得できません。'}</p>
                <button type="button" disabled={busy} onClick={() => void workspace.loadCatalog()}>設定目録を取得</button></div>}
              {catalog && <>
              {setupCategory === 'mode' && <section className="attack-live__panel" aria-label="モード設定">
                <h2>モード</h2><AttackModeNotice />
                <div className="attack-live__modes" data-tutorial-anchor="attack-mode-cards">
                  <button disabled={locked} aria-pressed={selection?.mode === 'free'} onClick={() => {
                    const mode = availableNoiseModes(catalog).find((candidate) => candidate.id === 'calibration_values')
                      ?? availableNoiseModes(catalog)[0]
                    if (mode) choose({ mode: 'free', noiseMode: mode.id })
                  }}>自由モード<span>公開された条件を編集して実験</span></button>
                  <button disabled={locked} aria-pressed={selection?.mode === 'challenge'} onClick={() => choose({ mode: 'challenge', challengeId: '' })}>
                    課題モード<span>プリセットの条件で挑戦</span></button>
                </div>
                {selection?.mode === 'challenge' && <div className="attack-live__challenge-list"><h3>課題を選ぶ</h3>
                  {catalog.challenges.map((challenge) => <article key={challenge.id} aria-label={challenge.title}
                    data-tutorial-anchor={`attack-challenge-${challenge.id}`}
                    className={selection.challengeId === challenge.id ? 'attack-live__challenge is-selected' : 'attack-live__challenge'}>
                    <h4>{challenge.title}{!challenge.available && ' · 準備中'}</h4>
                    {/* 長い説明は、文ごとに分けて1行ずつ読めるようにする。 */}
                    {challenge.description.split('。').map((sentence) => sentence.trim()).filter(Boolean)
                      .map((sentence, index) => <p key={index}>{sentence}。</p>)}
                    <div className="attack-live__row">
                      <button type="button" disabled={locked || !challenge.available} aria-pressed={selection.challengeId === challenge.id}
                        onClick={() => choose({ mode: 'challenge', challengeId: challenge.id })}>
                        {selection.challengeId === challenge.id ? '選択中' : 'この課題を選ぶ'}</button>
                      {challenge.free_equivalent && <button type="button" disabled={locked}
                        onClick={() => reproduceInFree(challenge.free_equivalent!)}>自由モードで再現</button>}
                    </div>
                  </article>)}
                </div>}
                {showSnapshot && <AttackSnapshotInfo standard={catalog.free_mode.standard_initial_values} />}
              </section>}
              {setupCategory === 'qpu' && <section aria-label="仮想QPU設定">
                {selection && (selection.mode === 'free' || !!selection.challengeId) && <>
                  {selection.mode === 'free' && standardSnapshot && regionCatalog && <>
                    {renderFields(placementFields)}
                    <AttackQpuTopology standard={standardSnapshot} regionCatalog={regionCatalog} placement={placementPolicy}
                      noiseMode={selection.noiseMode} regions={regions} setRegions={setRegions} owner={regionOwner}
                      setOwner={setRegionOwner} selectedQubit={selectedNoiseQubit} setSelectedQubit={setSelectedNoiseQubit}
                      onNoiseQubitSelect={(qid) => { setSelectedNoiseQubit(qid); setSetupCategory('noise') }}
                      editable={!locked} onNotice={showNotice} />
                  </>}
                  {selection.mode === 'challenge' && standardSnapshot && regionCatalog && challengeRegion && <>
                    {challengeRegion.primary_physical_qubit_ids
                      ? <AttackQpuTopology standard={standardSnapshot} regionCatalog={regionCatalog}
                        placement={challengeRegion.placement_policy ?? placementPolicy}
                        noiseMode={challengeRegion.noise_mode ?? 'physical_parameters'} regions={regions} setRegions={setRegions}
                        owner={regionOwner} setOwner={setRegionOwner} selectedQubit={selectedNoiseQubit}
                        setSelectedQubit={setSelectedNoiseQubit} onNoiseQubitSelect={() => setSetupCategory('noise')}
                        editable={false} onNotice={showNotice} />
                      : <p className="attack-live__notice">このpresetは領域を固定しています。領域情報は目録で公開されていません。</p>}
                  </>}
                </>}
                {(!selection || !challengeAvailable) && <p className="attack-live__muted">先に利用可能なモードまたは課題を選んでください。</p>}
                {selection?.mode === 'challenge' && !challengeRegion && challengeAvailable
                  && <p className="attack-live__muted">この課題の領域設定は公開目録にありません。</p>}
              </section>}
              {setupCategory === 'noise' && <section className="attack-live__panel" aria-label="待機ノイズ設定">
                <h2>待機ノイズ</h2>
                {selection?.mode === 'free' && <>
                  <label htmlFor="attack-idle-mode">入力方式</label><select id="attack-idle-mode" value={selection.noiseMode} disabled={locked}
                    onChange={(event) => {
                      const candidate = availableNoiseModes(catalog).find((item) => item.id === event.target.value)
                      if (candidate) choose({ mode: 'free', noiseMode: candidate.id })
                    }}>
                    {catalog.free_mode.idle_noise_modes.map((mode) => <option key={mode.id} value={mode.id} disabled={!mode.available}>
                      {mode.id === 'calibration_values' ? '実機較正値（T1・T2）' : '物理パラメタ'}{!mode.available && ' · 準備中'}
                    </option>)}
                  </select><p className="attack-live__muted">選んだ方式の項目だけを使用します。</p>
                  {selection.noiseMode === 'physical_parameters' && <>
                    <p className="attack-live__muted">磁束ノイズはtransmonの現象論です。固定周波数の実機へそのまま当てはまるとは限りません。</p>
                    {renderFields(noiseFields.filter((field) => field.path.includes('.physical_inputs.')))}
                  </>}
                  {standardSnapshot && <AttackQubitNoiseEditor standard={standardSnapshot} regions={regions} placement={placement}
                    fields={scopedNoiseFields} values={values} setValues={setValues} errors={errors} setErrors={setErrors}
                    noiseMode={selection.noiseMode} selectedQubit={selectedNoiseQubit} setSelectedQubit={setSelectedNoiseQubit}
                    owner={regionOwner} setOwner={setRegionOwner} editable={!locked} />}
                </>}
                {selection?.mode === 'challenge' && <>
                  {renderFields(noiseFields)}
                  <p className="attack-live__muted">課題の待機ノイズ条件はpresetに従います。</p>
                </>}
                {!selection && <p className="attack-live__muted">先にモードを選んでください。</p>}
              </section>}
              {setupCategory === 'service' && <section className="attack-live__panel" aria-label="サービスと時間の設定">
                <h2>サービスと時間</h2>
                {renderFields(serviceFields)}
                {!selection && <p className="attack-live__muted">先にモードを選んでください。</p>}
              </section>}
              {setupCategory === 'review' && <section className="attack-live__panel" aria-label="設定確認と環境作成">
                <h2>確認して作成</h2>
                <h3>初期値から変更した項目</h3>
                {changedItems.length
                  ? <ul className="attack-live__changes">{changedItems.map((item, index) => <li key={`${item.category}-${item.label}-${index}`}>
                    <span>{SETUP_CATEGORIES.find((category) => category.id === item.category)?.label}: {item.label}</span>
                    <small>{item.from} → {item.to}</small>
                  </li>)}</ul>
                  : <p className="attack-live__muted">変更した項目はありません。</p>}
                {Object.keys(errors).length > 0 && <div role="alert"><h3>入力エラー</h3><ul>
                  {Object.entries(errors).map(([fieldPath, message]) => <li key={fieldPath}>{visibleFields.find((field) => field.path === fieldPath)?.label ?? fieldPath}: {message}</li>)}
                </ul></div>}
                <div className="attack-live__actions">
                  <button disabled={busy || !selection || !challengeAvailable} onClick={() => void establish()}>
                    {state.environment ? '今の設定で作り直す' : '環境を作成'}</button>
                  {state.environment && <button disabled={busy} onClick={() => void workspace.discard()}>環境を破棄</button>}
                  <span>成立後の設定変更はできません。変更するには環境を破棄してください。</span>
                </div>
              </section>}
              </>}
            </>}
            {currentTab === 'workspace' && state.environment && editorView === 'circuit'
              && <div id="attack-circuit-editor" data-tutorial-anchor="attack-circuit-editor"><AttackCircuitEditor gateDurationDefaults={gateDurationDefaults}
                maximumLogicalQubits={state.maxProgramQubits ?? undefined} /></div>}
            {currentTab === 'workspace' && state.environment && editorView === 'script'
              && (scriptFiles ? <AttackScriptEditor value={scriptFiles} onChange={changeScriptFiles} running={scriptRunning}
                available={scriptUnavailable} saved={scriptsSaved} onStop={() => runner?.interrupt()}
                onRun={(file) => { setMessagesTab('terminal'); void runCommand(`python ${file}`) }} />
                : <p className="attack-live__muted">スクリプトの実行環境を準備しています…</p>)}
          </div>
        </section>
      )}
      messages={currentTab === 'setup' && errorEntries.length === 0 ? undefined : (
        <section className="ide__pane ide__messages" aria-label="メッセージ">
          <div className="ide__doc-tabs" role="group" aria-label="メッセージの種類">
            <button type="button" aria-pressed={messagesView === 'terminal'} onClick={() => setMessagesTab('terminal')}>
              {currentTab === 'setup' ? '入力エラー' : 'ターミナル'} <span>{currentTab === 'setup' ? errorEntries.length : preflightIssues.length}</span></button>
            {currentTab === 'workspace' && state.victimActivityAvailable && <button type="button" aria-pressed={messagesView === 'activity'} onClick={() => setMessagesTab('activity')}>
              被害者の動き</button>}
            {currentTab === 'workspace' && state.clockMode === 'explicit_wait' && <button type="button" aria-pressed={messagesView === 'pending'} onClick={() => setMessagesTab('pending')}>
              未完了の要求 <span>{state.pending.length}</span></button>}
          </div>
          <div className="ide__pane-body">
            {currentTab === 'setup' && <div className="attack-live__terminal-output" role="log" aria-label="入力エラー" aria-live="polite">
              {errorEntries.length
                ? errorEntries.map(([fieldPath, message]) => <pre key={fieldPath} className="attack-live__terminal-error">ERROR: {visibleFields.find((field) => field.path === fieldPath)?.label ?? fieldPath}: {message}</pre>)
                : <pre>入力エラーはありません。</pre>}
            </div>}
            {currentTab === 'workspace' && messagesView === 'activity' && <VictimActivityPanel activity={latestActivity} jobId={jobId} busy={busy}
              onRead={() => { print('output', `被害者の動きの照会を要求します（${jobId}）。`); void workspace.job('victim_activity', jobId) }} />}
            {currentTab === 'workspace' && messagesView === 'terminal' && <section className="attack-live__terminal" aria-label="攻撃者ターミナル" data-tutorial-anchor="attack-terminal">
              <div ref={terminalLogRef} className="ide__terminal-log" role="log" aria-label="コマンドの実行結果とreply" aria-live="polite">
                {preflightIssues.length
                  ? <>{preflightIssues.map((issue, index) => <pre key={`${issue.code}-${index}`} className="ide__terminal-hint ide__terminal-issue">送信前の確認: {issue.message}</pre>)}
                    <pre className="ide__terminal-hint">回路を直すとこの表示も更新されます。直るまで submit は送信されません。</pre></>
                  : <pre className="ide__terminal-hint">回路の測定位置と古典bit割当てを確認しました。</pre>}
                {!terminal.length && <pre className="ide__terminal-hint">help でコマンド一覧を表示します。回路の送信は submit です。</pre>}
                {terminal.map((line) => <pre key={line.id} data-kind={line.kind}>{line.text}</pre>)}
              </div>
              <form className="ide__prompt" onSubmit={(event) => {
                event.preventDefault()
                const line = commandInput
                setCommandInput('')
                void runCommand(line)
              }}>
                <label htmlFor="attack-terminal-input">{scriptWaitingInput ? scriptInputPrompt.trimEnd() || 'input>' : prompt}</label>
                <input id="attack-terminal-input" value={commandInput} autoComplete="off" spellCheck={false}
                  placeholder={scriptRunning ? (scriptWaitingInput ? 'スクリプトへの入力' : 'Ctrl+C で停止') : 'submit --shots 32'}
                  onChange={(event) => { setCommandInput(event.target.value); setHistoryIndex(null) }}
                  onKeyDown={(event) => {
                    const target = event.currentTarget
                    if (event.ctrlKey && event.key.toLowerCase() === 'c' && scriptRunning
                        && target.selectionStart === target.selectionEnd) {
                      event.preventDefault()
                      print('input', '^C')
                      runner?.interrupt()
                      return
                    }
                    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
                    if (!commandHistory.length) return
                    event.preventDefault()
                    const last = commandHistory.length - 1
                    const next = event.key === 'ArrowUp'
                      ? Math.max(0, (historyIndex ?? last + 1) - 1)
                      : historyIndex === null ? null : historyIndex + 1 > last ? null : historyIndex + 1
                    setHistoryIndex(next)
                    setCommandInput(next === null ? '' : commandHistory[next])
                  }} />
              </form>
            </section>}
            {currentTab === 'workspace' && messagesView === 'pending' && <>
              {state.pendingUnknown && <p className="attack-live__muted">再読み込み前の未完了要求一覧は取得できません。件数はこの画面で受け付けた分だけです。</p>}
              {!state.pending.length && !state.pendingUnknown && <p className="attack-live__muted">未完了の要求はありません。</p>}
              <ul className="attack-live__pending">{state.pending.map((request) => <li key={request.requestId}>
                <div><strong>{REQUEST_LABELS[request.kind]}</strong><code>{request.requestId}</code>
                  <small>送信時の世界時刻: {formatWorldTime(request.issuedAtWorldUs)}</small></div>
                <small>ターミナルで <code className="ide__inline-code">wait {request.requestId}</code></small>
              </li>)}</ul>
            </>}
          </div>
        </section>
      )}
    />
    <footer className="ide__statusbar">
      <span>{busy ? '処理中…' : '準備完了'}</span>
      {scriptRunning && <span role="status">{SCRIPT_PHASE_LABELS[scriptPhase]}</span>}
      <span>接続: {CONNECTION_LABELS[state.connectionState]}</span>
      {state.environment && <span>世界時刻 <output aria-label="世界時刻">{formatWorldTime(state.worldTimeUs)}</output></span>}
      {state.environment && <span>未完了 {state.pending.length} · reply {state.replies.length}</span>}
    </footer>
    {state.replacement && <div className="attack-live__modal" role="dialog" aria-modal="true" aria-labelledby="replace-title">
      <div className="attack-live__panel"><h2 id="replace-title">今の環境を破棄して作り直しますか？</h2>
        <p>現在のジョブと結果は失われます。破棄の確認後に、新しい環境を作成します。</p>
        {!state.environment && <label>破棄対象の環境ID<input value={replacementId} maxLength={32} onChange={(event) => setReplacementId(event.target.value)} />
          <small>上の「続きから」で所有環境を確認できます。破棄対象のIDを確認して入力してください。</small></label>}
        <div className="attack-live__row"><button disabled={busy || (!state.environment && !/^[a-f0-9]{32}$/.test(replacementId))} onClick={async () => {
          await workspace.replace(replacementId)
          if (workspace.getSnapshot().environment) setTab('workspace')
        }}>破棄して作成</button>
          <button disabled={busy} onClick={() => workspace.cancelReplacement()}>戻る</button></div></div>
    </div>}
  </main>
}
