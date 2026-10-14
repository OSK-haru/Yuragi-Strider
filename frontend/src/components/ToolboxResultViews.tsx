import { useState } from 'react'
import './ToolboxResultViews.css'
import type {
  ProcessCharacterizationResponse,
  RamseySweepResponse,
  SerializableComplexMatrix,
  SweepPoint,
  T1SweepResponse,
} from '../types/simulation'

/* 平均ゲート忠実度。厳密値か、モンテカルロ推定とその不確かさ。 */
export function AverageGateFidelityView({ result }: { result: ProcessCharacterizationResponse }) {
  const exact = result.method === 'exact_tomography'
  return (
    <div className="toolbox-result">
      <dl className="toolbox-result__cards">
        <div>
          <dt>平均ゲート忠実度 F̄</dt>
          <dd>{result.average_gate_fidelity.toFixed(6)}</dd>
        </div>
        {exact ? null : (
          <>
            <div>
              <dt>標準誤差</dt>
              <dd>{result.standard_error.toExponential(2)}</dd>
            </div>
            {result.confidence_interval_95 ? (
              <div>
                <dt>95% 区間</dt>
                <dd>{`${result.confidence_interval_95[0].toFixed(5)} – ${result.confidence_interval_95[1].toFixed(5)}`}</dd>
              </div>
            ) : null}
          </>
        )}
        <div>
          <dt>回路の長さ</dt>
          <dd>{result.circuit_duration_us.toFixed(4)} μs</dd>
        </div>
      </dl>
      <p className="toolbox-result__note">
        {exact
          ? `厳密なプロセストモグラフィー（入力 ${result.simulation_count} 通り）で求めた値です。`
          : `ランダムなパウリ固有状態を ${result.monte_carlo_samples ?? result.simulation_count} 回入力した不偏推定です（seed ${result.monte_carlo_seed ?? 0}）。`}
        {result.noise_free_environment ? ' 環境ノイズが0なので、過程は理想ユニタリそのものです。' : ''}
        回路の開始から完了までを1つの過程とし、完了後の待ち時間は含みません。
      </p>
    </div>
  )
}

/* χ 行列。符号のない大きさだけを文字色の濃さで描く。 */
export function ProcessMatrixView({ result }: { result: ProcessCharacterizationResponse }) {
  const matrix = result.process_matrix
  if (!matrix) {
    return <p className="toolbox-result__note">この結果には χ 行列がありません。</p>
  }
  return (
    <div className="toolbox-result">
      <div className="toolbox-result__matrices">
        <ChiHeatmap title="ノイズあり" basis={matrix.basis} matrix={matrix.noisy} />
        <ChiHeatmap title="理想" basis={matrix.basis} matrix={matrix.ideal} />
      </div>
      <dl className="toolbox-result__cards">
        <div>
          <dt>Tr χ</dt>
          <dd>{matrix.trace.toFixed(8)}</dd>
        </div>
        <div>
          <dt>最小固有値</dt>
          <dd>{matrix.minimum_eigenvalue.toExponential(2)}</dd>
        </div>
      </dl>
      <p className="toolbox-result__note">
        E(ρ) = Σ χ<sub>mn</sub> P<sub>m</sub> ρ P<sub>n</sub>、Tr χ = 1 の規格化です。最小固有値が負でないことは、
        再構成した過程が完全正値であることの確認です。
      </p>
    </div>
  )
}

