import './RunPanel.css'
import { CompiledCircuitDiagram } from './CompiledCircuitDiagram'
import { ResultDrawer } from './ResultDrawer'
import { SectionHeader } from './SectionHeader'
import { RunCostNotice } from './RunCostNotice'
import { SimulationProgressIndicator } from './SimulationProgressIndicator'
import { useInternalInfoVisible } from '../context/useAdminMode'
import type {
  CircuitCompilation,
  GateAwareEvolutionMethod,
  GateCompilationMode,
  RunPanelData,
  SimulationBackend,
  SimulationLoadStatus,
} from '../types/simulation'
import type { SimulationCostEstimate } from '../utils/simulationCost'

type RunPanelProps = {
  run: RunPanelData
  /**
   * 実行前に、いま編集中の回路をコンパイルした結果。実行済みの結果より
   * こちらを優先して描く（利用者が見ているのは編集中の回路なので）。
   */
  compilationPreview?: CircuitCompilation | null
  compilationPreviewPending?: boolean
  costEstimate: SimulationCostEstimate
  connectionLabel: string
  dataSourceLabel: string
  loadStatus: SimulationLoadStatus
  errorMessage: string | null
  /** 例外本文や HTTP ステータスなど、詳細モードでのみ出す技術的な詳細。 */
  errorDetail: string | null
  lastFetchResult: string
  lastFetchUrl: string
  lastFetchStartedAt: string
  frontendRunStartedAt: string
  frontendRunFinishedAt: string
  frontendRunElapsedMs: number | null
  frontendRunTimeoutMs: number | null
  evolutionMethod: GateAwareEvolutionMethod
  compilationMode: GateCompilationMode
  simulationBackend: SimulationBackend
  onEvolutionMethodChange: (method: GateAwareEvolutionMethod) => void
  onCompilationModeChange: (mode: GateCompilationMode) => void
  onSimulationBackendChange: (backend: SimulationBackend) => void
  onRunSimulation: () => void
  /* IDE画面では実行をツールバーに置くので、ここのボタンは出さない。 */
  showRunButton?: boolean
}

const evolutionMethodDescriptions: Record<GateAwareEvolutionMethod, string> = {
  fixed_step_rk4:
    'ゲート区間とアイドル区間を固定ステップ RK4 で時間発展し、更新後に密度行列を補正します。',
  explicit_cptp:
    '時間一定の GKSL 生成子から区間ごとの CPTP 写像を構築・監査し、密度行列の補正なしで適用します。',
}

const backendDescriptions: Record<SimulationBackend, string> = {
  python_dense: 'Python比較バックエンドです。',
  rust_dense_preview:
    '任意の Rust preview バックエンドです。RK4 は利用不可時に Python へフォールバックし、Explicit CPTP には Rust 拡張が必要です。',
  rust_dense_v1: '対応済み経路をRustで実行します。未対応の経路ではPythonへ切り替えず失敗します。',
}

/* 実行基盤の実装名を伏せた、利用者向けの説明。 */
const publicBackendDescriptions: Record<SimulationBackend, string> = {
  python_dense: '比較・検証用の計算エンジンです。',
  rust_dense_preview:
    '高速化を試験中の計算エンジンです。利用できない条件では自動的に標準エンジンへ切り替わります。',
  rust_dense_v1: 'Rust限定の計算経路です。未対応の機能は実行前に拒否します。',
}

const compilationModeDescriptions: Record<GateCompilationMode, string> = {
  logical_direct:
    '発展ゲートを1つの有効Hamiltonianとして直接実行します。',
  auto_decompose:
    '発展ゲートを基本ゲート列へ展開し、増加した深さと実行時間にもノイズを作用させます。',
}

const compilationModeLabels: Record<GateCompilationMode, string> = {
  logical_direct: '理想ゲート（直接）',
  auto_decompose: '基本ゲートへ自動分解',
}

const evolutionMethodLabels: Record<GateAwareEvolutionMethod, string> = {
  fixed_step_rk4: '固定ステップ RK4',
  explicit_cptp: '明示的 CPTP 写像',
}

const backendLabels: Record<SimulationBackend, string> = {
  python_dense: 'Python dense',
  rust_dense_preview: 'Rust dense (preview)',
  rust_dense_v1: 'Rust dense',
}

/* 実装名を出さない、利用者向けのエンジン呼称。 */
const publicBackendLabels: Record<SimulationBackend, string> = {
  python_dense: '比較エンジン',
  rust_dense_preview: '高速エンジン（試験）',
  rust_dense_v1: 'Rust限定エンジン',
}

function formatElapsedMs(value: number | null) {
  if (value === null) {
    return '利用できません'
  }
  return `${value.toFixed(1)} ms`
}

