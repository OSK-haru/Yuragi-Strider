import type { ReactNode } from 'react'
import './ToolboxPanel.css'
import { SectionHeader } from './SectionHeader'
import {
  EXACT_TOMOGRAPHY_MAX_QUBITS,
  EXPLORER_PANEL_KEYS,
  EXPLORER_PANEL_LABELS,
  type ExplorerPanelKey,
  MONTE_CARLO_SAMPLE_RANGE,
  TOOLBOX_METRIC_LABELS,
  processSimulationCount,
  resolvedEntropyQubits,
  type ToolboxCheck,
  type ToolboxMetricKey,
  type ToolboxSettings,
} from '../utils/toolbox'

type ToolboxPanelProps = {
  settings: ToolboxSettings
  check: ToolboxCheck
  qubitCount: number
  sweepDelays: { t1: number; ramsey: number }
  onChange: (settings: ToolboxSettings) => void
}

/* 各指標の1行説明。何を測るかと、どれだけ追加で計算するか。 */
const METRIC_DESCRIPTIONS: Record<ToolboxMetricKey, string> = {
  entropy: '選んだ量子ビット全体の S(ρ) [bit]。2量子ビット以上では純度と独立な量。',
  concurrence: '2量子ビットの縮約状態のエンタングルメント（0〜1）。',
  negativity: '選んだ量子ビットと残りの間のエンタングルメント。部分転置の負の固有値の和。',
  mutualInformation: 'I(A:B) = S(A) + S(B) − S(AB) [bit]。古典相関も含む全相関。',
  traceDistance: '½‖ρ − ρ理想‖₁。忠実度とは別の距離で、測定で見分けられる確率の上限に対応。',
  observables: 'ZZ やパリティのように2量子ビット以上に作用するパウリ演算子の ⟨P⟩。',
  averageGateFidelity: '回路全体を1つの過程とみなし、理想ユニタリとの平均忠実度。',
  processMatrix: '過程をパウリ基底で展開した χ 行列。理想の χ と並べて表示。',
  t1Sweep: '|1⟩ からの減衰を測り、指数＋観測床で T1 を適合。回路とは別の1量子ビット実験。',
  ramseySweep: 'Ramsey 干渉で T2* を測り、指数・ガウス包絡を比較。回路とは別の1量子ビット実験。',
}

const STATE_METRICS: ToolboxMetricKey[] = [
  'entropy',
  'concurrence',
  'negativity',
  'mutualInformation',
  'traceDistance',
  'observables',
]
const PROCESS_METRICS: ToolboxMetricKey[] = ['averageGateFidelity', 'processMatrix']
const SWEEP_METRICS: ToolboxMetricKey[] = ['t1Sweep', 'ramseySweep']

/* 標準の表示項目の1行説明。 */
const PANEL_DESCRIPTIONS: Record<ExplorerPanelKey, string> = {
  playback: 'スナップショットを時間順に再生・コマ送りするバー。',
  physical: '各スナップショットの物理時間と、実行にかかった時間の内訳。',
  metrics: '忠実度・純度などの指標を時間に沿って並べたグラフ。',
  probabilities: '計算基底の確率を、理想状態と並べて比べる棒グラフ。',
  output: '最終的な出力の確率分布。',
  bloch: '1量子ビットの状態をBloch球で表示。',
  density: '密度行列の各要素をヒートマップで表示。',
  rates: '環境ノイズの各レート（緩和・位相緩和など）の生値。',
}

