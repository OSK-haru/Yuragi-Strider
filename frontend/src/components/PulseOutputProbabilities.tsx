import { useMemo, useState } from 'react'
import './PulseOutputProbabilities.css'
import { SectionHeader } from './SectionHeader'
import {
  formatPulseProbability,
  nearestPulsePointIndex,
  type PulseExplorerView,
} from '../utils/pulseStateExplorer'

type PulseOutputProbabilitiesProps = {
  view: PulseExplorerView
  cursorTimeUs: number | null
}

/* 基底が多いモデルでは、確率の小さい基底まで並べても読めない。 */
const compactBasisThreshold = 12

/*
 * Gate-aware の「出力確率」に対応する分布表。
 * 読み出し誤差が無効なら密度行列の対角成分、有効ならそこへ
 * 観測段の混同行列を適用した分布を読む。計算部分空間の外は別枠で示す。
 */
export function PulseOutputProbabilities({
  view,
  cursorTimeUs,
}: PulseOutputProbabilitiesProps) {
  const [showAllBasis, setShowAllBasis] = useState(view.basisLabels.length <= compactBasisThreshold)
  const activeIndex = cursorTimeUs === null
    ? Math.max(view.points.length - 1, 0)
    : nearestPulsePointIndex(view.points, cursorTimeUs)
  const activePoint = view.points[activeIndex] ?? null
  const displayedPopulations = activePoint === null
    ? null
    : view.readoutEnabled
      ? activePoint.observedPopulations
      : activePoint.populations
  const computationalSet = useMemo(
    () => new Set(view.computationalLabels),
    [view.computationalLabels],
  )
  const rows = useMemo(() => {
    if (activePoint === null) {
      return []
    }
    const all = view.basisLabels.map((label) => ({
      label,
      probability: displayedPopulations?.[label] ?? 0,
      computational: computationalSet.has(label),
    }))
    if (showAllBasis) {
      return all
    }
    return [...all]
      .sort((left, right) => right.probability - left.probability)
      .slice(0, compactBasisThreshold)
  }, [activePoint, computationalSet, displayedPopulations, showAllBasis, view.basisLabels])
  const computationalTotal = activePoint === null
    ? 0
    : view.computationalLabels.reduce(
        (total, label) => total + (displayedPopulations?.[label] ?? 0),
        0,
      )

  return (
    <section className="pulse-output" aria-label="Pulseの占有確率分布">
      <SectionHeader
        icon="bars"
        eyebrow="結果"
        title="占有確率分布"
        description={`基底 ${view.basisLabels.length} 個 / 密度行列 ${view.dimension} × ${view.dimension}。${view.readoutEnabled ? '表示は対角成分へ読み出し誤差を適用した観測分布です。' : '表示は密度行列の対角成分です。'}`}
      />

      {activePoint === null ? (
        <p className="pulse-output__empty">占有確率を利用できません。</p>
      ) : (
        <>
          <div className="pulse-output__sample-explorer">
            <div className="pulse-output__sample-meta">
              <strong>{activePoint.timeUs.toFixed(4)} μs</strong>
              <span>
                {activePoint.timeUs > view.pulseEndTimeUs ? 'Pulse終了後' : '駆動中'}
              </span>
              {activePoint.stepLabel ? <span>{activePoint.stepLabel}</span> : null}
              <span>純度 {activePoint.purity.toFixed(6)}</span>
              {view.readoutEnabled ? <span>読み出し誤差を適用</span> : null}
            </div>
          </div>

          <div className="pulse-output__totals">
            <article>
              <span>計算部分空間</span>
              <strong>{formatPulseProbability(computationalTotal)}</strong>
            </article>
            {view.leakageLabel === null ? null : (
              <article data-tone="leakage">
                <span>{view.leakageLabel}</span>
                <strong>{formatPulseProbability(activePoint.leakage ?? 0)}</strong>
              </article>
            )}
          </div>

          {view.basisLabels.length > compactBasisThreshold ? (
            <button
              type="button"
              className="pulse-output__toggle"
              onClick={() => setShowAllBasis((current) => !current)}
            >
              {showAllBasis
                ? `確率の大きい上位 ${compactBasisThreshold} 基底だけを表示`
                : `全 ${view.basisLabels.length} 基底を表示`}
            </button>
          ) : null}

          <div className="pulse-output__rows" role="table" aria-label="占有確率">
            {rows.map((row) => (
              <div
                className="pulse-output__row"
                role="row"
                key={row.label}
                data-computational={row.computational}
              >
                <span className="pulse-output__state" role="cell">|{row.label}⟩</span>
                <div className="pulse-output__bar-track" aria-hidden="true">
                  <div
                    className="pulse-output__bar-fill"
                    style={{ width: `${Math.max(0, Math.min(row.probability, 1)) * 100}%` }}
                  />
                </div>
                <span className="pulse-output__probability" role="cell">
                  {row.probability.toFixed(6)}
                </span>
              </div>
            ))}
          </div>

          <p className="pulse-output__note">
            Pulse-levelでは射影測定そのものは時間発展に含めません。読み出し誤差が有効な場合だけ、
            密度行列の対角成分へ観測段の誤分類を適用して表示します。計算部分空間の外にある確率は、
            読み出し誤差では移動させず、そのまま漏れとして表示します。
          </p>
        </>
      )}
    </section>
  )
}
