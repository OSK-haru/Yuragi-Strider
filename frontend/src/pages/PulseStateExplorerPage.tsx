import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import './PulseStateExplorerPage.css'
import { PulseDensityMatrixHeatmap } from '../components/PulseDensityMatrixHeatmap'
import { PulseMetricTimeline } from '../components/PulseMetricTimeline'
import { PulseOutputProbabilities } from '../components/PulseOutputProbabilities'
import { PulsePopulationTimeline } from '../components/PulsePopulationTimeline'
import { PulseStateProbabilityComparison } from '../components/PulseStateProbabilityComparison'
import { PulseTimelinePlayback } from '../components/PulseTimelinePlayback'
import {
  PulseToolboxResultView,
  type PulseToolboxViewState,
} from '../components/PulseToolboxResultView'
import { RunComparisonBar } from '../components/RunComparisonBar'
import { useInternalInfoVisible } from '../context/useAdminMode'
import type { PulseRunRecord } from '../types/pulse'
import type { PulseCircuitState } from '../types/pulseCircuit'
import {
  buildPulseExplorerView,
  nearestPulsePointIndex,
  pulseComparabilitySignature,
  type PulseComparisonState,
} from '../utils/pulseStateExplorer'
import {
  PULSE_PANEL_KEYS,
  PULSE_PANEL_LABELS,
  PULSE_TOOLBOX_METRIC_KEYS,
  PULSE_TOOLBOX_METRIC_LABELS,
  cachedPulseToolbox,
  checkPulseToolbox,
  pulseToolboxSelection,
  runPulseToolbox,
  storePulseToolbox,
  type PulsePanelKey,
  type PulseToolboxGeometry,
  type PulseToolboxMetricKey,
  type PulseToolboxSettings,
  type PulseToolboxState,
} from '../utils/pulseToolbox'
import {
  buildPulseResultBundle,
  downloadJson,
  parsePulseResultJson,
  readResultFile,
  resultFileName,
} from '../utils/resultExport'
import { ResultImportButton } from '../components/ResultImportButton'

type PulseStateExplorerPageProps = {
  run: PulseRunRecord | null
  /* いまPulseラボに入っている設定の指紋。実行時のものと違えば結果は古い。 */
  currentSignature: string
  /*
   * いまPulseラボに入っている回路。保持した実行と重ねてよいかを
   * 判定するために、環境以外の指紋を作るのに使う。
   */
  currentCircuit: PulseCircuitState
  /*
   * 保持した実行と比較表示の状態。ページを移っても消えないよう App が持つ。
   * ここで useState すると、Pulseラボへ戻っただけで保持が捨てられてしまう。
   */
  comparison: PulseComparisonState
  onComparisonChange: (comparison: PulseComparisonState) => void
  /*
   * 表示項目の on/off と追加指標の選択。Pulseラボのツール箱と同じ1つを共有する。
   * ここで別に持つと、2か所の on/off が食い違う。
   */
  toolbox: PulseToolboxSettings
  onToolboxChange: (toolbox: PulseToolboxSettings) => void
  onOpenPulseLab: () => void
  /*
   * 読み込んだ結果を受け取る。回路と設定もファイルのものへ戻す必要があるので、
   * 記録だけでなく回路も一緒に渡す。
   */
  onImportedRun: (record: PulseRunRecord, circuit: PulseCircuitState) => void
}

type PulseExplorerPanelKey = PulsePanelKey | PulseToolboxMetricKey

const PULSE_EXPLORER_PANEL_LABELS: Record<PulseExplorerPanelKey, string> = {
  ...PULSE_PANEL_LABELS,
  ...PULSE_TOOLBOX_METRIC_LABELS,
}

