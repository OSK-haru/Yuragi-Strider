import type { ReactNode } from 'react'
import './ToolboxPanel.css'
import { SectionHeader } from './SectionHeader'
import {
  PULSE_PANEL_KEYS,
  PULSE_PANEL_LABELS,
  PULSE_TOOLBOX_METRIC_KEYS,
  PULSE_TOOLBOX_METRIC_LABELS,
  resolvedPulseEntropyTransmons,
  type PulsePanelKey,
  type PulseToolboxCheck,
  type PulseToolboxGeometry,
  type PulseToolboxMetricKey,
  type PulseToolboxSettings,
} from '../utils/pulseToolbox'

type PulseToolboxPanelProps = {
  settings: PulseToolboxSettings
  check: PulseToolboxCheck
  geometry: PulseToolboxGeometry
  onChange: (settings: PulseToolboxSettings) => void
}

/* 各指標の1行説明。何を測るか。 */
const METRIC_DESCRIPTIONS: Record<PulseToolboxMetricKey, string> = {
  entropy: '選んだトランズモンの縮約状態の S(ρ) [bit]。3準位では純度と独立な量で、複数台では残りとのもつれを表す。',
  concurrence: '2準位の2台の縮約状態のエンタングルメント（0〜1）。',
  negativity: '選んだトランズモンと残りの間のエンタングルメント。漏れ準位も含めた全空間で、部分転置の負の固有値の和。',
  mutualInformation: 'I(A:B) = S(A) + S(B) − S(AB) [bit]。古典相関も含む全相関。',
  observables: '計算準位 {0,1} 上のパウリ演算子 ⟨P⟩。X, Y は位相の情報で、準位 2 には作用しない。',
}

/* 標準の表示項目の1行説明。 */
const PANEL_DESCRIPTIONS: Record<PulsePanelKey, string> = {
  physical: '各時刻の物理時間と、実行にかかった時間の内訳。',
  metrics: '忠実度・純度などの指標を時間に沿って並べたグラフ。',
  populations: '各準位の占有数を時間に沿って並べたグラフ。',
  probabilities: '計算基底の確率を並べて比べる棒グラフ。',
  output: '最終的な占有確率の分布。',
  density: '密度行列の各要素をヒートマップで表示。',
}

const METRIC_ORDER: PulseToolboxMetricKey[] = [...PULSE_TOOLBOX_METRIC_KEYS]

