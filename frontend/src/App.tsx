import { useEffect, useRef, useState } from 'react'
import './App.css'
import { type SimulationDiagnostics } from './components/DiagnosticsCard'
import { type MockSimulationResult } from './types/simulation'
import type { GateDurationDefaults } from './types/simulation'
import { CircuitProvider } from './context/CircuitContext'
import { TutorialProvider } from './context/TutorialContext'
import { TutorialOverlay } from './components/TutorialOverlay'
import { TutorialCircuitWatcher } from './components/TutorialCircuitWatcher'
import { QuantumFluctuationField } from './components/QuantumFluctuationField'
import type { FieldPresence } from './components/QuantumFluctuationField'
import { HomePage } from './pages/HomePage'
import { AttackLivePage } from './pages/AttackLivePage'
import { HelpPage } from './pages/HelpPage'
import { ContextCircuitEditor } from './components/ContextCircuitEditor'
import { SimulatePage } from './pages/SimulatePage'
import { StateExplorerPage } from './pages/StateExplorerPage'
import { PulseLabPage } from './pages/PulseLabPage'
import { PulseCircuitStudioPage } from './pages/PulseCircuitStudioPage'
import { PulseStateExplorerPage } from './pages/PulseStateExplorerPage'
import {
  AppNavigation,
  type NavigationRoute,
} from './components/AppNavigation'
import type { SimulationResponse } from './types/simulation'
import type { CircuitConfig } from './utils/circuitConfig'
import {
  runToolboxJob,
  type ToolboxJobRequest,
  type ToolboxJobsState,
  type ToolboxSettings,
} from './utils/toolbox'
import { initialPulseLabForm } from './utils/pulseLab'
import {
  initialPulseToolboxSettings,
  type PulseToolboxSettings,
} from './utils/pulseToolbox'
import {
  initialGateAwareComparisonState,
  initialSimulateSettings,
  type GateAwareComparisonState,
  type SimulateSettings,
} from './utils/simulateSettings'
import {
  initialPulseComparisonState,
  type PulseComparisonState,
} from './utils/pulseStateExplorer'
import {
  applyPulseStepToForm,
  createDefaultPulseCircuit,
  resizePulseCircuit,
} from './utils/pulseCircuit'
import type { PulseCircuitState, PulseStepParameters } from './types/pulseCircuit'
import type { PulseRunRecord } from './types/pulse'
import { pulseSetupSignature } from './utils/pulseStateExplorer'
import { modeAtTurn, type ModeId } from './utils/homeModes'

const mockDiagnostics: SimulationDiagnostics = {
  simulation_model: 'gate_aware_open_system',
  evolution_mode: 'gate_aware_hamiltonian_lindblad_v1',
  simulation_backend: 'rust_dense_preview',
  backend_name: 'rust_dense_preview',
  rust_kernel_mode: 'sampled_cleaned_multi_output',
  rust_kernel_call_count: 2,
  rust_kernel_sampled_batch_count: 1,
  backend_fallback_used: false,
  rust_kernel_fallback_used: false,
}

const mockResult: MockSimulationResult = {
  final_fidelity: 0.981649092875,
  final_purity: 0.963818205819,
  completion_fidelity: 0.996993298356,
  completion_purity: 0.994004271723,
  effective_time_us: null,
  output_probabilities: {
    '00': 0.49,
    '01': 0.01,
    '10': 0.01,
    '11': 0.49,
  },
}

const initialGateDurationDefaults: GateDurationDefaults = {
  H: 0.02,
  X: 0.02,
  Y: 0.02,
  Z: 0.0,
  S: 0.0,
  T: 0.0,
  RX: 0.02,
  RY: 0.02,
  RZ: 0.02,
  CNOT: 0.2,
  CZ: 0.2,
  SWAP: 0.2,
  CP: 0.2,
  CCX: 0.4,
  QFT: 0.2,
  ORACLE: 0.2,
  MEASURE: 0.0,
  MESSAGE: 0.04,
  RECEIVED: 0.0,
}

type Screen = NavigationRoute