function CollapsiblePanel({
  panelKey,
  open,
  onToggle,
  children,
}: {
  panelKey: PulseExplorerPanelKey
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  /* 非表示の項目は畳んだ見出しも残さず消す。管理は「表示項目」メニューに寄せる。 */
  if (!open) return null
  return (
    <section className="pulse-explorer-panel">
      <div className="pulse-explorer-panel__toggle">
        <span>{PULSE_EXPLORER_PANEL_LABELS[panelKey]}</span>
        <button
          type="button"
          className="pulse-explorer-panel__hide"
          onClick={onToggle}
          aria-label={`${PULSE_EXPLORER_PANEL_LABELS[panelKey]}を非表示`}
        >
          ×
        </button>
      </div>
      <div className="pulse-explorer-panel__body">{children}</div>
    </section>
  )
}

function PanelVisibilityMenu({
  panels,
  openPanels,
  onToggle,
  onShowAll,
  onHideAll,
}: {
  panels: PulseExplorerPanelKey[]
  openPanels: Record<PulseExplorerPanelKey, boolean>
  onToggle: (panelKey: PulseExplorerPanelKey) => void
  onShowAll: () => void
  onHideAll: () => void
}) {
  const [isOpen, setIsOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const visibleCount = panels.filter((panelKey) => openPanels[panelKey]).length

  useEffect(() => {
    if (!isOpen) {
      return
    }

    function handlePointerDown(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false)
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setIsOpen(false)
      }
    }

    window.addEventListener('mousedown', handlePointerDown)
    window.addEventListener('keydown', handleKeyDown)
    return () => {
      window.removeEventListener('mousedown', handlePointerDown)
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [isOpen])

  return (
    <div className="pulse-explorer-page__panel-menu" ref={containerRef}>
      <button
        type="button"
        className="pulse-explorer-page__panel-menu-toggle"
        aria-expanded={isOpen}
        aria-controls="pulse-explorer-panel-menu-list"
        onClick={() => setIsOpen((open) => !open)}
      >
        <span className="pulse-explorer-page__panel-menu-icon" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
        <span>表示項目</span>
        <span className="pulse-explorer-page__panel-menu-count">
          {visibleCount}/{panels.length}
        </span>
      </button>
      {isOpen ? (
        <div
          id="pulse-explorer-panel-menu-list"
          className="pulse-explorer-page__panel-menu-list"
          role="menu"
          aria-label="表示項目の選択"
        >
          <div className="pulse-explorer-page__panel-menu-actions">
            <button type="button" onClick={onShowAll}>すべて表示</button>
            <button type="button" onClick={onHideAll}>すべて非表示</button>
          </div>
          {panels.map((panelKey) => (
            <label key={panelKey} className="pulse-explorer-page__panel-menu-item">
              <input
                type="checkbox"
                checked={openPanels[panelKey]}
                onChange={() => onToggle(panelKey)}
              />
              {PULSE_EXPLORER_PANEL_LABELS[panelKey]}
            </label>
          ))}
        </div>
      ) : null}
    </div>
  )
}

/*
 * Pulse-level専用の状態エクスプローラー。
 * Gate-awareの状態エクスプローラーと同じ読み方（時刻カーソルを1本共有し、
 * 指標・確率・密度行列を同じ瞬間で並べる）をPulseの軌跡へ移したもの。
 * Bloch球は置かない。qutritも結合トランズモンも2準位に縮約できず、
 * 縮約して描いた球はリーケージを隠してしまうため。
 */
export function PulseStateExplorerPage({
  run,
  currentSignature,
  currentCircuit,
  comparison,
  onComparisonChange,
  toolbox,
  onToolboxChange,
  onOpenPulseLab,
  onImportedRun,
}: PulseStateExplorerPageProps) {
  const internalInfoVisible = useInternalInfoVisible()
  const staleResult = run !== null && run.signature !== currentSignature
  const view = useMemo(
    () => (run === null ? null : buildPulseExplorerView(run.response, run.formAtRun)),
    [run],
  )
  const [cursorTimeUs, setCursorTimeUs] = useState(0)
  const [cursorRunKey, setCursorRunKey] = useState<string | null>(null)
  /*
   * 見比べるために取っておいた実行。保持は1件・ページ単位にする。
   * パネルは全部で1本の時刻カーソルを共有しているので、パネルごとに
   * 別の実行を保持できると、隣り合う図が別々の条件を指してしまう。
   *
   * comparability は保持した時点の「環境以外」の指紋。これが現在と一致する
   * あいだだけ、2本の差を環境の寄与として読める。
   */
  const { heldRun, comparing } = comparison
  const openPanels = useMemo(
    () => ({ ...toolbox.panels, ...toolbox.metrics }) as Record<PulseExplorerPanelKey, boolean>,
    [toolbox],
  )
  const setPanelVisibility = useCallback(
    (changes: Partial<Record<PulseExplorerPanelKey, boolean>>) => {
      const panels = { ...toolbox.panels }
      const metrics = { ...toolbox.metrics }
      for (const [key, visible] of Object.entries(changes) as Array<[PulseExplorerPanelKey, boolean]>) {
        if ((PULSE_PANEL_KEYS as readonly string[]).includes(key)) panels[key as PulsePanelKey] = visible
        else metrics[key as PulseToolboxMetricKey] = visible
      }
      onToolboxChange({ ...toolbox, panels, metrics })
    },
    [toolbox, onToolboxChange],
  )
  /*
   * 比較線を描くのは指標タイムラインと確率比較の2枚だけ。どちらも既定では
   * 開いているが、利用者が閉じたまま比較を入れると線がどこにも見えない。
   * 比較を入れたときは、線が出るパネルを開いておく。
   */
  const openComparisonPanels = useCallback(() => {
    if (!toolbox.panels.metrics || !toolbox.panels.probabilities) {
      setPanelVisibility({ metrics: true, probabilities: true })
    }
  }, [toolbox.panels.metrics, toolbox.panels.probabilities, setPanelVisibility])
  const setComparing = useCallback(
    (nextComparing: boolean) => {
      if (nextComparing) {
        openComparisonPanels()
      }
      onComparisonChange({ ...comparison, comparing: nextComparing })
    },
    [comparison, onComparisonChange, openComparisonPanels],
  )
  const heldView = useMemo(
    () => (heldRun === null
      ? null
      : buildPulseExplorerView(heldRun.record.response, heldRun.record.formAtRun)),
    [heldRun],
  )
  /*
   * 直接比較できなくなった保持は、その場で捨てる。
   *
   * 重ねて意味があるのは「回路も観測時間も同じまま、環境だけ変えた」場合だけで、
   * パルスの形や観測窓まで動かすと、差が環境由来なのか駆動由来なのか分けられない。
   * 読めない比較を残しておくより、履歴ごと消して保持し直してもらうほうが正しい。
   */
  const currentComparability = run === null
    ? null
    : pulseComparabilitySignature(run.formAtRun, currentCircuit)
  if (
    heldRun !== null
    && currentComparability !== null
    && heldRun.comparability !== currentComparability
  ) {
    onComparisonChange({ heldRun: null, comparing: false })
  }
  const comparisonView = comparing ? heldView : null

  /*
   * 新しい実行が入ったら、まずPulse終了時刻へカーソルを置く。
   * Gate-awareが「回路の後」のスナップショットを初期表示にするのと同じ理由で、
   * 駆動を切った直後の状態が最初に知りたい状態だから。
   * 実行が替わったことは描画中に分かるので、効果ではなくここで合わせる。
   */
  const runKey = run === null ? null : `${run.completedAt}/${run.signature}`
  if (cursorRunKey !== runKey) {
    setCursorRunKey(runKey)
    setCursorTimeUs(view?.pulseEndTimeUs ?? 0)
  }

  const togglePanel = useCallback(
    (panelKey: PulseExplorerPanelKey) => setPanelVisibility({ [panelKey]: !openPanels[panelKey] }),
    [openPanels, setPanelVisibility],
  )
  const holdCurrentRun = useCallback(() => {
    if (run === null) {
      return
    }
    onComparisonChange({
      heldRun: {
        record: run,
        comparability: pulseComparabilitySignature(run.formAtRun, currentCircuit),
        heldAt: new Date().toISOString(),
      },
      comparing: true,
    })
    openComparisonPanels()
  }, [run, currentCircuit, onComparisonChange, openComparisonPanels])
  const releaseHeldRun = useCallback(() => {
    onComparisonChange({ heldRun: null, comparing: false })
  }, [onComparisonChange])
  /*
   * 表示中の実行を、そのとき使った回路と設定ごと書き出す。
   *
   * Pulse側は実行記録が completedAt を持っているので、ファイル名の時刻には
   * 書き出した時刻ではなく実行時刻を使う。あとから並べたときに、いつ計算した
   * 結果なのかが名前で分かるようにするため。
   */
  /*
   * 書き出し済みの表示は「どの実行を書き出したか」とセットで持つ。実行が
   * 切り替われば、表示中のものと一致しなくなって自動的に消える。
   */
  const [exportedRun, setExportedRun] = useState<PulseRunRecord | null>(null)
  /*
   * 読み込みの結果表示は対象と紐づけずそのまま持つ。読み込みは「これから
   * 表示する結果」に対する操作で、成功すれば表示中の結果ごと入れ替わる。
   */
  const [importMessage, setImportMessage] = useState('')
  const exportStatus = exportedRun !== null && exportedRun === run
    ? '結果を書き出しました'
    : ''
  const transferStatus = importMessage || exportStatus
  const exportCurrentRun = useCallback(() => {
    if (run === null) {
      return
    }
    const bundle = buildPulseResultBundle(run, currentCircuit)
    downloadJson(
      JSON.stringify(bundle, null, 2),
      resultFileName('pulse結果', run.completedAt),
    )
    setExportedRun(run)
    setImportMessage('')
  }, [run, currentCircuit])
  /*
   * 結果ファイルを読み込む。回路とラボ設定もファイルのものへ戻すので、
   * 読み込み後も「画面の設定 = 図の条件」が成り立つ。
   *
   * 保持していた比較は捨てる。読み込んだ結果は別条件のものなので、
   * いま保持している実行と重ねて読める保証がない。
   */
  const importResultFile = useCallback((file: File) => {
    void (async () => {
      try {
        const text = await readResultFile(file)
        const imported = parsePulseResultJson(text)
        onComparisonChange({ heldRun: null, comparing: false })
        onImportedRun({ ...imported.record, origin: 'imported' }, imported.circuit)
        setImportMessage(
          imported.exportedAt === null
            ? '結果を読み込みました'
            : `結果を読み込みました（${new Date(imported.exportedAt).toLocaleString()} に書き出し）`,
        )
      } catch (error) {
        setImportMessage(
          internalInfoVisible && error instanceof Error
            ? error.message
            : '結果ファイルを読み込めませんでした。ファイルの形式を確認してください。',
        )
      }
    })()
  }, [internalInfoVisible, onComparisonChange, onImportedRun])

  /* 追加指標は、保存した軌跡の形（準位数・台数・密度行列の有無）で使えるかが決まる。 */
  const toolboxGeometry = useMemo<PulseToolboxGeometry | null>(
    () => (view === null
      ? null
      : {
          levels: view.levelCount,
          count: view.transmonCount,
          hasDensityMatrices: view.hasPerPointDensityMatrix,
          pointCount: view.points.length,
        }),
    [view],
  )
  const toolboxCheck = useMemo(
    () => (toolboxGeometry === null ? null : checkPulseToolbox(toolbox, toolboxGeometry)),
    [toolbox, toolboxGeometry],
  )
  const toolboxSelection = useMemo(
    () => (toolboxGeometry === null || toolboxCheck === null
      ? null
      : pulseToolboxSelection(toolbox, toolboxGeometry, toolboxCheck)),
    [toolbox, toolboxGeometry, toolboxCheck],
  )
  const toolboxSelectionKey = toolboxSelection === null ? null : JSON.stringify(toolboxSelection)
  /* 計算の結果は、どの軌跡・どの選択に対するものかを一緒に持つ。 */
  const [toolboxFetch, setToolboxFetch] = useState<{
    points: object
    key: string
    state: PulseToolboxState
  } | null>(null)
  useEffect(() => {
    if (view === null || toolboxGeometry === null || toolboxSelection === null || toolboxSelectionKey === null) {
      return
    }
    if (cachedPulseToolbox(view.points, toolboxSelectionKey) !== undefined) {
      return
    }
    const controller = new AbortController()
    const points = view.points
    void runPulseToolbox(
      toolboxSelection,
      toolboxGeometry,
      points.map((point) => ({ timeUs: point.timeUs, densityMatrix: point.densityMatrix })),
      controller.signal,
    ).then((state) => {
      if (controller.signal.aborted) {
        return
      }
      if (state.status === 'done') {
        storePulseToolbox(points, toolboxSelectionKey, state.data)
      }
      setToolboxFetch({ points, key: toolboxSelectionKey, state })
    })
    return () => controller.abort()
  }, [view, toolboxGeometry, toolboxSelection, toolboxSelectionKey])
  const availablePanelKeys = useMemo(
    () => (Object.keys(PULSE_EXPLORER_PANEL_LABELS) as PulseExplorerPanelKey[]).filter((panelKey) => {
      if (view === null) {
        return false
      }
      if (panelKey === 'density') {
        return view.finalDensityMatrix !== null
      }
      if ((PULSE_TOOLBOX_METRIC_KEYS as readonly string[]).includes(panelKey)) {
        /* 使えない指標も、選ばれているあいだは理由を見せるために残す。 */
        const metric = panelKey as PulseToolboxMetricKey
        return toolboxCheck?.unavailable[metric] === undefined || toolbox.metrics[metric]
      }
      return view.points.length > 0
    }),
    [view, toolboxCheck, toolbox.metrics],
  )
  const showAllPanels = useCallback(
    () => setPanelVisibility(Object.fromEntries(availablePanelKeys.map((key) => [key, true]))),
    [availablePanelKeys, setPanelVisibility],
  )
  const hideAllPanels = useCallback(
    () => setPanelVisibility(Object.fromEntries(availablePanelKeys.map((key) => [key, false]))),
    [availablePanelKeys, setPanelVisibility],
  )

  if (run === null || view === null) {
    return (
      <div className="pulse-explorer-page">
        <section className="pulse-explorer-page__empty" aria-labelledby="pulse-explorer-empty-title">
          <span className="pulse-explorer-page__eyebrow">結果なし</span>
          <h2 id="pulse-explorer-empty-title">Pulseシミュレーション結果がありません。</h2>
          <p>Pulse 回路を組んで、上のツールバーの実行ボタンを押してください。</p>
          <button type="button" onClick={onOpenPulseLab}>Pulse 回路に戻る</button>
          {/*
            * 結果が無い状態でも読み込みは要る。人から受け取ったファイルを
            * 開くのは、たいてい自分ではまだ何も実行していないときだから。
            */}
          <ResultImportButton onImport={importResultFile} status={transferStatus} />
        </section>
      </div>
    )
  }

  const toolboxViewState = (metric: PulseToolboxMetricKey): PulseToolboxViewState => {
    const unavailable = toolboxCheck?.unavailable[metric]
    if (unavailable !== undefined) {
      return { kind: 'idle', message: `この結果では使えません: ${unavailable}` }
    }
    const error = toolboxCheck?.errors[metric]
    if (error !== undefined) {
      return { kind: 'idle', message: `Pulseラボのツール箱で設定を直してください: ${error}` }
    }
    if (toolboxSelectionKey === null) {
      return { kind: 'idle', message: 'この結果では計算していません。' }
    }
    const cached = cachedPulseToolbox(view.points, toolboxSelectionKey)
    if (cached !== undefined) {
      return { kind: 'done', data: cached }
    }
    if (
      toolboxFetch !== null
      && toolboxFetch.points === view.points
      && toolboxFetch.key === toolboxSelectionKey
      && toolboxFetch.state.status === 'error'
    ) {
      return { kind: 'error', message: toolboxFetch.state.message }
    }
    return { kind: 'pending' }
  }

  const cursorIndex = nearestPulsePointIndex(view.points, cursorTimeUs)
  const cursorPoint = cursorIndex >= 0 ? view.points[cursorIndex] : null
  const cursorDensityMatrix = view.hasPerPointDensityMatrix
    ? cursorPoint?.densityMatrix ?? view.finalDensityMatrix
    : view.finalDensityMatrix

  return (
    <div className="pulse-explorer-page">

      {staleResult ? (
        <aside className="pulse-explorer-page__stale" role="status">
          <strong>再実行が必要</strong>
          <span>
            この結果を出したあとにPulseの設定または回路が変更されています。
            表示中の数値は変更前の条件のものです。現在の設定でもう一度実行してください。
          </span>
          <button type="button" onClick={onOpenPulseLab}>Pulseラボへ戻る</button>
        </aside>
      ) : null}

      {/*
        * 読み込んだ結果であることを、図より先に出す。計算し直した結果と
        * 見分けが付かないと、いまの設定で得た数字だと誤解される。
        */}
      {run.origin === 'imported' ? (
        <p className="pulse-explorer-page__imported-note">
          読み込んだ結果を表示しています。回路と設定もこのファイルのものへ復元済みです。
          この場で計算し直した値ではありません。
        </p>
      ) : null}

      <PanelVisibilityMenu
        panels={availablePanelKeys}
        openPanels={openPanels}
        onToggle={togglePanel}
        onShowAll={showAllPanels}
        onHideAll={hideAllPanels}
      />

      <RunComparisonBar
        heldLabel={heldRun === null
          ? null
          : `${new Date(heldRun.heldAt).toLocaleTimeString()} に保存`}
        canHold={run !== null && heldRun?.record !== run}
        comparing={comparing}
        onHold={holdCurrentRun}
        onRelease={releaseHeldRun}
        onComparingChange={setComparing}
        onExport={exportCurrentRun}
        canExport={run !== null}
        onImport={importResultFile}
        transferStatus={transferStatus}
      />

      {comparisonView !== null && heldRun !== null && heldRun.record.signature === run.signature ? (
        <p className="pulse-explorer-page__compare-note">
          環境パラメタがまだ変わっていないため、2本は完全に重なります。
          Pulseラボで環境の値を変えて再実行すると、差が現れます。
        </p>
      ) : null}

      <div className="pulse-explorer-page__panels">
        <CollapsiblePanel
          panelKey="physical"
          open={openPanels.physical}
          onToggle={() => togglePanel('physical')}
        >
          <PulseTimelinePlayback
            view={view}
            simulationTimeUs={cursorTimeUs}
            onSimulationTimeChange={setCursorTimeUs}
          />
        </CollapsiblePanel>

        <CollapsiblePanel
          panelKey="metrics"
          open={openPanels.metrics}
          onToggle={() => togglePanel('metrics')}
        >
          <PulseMetricTimeline
            view={view}
            cursorTimeUs={cursorTimeUs}
            heldView={comparisonView}
          />
        </CollapsiblePanel>

        <CollapsiblePanel
          panelKey="populations"
          open={openPanels.populations}
          onToggle={() => togglePanel('populations')}
        >
          <PulsePopulationTimeline
            response={run.response}
            formAtRun={run.formAtRun}
            cursorTimeUs={cursorTimeUs}
          />
        </CollapsiblePanel>

        <CollapsiblePanel
          panelKey="probabilities"
          open={openPanels.probabilities}
          onToggle={() => togglePanel('probabilities')}
        >
          <PulseStateProbabilityComparison
            view={view}
            cursorTimeUs={cursorTimeUs}
            heldView={comparisonView}
          />
        </CollapsiblePanel>

        <CollapsiblePanel
          panelKey="output"
          open={openPanels.output}
          onToggle={() => togglePanel('output')}
        >
          <PulseOutputProbabilities view={view} cursorTimeUs={cursorTimeUs} />
        </CollapsiblePanel>

        {view.finalDensityMatrix === null ? null : (
          <CollapsiblePanel
            panelKey="density"
            open={openPanels.density}
            onToggle={() => togglePanel('density')}
          >
            <div className="pulse-explorer-page__section-heading">
              <div>
                <span className="pulse-explorer-page__eyebrow">Full quantum state</span>
                <h2>密度行列</h2>
              </div>
              <p>
                {view.hasPerPointDensityMatrix
                  ? 'カーソル時刻の完全な密度行列です。占有確率は対角成分、コヒーレンスは非対角成分に現れます。'
                  : '2準位モデルの軌跡は占有確率だけを返すため、密度行列はPulse終了時と最終時刻の2点でのみ利用できます。ここでは最終時刻の密度行列を表示します。'}
              </p>
            </div>
            {cursorDensityMatrix === null ? (
              <div className="pulse-explorer-page__unavailable">
                この結果には密度行列が含まれていません。
              </div>
            ) : (
              <PulseDensityMatrixHeatmap
                key={`pulse-explorer-density-${view.dimension}`}
                matrix={cursorDensityMatrix}
                basisLabels={view.basisLabels}
                eyebrow={view.hasPerPointDensityMatrix ? 'DENSITY OPERATOR AT CURSOR' : 'FULL DENSITY OPERATOR'}
                title={
                  view.hasPerPointDensityMatrix
                    ? `密度行列（${cursorPoint?.timeUs.toFixed(4) ?? '—'} μs）`
                    : '最終密度行列'
                }
              />
            )}
          </CollapsiblePanel>
        )}

        {PULSE_TOOLBOX_METRIC_KEYS.filter((key) => availablePanelKeys.includes(key)).map((key) => (
          <CollapsiblePanel
            key={key}
            panelKey={key}
            open={openPanels[key]}
            onToggle={() => togglePanel(key)}
          >
            <PulseToolboxResultView
              metric={key}
              state={toolboxViewState(key)}
              cursorTimeUs={cursorTimeUs}
              levels={view.levelCount}
            />
          </CollapsiblePanel>
        ))}
      </div>

      <section className="pulse-explorer-page__summary" aria-label="Pulse結果の概要">
        <span>{view.modelLabel}</span>
        <span>トランズモン {view.transmonCount}</span>
        <span>{view.dimension} x {view.dimension}</span>
        <span>サンプル {view.points.length} 個</span>
        <span>総観測時間 {view.totalTimeUs.toPrecision(4)} μs</span>
        {view.sequenceLength > 1 ? <span>操作 {view.sequenceLength} 個</span> : null}
        {internalInfoVisible ? (
          <span>{new Date(run.completedAt).toLocaleTimeString()} 実行</span>
        ) : null}
      </section>

    </div>
  )
}