export function RunPanel({
  run,
  compilationPreview = null,
  compilationPreviewPending = false,
  costEstimate,
  connectionLabel,
  dataSourceLabel,
  loadStatus,
  errorMessage,
  errorDetail,
  lastFetchResult,
  lastFetchUrl,
  lastFetchStartedAt,
  frontendRunStartedAt,
  frontendRunFinishedAt,
  frontendRunElapsedMs,
  frontendRunTimeoutMs,
  evolutionMethod,
  compilationMode,
  simulationBackend,
  onEvolutionMethodChange,
  onCompilationModeChange,
  onSimulationBackendChange,
  onRunSimulation,
  showRunButton = true,
}: RunPanelProps) {
  const internalInfoVisible = useInternalInfoVisible()
  const canRun = run.can_run ?? true
  const isRequestPending = loadStatus === 'loading'
  const backendLabel = internalInfoVisible
    ? backendLabels[simulationBackend]
    : publicBackendLabels[simulationBackend]
  const backendDescription = internalInfoVisible
    ? backendDescriptions[simulationBackend]
    : publicBackendDescriptions[simulationBackend]
  // 編集中の回路のプレビューがあればそれを、なければ直近の実行結果を描く。
  const compilation = compilationPreview ?? run.compilation ?? null
  const isCompilationPreview = compilationPreview !== null

  return (
    <section className="run-panel" aria-label="シミュレーションの実行" data-load-status={loadStatus}>
      <div className="run-panel__header">
        <SectionHeader icon="terminal" eyebrow="実行" title="シミュレーションを実行" />
        <div className="run-panel__meta">
          {/* 生の内部状態値なので詳細モードのみ。 */}
          {internalInfoVisible ? (
            <p className="run-panel__status">読み込み状態: {loadStatus}</p>
          ) : null}
          <p className="run-panel__source">接続: {connectionLabel}</p>
          <p className="run-panel__source">データソース: {dataSourceLabel}</p>
          {errorMessage ? (
            <p className="run-panel__error" role="alert">
              {errorMessage}
              {internalInfoVisible && errorDetail ? (
                <span className="run-panel__error-detail">{errorDetail}</span>
              ) : null}
            </p>
          ) : null}
        </div>
      </div>

      <SimulationProgressIndicator isPending={isRequestPending} timeoutMs={frontendRunTimeoutMs} />

      <div className="run-panel__rows">
        <details className="run-panel__methods">
          <summary className="run-panel__methods-summary">
            <span>
              <strong>計算方法</strong>
              <small>
                推奨設定を使用中: {compilationModeLabels[compilationMode]} /{' '}
                {evolutionMethodLabels[evolutionMethod]} / {backendLabel}
              </small>
            </span>
            <span className="run-panel__methods-toggle" aria-hidden="true" />
          </summary>
          <div className="run-panel__methods-body">
            <label className="run-panel__method">
              <span>
                <strong>ゲートの実行方法</strong>
                <small>{compilationModeDescriptions[compilationMode]}</small>
              </span>
              <select
                value={compilationMode}
                onChange={(event) => {
                  onCompilationModeChange(event.target.value as GateCompilationMode)
                }}
                disabled={isRequestPending}
                aria-label="Gate compilation mode"
              >
                <option value="logical_direct">理想ゲート（直接）</option>
                <option value="auto_decompose">基本ゲートへ自動分解</option>
              </select>
            </label>
            <label className="run-panel__method">
              <span>
                <strong>時間発展の解法</strong>
                <small>{evolutionMethodDescriptions[evolutionMethod]}</small>
              </span>
              <select
                value={evolutionMethod}
                onChange={(event) => {
                  onEvolutionMethodChange(
                    event.target.value as GateAwareEvolutionMethod,
                  )
                }}
                disabled={isRequestPending}
                aria-label="時間発展の解法"
              >
                <option value="fixed_step_rk4">{evolutionMethodLabels.fixed_step_rk4}</option>
                <option value="explicit_cptp">{evolutionMethodLabels.explicit_cptp}</option>
              </select>
            </label>
            <label className="run-panel__method">
              <span>
                <strong>計算エンジン</strong>
                <small>{backendDescription}</small>
              </span>
              <select
                value={simulationBackend}
                onChange={(event) => {
                  onSimulationBackendChange(event.target.value as SimulationBackend)
                }}
                disabled={isRequestPending}
                aria-label="計算エンジン"
              >
                <option value="python_dense">
                  {internalInfoVisible ? 'Python dense (comparison)' : '比較エンジン'}
                </option>
                <option value="rust_dense_preview">
                  {internalInfoVisible ? 'Rust dense (preview)' : '高速エンジン（試験）'}
                </option>
                <option value="rust_dense_v1">
                  {internalInfoVisible ? 'Rust dense (standard)' : '標準エンジン（Rust）'}
                </option>
              </select>
            </label>
          </div>
        </details>
        {/*
          「状態 / 前回のバックエンド / 前回の実行」はここから外した。
          実際に走ったバックエンドとフォールバック有無は DiagnosticsCard が
          より詳しく出しており、残り2つはヘッダーの読み込み状態・データソースと
          同じことを言っていた。
        */}
        {/*
          「Gate count / Depth / Gate duration」もここから外した。
          論理→コンパイル後の対応は下の GATE COMPILER ドロワーが
          カラム単位で出しており、要約3行は同じ情報の重複だった。
        */}
      </div>

      {compilation ? (
        <ResultDrawer
          eyebrow="GATE COMPILER"
          title={isCompilationPreview ? '分解後の回路（実行前プレビュー）' : '分解後の回路'}
          icon="chip"
          description={
            isCompilationPreview
              ? '実行するとGate-awareモデルへ渡される基本ゲート列を、実行前に先読みして描いています。回路や実行方法を変えると即座に更新されます。'
              : '実際にGate-awareモデルへ渡された基本ゲート列を、そのまま回路図として描いています。'
          }
          defaultOpen={compilation.mode === 'auto_decompose'}
        >
          <p className="run-panel__compilation-summary">
            論理 {compilation.logical_gate_count} ゲート / 深さ {compilation.logical_depth}
            {' → '}
            分解後 {compilation.compiled_gate_count} ゲート / 深さ {compilation.compiled_depth}
            {compilationPreviewPending ? (
              <span className="run-panel__compilation-pending">更新中…</span>
            ) : null}
          </p>
          <CompiledCircuitDiagram
            circuit={compilation.compiled_circuit}
            sourceMap={compilation.source_map}
          />
          {compilation.decomposition_rules_used.length > 0 ? (
            <p className="run-panel__rules">
              適用した分解規則: {compilation.decomposition_rules_used.join(' / ')}
            </p>
          ) : null}
        </ResultDrawer>
      ) : null}

      {showRunButton ? <div className="run-panel__actions">
        <button
          className="run-panel__button"
          type="button"
          data-tutorial-anchor="run-button"
          onClick={onRunSimulation}
          disabled={!canRun || isRequestPending}
        >
          {isRequestPending ? '実行中…' : 'シミュレーションを実行'}
        </button>
      </div> : null}

      <RunCostNotice estimate={costEstimate} />

      {/* 取得 URL・タイムスタンプ等の低レベル情報は詳細モード専用。 */}
      {internalInfoVisible ? (
      <div className="run-panel__debug">
        <ResultDrawer
          eyebrow="API デバッグ"
          title="リクエストのスナップショット"
          icon="terminal"
          description="最新のリクエストに関する低レベルの取得メタデータです。"
          defaultOpen={false}
        >
          <div className="run-panel__debug-grid">
            <div className="run-panel__debug-item">
                <span className="run-panel__debug-label">前回の取得結果</span>
              <strong className="run-panel__debug-value">{lastFetchResult}</strong>
            </div>
            <div className="run-panel__debug-item">
                <span className="run-panel__debug-label">前回の取得 URL</span>
              <strong className="run-panel__debug-value">
                {lastFetchUrl || '未リクエスト'}
              </strong>
            </div>
            <div className="run-panel__debug-item">
                <span className="run-panel__debug-label">前回の取得開始</span>
              <strong className="run-panel__debug-value">
                {lastFetchStartedAt || '未開始'}
              </strong>
            </div>
            <div className="run-panel__debug-item">
                <span className="run-panel__debug-label">フロントエンド実行開始</span>
              <strong className="run-panel__debug-value">
                {frontendRunStartedAt || '未開始'}
              </strong>
            </div>
            <div className="run-panel__debug-item">
                <span className="run-panel__debug-label">フロントエンド実行終了</span>
              <strong className="run-panel__debug-value">
                {frontendRunFinishedAt || '未終了'}
              </strong>
            </div>
            <div className="run-panel__debug-item">
                <span className="run-panel__debug-label">フロントエンド実行時間</span>
              <strong className="run-panel__debug-value">
                {formatElapsedMs(frontendRunElapsedMs)}
              </strong>
            </div>
            <div className="run-panel__debug-item">
                <span className="run-panel__debug-label">フロントエンドのタイムアウト</span>
              <strong className="run-panel__debug-value">
                {frontendRunTimeoutMs === null ? '利用できません' : `${frontendRunTimeoutMs} ms`}
              </strong>
            </div>
          </div>
        </ResultDrawer>
      </div>
      ) : null}
    </section>
  )
}
