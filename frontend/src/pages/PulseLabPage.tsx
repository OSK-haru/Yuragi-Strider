import { useEffect, useRef, useState, type ReactNode } from 'react'
import { PulseEnvironmentPanel } from '../components/PulseEnvironmentPanel'
import { PulseToolboxPanel } from '../components/PulseToolboxPanel'
import { PulseWaveform } from '../components/PulseWaveform'
import { IdeViewToggles, IdeWorkbench } from '../components/IdeWorkbench'
import { SimulationCompletionPopup } from '../components/SimulationCompletionPopup'
import { apiUrl } from '../utils/apiBase'
import { useIdeLayout } from '../hooks/useIdeLayout'
import { useInternalInfoVisible } from '../context/useAdminMode'
import {
  pulseModelUnavailableReason,
  usePulseApiCompatibility,
} from '../hooks/usePulseApiCompatibility'
import {
  isQutritPulseResponse,
  isCoupledTransmonNetworkResponse,
  COUPLED_TRANSMON_NETWORK_PULSE_MODEL,
  QUTRIT_PULSE_MODEL,
  type PulseLabForm,
  type PulseComplexValue,
  type PulseCostEstimate,
  type QutritPulsePoint,
  type QutritPulseResponse,
  type PulseResponse,
  type PulseRunRecord,
} from '../types/pulse'
import type {
  PulseCircuitState,
  PulseCircuitStep,
  PulseExecutionConstraints,
} from '../types/pulseCircuit'
import {
  buildPulsePayload,
  buildTransmonNetworkPayload,
  circuitLaneWaveform,
  deriveModelId,
  estimatePulseCost,
  estimateTransmonNetworkCost,
  hasPulseResponseShape,
  pulseDurationUs,
  pulseWaveform,
  sequentialPulseWaveform,
  validatePulseLabForm,
} from '../utils/pulseLab'
import {
  applyPulseStepToForm,
  isDrivePulseStep,
  normalizeFramePhase,
  pulseStepDurationUs,
} from '../utils/pulseCircuit'
import {
  checkPulseToolbox,
  type PulseToolboxSettings,
} from '../utils/pulseToolbox'
import {
  constraintsAreWellFormed,
  drivePulseConstraintIssues,
  virtualZConstraintIssues,
} from '../utils/pulseConstraints'
import '../components/IdeLayout.css'
import './PulseLabPage.css'

type RequestStatus = 'idle' | 'loading' | 'success' | 'error'
type PulseIdeView = 'sequence' | 'explorer'
type PulseIdeMessagesTab = 'run' | 'waveform' | 'result'

type PulseLabPageProps = {
  form: PulseLabForm
  onFormChange: (form: PulseLabForm) => void
  circuit: PulseCircuitState
  sequence: PulseCircuitStep[]
  activeTransmonIndex: number
  executionConstraints: PulseExecutionConstraints
  onExecutionConstraintsChange: (next: PulseExecutionConstraints) => void
  /* 台数はモデル選択の一軸なので、環境パネルからも直接変更できる。 */
  onTransmonCountChange: (count: number) => void
  /*
   * 直近の実行結果はアプリ側が持つ。Pulse状態エクスプローラーが同じ結果を読むので、
   * このページを離れても消えない場所へ置く必要がある。
   */
  latestRun: PulseRunRecord | null
  runSignature: string
  onRunCommitted: (run: PulseRunRecord) => void
  /*
   * ツール箱の設定。状態エクスプローラーの表示項目メニューと同じ1つを共有する。
   * 追加指標は保存した軌跡から計算するので、実行そのものには影響しない。
   */
  toolbox: PulseToolboxSettings
  onToolboxChange: (toolbox: PulseToolboxSettings) => void
  /*
   * IDE の作業領域に置く Pulse 回路スタジオと Pulse 状態エクスプローラー。
   * App が用意する。Pulse モードはこの IDE 画面に一本化している。
   */
  ide: {
    renderEditor: () => ReactNode
    renderExplorer: (showSequence: () => void) => ReactNode
  }
  /* 最初に開く表示。旧 /pulse-state-explorer から来たときは 'explorer'。 */
  initialIdeView?: PulseIdeView
}

