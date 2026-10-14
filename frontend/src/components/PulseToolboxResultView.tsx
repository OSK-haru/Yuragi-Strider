import './PulseToolboxResultView.css'
import { ToolboxSeriesChart } from './ToolboxSeriesChart'
import type { PulseToolboxMetricKey, PulseToolboxResponse } from '../utils/pulseToolbox'

export type PulseToolboxViewState =
  | { kind: 'idle'; message: string }
  | { kind: 'pending' }
  | { kind: 'error'; message: string }
  | { kind: 'done'; data: PulseToolboxResponse }

type PulseToolboxResultViewProps = {
  metric: PulseToolboxMetricKey
  state: PulseToolboxViewState
  cursorTimeUs: number
  /* 状態空間の形。縦軸の取りうる範囲の計算に使う。 */
  levels: number
}

const transmonList = (transmons: number[]) => transmons.map((transmon) => `q${transmon}`).join(',')
const bits = (value: number) => `${value.toFixed(4)} bit`
const plain = (value: number) => value.toFixed(4)

export function PulseToolboxResultView({ metric, state, cursorTimeUs, levels }: PulseToolboxResultViewProps) {
  if (state.kind === 'idle') return <p className="pulse-toolbox-result__note">{state.message}</p>
  if (state.kind === 'pending') return <p className="pulse-toolbox-result__note" role="status">計算中…</p>
  if (state.kind === 'error') return <p className="pulse-toolbox-result__note" role="alert">{state.message}</p>

  const { data } = state
  const times = data.times_us
  const chart = (
    label: string,
    values: number[],
    domain: [number, number],
    format: (value: number) => string,
    description: string,
  ) => (
    <>
      <p className="pulse-toolbox-result__note">{description}</p>
      <ToolboxSeriesChart label={label} times={times} values={values} yDomain={domain} format={format} cursorTimeUs={cursorTimeUs} />
    </>
  )

  if (metric === 'entropy' && data.entropy) {
    const { transmons, values } = data.entropy
    /* 最大値は log2(次元)。 */
    const upper = transmons.length * Math.log2(levels)
    const whole = transmons.length === data.transmon_count
    return chart(
      `S(${transmonList(transmons)})`,
      values,
      [0, upper],
      bits,
      whole
        ? `${transmonList(transmons)} 全体のフォン・ノイマン・エントロピー。混合の度合いで、環境との相関を含む。純度とは別の量。`
        : `${transmonList(transmons)} の縮約状態のエントロピー。全体が純粋なら、残りのトランズモンとのエンタングルメントの量。`,
    )
  }
  if (metric === 'concurrence' && data.concurrence) {
    const { transmons, values } = data.concurrence
    return chart(`C(${transmonList(transmons)})`, values, [0, 1], plain, `${transmonList(transmons)} の縮約状態のコンカレンス。0 は分離可能、1 は最大エンタングル。`)
  }
  if (metric === 'negativity' && data.negativity) {
    const { subsystem, values } = data.negativity
    /* 最大値は (min(d_A, d_B) − 1) / 2。 */
    const dimensionA = levels ** subsystem.length
    const dimensionB = levels ** (data.transmon_count - subsystem.length)
    const upper = (Math.min(dimensionA, dimensionB) - 1) / 2
    return chart(`N(${transmonList(subsystem)} | 残り)`, values, [0, upper], plain, `${transmonList(subsystem)} とそれ以外の分割に対するネガティビティ。漏れ準位を含む全空間で測る。0 より大きければエンタングルしている。`)
  }
  if (metric === 'mutualInformation' && data.mutual_information) {
    const { a, b, values } = data.mutual_information
    const upper = 2 * Math.min(a.length, b.length) * Math.log2(levels)
    return chart(`I(${transmonList(a)} : ${transmonList(b)})`, values, [0, upper], bits, `A=${transmonList(a)} と B=${transmonList(b)} の相互情報量。古典相関と量子相関の合計。`)
  }
  if (metric === 'observables' && data.observables) {
    return (
      <div className="pulse-toolbox-result__multiples">
        {data.observables.map((entry) => (
          <section key={entry.pauli}>
            <h3>⟨{entry.pauli}⟩</h3>
            <ToolboxSeriesChart label={`⟨${entry.pauli}⟩`} times={times} values={entry.values} yDomain={[-1, 1]} format={plain} cursorTimeUs={cursorTimeUs} compact />
          </section>
        ))}
      </div>
    )
  }
  return <p className="pulse-toolbox-result__note">この結果では計算していません。</p>
}