function ChiHeatmap({ title, basis, matrix }: { title: string; basis: string[]; matrix: SerializableComplexMatrix }) {
  const [focus, setFocus] = useState<{ row: number; column: number } | null>(null)
  const size = basis.length
  const magnitudes = matrix.real.map((row, rowIndex) => row.map((real, columnIndex) => Math.hypot(real, matrix.imag[rowIndex][columnIndex])))
  const maximum = Math.max(...magnitudes.flat(), 1e-12)
  const cell = size <= 16 ? 18 : 6
  const labelSpace = size <= 16 ? 8 * basis[0].length + 10 : 0
  const extent = labelSpace + size * cell
  const focused = focus === null ? null : {
    label: `χ(${basis[focus.row]}, ${basis[focus.column]})`,
    real: matrix.real[focus.row][focus.column],
    imag: matrix.imag[focus.row][focus.column],
  }
  return (
    <figure className="toolbox-result__matrix">
      <figcaption>{title}</figcaption>
      <svg
        viewBox={`0 0 ${extent} ${extent}`}
        role="img"
        aria-label={`${title}の χ 行列の大きさ。最大 ${maximum.toFixed(4)}`}
        onPointerLeave={() => setFocus(null)}
      >
        {labelSpace > 0 ? basis.map((label, index) => (
          <g key={label}>
            <text className="toolbox-result__axis-label" x={labelSpace - 4} y={labelSpace + index * cell + cell * 0.7} textAnchor="end">{label}</text>
            <text
              className="toolbox-result__axis-label"
              x={labelSpace + index * cell + cell * 0.7}
              y={labelSpace - 4}
              transform={`rotate(-90 ${labelSpace + index * cell + cell * 0.7} ${labelSpace - 4})`}
            >
              {label}
            </text>
          </g>
        )) : null}
        {magnitudes.map((row, rowIndex) => row.map((magnitude, columnIndex) => (
          <rect
            key={`${rowIndex}-${columnIndex}`}
            className="toolbox-result__cell"
            x={labelSpace + columnIndex * cell}
            y={labelSpace + rowIndex * cell}
            width={cell}
            height={cell}
            style={{ fillOpacity: magnitude / maximum }}
            data-focused={focus?.row === rowIndex && focus?.column === columnIndex}
            onPointerEnter={() => setFocus({ row: rowIndex, column: columnIndex })}
          />
        )))}
      </svg>
      <p className="toolbox-result__readout" aria-live="polite">
        {focused === null
          ? 'セルに触れると値を表示します。'
          : `${focused.label} = ${focused.real.toFixed(5)} ${focused.imag < 0 ? '−' : '+'} ${Math.abs(focused.imag).toFixed(5)}i`}
      </p>
      <details className="toolbox-result__table">
        <summary>大きい要素の表</summary>
        <table>
          <thead><tr><th scope="col">m</th><th scope="col">n</th><th scope="col">実部</th><th scope="col">虚部</th></tr></thead>
          <tbody>
            {largestEntries(matrix, basis).map((entry) => (
              <tr key={`${entry.m}-${entry.n}`}>
                <td>{entry.m}</td><td>{entry.n}</td><td>{entry.real.toFixed(5)}</td><td>{entry.imag.toFixed(5)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  )
}

function largestEntries(matrix: SerializableComplexMatrix, basis: string[]) {
  const entries = matrix.real.flatMap((row, rowIndex) => row.map((real, columnIndex) => ({
    m: basis[rowIndex],
    n: basis[columnIndex],
    real,
    imag: matrix.imag[rowIndex][columnIndex],
    magnitude: Math.hypot(real, matrix.imag[rowIndex][columnIndex]),
  })))
  return entries.sort((left, right) => right.magnitude - left.magnitude).slice(0, 16)
}

/* T1 / Ramsey の観測点と適合曲線。どちらも同じ点の形を返す。 */
function SweepChart({ points, floor, label }: { points: SweepPoint[]; floor: number; label: string }) {
  const width = 640
  const height = 260
  const margin = { left: 52, right: 16, top: 16, bottom: 40 }
  const innerWidth = width - margin.left - margin.right
  const innerHeight = height - margin.top - margin.bottom
  const maxTime = Math.max(...points.map((point) => point.delay_us), 1e-12)
  const x = (time: number) => margin.left + (time / maxTime) * innerWidth
  const y = (probability: number) => margin.top + (1 - probability) * innerHeight
  const path = (key: 'true_p1' | 'fitted_observed_p1') => points
    .map((point, index) => `${index === 0 ? 'M' : 'L'} ${x(point.delay_us).toFixed(2)} ${y(point[key]).toFixed(2)}`)
    .join(' ')
  return (
    <svg className="toolbox-result__sweep" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      {[0, 0.5, 1].map((tick) => (
        <g key={tick}>
          <line className="toolbox-result__grid" x1={margin.left} x2={width - margin.right} y1={y(tick)} y2={y(tick)} />
          <text className="toolbox-result__tick" x={margin.left - 8} y={y(tick) + 4} textAnchor="end">{tick.toFixed(1)}</text>
        </g>
      ))}
      {[0, 0.5, 1].map((fraction) => (
        <text key={fraction} className="toolbox-result__tick" x={x(maxTime * fraction)} y={height - 20} textAnchor="middle">
          {(maxTime * fraction).toFixed(0)}
        </text>
      ))}
      <text className="toolbox-result__tick" x={margin.left + innerWidth / 2} y={height - 4} textAnchor="middle">遅延 [μs]</text>
      <line className="toolbox-result__floor" x1={margin.left} x2={width - margin.right} y1={y(floor)} y2={y(floor)} />
      <path className="toolbox-result__true" d={path('true_p1')} />
      <path className="toolbox-result__fit" d={path('fitted_observed_p1')} />
      {points.map((point) => (
        <circle key={point.delay_us} className="toolbox-result__point" cx={x(point.delay_us)} cy={y(point.observed_p1)} r={4}>
          <title>{`${point.delay_us.toFixed(3)} μs: 観測 P₁ = ${point.observed_p1.toFixed(6)}`}</title>
        </circle>
      ))}
    </svg>
  )
}

function SweepLegend({ fitLabel }: { fitLabel: string }) {
  return (
    <ul className="toolbox-result__legend">
      <li data-series="observed">観測 P₁（点）</li>
      <li data-series="fit">{fitLabel}（実線）</li>
      <li data-series="true">読み出し前 P₁（破線）</li>
      <li data-series="floor">観測床 B（点線）</li>
    </ul>
  )
}

function formatOptionalUs(value: number | null) {
  return value === null ? '—' : `${value.toFixed(3)} μs`
}

export function T1SweepView({ result }: { result: T1SweepResponse }) {
  return (
    <div className="toolbox-result">
      <dl className="toolbox-result__cards">
        <div><dt>適合 T1</dt><dd>{result.fit.t1_us.toFixed(3)} μs</dd></div>
        <div><dt>設定から導いた T1</dt><dd>{formatOptionalUs(result.reference.t1_effective_us)}</dd></div>
        <div><dt>観測床 B</dt><dd>{result.fit.observation_floor.toFixed(6)}</dd></div>
        <div><dt>R²</dt><dd>{result.fit.r_squared === null ? '—' : result.fit.r_squared.toFixed(8)}</dd></div>
      </dl>
      <SweepChart points={result.points} floor={result.fit.observation_floor} label="T1 減衰の観測点と指数適合" />
      <SweepLegend fitLabel="A exp(−t/T₁) + B の適合" />
      <p className="toolbox-result__note">
        |1⟩ から待つだけの1量子ビット実験です。B は熱平衡の占有と読み出し誤差を合わせた観測床です。
      </p>
    </div>
  )
}

export function RamseySweepView({ result }: { result: RamseySweepResponse }) {
  return (
    <div className="toolbox-result">
      <dl className="toolbox-result__cards">
        <div><dt>適合 T2*</dt><dd>{result.fit.t2_star_us.toFixed(3)} μs</dd></div>
        <div><dt>設定から導いた T2</dt><dd>{formatOptionalUs(result.reference.t2_effective_us)}</dd></div>
        <div><dt>適合した離調</dt><dd>{result.fit.frequency_mhz.toFixed(6)} MHz</dd></div>
        <div><dt>選ばれた包絡</dt><dd>{result.selected_model === 'exponential_envelope' ? '指数' : 'ガウス'}</dd></div>
      </dl>
      <SweepChart points={result.points} floor={result.fit.observation_floor} label="Ramsey 干渉の観測点と包絡適合" />
      <SweepLegend fitLabel="選ばれた包絡の適合" />
      <table className="toolbox-result__candidates">
        <caption>AICc による包絡の比較</caption>
        <thead><tr><th scope="col">包絡</th><th scope="col">T2*</th><th scope="col">RMSE</th><th scope="col">ΔAICc</th></tr></thead>
        <tbody>
          {result.candidate_ranking_by_aicc.map((candidate, index) => (
            <tr key={candidate.model} data-selected={index === 0}>
              <td>{candidate.model === 'exponential_envelope' ? '指数' : 'ガウス'}{index === 0 ? '（選択）' : ''}</td>
              <td>{candidate.t2_star_us.toFixed(3)} μs</td>
              <td>{candidate.root_mean_square_error.toExponential(3)}</td>
              <td>{candidate.delta_aicc.toFixed(3)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