export function PulseLabPage({
  form,
  onFormChange,
  circuit,
  sequence,
  activeTransmonIndex,
  executionConstraints,
  onExecutionConstraintsChange,
  onTransmonCountChange,
  latestRun,
  runSignature,
  onRunCommitted,
  toolbox,
  onToolboxChange,
  ide,
  initialIdeView = 'sequence',
}: PulseLabPageProps) {
  const [ideView, setIdeView] = useState<PulseIdeView>(initialIdeView)
  const [messagesTab, setMessagesTab] = useState<PulseIdeMessagesTab>('run')
  const ideLayout = useIdeLayout('pulse', { inspectorWidth: 340 }, { navigator: 'Pulse 回路' })
  const internalInfoVisible = useInternalInfoVisible()
  const result = latestRun?.response ?? null
  const lastResponseAt = latestRun?.completedAt ?? null
  const [status, setStatus] = useState<RequestStatus>('idle')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [errorDetail, setErrorDetail] = useState<string | null>(null)
  const [showCompletionPopup, setShowCompletionPopup] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)
  /*
   * modelId は「準位数トグル × 回路の台数」から導出する内部値。フォームに
   * 保存された値がズレていても、このページ内では常にこの導出値で解釈する。
   * 準位数・台数を変える操作側が form.modelId も併せて更新するので、実行時に
   * 送るペイロードとも一致する。
   */
  const modelId = deriveModelId(form.localLevels, circuit.transmons.length)
  const formForRun: PulseLabForm = { ...form, modelId }
  const networkMode = modelId === COUPLED_TRANSMON_NETWORK_PULSE_MODEL
  const executionPlan = networkMode
    ? transmonNetworkExecutionPlan(formForRun, circuit)
    : sequenceExecutionPlan(formForRun, sequence, executionConstraints)
  const executionForms = executionPlan
    .filter((operation): operation is SequenceDriveOperation => operation.kind === 'drive')
    .map((operation) => operation.form)
  const sequenceMode = modelId === QUTRIT_PULSE_MODEL
  /*
   * シーケンス実行（ブロックを順に流して密度行列を引き継ぐ経路）は、
   * 引き継ぎ・集約とも qutrit の応答形にだけ対応している。2準位×1台で
   * レーンにブロックが積んであっても実行されるのは単一Pulse設定なので、
   * 黙って無視せずその旨を出す。
   */
  const ignoresSequence = !networkMode && !sequenceMode && sequence.length > 0
  const sequenceDurationUs = networkMode
    ? transmonNetworkDurationUs(circuit)
    : sequenceMode
    ? executionForms.reduce((total, executionForm) => total + pulseDurationUs(executionForm), 0)
      + Math.max(0, executionForms.length - 1) * executionConstraints.interPulseGapUs
    : pulseDurationUs(form)
  const errors = validatePulseLabForm(formForRun, circuit.transmons.length)
  const executionErrors = executionForms.flatMap((executionForm) =>
    Object.values(validatePulseLabForm(executionForm, circuit.transmons.length)),
  )
  const cost = networkMode
    ? estimateTransmonNetworkCost(formForRun, circuit)
    : combinedPulseCost(executionForms)
  const waveform = networkMode
    ? circuitLaneWaveform(formForRun, circuit, activeTransmonIndex)
    : sequenceMode
      ? sequentialPulseWaveform(executionForms, executionConstraints.interPulseGapUs)
      : pulseWaveform(formForRun)
  const waveformPulseEndTimeUs = networkMode || sequenceMode
    ? sequenceDurationUs
    : pulseDurationUs(form)
  const waveformScopeLabel = networkMode
    ? `q${activeTransmonIndex} レーンの回路波形`
    : sequenceMode
      ? `q${activeTransmonIndex} シーケンスの回路波形`
      : '単一 Pulse の波形'
  const constraintIssues = [
    ...validateExecutionConstraints(executionPlan, executionConstraints),
    ...(networkMode
      ? validateTransmonNetworkScope(circuit, sequenceDurationUs, form.totalSimulationTimeUs)
      : []),
  ]
  const hasNoDriveInSequence = sequenceMode && executionForms.length === 0
  const isValid = Object.keys(errors).length === 0
    && executionErrors.length === 0
    && constraintIssues.length === 0
    && !hasNoDriveInSequence
    && (!networkMode || (
      circuit.transmons.length >= 2
      && circuit.transmons.length <= 4
      && executionForms.length > 0
      && sequenceDurationUs <= form.totalSimulationTimeUs
    ))
  /*
   * 実行前にAPI側の対応モデルを突き合わせる。噛み合わない場合は入力をいくら
   * 直しても通らないので、実行を止めたうえで「サーバーが古い」と明示する。
   */
  const apiCompatibility = usePulseApiCompatibility()
  const apiUnavailableReason = pulseModelUnavailableReason(apiCompatibility, modelId)
  const canRun = isValid
    && !cost.overBudget
    && status !== 'loading'
    && apiUnavailableReason === null
  /*
   * スタジオで複数レーンを組んでも、モデルがネットワークでなければ実行されるのは
   * 選択中の1レーンだけ。密度行列の次元が回路と噛み合わない原因がこれなので、
   * 何が走るのかを実行ボタンの手前で明示し、一斉実行へ1クリックで移れるようにする。
   */
  /*
   * ツール箱の構成。2準位1台のモデルだけは軌跡に密度行列がなく、指標は使えない。
   * 実行後の判定はエクスプローラーが結果の形で行い、ここは実行前の目安。
   */
  const toolboxGeometry = {
    levels: form.localLevels,
    count: circuit.transmons.length,
    hasDensityMatrices: !(form.localLevels === 2 && circuit.transmons.length === 1),
    pointCount: 0,
  }
  const toolboxCheck = checkPulseToolbox(toolbox, toolboxGeometry)
  const lanesWithDrives = circuit.lanes.filter((lane) => lane.steps.some(isDrivePulseStep))
  const networkDimension = form.localLevels ** circuit.transmons.length
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
    }
  }, [])

  async function runPulseSimulation() {
    if (!canRun) {
      return
    }
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const timeoutId = window.setTimeout(
      () => controller.abort(),
      /*
       * 1台でも実測で数十秒かかる条件があるため、シーケンス長によらず
       * 60秒を下限として確保する。上限120秒とネットワークの90秒は据え置き。
       */
      networkMode
        ? 90000
        : Math.min(
            120000,
            Math.max(
              60000,
              (form.evolutionMethod === 'explicit_cptp' ? 30000 : 16000)
                * executionForms.length,
            ),
          ),
    )
    setStatus('loading')
    setErrorMessage(null)
    setErrorDetail(null)

    try {
      let initialDensityMatrix: PulseComplexValue[][] | undefined
      const responses: PulseResponse[] = []
      const driveStepIndices: number[] = []
      const driveLabels: string[] = []
      const virtualEvents: SequenceVirtualEvent[] = []
      let latestQutritPoint: QutritPulsePoint | null = null
      let framePhaseRad = 0
      if (networkMode) {
        const payload = buildTransmonNetworkPayload(formForRun, circuit)
        const response = await fetch(apiUrl('/api/pulse/simulate'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          cache: 'no-store',
          signal: controller.signal,
        })
        if (!response.ok) {
          throw new Error(await pulseApiError(response))
        }
        const parsed: unknown = await response.json()
        if (!hasPulseResponseShape(parsed) || !isCoupledTransmonNetworkResponse(parsed)) {
          throw new Error('複数トランズモンAPIが不正な形式のレスポンスを返しました。')
        }
        responses.push(parsed)
      } else for (const operation of executionPlan) {
        if (operation.kind === 'virtual_z') {
          initialDensityMatrix = applyVirtualZToDensityMatrix(
            initialDensityMatrix ?? groundQutritDensityMatrix(),
            operation.angleRad,
          )
          framePhaseRad = normalizeFramePhase(framePhaseRad + operation.angleRad)
          latestQutritPoint = virtualZTrajectoryPoint(
            latestQutritPoint,
            initialDensityMatrix,
            operation.stepIndex,
            operation.label,
          )
          virtualEvents.push({
            afterDriveCount: responses.length,
            point: latestQutritPoint,
            angleRad: operation.angleRad,
            framePhaseRad,
          })
          continue
        }

        const payload = buildPulsePayload(operation.form, initialDensityMatrix)
        const response = await fetch(apiUrl('/api/pulse/simulate'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          cache: 'no-store',
          signal: controller.signal,
        })
        if (!response.ok) {
          throw new Error(await pulseApiError(response))
        }
        const parsed: unknown = await response.json()
        if (!hasPulseResponseShape(parsed)) {
          throw new Error('Pulse APIが不正な形式のレスポンスを返しました。')
        }
        if (sequenceMode && !isQutritPulseResponse(parsed)) {
          throw new Error('Pulseシーケンスの実行には現在qutritモデルが必要です。')
        }
        responses.push(parsed)
        driveStepIndices.push(operation.stepIndex)
        driveLabels.push(operation.label)
        if (isQutritPulseResponse(parsed)) {
          initialDensityMatrix = parsed.final.density_matrix
          latestQutritPoint = parsed.final
        }
      }
      if (!mountedRef.current || abortRef.current !== controller) {
        return
      }
      const nextResult = sequenceMode
        ? aggregateQutritSequence(
            responses as QutritPulseResponse[],
            driveStepIndices,
            driveLabels,
            virtualEvents,
            sequence.length,
            activeTransmonIndex,
          )
        : responses[0]
      onRunCommitted({
        response: nextResult,
        formAtRun: { ...formForRun },
        completedAt: new Date().toISOString(),
        signature: runSignature,
        /* 実行し直したら、読み込み由来という印は外れる。 */
        origin: 'computed',
      })
      setStatus('success')
      setErrorMessage(null)
      setErrorDetail(null)
      setShowCompletionPopup(true)
    } catch (error) {
      if (!mountedRef.current || abortRef.current !== controller) {
        return
      }
      setStatus('error')
      /* 例外本文にはサーバ側の内部情報が載りうるので、詳細は詳細モードだけに出す。 */
      const timedOut = error instanceof Error && error.name === 'AbortError'
      setErrorMessage(
        timedOut
          ? '待ち時間の上限に達したため、実行を打ち切りました。前回の有効な結果を表示しています。'
          : 'Pulseの実行に失敗しました。前回の有効な結果を表示しています。',
      )
      setErrorDetail(
        timedOut
          ? 'request aborted by timeout'
          : error instanceof Error
            ? error.message
            : null,
      )
    } finally {
      window.clearTimeout(timeoutId)
      if (abortRef.current === controller) {
        abortRef.current = null
      }
    }
  }

  const runLabel = status === 'loading'
    ? `${executionForms.length}個のブロックを実行中...`
    : networkMode
      ? `全 ${circuit.transmons.length} レーンを同時実行`
      : sequenceMode
        ? `q${activeTransmonIndex} シーケンスのみ実行`
        : 'Pulseシミュレーションを実行'
  const scopeTitle = networkMode
    ? '複数レーン同時実行。'
    : sequenceMode
      ? `q${activeTransmonIndex} シーケンス実行。`
      : ignoresSequence
        ? '単一Pulse実験（シーケンスは実行されません）。'
        : '単一Pulse実験。'
  const scopeDetail = networkMode
    ? `${circuit.transmons.length}台の${form.localLevels}準位トランズモンと全レーンのPulseを、${form.localLevels ** circuit.transmons.length}次元の密度行列で同時に発展させます。`
    : sequenceMode
      ? `${sequence.length}個のPulseを、密度行列と共通環境を引き継いで順番に実行します。`
      : ignoresSequence
        ? `レーンに ${sequence.length} 個のブロックがありますが、シーケンス実行は3準位でのみ対応しています。現在の単一Pulse設定を実行します。ブロックを流したい場合は「準位数」を「3準位 qutrit（漏れ準位あり）」にしてください。`
        : 'Pulse回路にブロックがないため、現在の単一Pulse設定を実行します。'
  /* 実行できない理由。入力では直せないサーバー側の不一致を最優先で出す。 */
  const blockingMessage = apiUnavailableReason
    ?? (hasNoDriveInSequence
      ? '回路に駆動 Pulse がありません。波形はゼロのままです。回路スタジオで Pulse ブロックを追加してください。'
      : !isValid
        ? '実行前に、強調表示されている項目を修正してください。'
        : null)
  const runIssueCount = (blockingMessage ? 1 : 0) + constraintIssues.length + (errorMessage ? 1 : 0)
  const messageTabs: Array<[PulseIdeMessagesTab, string, number | null]> = [
    ['run', '実行', runIssueCount],
    ['waveform', '波形', null],
    ['result', '結果', null],
  ]

  /*
   * Quartus型のIDEレイアウト。作業領域は Pulse 回路スタジオと
   * Pulse 状態エクスプローラーを切り替え、設定はプロパティ、
   * 見積もり・波形・結果はメッセージに置く。
   */
  return (
    <main className="pulse-lab pulse-ide ide">
      <header className="ide__titlebar">
        <h1>Pulse IDE</h1>
        <nav className="ide__tabs" aria-label="作業領域の表示">
          <button type="button" aria-pressed={ideView === 'sequence'} onClick={() => setIdeView('sequence')}>Pulse 回路</button>
          <button type="button" aria-pressed={ideView === 'explorer'} onClick={() => setIdeView('explorer')}>状態エクスプローラー</button>
        </nav>
        <IdeViewToggles controls={ideLayout} />
        <span className="ide__badge" role="status">
          {status === 'loading' ? '実行中' : result ? '結果あり' : '未実行'}
        </span>
      </header>
      <div className="ide__toolbar" role="toolbar" aria-label="操作">
        <button type="button" disabled={!canRun} onClick={() => void runPulseSimulation()}>{runLabel}</button>
        <span className="ide__toolbar-note"><strong>{scopeTitle}</strong> {scopeDetail}</span>
      </div>
      {blockingMessage || errorMessage ? (
        <p className="ide__notice" role="alert">{errorMessage ?? blockingMessage}</p>
      ) : null}
      <IdeWorkbench
        controls={ideLayout}
        navigator={(
          <aside className="ide__pane ide__navigator" aria-label="Pulse 回路">
            <h2 className="ide__pane-title">Pulse 回路</h2>
            <div className="ide__pane-body">
              <dl className="ide__facts">
                <div><dt>トランズモン</dt><dd>{circuit.transmons.length} 台</dd></div>
                <div><dt>準位数</dt><dd>{form.localLevels} 準位</dd></div>
                <div><dt>実行対象</dt><dd>{networkMode ? `全 ${circuit.transmons.length} レーン` : `q${activeTransmonIndex}`}</dd></div>
                <div>
                  <dt>密度行列</dt>
                  <dd>{networkMode ? `${networkDimension} × ${networkDimension}` : `${form.localLevels} × ${form.localLevels}`}</dd>
                </div>
                <div><dt>Pulse 時間幅</dt><dd>{sequenceDurationUs.toPrecision(4)} μs</dd></div>
                <div><dt>総観測時間</dt><dd>{form.totalSimulationTimeUs.toPrecision(4)} μs</dd></div>
              </dl>
              {lanesWithDrives.length >= 2 && circuit.transmons.length > 4 ? (
                <small>
                  Pulse を持つレーンが {lanesWithDrives.length} 本ありますが、同時実行できるのは 4 トランズモンまでです。
                  トランズモン数を 4 台以下にしてください。
                </small>
              ) : null}
              {/* モデル識別と発展方式の内訳は内部情報。 */}
              {internalInfoVisible ? (
                <dl className="ide__facts" aria-label="Pulseモデルの識別情報">
                  <div>
                    <dt>モデル</dt>
                    <dd>
                      {networkMode
                        ? `${circuit.transmons.length}トランズモン・ネットワーク`
                        : form.localLevels === 3 ? '3準位 qutrit' : '2準位ベースライン'}
                    </dd>
                  </div>
                  <div><dt>フレーム</dt><dd>回転フレーム / RWA</dd></div>
                  <div><dt>発展方式</dt><dd>{form.evolutionMethod === 'explicit_cptp' ? '明示的 CPTP 写像' : '固定ステップ RK4'}</dd></div>
                </dl>
              ) : null}
              <small>回転フレームRWA制御エンベロープの実験的モデルです。校正済みのハードウェアモデルではありません。</small>
            </div>
          </aside>
        )}
        editor={(
          <section className="ide__pane ide__editor" aria-label="作業領域">
            <div className="ide__doc-tabs">
              <span className="ide__doc-tab">{ideView === 'sequence' ? 'Pulse 回路スタジオ' : 'Pulse 状態エクスプローラー'}</span>
            </div>
            <div className="ide__pane-body">
              {ideView === 'sequence' ? ide.renderEditor() : ide.renderExplorer(() => setIdeView('sequence'))}
            </div>
          </section>
        )}
        toolbox={(
          <aside className="ide__pane ide__toolbox" aria-label="分析ツール箱">
            <h2 className="ide__pane-title">分析ツール箱</h2>
            <div className="ide__pane-body">
              <PulseToolboxPanel
                settings={toolbox}
                check={toolboxCheck}
                geometry={toolboxGeometry}
                onChange={onToolboxChange}
              />
            </div>
          </aside>
        )}
        inspector={(
          <aside className="ide__pane ide__inspector" aria-label="プロパティ">
            <h2 className="ide__pane-title">環境と Pulse の設定</h2>
            <div className="ide__pane-body">
              <PulseEnvironmentPanel
                form={form}
                errors={errors}
                disabled={status === 'loading'}
                transmonCount={circuit.transmons.length}
                onTransmonCountChange={onTransmonCountChange}
                onChange={onFormChange}
                executionConstraints={executionConstraints}
                onExecutionConstraintsChange={onExecutionConstraintsChange}
              />
            </div>
          </aside>
        )}
        messages={(
          <section className="ide__pane ide__messages" aria-label="メッセージ">
            <div className="ide__doc-tabs" role="group" aria-label="メッセージの種類">
              {messageTabs.map(([key, label, count]) => (
                <button key={key} type="button" aria-pressed={messagesTab === key} onClick={() => setMessagesTab(key)}>
                  {label}{count === null ? null : <span>{count}</span>}
                </button>
              ))}
            </div>
            <div className="ide__pane-body pulse-ide__messages-body">
              {messagesTab === 'run' ? (
                <section className="pulse-lab__run" aria-labelledby="pulse-run-title">
                  <div>
                    <span>実行上限管理</span>
                    <h2 id="pulse-run-title">{sequenceMode ? 'Pulseシーケンスを実行' : 'Pulseシミュレーションを実行'}</h2>
                    <p data-level={cost.level}>
                      {cost.message} UIによる保守的な見積り: 約{' '}
                      {cost.estimatedInternalSteps.toLocaleString()} /{' '}
                      {cost.maximumInternalSteps.toLocaleString()} ステップ。
                    </p>
                  </div>
                  {apiUnavailableReason ? (
                    <p className="pulse-lab__run-error pulse-lab__run-error--server" role="alert">
                      {apiUnavailableReason}
                      {internalInfoVisible ? (
                        <span className="pulse-lab__run-error-detail">
                          required model_id: {modelId}
                          {apiCompatibility.state === 'ready'
                            ? ` / api offers: ${[...apiCompatibility.supportedModels].join(', ')}`
                            : ` / api state: ${apiCompatibility.state}`}
                        </span>
                      ) : null}
                    </p>
                  ) : blockingMessage ? (
                    <p className="pulse-lab__run-error" role="alert">{blockingMessage}</p>
                  ) : null}
                  {constraintIssues.length > 0 ? (
                    <ul className="pulse-lab__run-error" aria-label="実行制約の違反">
                      {constraintIssues.map((issue) => (
                        <li key={`${issue.stepIndex}-${issue.message}`}>{issue.label}: {issue.message}</li>
                      ))}
                    </ul>
                  ) : null}
                  {errorMessage ? (
                    <p className="pulse-lab__run-error" role="alert">
                      {errorMessage}
                      {internalInfoVisible && errorDetail ? (
                        <span className="pulse-lab__run-error-detail">{errorDetail}</span>
                      ) : null}
                    </p>
                  ) : null}
                </section>
              ) : null}
              {messagesTab === 'waveform' ? (
                <PulseWaveform
                  points={waveform}
                  pulseDurationUs={waveformPulseEndTimeUs}
                  totalSimulationTimeUs={form.totalSimulationTimeUs}
                  scopeLabel={waveformScopeLabel}
                  dragAvailable={form.localLevels !== 2}
                />
              ) : null}
              {messagesTab === 'result' ? (
                result ? (
                  <section className="pulse-lab__explorer-link">
                    <div>
                      <span>実行完了</span>
                      <strong>状態エクスプローラーで結果を読む</strong>
                      <p>
                        時間発展した状態（占有数・密度行列・指標）は、作業領域の状態エクスプローラーで読みます。
                        {internalInfoVisible && lastResponseAt ? (
                          <> 最終実行 {new Date(lastResponseAt).toLocaleTimeString()}。</>
                        ) : null}
                      </p>
                    </div>
                    <button type="button" onClick={() => setIdeView('explorer')}>状態エクスプローラーを開く</button>
                  </section>
                ) : (
                  <section className="pulse-lab__empty">
                    <span>実行準備完了</span>
                    <h2>まだPulseの結果がありません</h2>
                    <p>波形と計算量見積りを確認してから、ツールバーの実行ボタンを押してください。</p>
                  </section>
                )
              ) : null}
            </div>
          </section>
        )}
      />
      <footer className="ide__statusbar">
        <span>{status === 'loading' ? '実行中…' : '準備完了'}</span>
        <span>{form.localLevels}準位 · {circuit.transmons.length}台</span>
        <span>Pulse 時間幅 {sequenceDurationUs.toPrecision(4)} μs</span>
        {lastResponseAt ? <span>最終実行 {new Date(lastResponseAt).toLocaleTimeString()}</span> : null}
      </footer>
      {showCompletionPopup ? (
        <SimulationCompletionPopup
          mode="pulse"
          title="Pulse シミュレーションが完了しました"
          detail={
            internalInfoVisible
              ? `発展方式: ${result?.diagnostics.evolution.resolved ?? '完了'}`
              : '波形どおりに時間発展を計算しました'
          }
          onDismiss={() => setShowCompletionPopup(false)}
        />
      ) : null}
    </main>
  )
}