/*
 * 背景のゆらぎの相。
 * ホームだけはドラムが正面に出している面で決まる。
 * ヘルプは中身が説明文なので、ドキュメントと同じ凪いだ相に置く。
 * Gate-aware / Pulse の作業画面は、ホームと同じ背景アニメーションが
 * 見えるよう、面ごとに切り替えず基準の 'gate-aware' 相へ統一する。
 */
function fieldRegimeForScreen(screen: Screen, homeModeTurn: number): ModeId {
  if (screen === 'home') {
    return modeAtTurn(homeModeTurn)
  }
  if (screen === 'help') {
    return 'docs'
  }
  return 'gate-aware'
}

/*
 * 背景の出し具合。ホームと Gate-aware / Pulse の作業画面は素の濃さ。
 * 作業画面では中央のデータパネルが場を覆うので、余白に出る分は
 * ホームと同じ見え方にそろえる。ヘルプだけは控えめに引く。
 */
function fieldPresenceForScreen(screen: Screen): FieldPresence {
  return screen === 'help' ? 'ambient' : 'feature'
}

/*
 * 通常モードは IDE 画面に一本化した。以前の画面の URL（公式ドキュメントや
 * 配布物からのリンク）は消さずに IDE へ転送し、状態エクスプローラーだけは
 * その表示で開く。
 */
const LEGACY_GATE_AWARE_PATHS: Record<string, 'circuit' | 'explorer'> = {
  '/simulate': 'circuit',
  '/circuit-studio': 'circuit',
  '/algorithm-library': 'circuit',
  '/state-explorer': 'explorer',
}

function ideViewFromPath(pathname: string): 'circuit' | 'explorer' {
  return LEGACY_GATE_AWARE_PATHS[pathname] ?? 'circuit'
}

/* Pulse モードも同じく IDE 画面へ一本化した。旧URLは転送する。 */
const LEGACY_PULSE_PATHS: Record<string, 'sequence' | 'explorer'> = {
  '/pulse-lab': 'sequence',
  '/pulse-circuit-studio': 'sequence',
  '/pulse-state-explorer': 'explorer',
}

function pulseIdeViewFromPath(pathname: string): 'sequence' | 'explorer' {
  return LEGACY_PULSE_PATHS[pathname] ?? 'sequence'
}

function screenFromPath(pathname: string): Screen {
  if (pathname === '/attack-simulator') return 'attack-simulator'
  if (pathname === '/ide' || pathname in LEGACY_GATE_AWARE_PATHS) {
    return 'gate-aware-ide'
  }
  if (pathname === '/pulse-ide' || pathname in LEGACY_PULSE_PATHS) {
    return 'pulse-ide'
  }
  if (pathname === '/help') {
    return 'help'
  }
  return 'home'
}

function pathFromScreen(screen: Screen) {
  if (screen === 'attack-simulator') return '/attack-simulator'
  if (screen === 'gate-aware-ide') {
    return '/ide'
  }
  if (screen === 'pulse-ide') {
    return '/pulse-ide'
  }
  if (screen === 'help') {
    return '/help'
  }
  return '/'
}

