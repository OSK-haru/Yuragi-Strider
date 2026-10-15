import './GatePalette.css'
import { useState } from 'react'
import { useRovingToolbars } from '../hooks/useRovingToolbars'
import type { GateType } from '../types/circuit'
import { setCircuitDragPreview } from '../utils/dragPreview'
import {
  isControlledGateType,
  isPairGateType,
  isRegisterGateType,
  MAX_SUPPORTED_LOGICAL_QUBITS,
  MIN_SUPPORTED_LOGICAL_QUBITS,
  MIN_REGISTER_GATE_QUBITS,
} from '../utils/circuitEditing'
import { GateInfoCard } from './GateInfoCard'
import { gateReference } from '../utils/gateReference'

type GatePaletteProps = {
  minimumLogicalQubits?: 1 | 2
  /** Upper bound for this editor, never above the editor's own support. */
  maximumLogicalQubits?: number
  selectedGateType: GateType | null
  selectedControlValue: 0 | 1 | null
  logicalQubits: number
  onSelectGateType: (gateType: GateType | null) => void
  onSelectControlValue: (controlValue: 0 | 1 | null) => void
  onControlMarkerDragStart: (controlValue: 0 | 1) => void
  onSelectLogicalQubits: (logicalQubits: number) => void
  onGateDragStart: (gateType: GateType) => void
  onGateDragEnd: () => void
}

const singleQubitGateTypes: GateType[] = [
  'H', 'X', 'Y', 'Z', 'S', 'T', 'RX', 'RY', 'RZ',
]
const draggableGateTypes = new Set<GateType>([
  'H', 'X', 'Y', 'Z', 'S', 'T', 'RX', 'RY', 'RZ', 'MEASURE',
  'CNOT', 'CZ', 'CP', 'CCX', 'SWAP', 'QFT', 'ORACLE', 'MESSAGE', 'RECEIVED',
])
function clampLogicalQubits(value: number, minimumLogicalQubits: 1 | 2, maximumLogicalQubits: number) {
  if (!Number.isFinite(value)) {
    return minimumLogicalQubits
  }
  return Math.min(
    maximumLogicalQubits,
    Math.max(minimumLogicalQubits, Math.round(value)),
  )
}