type SequenceDriveOperation = {
  kind: 'drive'
  stepIndex: number
  label: string
  form: PulseLabForm
}

type SequenceVirtualZOperation = {
  kind: 'virtual_z'
  stepIndex: number
  label: string
  angleRad: number
}

type SequenceExecutionOperation = SequenceDriveOperation | SequenceVirtualZOperation

type SequenceVirtualEvent = {
  afterDriveCount: number
  point: QutritPulsePoint
  angleRad: number
  framePhaseRad: number
}

function transmonNetworkExecutionPlan(
  globalForm: PulseLabForm,
  circuit: PulseCircuitState,
): SequenceExecutionOperation[] {
  let stepIndex = 0
  return circuit.lanes.flatMap((lane) => lane.steps.map((step) => {
    const currentIndex = stepIndex
    stepIndex += 1
    return isDrivePulseStep(step)
      ? {
          kind: 'drive' as const,
          stepIndex: currentIndex,
          label: `q${lane.transmonIndex} / ${step.label}`,
          form: applyPulseStepToForm(globalForm, step.pulse),
        }
      : {
          kind: 'virtual_z' as const,
          stepIndex: currentIndex,
          label: `q${lane.transmonIndex} / ${step.label}`,
          angleRad: step.angleRad,
        }
  }))
}

