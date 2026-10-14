import { useState, type PointerEvent as ReactPointerEvent } from 'react'
import './ToolboxSeriesChart.css'

type ToolboxSeriesChartProps = {
  /* 図の名前。読み上げと表の見出しに使う。 */
  label: string
  times: number[]
  values: Array<number | null>
  /* 縦軸の範囲。指標ごとに理論上の取りうる範囲を渡す。 */
  yDomain: [number, number]
  format: (value: number) => string
  /* 再生バーと共有する時刻。 */
  cursorTimeUs: number | null
  compact?: boolean
}

const width = 640
const padding = { left: 48, right: 16, top: 14, bottom: 30 }

/*
 * ツール箱の指標を1系列ずつ描く。系列ごとに図を分けるので色で区別する必要がなく、
 * 線は文字色1色にする。赤は再生バーと共有する時刻カーソルだけに使う。
 */
export function ToolboxSeriesChart({
  label,
  times,
  values,
  yDomain,
  format,
  cursorTimeUs,
  compact = false,
}: ToolboxSeriesChartProps) {
  const height = compact ? 132 : 200
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const points = times
    .map((time, index) => ({ time, value: values[index] ?? null, index }))
    .filter((point): point is { time: number; value: number; index: number } => point.value !== null)

  if (points.length === 0) {
    return <p className="toolbox-series__empty">表示できる値がありません。</p>
  }

  const minimumTime = Math.min(...times)
  const maximumTime = Math.max(...times)
  const [low, high] = yDomain
  const innerWidth = width - padding.left - padding.right
  const innerHeight = height - padding.top - padding.bottom
  const x = (time: number) => maximumTime <= minimumTime
    ? padding.left
    : padding.left + ((time - minimumTime) / (maximumTime - minimumTime)) * innerWidth
  const y = (value: number) => {
    const bounded = Math.max(low, Math.min(high, value))
    return padding.top + (1 - (bounded - low) / (high - low)) * innerHeight
  }

  /* 値の欠けた点（理想状態と対応しないスナップショット）で線を切る。 */
  let path = ''
  let previousIndex = -2
  for (const point of points) {
    path += `${point.index === previousIndex + 1 ? 'L' : 'M'} ${x(point.time).toFixed(2)} ${y(point.value).toFixed(2)} `
    previousIndex = point.index
  }

  const valueAt = (time: number): number | null => {
    if (time <= points[0].time) return points[0].value
    const last = points[points.length - 1]
    if (time >= last.time) return last.value
    for (let index = 1; index < points.length; index += 1) {
      const current = points[index]
      if (time <= current.time) {
        const previous = points[index - 1]
        const span = current.time - previous.time
        return span <= 0 ? current.value : previous.value + (current.value - previous.value) * ((time - previous.time) / span)
      }
    }
    return last.value
  }

  const clampedCursor = cursorTimeUs === null ? null : Math.max(minimumTime, Math.min(maximumTime, cursorTimeUs))
  const cursorValue = clampedCursor === null ? null : valueAt(clampedCursor)
  const finalPoint = points[points.length - 1]
  const hovered = hoverIndex === null ? null : points[hoverIndex] ?? null
  const ticks = [low, (low + high) / 2, high]
  const showMarkers = points.length <= 40

  const handlePointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const svgX = ((event.clientX - rect.left) / rect.width) * width
    let nearest = 0
    for (let index = 1; index < points.length; index += 1) {
      if (Math.abs(x(points[index].time) - svgX) < Math.abs(x(points[nearest].time) - svgX)) nearest = index
    }
    setHoverIndex(nearest)
  }

  return (
    <div className="toolbox-series">
      <svg
        className="toolbox-series__chart"
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${label}の時間変化。最終値 ${format(finalPoint.value)}`}
        onPointerMove={handlePointerMove}
        onPointerLeave={() => setHoverIndex(null)}
      >
        {ticks.map((tick) => (
          <g key={tick}>
            <line className="toolbox-series__grid" x1={padding.left} x2={width - padding.right} y1={y(tick)} y2={y(tick)} />
            {/* 目盛りは単位を外した数字だけにする。単位は値の欄と見出しにある。 */}
            <text className="toolbox-series__tick" x={padding.left - 8} y={y(tick) + 4} textAnchor="end">{Number(tick.toFixed(2)).toString()}</text>
          </g>
        ))}
        <text className="toolbox-series__tick" x={padding.left} y={height - 8} textAnchor="start">{minimumTime.toFixed(3)} μs</text>
        <text className="toolbox-series__tick" x={width - padding.right} y={height - 8} textAnchor="end">{maximumTime.toFixed(3)} μs</text>
        <path className="toolbox-series__line" d={path} />
        {showMarkers ? points.map((point) => (
          <circle key={point.index} className="toolbox-series__dot" cx={x(point.time)} cy={y(point.value)} r={4} />
        )) : null}
        {clampedCursor === null ? null : (
          <g className="toolbox-series__cursor">
            <line x1={x(clampedCursor)} x2={x(clampedCursor)} y1={padding.top} y2={height - padding.bottom} />
            {cursorValue === null ? null : <circle cx={x(clampedCursor)} cy={y(cursorValue)} r={5} />}
          </g>
        )}
        {hovered === null ? null : (
          <g className="toolbox-series__hover">
            <line x1={x(hovered.time)} x2={x(hovered.time)} y1={padding.top} y2={height - padding.bottom} />
            <circle cx={x(hovered.time)} cy={y(hovered.value)} r={5} />
            <text
              x={x(hovered.time) + (x(hovered.time) > width / 2 ? -10 : 10)}
              y={padding.top + 12}
              textAnchor={x(hovered.time) > width / 2 ? 'end' : 'start'}
            >
              {`${hovered.time.toFixed(4)} μs · ${format(hovered.value)}`}
            </text>
          </g>
        )}
      </svg>
      <dl className="toolbox-series__values">
        <div>
          <dt>再生位置</dt>
          <dd>{cursorValue === null ? '—' : format(cursorValue)}</dd>
        </div>
        <div>
          <dt>最終値（{finalPoint.time.toFixed(3)} μs）</dt>
          <dd>{format(finalPoint.value)}</dd>
        </div>
      </dl>
      <details className="toolbox-series__table">
        <summary>数値の表</summary>
        <table>
          <caption>{label}</caption>
          <thead><tr><th scope="col">時刻 [μs]</th><th scope="col">値</th></tr></thead>
          <tbody>
            {times.map((time, index) => (
              <tr key={index}>
                <td>{time.toFixed(4)}</td>
                <td>{values[index] === null || values[index] === undefined ? '—' : format(values[index] as number)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  )
}