export function ToolboxPanel({ settings, check, qubitCount, sweepDelays, onChange }: ToolboxPanelProps) {
  const qubits = Array.from({ length: qubitCount }, (_, index) => index)
  const update = <Key extends keyof ToolboxSettings>(key: Key, value: ToolboxSettings[Key]) => {
    onChange({ ...settings, [key]: value })
  }
  const toggleMetric = (key: ToolboxMetricKey, enabled: boolean) => {
    onChange({ ...settings, metrics: { ...settings.metrics, [key]: enabled } })
  }
  const enabledCount = Object.values(settings.metrics).filter(Boolean).length

  const qubitChips = (
    selected: number[],
    onSelect: (next: number[]) => void,
    label: string,
  ) => (
    <fieldset className="toolbox-panel__chips">
      <legend>{label}</legend>
      {qubits.map((qubit) => (
        <label key={qubit} className="toolbox-panel__chip">
          <input
            type="checkbox"
            checked={selected.includes(qubit)}
            onChange={(event) => onSelect(
              event.target.checked
                ? [...selected, qubit].sort((left, right) => left - right)
                : selected.filter((entry) => entry !== qubit),
            )}
          />
          q{qubit}
        </label>
      ))}
    </fieldset>
  )

  const qubitSelect = (value: number, onSelect: (next: number) => void, label: string) => (
    <label className="toolbox-panel__field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onSelect(Number(event.target.value))}>
        {qubits.map((qubit) => <option key={qubit} value={qubit}>q{qubit}</option>)}
      </select>
    </label>
  )

  const numberField = (
    label: string,
    value: number,
    onValue: (next: number) => void,
    range: { min: number; max: number; step: number },
  ) => (
    <label className="toolbox-panel__field">
      <span>{label}</span>
      <input
        type="number"
        min={range.min}
        max={range.max}
        step={range.step}
        value={Number.isFinite(value) ? value : ''}
        onChange={(event) => onValue(event.currentTarget.valueAsNumber)}
      />
    </label>
  )

  /* 最大遅延は空欄で自動（T1 max / Tφ max の3倍）。 */
  const delayField = (
    label: string,
    value: number | null,
    resolved: number,
    onValue: (next: number | null) => void,
  ) => (
    <label className="toolbox-panel__field">
      <span>{label}</span>
      <input
        type="number"
        min={0.001}
        max={10_000}
        step={10}
        value={value ?? ''}
        placeholder={`自動 (${resolved.toFixed(0)})`}
        onChange={(event) => {
          const next = event.currentTarget.valueAsNumber
          onValue(Number.isFinite(next) ? next : null)
        }}
      />
    </label>
  )

  const options = (key: ToolboxMetricKey): ReactNode => {
    switch (key) {
      case 'entropy':
        return (
          <>
            {qubitChips(
              resolvedEntropyQubits(settings, qubitCount),
              (next) => update('entropyQubits', next.length === qubitCount ? [] : next),
              '対象の量子ビット',
            )}
          </>
        )
      case 'concurrence':
        return (
          <div className="toolbox-panel__row">
            {qubitSelect(settings.concurrenceQubits[0], (next) => update('concurrenceQubits', [next, settings.concurrenceQubits[1]]), '1つ目')}
            {qubitSelect(settings.concurrenceQubits[1], (next) => update('concurrenceQubits', [settings.concurrenceQubits[0], next]), '2つ目')}
          </div>
        )
      case 'negativity':
        return qubitChips(settings.negativitySubsystem, (next) => update('negativitySubsystem', next), '分割の片側 A（残りが B）')
      case 'mutualInformation':
        return (
          <>
            {qubitChips(settings.mutualInformationA, (next) => update('mutualInformationA', next), 'A')}
            {qubitChips(settings.mutualInformationB, (next) => update('mutualInformationB', next), 'B')}
          </>
        )
      case 'observables':
        return (
          <label className="toolbox-panel__field toolbox-panel__field--wide">
            <span>パウリ文字列（カンマ区切り、左端が q0）</span>
            <input
              type="text"
              value={settings.observablesInput}
              placeholder={qubitCount >= 2 ? `${'Z'.repeat(2)}${'I'.repeat(qubitCount - 2)}, ${'X'.repeat(qubitCount)}` : 'ZZ'}
              onChange={(event) => update('observablesInput', event.target.value)}
              spellCheck={false}
            />
          </label>
        )
      case 'averageGateFidelity':
        return qubitCount > EXACT_TOMOGRAPHY_MAX_QUBITS ? (
          <div className="toolbox-panel__row">
            {numberField(
              'モンテカルロのサンプル数',
              settings.monteCarloSamples,
              (next) => update('monteCarloSamples', next),
              { ...MONTE_CARLO_SAMPLE_RANGE, step: 16 },
            )}
          </div>
        ) : null
      case 't1Sweep':
        return (
          <div className="toolbox-panel__row">
            {delayField('最大遅延 [μs]', settings.t1MaximumDelayUs, sweepDelays.t1, (next) => update('t1MaximumDelayUs', next))}
            {numberField('測定点数', settings.t1PointCount, (next) => update('t1PointCount', next), { min: 5, max: 101, step: 1 })}
          </div>
        )
      case 'ramseySweep':
        return (
          <div className="toolbox-panel__row">
            {delayField('最大遅延 [μs]', settings.ramseyMaximumDelayUs, sweepDelays.ramsey, (next) => update('ramseyMaximumDelayUs', next))}
            {numberField('測定点数', settings.ramseyPointCount, (next) => update('ramseyPointCount', next), { min: 9, max: 101, step: 1 })}
            {numberField('離調 [MHz]', settings.ramseyDetuningMhz, (next) => update('ramseyDetuningMhz', next), { min: 0.0001, max: 1, step: 0.001 })}
          </div>
        )
      default:
        return null
    }
  }

  /* 追加で走らせる実行の回数。多いほど待つ。 */
  const costNote = (key: ToolboxMetricKey): string | null => {
    if (key === 'averageGateFidelity' || key === 'processMatrix') {
      const count = processSimulationCount(settings, qubitCount)
      return qubitCount <= EXACT_TOMOGRAPHY_MAX_QUBITS
        ? `厳密トモグラフィー: 追加で ${count} 回実行（平均ゲート忠実度と χ 行列で共有）`
        : `モンテカルロ推定: 追加で ${count} 回実行（標準誤差と95%区間つき）`
    }
    if (key === 't1Sweep') return `追加で ${settings.t1PointCount} 点の実行`
    if (key === 'ramseySweep') return `追加で ${settings.ramseyPointCount} 点の実行`
    return null
  }

  const metricRow = (key: ToolboxMetricKey) => {
    const unavailable = check.unavailable[key]
    const error = check.errors[key]
    const enabled = settings.metrics[key]
    const cost = enabled && unavailable === undefined ? costNote(key) : null
    const descriptionId = `toolbox-${key}-description`
    return (
      <li key={key} className="toolbox-panel__metric" data-enabled={enabled} data-unavailable={unavailable !== undefined}>
        <label className="toolbox-panel__metric-toggle">
          <input
            type="checkbox"
            checked={enabled}
            disabled={unavailable !== undefined && !enabled}
            aria-describedby={descriptionId}
            onChange={(event) => toggleMetric(key, event.target.checked)}
          />
          <span>{TOOLBOX_METRIC_LABELS[key]}</span>
        </label>
        <p id={descriptionId} className="toolbox-panel__description">{METRIC_DESCRIPTIONS[key]}</p>
        {unavailable !== undefined ? (
          <p className="toolbox-panel__unavailable">この回路では使えません: {unavailable}</p>
        ) : null}
        {enabled && unavailable === undefined ? (
          <div className="toolbox-panel__options">{options(key)}</div>
        ) : null}
        {cost ? <p className="toolbox-panel__cost">{cost}</p> : null}
        {error ? <p className="toolbox-panel__error" role="alert">{error}</p> : null}
      </li>
    )
  }

  return (
    <section className="toolbox-panel" aria-label="ツール箱">
      <SectionHeader
        icon="wrench"
        eyebrow="ツール箱"
        title="状態エクスプローラーで見る項目"
        description="標準の表示は出し入れだけです。追加の指標は、選んだものだけを次の実行で計算します。"
      />

      <div className="toolbox-panel__group">
        <h3>標準の表示 <small>出し入れだけ・計算は変わらない</small></h3>
        <ul className="toolbox-panel__metrics">
          {EXPLORER_PANEL_KEYS.map((key) => (
            <li key={key} className="toolbox-panel__metric" data-enabled={settings.panels[key]}>
              <label className="toolbox-panel__metric-toggle">
                <input
                  type="checkbox"
                  checked={settings.panels[key]}
                  aria-describedby={`toolbox-panel-${key}-description`}
                  onChange={(event) => update('panels', { ...settings.panels, [key]: event.target.checked })}
                />
                <span>{EXPLORER_PANEL_LABELS[key]}</span>
              </label>
              <p id={`toolbox-panel-${key}-description`} className="toolbox-panel__description">{PANEL_DESCRIPTIONS[key]}</p>
            </li>
          ))}
        </ul>
      </div>

      <div className="toolbox-panel__group">
        <h3>状態の指標 <small>各スナップショットの密度行列から計算・追加の実行なし</small></h3>
        <ul className="toolbox-panel__metrics">{STATE_METRICS.map(metricRow)}</ul>
      </div>
      <div className="toolbox-panel__group">
        <h3>プロセスの評価 <small>入力状態を変えて回路を繰り返し実行</small></h3>
        <ul className="toolbox-panel__metrics">{PROCESS_METRICS.map(metricRow)}</ul>
      </div>
      <div className="toolbox-panel__group">
        <h3>環境の検証 <small>現在の環境設定で行う1量子ビットの別実験</small></h3>
        <ul className="toolbox-panel__metrics">{SWEEP_METRICS.map(metricRow)}</ul>
      </div>
      <p className="toolbox-panel__summary" role="status">
        追加の指標: {enabledCount === 0 ? 'なし' : `${enabledCount} 項目`}
      </p>
    </section>
  )
}