export function GatePalette({
  minimumLogicalQubits = MIN_SUPPORTED_LOGICAL_QUBITS,
  maximumLogicalQubits: requestedMaximum,
  selectedGateType,
  selectedControlValue,
  logicalQubits,
  onSelectGateType,
  onSelectControlValue,
  onControlMarkerDragStart,
  onSelectLogicalQubits,
  onGateDragStart,
  onGateDragEnd,
}: GatePaletteProps) {
  const maximumLogicalQubits = Math.max(minimumLogicalQubits,
    Math.min(MAX_SUPPORTED_LOGICAL_QUBITS, requestedMaximum ?? MAX_SUPPORTED_LOGICAL_QUBITS))
  const rootRef = useRovingToolbars<HTMLElement>()
  // 入力途中の空欄や範囲外の値を一時的に持てるよう、表示用の文字列は別で管理する。
  const [qubitDraft, setQubitDraft] = useState(String(logicalQubits))
  /*
   * 外から量子ビット数が変わったとき（Undo・プリセット読み込みなど）は、
   * 下書きを追従させる。effect で setState すると再レンダリングが連鎖するので、
   * 直前の値を覚えておいてレンダリング中に判定する。
   * https://react.dev/learn/you-might-not-need-an-effect
   */
  const [lastLogicalQubits, setLastLogicalQubits] = useState(logicalQubits)
  if (lastLogicalQubits !== logicalQubits) {
    setLastLogicalQubits(logicalQubits)
    setQubitDraft(String(logicalQubits))
  }

  function commitQubitDraft(rawValue: string) {
    const parsed = Number.parseInt(rawValue, 10)
    if (Number.isNaN(parsed)) {
      setQubitDraft(String(logicalQubits))
      return
    }
    const next = clampLogicalQubits(parsed, minimumLogicalQubits, maximumLogicalQubits)
    setQubitDraft(String(next))
    if (next !== logicalQubits) {
      onSelectLogicalQubits(next)
    }
  }

  function renderGateButton(gateType: GateType) {
    const isSelected = selectedGateType === gateType
    const isRegisterType = isRegisterGateType(gateType)
    const isUnavailable =
      (gateType === 'CCX' && logicalQubits < 3) ||
      (isRegisterType && logicalQubits < MIN_REGISTER_GATE_QUBITS)
    const isDraggable = draggableGateTypes.has(gateType) && !isUnavailable
    const isStretchType = isControlledGateType(gateType) || isPairGateType(gateType)
    const label = gateType === 'MEASURE' ? 'M' : gateType
    const entry = gateReference[gateType]
    const title = isUnavailable
      ? gateType === 'CCX'
        ? 'CCXには3量子ビット以上が必要です'
        : `${gateType}には${MIN_REGISTER_GATE_QUBITS}量子ビット以上が必要です`
      : isRegisterType
        ? `${gateType} を選択し、量子ビットをビット0(最下位)から順にクリックしてレジスタを作成（選択済みをもう一度クリックで確定）。ドラッグすると落とした行から下を一括で指定`
          + (gateType === 'ORACLE' ? '。配置後にインスペクタでマークする状態を指定します' : '')
        : isStretchType
          ? `${gateType} を選択し、量子ビットをクリックしてから接続先をクリック、またはドラッグして配置`
          : `${gateType} を選択してスロットをクリック、またはドラッグして配置。列と列のあいだに落とすと新しい列が入ります`

    return (
      <div
        key={gateType}
        className="gate-palette__item"
        data-family={entry.family}
        /* チュートリアルが「このゲートを置いて」と指し示すための目印。 */
        data-tutorial-anchor={`gate-${gateType}`}
      >
        <button
          type="button"
          className={`gate-palette__button${
            isSelected ? ' gate-palette__button--selected' : ''
          }${isDraggable ? ' gate-palette__button--draggable' : ''}`}
          aria-pressed={isSelected}
          disabled={isUnavailable}
          draggable={isDraggable}
          title={title}
          onDragStart={(event) => {
            if (!isDraggable) {
              event.preventDefault()
              return
            }
            event.dataTransfer.effectAllowed = 'copy'
            event.dataTransfer.setData('text/plain', `palette:${gateType}`)
            setCircuitDragPreview(event, gateType, 'palette')
            onGateDragStart(gateType)
          }}
          onDragEnd={onGateDragEnd}
          onClick={() => onSelectGateType(gateType)}
        >
          {label}
        </button>
        <div className="gate-palette__popover">
          <GateInfoCard gateLabel={gateType} entry={entry} />
        </div>
      </div>
    )
  }

  return (
    <section ref={rootRef} className="gate-palette" aria-label="ゲートパレット" data-tutorial-anchor="gate-palette">
      <div className="gate-palette__group" aria-label="論理量子ビット">
        <label className="gate-palette__label" htmlFor="gate-palette-qubit-input">
          量子ビット
        </label>
        <div className="gate-palette__qubit-input-row">
          <input
            id="gate-palette-qubit-input"
            className="gate-palette__qubit-input"
            type="number"
            inputMode="numeric"
            min={minimumLogicalQubits}
            max={maximumLogicalQubits}
            step={1}
            value={qubitDraft}
            onChange={(event) => setQubitDraft(event.target.value)}
            onBlur={(event) => commitQubitDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                commitQubitDraft(event.currentTarget.value)
              }
            }}
          />
          <span className="gate-palette__qubit-input-range">
            {minimumLogicalQubits === maximumLogicalQubits
              ? `${maximumLogicalQubits}（この環境の上限）`
              : `${minimumLogicalQubits}〜${maximumLogicalQubits}`}
          </span>
        </div>
        {logicalQubits > 5 ? (
          <p className="gate-palette__qubit-limit-note" role="status">
            6〜8量子ビットのノイズありシミュレーションは高コストです。少ない時間ステップから試してください。
          </p>
        ) : null}
      </div>

      <p className="gate-palette__placement-hint">
        ゲートを選んでスロットを<strong>クリック</strong>、または<strong>ドラッグ</strong>して配置。
        列と列のあいだに落とすと新しい列が入ります。選択中のゲートは Delete で削除できます。
      </p>

      <div className="gate-palette__group" role="toolbar" aria-label="1量子ビットゲート">
        <span className="gate-palette__label">1量子ビット</span>
        <div className="gate-palette__buttons">
          {singleQubitGateTypes.map((gateType) => renderGateButton(gateType))}
        </div>
      </div>

      <div className="gate-palette__group" role="toolbar" aria-label="測定ゲート">
        <span className="gate-palette__label">測定</span>
        <div className="gate-palette__buttons">{renderGateButton('MEASURE')}</div>
      </div>

      <div className="gate-palette__group" role="toolbar" aria-label="制御ゲート">
        <span className="gate-palette__label">制御</span>
        <div className="gate-palette__buttons">
          <div className="gate-palette__item" data-family="control">
            <button
              type="button"
              className={`gate-palette__button gate-palette__control-symbol gate-palette__button--draggable${selectedControlValue === 1 ? ' gate-palette__button--selected' : ''}`}
              aria-label="制御点"
              aria-pressed={selectedControlValue === 1}
              draggable
              title="制御点 ●：単体ではゲートになりません。同じ列の X（または既存CNOTの縦線）へ繋げると制御Xになります"
              onClick={() => onSelectControlValue(selectedControlValue === 1 ? null : 1)}
              onDragStart={(event) => {
                event.dataTransfer.effectAllowed = 'copy'
                event.dataTransfer.setData('text/plain', 'palette:control-1')
                setCircuitDragPreview(event, '●', 'palette')
                onControlMarkerDragStart(1)
              }}
              onDragEnd={onGateDragEnd}
            >
              ●
            </button>
          </div>
          <div className="gate-palette__item" data-family="control">
            <button
              type="button"
              className={`gate-palette__button gate-palette__control-symbol gate-palette__button--draggable${selectedControlValue === 0 ? ' gate-palette__button--selected' : ''}`}
              aria-label="反制御点"
              aria-pressed={selectedControlValue === 0}
              draggable
              title="反制御点 ○（制御値0）：単体ではゲートになりません。同じ列の X（または既存CNOTの縦線）へ繋げると制御Xになります"
              onClick={() => onSelectControlValue(selectedControlValue === 0 ? null : 0)}
              onDragStart={(event) => {
                event.dataTransfer.effectAllowed = 'copy'
                event.dataTransfer.setData('text/plain', 'palette:control-0')
                setCircuitDragPreview(event, '○', 'palette')
                onControlMarkerDragStart(0)
              }}
              onDragEnd={onGateDragEnd}
            >
              ○
            </button>
          </div>
          {renderGateButton('CNOT')}
          {renderGateButton('CZ')}
          {renderGateButton('CP')}
          {renderGateButton('CCX')}
          {renderGateButton('SWAP')}
        </div>
      </div>
      <div className="gate-palette__group" role="toolbar" aria-label="レジスタゲート">
        <span className="gate-palette__label">レジスタ</span>
        <div className="gate-palette__buttons">
          {renderGateButton('QFT')}
          {renderGateButton('ORACLE')}
        </div>
      </div>
      <div className="gate-palette__group" role="toolbar" aria-label="Teleportation display markers">
        <span className="gate-palette__label">通信表示</span>
        <div className="gate-palette__buttons">
          {renderGateButton('MESSAGE')}
          {renderGateButton('RECEIVED')}
        </div>
      </div>
    </section>
  )
}