export function PulseToolboxPanel({ settings, check, geometry, onChange }: PulseToolboxPanelProps) {
  const transmons = Array.from({ length: geometry.count }, (_, index) => index)
  const update = <Key extends keyof PulseToolboxSettings>(key: Key, value: PulseToolboxSettings[Key]) => {
    onChange({ ...settings, [key]: value })
  }
  const toggleMetric = (key: PulseToolboxMetricKey, enabled: boolean) => {
    onChange({ ...settings, metrics: { ...settings.metrics, [key]: enabled } })
  }
  const enabledCount = Object.values(settings.metrics).filter(Boolean).length

  const transmonChips = (
    selected: number[],
    onSelect: (next: number[]) => void,
    label: string,
  ) => (
    <fieldset className="toolbox-panel__chips">
      <legend>{label}</legend>
      {transmons.map((transmon) => (
        <label key={transmon} className="toolbox-panel__chip">
          <input
            type="checkbox"
            checked={selected.includes(transmon)}
            onChange={(event) => onSelect(
              event.target.checked
                ? [...selected, transmon].sort((left, right) => left - right)
                : selected.filter((entry) => entry !== transmon),
            )}
          />
          q{transmon}
        </label>
      ))}
    </fieldset>
  )

  const transmonSelect = (value: number, onSelect: (next: number) => void, label: string) => (
    <label className="toolbox-panel__field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onSelect(Number(event.target.value))}>
        {transmons.map((transmon) => <option key={transmon} value={transmon}>q{transmon}</option>)}
      </select>
    </label>
  )

  const options = (key: PulseToolboxMetricKey): ReactNode => {
    switch (key) {
      case 'entropy':
        return transmonChips(
          resolvedPulseEntropyTransmons(settings, geometry.count),
          (next) => update('entropyTransmons', next.length === geometry.count ? [] : next),
          '対象のトランズモン',
        )
      case 'concurrence':
        return (
          <div className="toolbox-panel__row">
            {transmonSelect(settings.concurrenceTransmons[0], (next) => update('concurrenceTransmons', [next, settings.concurrenceTransmons[1]]), '1つ目')}
            {transmonSelect(settings.concurrenceTransmons[1], (next) => update('concurrenceTransmons', [settings.concurrenceTransmons[0], next]), '2つ目')}
          </div>
        )
      case 'negativity':
        return transmonChips(settings.negativitySubsystem, (next) => update('negativitySubsystem', next), '分割の片側 A（残りが B）')
      case 'mutualInformation':
        return (
          <>
            {transmonChips(settings.mutualInformationA, (next) => update('mutualInformationA', next), 'A')}
            {transmonChips(settings.mutualInformationB, (next) => update('mutualInformationB', next), 'B')}
          </>
        )
      case 'observables':
        return (
          <label className="toolbox-panel__field toolbox-panel__field--wide">
            <span>パウリ文字列（カンマ区切り、左端が q0）</span>
            <input
              type="text"
              value={settings.observablesInput}
              placeholder={geometry.count >= 2
                ? `${'X'.repeat(geometry.count)}, ${'Z'.repeat(2)}${'I'.repeat(geometry.count - 2)}`
                : 'X, Y'}
              onChange={(event) => update('observablesInput', event.target.value)}
              spellCheck={false}
            />
          </label>
        )
      default:
        return null
    }
  }

  const metricRow = (key: PulseToolboxMetricKey) => {
    const unavailable = check.unavailable[key]
    const error = check.errors[key]
    const enabled = settings.metrics[key]
    const descriptionId = `pulse-toolbox-${key}-description`
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
          <span>{PULSE_TOOLBOX_METRIC_LABELS[key]}</span>
        </label>
        <p id={descriptionId} className="toolbox-panel__description">{METRIC_DESCRIPTIONS[key]}</p>
        {unavailable !== undefined ? (
          <p className="toolbox-panel__unavailable">この構成では使えません: {unavailable}</p>
        ) : null}
        {enabled && unavailable === undefined ? (
          <div className="toolbox-panel__options">{options(key)}</div>
        ) : null}
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
        description="標準の表示は出し入れだけです。追加の指標は、保存した軌跡の密度行列から計算するので、シミュレーションをやり直さずに選べます。"
      />

      <div className="toolbox-panel__group">
        <h3>標準の表示 <small>出し入れだけ・計算は変わらない</small></h3>
        <ul className="toolbox-panel__metrics">
          {PULSE_PANEL_KEYS.map((key) => (
            <li key={key} className="toolbox-panel__metric" data-enabled={settings.panels[key]}>
              <label className="toolbox-panel__metric-toggle">
                <input
                  type="checkbox"
                  checked={settings.panels[key]}
                  aria-describedby={`pulse-toolbox-panel-${key}-description`}
                  onChange={(event) => update('panels', { ...settings.panels, [key]: event.target.checked })}
                />
                <span>{PULSE_PANEL_LABELS[key]}</span>
              </label>
              <p id={`pulse-toolbox-panel-${key}-description`} className="toolbox-panel__description">{PANEL_DESCRIPTIONS[key]}</p>
            </li>
          ))}
        </ul>
      </div>

      <div className="toolbox-panel__group">
        <h3>状態の指標 <small>各時刻の密度行列から計算・追加の実行なし</small></h3>
        <ul className="toolbox-panel__metrics">{METRIC_ORDER.map(metricRow)}</ul>
      </div>
      <p className="toolbox-panel__summary" role="status">
        追加の指標: {enabledCount === 0 ? 'なし' : `${enabledCount} 項目`}
      </p>
    </section>
  )
}