function transmonNetworkDurationUs(circuit: PulseCircuitState): number {
  return Math.max(0, ...circuit.lanes.map((lane) => {
    const driveSteps = lane.steps.filter(isDrivePulseStep)
    return driveSteps.reduce(
      (total, step) => total + pulseStepDurationUs(step.pulse),
      0,
    ) + Math.max(0, driveSteps.length - 1) * circuit.executionConstraints.interPulseGapUs
  }))
}

function sequenceExecutionPlan(
  globalForm: PulseLabForm,
  sequence: PulseCircuitStep[],
  constraints: PulseExecutionConstraints,
): SequenceExecutionOperation[] {
  if (globalForm.modelId !== QUTRIT_PULSE_MODEL) {
    return [{ kind: 'drive', stepIndex: 0, label: '単一Pulse', form: { ...globalForm } }]
  }
  if (sequence.length === 0) {
    return []
  }

  const driveSteps = sequence.filter(isDrivePulseStep)
  const pulseDurations = driveSteps.map((step) =>
    pulseDurationUs(applyPulseStepToForm(globalForm, step.pulse)),
  )
  const gapCount = Math.max(0, driveSteps.length - 1)
  const scheduledDuration = pulseDurations.reduce((total, duration) => total + duration, 0)
    + gapCount * constraints.interPulseGapUs
  const finalIdleDuration = Math.max(0, globalForm.totalSimulationTimeUs - scheduledDuration)
  const snapshotsPerBlock = Math.max(2, Math.ceil(globalForm.snapshotCount / Math.max(1, driveSteps.length)))
  let driveIndex = 0

  return sequence.map((step, stepIndex): SequenceExecutionOperation => {
    if (!isDrivePulseStep(step)) {
      return {
        kind: 'virtual_z',
        stepIndex,
        label: step.label,
        angleRad: step.angleRad,
      }
    }
    const pulseForm = applyPulseStepToForm(globalForm, step.pulse)
    const currentDriveIndex = driveIndex
    driveIndex += 1
    return {
      kind: 'drive',
      stepIndex,
      label: step.label,
      form: {
        ...pulseForm,
        totalSimulationTimeUs: pulseDurations[currentDriveIndex]
          + (currentDriveIndex < driveSteps.length - 1 ? constraints.interPulseGapUs : 0)
          + (currentDriveIndex === driveSteps.length - 1 ? finalIdleDuration : 0),
        snapshotCount: snapshotsPerBlock,
      },
    }
  })
}

