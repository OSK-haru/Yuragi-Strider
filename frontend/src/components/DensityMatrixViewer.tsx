import { Fragment, useMemo, useState, type CSSProperties } from 'react'
import './DensityMatrixViewer.css'
import type { StateSnapshot } from '../types/simulation'
import { DensityMatrixCanvas } from './DensityMatrixCanvas'
import { phaseBand } from '../utils/phaseColor'
import {
  densityCellAt,
  formatDensityValue,
  formatSnapshotProgress,
  formatSnapshotTimeUs,
  snapshotKindLabel,
  validateDensityMatrixSnapshot,
  type DensityMatrixCell,
  type DensityMatrixMode,
  type ValidatedDensityMatrix,
} from '../utils/densityMatrix'

type DensityView = 'grid' | 'raster'

type DensityMatrixViewerProps = {
  snapshots?: StateSnapshot[] | null
  snapshotIndex?: number
}

const MODE_OPTIONS: Array<{ label: string; value: DensityMatrixMode }> = [
  { label: '絶対値', value: 'magnitude' },
  { label: '実部', value: 'real' },
  { label: '虚部', value: 'imaginary' },
  { label: '位相', value: 'phase' },
]

type DensityCellStyle = CSSProperties & {
  '--density-cell-alpha': string
  '--density-cell-ink': string
}

type ActiveCellSelection = {
  key: string
  cell: DensityMatrixCell
}

type MatrixCoordinate = {
  row: number
  column: number
}

const NEIGHBORHOOD_SIZE = 5