function App() {
  const [screen, setScreen] = useState<Screen>(() => screenFromPath(window.location.pathname))
  /* 旧URLで来たときに IDE をどの表示で開くか。最初の1回だけ使う。 */
  const [ideInitialView] = useState(() => ideViewFromPath(window.location.pathname))
  const [pulseIdeInitialView] = useState(() => pulseIdeViewFromPath(window.location.pathname))
  const [gateDurationDefaults, setGateDurationDefaults] =
    useState<GateDurationDefaults>(initialGateDurationDefaults)
  const [latestGateAwareResult, setLatestGateAwareResult] = useState<{
    response: SimulationResponse
    circuitConfig: CircuitConfig
    /*
     * この結果がどこから来たか。結果ファイルから復元したものは 'imported'。
     * 画面はこれを見て「読み込んだ結果」であることを明示する。計算し直した
     * ものと区別が付かないと、いまの設定で得た数字だと誤解されるため。
     */
    origin?: 'computed' | 'imported'
  } | null>(null)
  /*
   * Gate-awareラボの設定。Pulse側と同じ理由でここに置く。ページ側で持つと
   * 状態エクスプローラーへ寄り道して戻っただけで初期値に戻ってしまう。
   */
  const [simulateSettings, setSimulateSettings] = useState<SimulateSettings>(
    () => ({ ...initialSimulateSettings }),
  )
  /*
   * 状態エクスプローラーで保持した実行と比較表示の状態。設定と同じ理由でここに置く。
   */
  const [gateAwareComparison, setGateAwareComparison] = useState<GateAwareComparisonState>(
    initialGateAwareComparisonState,
  )
  /*
   * ツール箱の追加実行（プロセス評価・スイープ）。ページ側で走らせると、
   * 状態エクスプローラーへ移った瞬間に打ち切られるので、ここで持つ。
   */
  const [toolboxJobs, setToolboxJobs] = useState<ToolboxJobsState | null>(null)
  const toolboxAbortRef = useRef<AbortController | null>(null)

  function startToolboxJobs(response: SimulationResponse, requests: ToolboxJobRequest[]) {
    toolboxAbortRef.current?.abort()
    toolboxAbortRef.current = null
    if (requests.length === 0) {
      setToolboxJobs(null)
      return
    }
    const controller = new AbortController()
    toolboxAbortRef.current = controller
    setToolboxJobs({
      response,
      jobs: Object.fromEntries(requests.map((request) => [request.key, { status: 'pending' }])),
    })
    /* 実行枠を食い合わないよう、1つずつ順に走らせる。 */
    void (async () => {
      for (const request of requests) {
        const state = await runToolboxJob(request, controller.signal)
        if (controller.signal.aborted) return
        setToolboxJobs((current) => (
          current !== null && current.response === response
            ? { response, jobs: { ...current.jobs, [request.key]: state } }
            : current
        ))
      }
    })()
  }

  useEffect(() => () => toolboxAbortRef.current?.abort(), [])
  const [pulseLabForm, setPulseLabForm] = useState(() => ({ ...initialPulseLabForm }))
  const [pulseCircuit, setPulseCircuit] = useState<PulseCircuitState>(() =>
    createDefaultPulseCircuit(initialPulseLabForm),
  )
  const [activePulseTransmonIndex, setActivePulseTransmonIndex] = useState(0)
  /*
   * Pulseの直近の実行結果。Pulseラボと状態エクスプローラーが同じ1件を見るので、
   * ページを移っても消えないここで保持する。
   */
  const [latestPulseRun, setLatestPulseRun] = useState<PulseRunRecord | null>(null)
  /*
   * 状態エクスプローラーで保持した実行と比較表示の状態。ページ側で持つと、
   * Pulseラボへ戻って再実行しようとしただけで保持が消えてしまうので、
   * 結果そのものと同じくここで持つ。
   */
  const [pulseComparison, setPulseComparison] = useState<PulseComparisonState>(
    initialPulseComparisonState,
  )
  /*
   * Pulseのツール箱。表示項目の on/off と追加指標の選択を、Pulseラボの設定欄と
   * 状態エクスプローラーの表示項目メニューで共有する。どちらかに持たせると
   * 2か所の on/off が食い違うので、ここで持つ。
   */
  const [pulseToolbox, setPulseToolbox] = useState<PulseToolboxSettings>(
    () => ({ ...initialPulseToolboxSettings }),
  )
  /*
   * ホームのドラムの回転数。ここで持っているのは、背景のゆらぎが
   * 全画面で1枚きりだから。ホームを離れても値が残るので、戻ってきたときに
   * 続きから回る。
   */
  const [homeModeTurn, setHomeModeTurn] = useState(0)

  function selectPulseForRun(transmonIndex: number, pulse: PulseStepParameters) {
    setActivePulseTransmonIndex(transmonIndex)
    setPulseLabForm((current) => applyPulseStepToForm(current, pulse))
  }

  function navigate(nextScreen: Screen) {
    const nextPath = pathFromScreen(nextScreen)
    if (window.location.pathname !== nextPath) {
      window.history.pushState(null, '', nextPath)
    }
    setScreen(nextScreen)
  }

  useEffect(() => {
    /* 旧URLで開かれたら、アドレス欄を IDE の URL に置き換える（履歴は増やさない）。 */
    if (window.location.pathname in LEGACY_GATE_AWARE_PATHS) {
      window.history.replaceState(null, '', '/ide')
    } else if (window.location.pathname in LEGACY_PULSE_PATHS) {
      window.history.replaceState(null, '', '/pulse-ide')
    }
  }, [])

  useEffect(() => {
    function handlePopState() {
      setScreen(screenFromPath(window.location.pathname))
    }

    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  function renderScreen() {
    if (screen === 'attack-simulator') {
      return <AppNavigation currentRoute={screen} onNavigate={navigate} />
    }
    if (screen === 'home') {
      return (
        <>
          <AppNavigation currentRoute={screen} onNavigate={navigate} />
          <HomePage
            modeTurn={homeModeTurn}
            onModeTurnChange={setHomeModeTurn}
            onStartSimulation={() => navigate('gate-aware-ide')}
            onOpenPulseLab={() => navigate('pulse-ide')}
            onOpenAttackSimulator={() => navigate('attack-simulator')}
          />
        </>
      )
    }

    if (screen === 'pulse-ide') {
      const pulseSignature = pulseSetupSignature(pulseLabForm, pulseCircuit)
      return (
        <>
          <AppNavigation currentRoute={screen} onNavigate={navigate} />
          <PulseLabPage
            form={pulseLabForm}
            onFormChange={setPulseLabForm}
            circuit={pulseCircuit}
            sequence={pulseCircuit.lanes[activePulseTransmonIndex]?.steps ?? []}
            activeTransmonIndex={activePulseTransmonIndex}
            executionConstraints={pulseCircuit.executionConstraints}
            onExecutionConstraintsChange={(executionConstraints) => setPulseCircuit((current) => ({
              ...current,
              executionConstraints,
            }))}
            onTransmonCountChange={(count) => setPulseCircuit((current) => resizePulseCircuit(current, count))}
            latestRun={latestPulseRun}
            runSignature={pulseSignature}
            onRunCommitted={setLatestPulseRun}
            toolbox={pulseToolbox}
            onToolboxChange={setPulseToolbox}
            initialIdeView={pulseIdeInitialView}
            ide={{
              renderEditor: () => (
                <PulseCircuitStudioPage
                  circuit={pulseCircuit}
                  currentForm={pulseLabForm}
                  onCircuitChange={setPulseCircuit}
                  onSelectPulseForRun={selectPulseForRun}
                />
              ),
              renderExplorer: (showSequence) => (
                <PulseStateExplorerPage
                  run={latestPulseRun}
                  currentSignature={pulseSignature}
                  currentCircuit={pulseCircuit}
                  comparison={pulseComparison}
                  onComparisonChange={setPulseComparison}
                  toolbox={pulseToolbox}
                  onToolboxChange={setPulseToolbox}
                  onOpenPulseLab={showSequence}
                  onImportedRun={(record, circuit) => {
                    /*
                     * 回路とラボ設定を、その結果を生んだものへ戻してから記録を
                     * 載せる。戻さないと signature が現在の設定と食い違い、
                     * 復元した結果が「再実行が必要」として隠れてしまう。
                     */
                    setPulseCircuit(circuit)
                    setPulseLabForm(record.formAtRun)
                    setActivePulseTransmonIndex(0)
                    setLatestPulseRun(record)
                  }}
                />
              ),
            }}
          />
        </>
      )
    }

    /*
     * 通常モードは IDE 画面ひとつ。実行ロジックは SimulatePage が持ち、
     * 作業領域の回路エディターと状態エクスプローラーはここで用意する。
     */
    const simulatePageProps = {
      diagnostics: mockDiagnostics,
      result: mockResult,
      gateDurationDefaults,
      onGateDurationDefaultsChange: setGateDurationDefaults,
      previousResponse: latestGateAwareResult?.response ?? null,
      onSuccessfulResponse: (
        response: SimulationResponse,
        circuitConfig: CircuitConfig,
        toolboxJobRequests: ToolboxJobRequest[],
      ) => {
        /* 実行し直したら、読み込み由来という印は外れる。 */
        setLatestGateAwareResult({ response, circuitConfig, origin: 'computed' })
        startToolboxJobs(response, toolboxJobRequests)
      },
      settings: simulateSettings,
      onSettingsChange: setSimulateSettings,
    }
    const renderStateExplorer = (onOpenSimulation: () => void) => (
      <StateExplorerPage
        response={latestGateAwareResult?.response ?? null}
        executedCircuitConfig={latestGateAwareResult?.circuitConfig ?? null}
        resultOrigin={latestGateAwareResult?.origin ?? 'computed'}
        gateDurationDefaults={gateDurationDefaults}
        comparison={gateAwareComparison}
        onComparisonChange={setGateAwareComparison}
        onOpenSimulation={onOpenSimulation}
        toolbox={simulateSettings.toolbox}
        onToolboxChange={(toolbox: ToolboxSettings) => setSimulateSettings((current) => ({ ...current, toolbox }))}
        toolboxJobs={toolboxJobs}
        onImportedResult={(response, circuitConfig, importedGateDurations) => {
          /*
           * ゲート所要時間も戻す。これが実行時と違うと、物理時間の軸が
           * 結果と合わなくなる。古い形式で入っていなければ現状を保つ。
           */
          if (importedGateDurations !== null) {
            setGateDurationDefaults(importedGateDurations)
          }
          setLatestGateAwareResult({ response, circuitConfig, origin: 'imported' })
        }}
      />
    )

    return (
      <>
        <AppNavigation currentRoute={screen} onNavigate={navigate} />
        <CircuitProvider gateDurationDefaults={gateDurationDefaults}>
        <TutorialCircuitWatcher />

        {screen === 'help' ? (
          <HelpPage />
        ) : null}

        {screen === 'gate-aware-ide' ? (
          <SimulatePage
            {...simulatePageProps}
            initialIdeView={ideInitialView}
            ide={{
              renderEditor: () => <ContextCircuitEditor gateDurationDefaults={gateDurationDefaults} />,
              renderExplorer: renderStateExplorer,
            }}
          />
        ) : null}
        </CircuitProvider>
      </>
    )
  }

  /*
   * チュートリアルはページをまたいで進むので、
   * 画面の出し分けごと Provider の内側へ入れる。
   * 案内幕（TutorialOverlay）はどの画面でも1枚だけ出す。
   */
  return (
    <TutorialProvider currentRoute={screen} onNavigate={navigate}>
      {/*
        真空のゆらぎの下地は、全画面を通して1枚だけ。ページごとに置くと
        画面を移るたびに作り直されて模様が途切れるが、ここに置けば
        相（regime）と出し具合（presence）が緩和で滑らかにつながる。
      */}
      <QuantumFluctuationField
        regime={fieldRegimeForScreen(screen, homeModeTurn)}
        presence={fieldPresenceForScreen(screen)}
        motionBudget={screen === 'home' ? 'full' : 'reduced'}
      />
      {renderScreen()}
      {/* Keep the attack client mounted across navigation. Hiding is not discard.
          Its independent editor must not inherit Gate-aware user data. */}
      <div hidden={screen !== 'attack-simulator'}>
        <CircuitProvider gateDurationDefaults={initialGateDurationDefaults} keyboardEnabled={screen === 'attack-simulator'} keyboardScopeId="attack-circuit-editor" minimumLogicalQubits={1}>
          <AttackLivePage gateDurationDefaults={initialGateDurationDefaults} active={screen === 'attack-simulator'} />
        </CircuitProvider>
      </div>
      <TutorialOverlay />
    </TutorialProvider>
  )
}

export default App