type PulseConstraintIssue = {
  stepIndex: number
  label: string
  message: string
}

function validateExecutionConstraints(
  executionPlan: SequenceExecutionOperation[],
  constraints: PulseExecutionConstraints,
): PulseConstraintIssue[] {
  if (!constraintsAreWellFormed(constraints)) {
    return [{
      stepIndex: -1,
      label: '全体制約',
      message: '上限値と分解能は正の有限値である必要があります(Pulse間隔は0でも構いません)。',
    }]
  }

  return executionPlan.flatMap((operation) => {
    const messages = operation.kind === 'virtual_z'
      ? virtualZConstraintIssues(operation.angleRad, constraints)
      : drivePulseConstraintIssues(operation.form, constraints)
    return messages.map((message) => ({
      stepIndex: operation.stepIndex,
      label: operation.label,
      message,
    }))
  })
}

function validateTransmonNetworkScope(
  circuit: PulseCircuitState,
  scheduledDurationUs: number,
  totalSimulationTimeUs: number,
): PulseConstraintIssue[] {
  const issues: PulseConstraintIssue[] = []
  if (circuit.transmons.length < 2 || circuit.transmons.length > 4) {
    issues.push({
      stepIndex: -3,
      label: 'トランズモン数',
      message: '複数トランズモン・ネットワークは2〜4台で実行してください。',
    })
  }
  if (scheduledDurationUs > totalSimulationTimeUs + 1e-14) {
    issues.push({
      stepIndex: -4,
      label: 'ネットワーク時間軸',
      message: `最長レーン ${scheduledDurationUs.toPrecision(4)} us が総観測時間 ${totalSimulationTimeUs.toPrecision(4)} us を超えています。`,
    })
  }
  return issues
}