export function DensityMatrixViewer({
  snapshots,
  snapshotIndex: requestedSnapshotIndex = 0,
}: DensityMatrixViewerProps) {
  const safeSnapshots = Array.isArray(snapshots) ? snapshots : []
  const [mode, setMode] = useState<DensityMatrixMode>('magnitude')
  const [preferredView, setPreferredView] = useState<DensityView | null>(null)
  const [activeCellSelection, setActiveCellSelection] = useState<ActiveCellSelection | null>(null)
  const [rowQuery, setRowQuery] = useState('')
  const [columnQuery, setColumnQuery] = useState('')
  const [searchedCoordinate, setSearchedCoordinate] = useState<MatrixCoordinate | null>(null)
  const [searchError, setSearchError] = useState<string | null>(null)
  // 共有インデックスは、実際に描画する行列だけ自分の配列に丸める。
  const snapshotIndex = clamp(requestedSnapshotIndex, 0, Math.max(safeSnapshots.length - 1, 0))
  const activeCellKey = `${snapshotIndex}:${mode}`

  const activeSnapshot = safeSnapshots[snapshotIndex]

  /*
   * 8量子ビットでは 65536 セル分の走査になるので、毎レンダーでは回さない。
   * （ホバーで state が動くだけでも再計算されてしまう。）
   */
  const validation = useMemo(
    () => validateDensityMatrixSnapshot(activeSnapshot, mode),
    [activeSnapshot, mode],
  )
  const searchedCell = validation.valid && searchedCoordinate !== null
    ? densityCellAt(validation.matrix, searchedCoordinate.row, searchedCoordinate.column)
    : null
  const activeCell = activeCellSelection?.key === activeCellKey
    ? activeCellSelection.cell
    : searchedCell

  if (safeSnapshots.length === 0) {
    return (
      <p className="density-matrix-viewer__empty">
        この実行には密度行列スナップショットがありません。
      </p>
    )
  }

  function searchMatrixElement() {
    if (!validation.valid) {
      return
    }

    const { dimension, qubitCount } = validation.matrix
    const row = parseBasisIndex(rowQuery, dimension, qubitCount)
    const column = parseBasisIndex(columnQuery, dimension, qubitCount)
    if (row === null || column === null) {
      setSearchError(
        `行と列には、${qubitCount}ビットの基底ラベルまたは0〜${dimension - 1}の番号を入力してください。`,
      )
      return
    }

    selectSearchedCoordinate({ row, column }, validation.matrix.labels)
  }

  function selectSearchedCoordinate(
    coordinate: MatrixCoordinate,
    labels: string[],
  ) {
    setSearchedCoordinate(coordinate)
    setRowQuery(labels[coordinate.row])
    setColumnQuery(labels[coordinate.column])
    setSearchError(null)
    setActiveCellSelection(null)
  }

  return (
    <div className="density-matrix-viewer">
      <div className="density-matrix-viewer__toolbar">
        <div className="density-matrix-viewer__modes" aria-label="密度行列の表示モード">
          {MODE_OPTIONS.map((option) => (
            <button
              className="density-matrix-viewer__mode"
              data-active={mode === option.value}
              type="button"
              aria-pressed={mode === option.value}
              key={option.value}
              onClick={() => setMode(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div className="density-matrix-viewer__metadata" aria-label="スナップショットのメタデータ">
        <span>{snapshotKindLabel(activeSnapshot)}</span>
        <span>経過時間 {formatSnapshotTimeUs(activeSnapshot.time_us)}</span>
        <span>回路全体の進捗 {formatSnapshotProgress(activeSnapshot.progress)}</span>
        {activeSnapshot?.column_index == null ? null : (
          <span>列 {activeSnapshot.column_index + 1}</span>
        )}
        {activeSnapshot?.requested_time_us == null ? null : (
          <span>指定時刻 {formatSnapshotTimeUs(activeSnapshot.requested_time_us)}</span>
        )}
      </div>

      {!validation.valid ? (
        <p className="density-matrix-viewer__empty">{validation.message}</p>
      ) : (
        <>
          <div className="density-matrix-viewer__caption">
            <span>
              {validation.matrix.dimension}x{validation.matrix.dimension} 密度行列 /
              {' '}
              {validation.matrix.qubitCount} qubits
            </span>
            <span>スナップショットごとのスケール: {validation.matrix.scaleLabel}</span>
            {validation.matrix.gridRenderable ? (
              <span className="density-matrix-viewer__views">
                {(['grid', 'raster'] as const).map((view) => (
                  <button
                    className="density-matrix-viewer__view"
                    data-active={resolveView(preferredView, validation.matrix.gridRenderable) === view}
                    type="button"
                    aria-pressed={resolveView(preferredView, validation.matrix.gridRenderable) === view}
                    key={view}
                    onClick={() => setPreferredView(view)}
                  >
                    {view === 'grid' ? 'グリッド' : 'ヒートマップ'}
                  </button>
                ))}
              </span>
            ) : (
              <span className="density-matrix-viewer__views">
                GPU ラスター描画（{validation.matrix.dimension}² セル）
              </span>
            )}
          </div>

          <section
            className="density-matrix-viewer__search"
            aria-label="行列要素を検索"
          >
            <div className="density-matrix-viewer__search-heading">
              <div>
                <h4>行列要素を検索</h4>
                <p>
                  行と列を指定すると、その要素を中心に周辺を拡大表示します。
                </p>
              </div>
              {searchedCoordinate === null ? null : (
                <button
                  className="density-matrix-viewer__search-clear"
                  type="button"
                  onClick={() => {
                    setSearchedCoordinate(null)
                    setSearchError(null)
                    setActiveCellSelection(null)
                  }}
                >
                  選択を解除
                </button>
              )}
            </div>

            <form
              className="density-matrix-viewer__search-form"
              onSubmit={(event) => {
                event.preventDefault()
                searchMatrixElement()
              }}
            >
              <label className="density-matrix-viewer__search-field">
                <span>行の基底状態</span>
                <input
                  className="density-matrix-viewer__search-input"
                  value={rowQuery}
                  inputMode="numeric"
                  placeholder={validation.matrix.labels[0]}
                  aria-invalid={searchError !== null}
                  onChange={(event) => {
                    setRowQuery(event.currentTarget.value)
                    setSearchError(null)
                  }}
                />
              </label>
              <label className="density-matrix-viewer__search-field">
                <span>列の基底状態</span>
                <input
                  className="density-matrix-viewer__search-input"
                  value={columnQuery}
                  inputMode="numeric"
                  placeholder={validation.matrix.labels[validation.matrix.dimension - 1]}
                  aria-invalid={searchError !== null}
                  onChange={(event) => {
                    setColumnQuery(event.currentTarget.value)
                    setSearchError(null)
                  }}
                />
              </label>
              <button className="density-matrix-viewer__search-submit" type="submit">
                周辺を表示
              </button>
              <p className="density-matrix-viewer__search-help">
                {validation.matrix.qubitCount}ビットの2進基底ラベル（例:
                {' '}
                {validation.matrix.labels[Math.min(1, validation.matrix.dimension - 1)]}）または
                10進番号（0〜{validation.matrix.dimension - 1}）で指定できます。
              </p>
              {searchError === null ? null : (
                <p className="density-matrix-viewer__search-error" role="alert">
                  {searchError}
                </p>
              )}
            </form>
          </section>

          {resolveView(preferredView, validation.matrix.gridRenderable) === 'raster' ? (
            <DensityMatrixCanvas
              matrix={validation.matrix}
              mode={mode}
              selectedCell={searchedCell === null ? null : searchedCoordinate}
              onInspect={(cell) => setActiveCellSelection(
                cell === null ? null : { key: activeCellKey, cell },
              )}
            />
          ) : (
          <div
            className="density-matrix-viewer__matrix-scroll"
            tabIndex={0}
            onMouseLeave={() => setActiveCellSelection(null)}
          >
            <div
              className="density-matrix-viewer__matrix"
              style={{
                gridTemplateColumns: `var(--density-label-size) repeat(${validation.matrix.dimension}, var(--density-cell-size))`,
              }}
              role="grid"
              aria-label={`${MODE_OPTIONS.find((option) => option.value === mode)?.label} 密度行列ヒートマップ`}
            >
              <div className="density-matrix-viewer__corner" aria-hidden="true" />
              {validation.matrix.labels.map((label) => (
                <div
                  className="density-matrix-viewer__axis-label density-matrix-viewer__axis-label--column"
                  role="columnheader"
                  key={`column-${label}`}
                >
                  {label}
                </div>
              ))}

              {validation.matrix.labels.map((rowLabel, rowIndex) => (
                <Fragment key={`row-${rowLabel}`}>
                  <div
                    className="density-matrix-viewer__axis-label density-matrix-viewer__axis-label--row"
                    role="rowheader"
                    key={`row-label-${rowLabel}`}
                  >
                    {rowLabel}
                  </div>
                  {validation.matrix.cells
                    .filter((cell) => cell.row === rowIndex)
                    .map((cell) => {
                      const { fill, band } = densityCellFill(cell, mode)
                      const cellStyle = densityCellStyle(cell, mode)

                      return (
                        <div
                          className="density-matrix-viewer__cell"
                          data-sign={fill}
                          data-phase-band={band ?? undefined}
                          data-selected={
                            searchedCoordinate?.row === cell.row
                            && searchedCoordinate.column === cell.column
                          }
                          role="gridcell"
                          tabIndex={0}
                          key={`${cell.row}-${cell.column}`}
                          style={cellStyle}
                          title={cellTitle(cell)}
                          aria-label={cellTitle(cell)}
                          onFocus={() => setActiveCellSelection({ key: activeCellKey, cell })}
                          onMouseEnter={() => setActiveCellSelection({ key: activeCellKey, cell })}
                        >
                          <span className="density-matrix-viewer__cell-value">
                            {mode === 'phase' ? formatPhaseCellValue(cell) : formatCellValue(cell.value)}
                          </span>
                        </div>
                      )
                    })}
                </Fragment>
              ))}
            </div>
          </div>
          )}

          {searchedCoordinate === null || searchedCell === null ? null : (
            <DensityMatrixNeighborhood
              matrix={validation.matrix}
              mode={mode}
              selected={searchedCoordinate}
              activeCellKey={activeCellKey}
              onInspect={setActiveCellSelection}
              onSelect={(coordinate) => selectSearchedCoordinate(
                coordinate,
                validation.matrix.labels,
              )}
            />
          )}

          <div className="density-matrix-viewer__details" aria-live="polite">
            {activeCell === null ? (
              <span>セルにカーソルを合わせるかフォーカスすると、正確な値を確認できます。</span>
            ) : (
              <>
                <strong>
                  rho[{activeCell.rowLabel}, {activeCell.columnLabel}]
                </strong>
                <span>実部: {formatDensityValue(activeCell.real)}</span>
                <span>虚部: {formatDensityValue(activeCell.imag)}</span>
                <span>絶対値: {formatDensityValue(activeCell.magnitude)}</span>
                <span>位相: {formatPhase(activeCell.phase)}</span>
              </>
            )}
          </div>

          <p className="density-matrix-viewer__note">
            基底ラベルは Yuragi-Strider の q0 -&gt; qN-1 の順序に従います。
          </p>
        </>
      )}
    </div>
  )
}

type DensityMatrixNeighborhoodProps = {
  matrix: ValidatedDensityMatrix
  mode: DensityMatrixMode
  selected: MatrixCoordinate
  activeCellKey: string
  onInspect: (selection: ActiveCellSelection | null) => void
  onSelect: (coordinate: MatrixCoordinate) => void
}

function DensityMatrixNeighborhood({
  matrix,
  mode,
  selected,
  activeCellKey,
  onInspect,
  onSelect,
}: DensityMatrixNeighborhoodProps) {
  const rowIndices = neighborhoodIndices(selected.row, matrix.dimension, NEIGHBORHOOD_SIZE)
  const columnIndices = neighborhoodIndices(selected.column, matrix.dimension, NEIGHBORHOOD_SIZE)
  const selectedCell = densityCellAt(matrix, selected.row, selected.column)

  if (selectedCell === null) {
    return null
  }

  return (
    <section className="density-matrix-viewer__neighborhood" aria-label="検索した行列要素の周辺">
      <div className="density-matrix-viewer__neighborhood-heading">
        <div>
          <span className="density-matrix-viewer__neighborhood-eyebrow">検索結果</span>
          <h4>
            ρ[{selectedCell.rowLabel}, {selectedCell.columnLabel}] の周辺
          </h4>
        </div>
        <span>
          行 {selectedCell.row} / 列 {selectedCell.column}
        </span>
      </div>

      <div className="density-matrix-viewer__neighborhood-scroll">
        <div
          className="density-matrix-viewer__neighborhood-matrix"
          style={{
            gridTemplateColumns: `var(--density-neighborhood-label-size) repeat(${columnIndices.length}, var(--density-neighborhood-cell-size))`,
          }}
          role="grid"
          aria-label={`ρ[${selectedCell.rowLabel}, ${selectedCell.columnLabel}] を中心とした周辺要素`}
          onMouseLeave={() => onInspect(null)}
        >
          <div className="density-matrix-viewer__neighborhood-corner" aria-hidden="true" />
          {columnIndices.map((column) => (
            <div
              className="density-matrix-viewer__neighborhood-axis"
              role="columnheader"
              key={`neighborhood-column-${column}`}
            >
              {matrix.labels[column]}
            </div>
          ))}

          {rowIndices.map((row) => (
            <Fragment key={`neighborhood-row-${row}`}>
              <div className="density-matrix-viewer__neighborhood-axis" role="rowheader">
                {matrix.labels[row]}
              </div>
              {columnIndices.map((column) => {
                const cell = densityCellAt(matrix, row, column)!
                const { fill, band } = densityCellFill(cell, mode)
                const cellStyle = densityCellStyle(cell, mode)
                const isSelected = row === selected.row && column === selected.column

                return (
                  <button
                    className="density-matrix-viewer__cell density-matrix-viewer__cell--neighborhood"
                    data-sign={fill}
                    data-phase-band={band ?? undefined}
                    data-selected={isSelected}
                    type="button"
                    role="gridcell"
                    key={`${row}-${column}`}
                    style={cellStyle}
                    title={cellTitle(cell)}
                    aria-label={`${cellTitle(cell)}${isSelected ? '\n検索した要素' : ''}`}
                    onFocus={() => onInspect({ key: activeCellKey, cell })}
                    onMouseEnter={() => onInspect({ key: activeCellKey, cell })}
                    onClick={() => onSelect({ row, column })}
                  >
                    <span className="density-matrix-viewer__cell-value">
                      {formatNeighborhoodValue(cell.value)}
                    </span>
                  </button>
                )
              })}
            </Fragment>
          ))}
        </div>
      </div>
      <p className="density-matrix-viewer__neighborhood-note">
        白い二重枠が検索した要素です。周辺のセルを選ぶと、その位置を中心に表示し直します。
      </p>
    </section>
  )
}

/* 格子が読めるサイズならグリッド既定、超えたら強制的にラスター。 */
function resolveView(preferred: DensityView | null, gridRenderable: boolean): DensityView {
  if (!gridRenderable) {
    return 'raster'
  }
  return preferred ?? 'grid'
}

function cellTitle(cell: DensityMatrixCell): string {
  return [
    `rho[${cell.rowLabel}, ${cell.columnLabel}]`,
    `実部: ${formatDensityValue(cell.real)}`,
    `虚部: ${formatDensityValue(cell.imag)}`,
    `絶対値: ${formatDensityValue(cell.magnitude)}`,
    `位相: ${formatPhase(cell.phase)}`,
  ].join('\n')
}

/*
 * セルの塗り分け。絶対値はインク一色、実部・虚部は全画面共通の
 * 正=赤 / 負=青、位相は循環ランプの帯（utils/phaseColor.ts）。
 */
function densityCellFill(
  cell: DensityMatrixCell,
  mode: DensityMatrixMode,
): { fill: 'magnitude' | 'phase' | DensityMatrixCell['sign']; band: number | null } {
  if (mode === 'magnitude') {
    return { fill: 'magnitude', band: null }
  }
  if (mode === 'phase') {
    /*
     * 位相モードの value は位相そのものなので、その符号で「ゼロ」を判定すると
     * 位相 0 の要素（対角成分など）が空セルに見えてしまう。絶対値で判定する。
     */
    return cell.magnitude < 1e-12
      ? { fill: 'zero', band: null }
      : { fill: 'phase', band: phaseBand(cell.phase) }
  }
  return { fill: cell.sign, band: null }
}

function densityCellStyle(cell: DensityMatrixCell, mode: DensityMatrixMode): DensityCellStyle {
  const { fill, band } = densityCellFill(cell, mode)
  const alpha = 0.08 + cell.intensity * 0.82
  return {
    '--density-cell-alpha': `${alpha}`,
    /* 塗りが薄いうちは地の上の文字と同じ。濃くなったら塗りに合わせて選ぶ。 */
    '--density-cell-ink': alpha > 0.45 ? strongFillText(fill, band) : 'var(--tt-ink-max)',
  }
}

/*
 * 濃い塗りの上の文字色。赤と赤寄りの帯は両テーマで暗いので白、青と橙は
 * 両テーマで明るいので黒。インク色はテーマで明暗が入れ替わるので、
 * その反対の地色（--tt-void）を使う。
 */
function strongFillText(fill: ReturnType<typeof densityCellFill>['fill'], band: number | null): string {
  if (band !== null) {
    return PHASE_BAND_TEXT[band] ?? 'var(--tt-ink-max)'
  }
  if (fill === 'positive') return '#ffffff'
  if (fill === 'negative') return '#0a0a0a'
  if (fill === 'magnitude') return 'var(--tt-void)'
  return 'var(--tt-ink-max)'
}

/* 帯 0..7: インク, インク/橙, 橙, 橙/赤, 赤, 赤/青, 青, 青/インク。 */
const PHASE_BAND_TEXT = [
  'var(--tt-void)', 'var(--tt-void)', '#0a0a0a', '#ffffff',
  '#ffffff', '#ffffff', '#0a0a0a', 'var(--tt-void)',
]

function parseBasisIndex(query: string, dimension: number, qubitCount: number): number | null {
  const normalized = query.trim().replace(/^\|/, '').replace(/[>⟩]$/, '').trim()
  if (new RegExp(`^[01]{${qubitCount}}$`).test(normalized)) {
    return Number.parseInt(normalized, 2)
  }
  if (!/^\d+$/.test(normalized)) {
    return null
  }
  const index = Number.parseInt(normalized, 10)
  return index >= 0 && index < dimension ? index : null
}

function neighborhoodIndices(center: number, dimension: number, size: number): number[] {
  const visibleSize = Math.min(size, dimension)
  const start = clamp(
    center - Math.floor(visibleSize / 2),
    0,
    dimension - visibleSize,
  )
  return Array.from({ length: visibleSize }, (_, offset) => start + offset)
}

function formatPhase(value: number): string {
  if (!Number.isFinite(value) || Math.abs(value) < 0.0000005) {
    return '0 rad'
  }
  return `${value.toFixed(4)} rad`
}

/* 位相 0 は「値が無い」ではないので空欄にしない。空欄は絶対値がゼロの要素だけ。 */
function formatPhaseCellValue(cell: DensityMatrixCell): string {
  if (cell.magnitude < 1e-12) {
    return ''
  }
  return Math.abs(cell.phase) < 0.005 ? '0' : formatCellValue(cell.phase)
}

function formatCellValue(value: number): string {
  if (Math.abs(value) < 0.0005) {
    return ''
  }
  if (Math.abs(value) >= 1) {
    return value.toFixed(1)
  }
  return value.toFixed(2)
}

function formatNeighborhoodValue(value: number): string {
  if (Math.abs(value) < 0.0000005) {
    return '0'
  }
  if (Math.abs(value) < 0.001) {
    return value.toExponential(1)
  }
  return value.toFixed(3)
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value))
}