function combinedPulseCost(forms: PulseLabForm[]): PulseCostEstimate {
  const costs = forms.map(estimatePulseCost)
  const estimatedInternalSteps = costs.reduce(
    (total, cost) => total + cost.estimatedInternalSteps,
    0,
  )
  const maximumInternalSteps = costs.reduce(
    (total, cost) => total + cost.maximumInternalSteps,
    0,
  )
  const overBudget = costs.some((cost) => cost.overBudget)
  const ratio = maximumInternalSteps > 0
    ? estimatedInternalSteps / maximumInternalSteps
    : 0
  return {
    estimatedInternalSteps,
    maximumInternalSteps,
    overBudget,
    level: overBudget ? 'blocked' : ratio >= 0.7 ? 'elevated' : 'normal',
    message: overBudget
      ? '少なくとも1つのPulseブロックがAPIのリクエストあたりの計算量上限を超えています。'
      : forms.length > 1
        ? `${forms.length}個の連続したPulseブロックの推定計算量はAPI上限内です。`
        : costs[0]?.message ?? '推定計算量はAPI上限内です。',
  }
}

function aggregateQutritSequence(
  responses: QutritPulseResponse[],
  driveStepIndices: number[],
  driveLabels: string[],
  virtualEvents: SequenceVirtualEvent[],
  totalOperationCount: number,
  transmonIndex: number,
): QutritPulseResponse {
  const last = responses.at(-1)
  if (!last) {
    throw new Error('Pulseシーケンスから結果が返されませんでした。')
  }

  let timeOffset = 0
  let finalStepOffset = 0
  const trajectory: QutritPulseResponse['trajectory'] = []
  let maximumLeakage = 0
  let estimatedInternalSteps = 0
  const operationLabels = Array.from({ length: totalOperationCount }, (_, index) => `ステップ${index + 1}`)

  driveStepIndices.forEach((operationIndex, driveIndex) => {
    operationLabels[operationIndex] = driveLabels[driveIndex] ?? `Pulse ${driveIndex + 1}`
  })
  virtualEvents.forEach((event) => {
    if (event.point.sequence_step_index !== undefined) {
      operationLabels[event.point.sequence_step_index] = event.point.sequence_step_label ?? 'VZ'
    }
  })

  function appendVirtualEvents(afterDriveCount: number) {
    virtualEvents
      .filter((event) => event.afterDriveCount === afterDriveCount)
      .forEach((event) => {
        trajectory.push({ ...event.point, time_us: timeOffset })
      })
  }

  appendVirtualEvents(0)
  responses.forEach((response, driveIndex) => {
    finalStepOffset = timeOffset
    response.trajectory.forEach((point) => {
      trajectory.push({
        ...point,
        time_us: point.time_us + timeOffset,
        sequence_step_index: driveStepIndices[driveIndex] ?? driveIndex,
        sequence_step_label: driveLabels[driveIndex] ?? `Pulse ${driveIndex + 1}`,
      })
    })
    maximumLeakage = Math.max(
      maximumLeakage,
      response.leakage.maximum_recorded_leakage_probability,
    )
    estimatedInternalSteps += Number(
      response.step_policy.estimated_internal_step_count ?? 0,
    )
    timeOffset += Number(response.input.total_simulation_time_us ?? 0)
    appendVirtualEvents(driveIndex + 1)
  })

  const trailingVirtualEvent = [...virtualEvents]
    .reverse()
    .find((event) => event.afterDriveCount === responses.length)
  const finalPoint = trailingVirtualEvent?.point ?? last.final
  const finalFramePhaseRad = virtualEvents.at(-1)?.framePhaseRad ?? 0

  return {
    ...last,
    input: {
      ...last.input,
      sequence_length: totalOperationCount,
      drive_pulse_count: responses.length,
      virtual_z_count: virtualEvents.length,
      sequence_labels: operationLabels.join(' -> '),
      transmon_index: transmonIndex,
      total_sequence_time_us: timeOffset,
    },
    step_policy: {
      ...last.step_policy,
      estimated_internal_step_count: estimatedInternalSteps,
      maximum_internal_step_count:
        Number(last.step_policy.maximum_internal_step_count ?? 0) * responses.length,
    },
    sample_times_us: trajectory.map((point) => point.time_us),
    trajectory,
    leakage: {
      maximum_recorded_leakage_probability: maximumLeakage,
      leakage_at_pulse_end: last.leakage.leakage_at_pulse_end,
      leakage_at_final_time: last.leakage.leakage_at_final_time,
    },
    pulse_end: {
      ...last.pulse_end,
      time_us: last.pulse_end.time_us + finalStepOffset,
    },
    final: {
      ...finalPoint,
      time_us: trailingVirtualEvent ? timeOffset : last.final.time_us + finalStepOffset,
    },
    diagnostics: {
      ...last.diagnostics,
      sequence: {
        transmon_index: transmonIndex,
        block_count: totalOperationCount,
        drive_pulse_count: responses.length,
        virtual_z_count: virtualEvents.length,
        final_frame_phase_rad: finalFramePhaseRad,
        virtual_z_convention: 'U=diag(1, exp(-i lambda), exp(-i 2 lambda))',
        state_handoff: 'density_matrix',
        common_environment: true,
      },
    },
    warnings: [
      ...last.warnings,
      `密度行列の引き継ぎと位相フレーム追跡を伴い、${totalOperationCount}個の操作を実行しました。`,
    ],
  }
}

/*
 * VZ が先頭に来たときの初期状態。3x3 固定でよいのは、virtual_z を含む
 * 実行計画を返すのが qutrit 経路（sequenceExecutionPlan の
 * QUTRIT_PULSE_MODEL 分岐）だけだからで、2準位では単一driveしか返らない。
 * ネットワークは VZ を後続Pulseの位相へ畳むので、ここを通らない。
 */
function groundQutritDensityMatrix(): PulseComplexValue[][] {
  return [
    [{ real: 1, imag: 0 }, { real: 0, imag: 0 }, { real: 0, imag: 0 }],
    [{ real: 0, imag: 0 }, { real: 0, imag: 0 }, { real: 0, imag: 0 }],
    [{ real: 0, imag: 0 }, { real: 0, imag: 0 }, { real: 0, imag: 0 }],
  ]
}

function applyVirtualZToDensityMatrix(
  matrix: PulseComplexValue[][],
  angleRad: number,
): PulseComplexValue[][] {
  return matrix.map((row, rowIndex) => row.map((value, columnIndex) => {
    const phase = (columnIndex - rowIndex) * angleRad
    const cosine = Math.cos(phase)
    const sine = Math.sin(phase)
    return {
      real: value.real * cosine - value.imag * sine,
      imag: value.real * sine + value.imag * cosine,
    }
  }))
}

function virtualZTrajectoryPoint(
  previous: QutritPulsePoint | null,
  densityMatrix: PulseComplexValue[][],
  stepIndex: number,
  label: string,
): QutritPulsePoint {
  if (previous) {
    return {
      ...previous,
      segment: 'virtual_z',
      density_matrix: densityMatrix,
      sequence_step_index: stepIndex,
      sequence_step_label: label,
    }
  }

  const physicality = {
    trace_error: 0,
    hermiticity_error: 0,
    minimum_eigenvalue: 0,
  }
  return {
    time_us: 0,
    segment: 'virtual_z',
    population_0: 1,
    population_1: 0,
    population_2: 0,
    computational_population: 1,
    leakage_probability: 0,
    population_sum_error: 0,
    purity: 1,
    density_matrix: densityMatrix,
    raw_physicality: physicality,
    cleaned_physicality: physicality,
    cleanup_correction_norm: 0,
    sequence_step_index: stepIndex,
    sequence_step_label: label,
  }
}

async function pulseApiError(response: Response) {
  const raw = await response.text()
  let detail = raw
  try {
    const body = JSON.parse(raw) as { detail?: unknown }
    detail =
      typeof body.detail === 'string'
        ? body.detail
        : JSON.stringify(body.detail ?? body)
  } catch {
    // Keep the plain response body when the API did not return JSON.
  }
  const label =
    response.status === 422
      ? 'Pulseリクエストが検証または計算量ゲートで拒否されました。'
      : response.status === 504
        ? 'PulseリクエストがAPIのタイムアウトを超過しました。'
        : `Pulse APIがHTTP ${response.status}を返しました。`
  return detail ? `${label} ${detail}` : label
}
