import { useEffect, useRef, useState, type ChangeEvent } from 'react'
import './CircuitWorkspace.css'
import { CircuitConfigPreview } from './CircuitConfigPreview'
import { useInternalInfoVisible } from '../context/useAdminMode'
import { CircuitPreview } from './CircuitPreview'
import { CircuitZoomControls } from './CircuitZoomControls'
import { GateInspector } from './GateInspector'
import { GatePalette } from './GatePalette'
import type { CircuitEditorState, CircuitGate, DragGatePayload, GateType } from '../types/circuit'
import type { GateDurationDefaults } from '../types/simulation'
import { isTypingTarget, useCircuitViewport } from '../hooks/useCircuitViewport'
import { exportCircuitConfigBundleJson } from '../utils/circuitConfigTransfer'
import {
  isControlledGateType,
  isMultiControlledGateType,
  isMultiQubitGateType,
  isPairGateType,
} from '../utils/circuitEditing'

type PendingCnotControl = {
  columnIndex: number
  qubitIndex: number
  controlValue?: 0 | 1
  additionalQubits?: number[]
  additionalControlValues?: Array<0 | 1>
}

type CircuitWorkspaceProps = {
  minimumLogicalQubits?: 1 | 2
  maximumLogicalQubits?: number
  circuit: CircuitEditorState
  gateDurationDefaults: GateDurationDefaults
  selectedGateType: GateType | null
  selectedControlValue: 0 | 1 | null
  selectedGateId: string | null
  pendingCnotControl: PendingCnotControl | null
  dragPayload: DragGatePayload | null
  editorHint: string
  canUndo: boolean
  canRedo: boolean
  canDeleteSelected: boolean
  canClearCircuit: boolean
  canRemoveLastColumn: boolean
  onSelectGateType: (gateType: GateType | null) => void
  onSelectControlValue: (controlValue: 0 | 1 | null) => void
  onControlMarkerDragStart: (controlValue: 0 | 1) => void
  onSelectLogicalQubits: (logicalQubits: number) => void
  onUndo: () => void
  onRedo: () => void
  onDeleteSelected: () => void
  onUpdateSelectedGateTheta: (thetaRad: number) => void
  onUpdateSelectedGateMarkedIndex: (markedIndex: number) => void
  onReverseSelectedGateRegister: () => void
  onClearCircuit: () => void
  onAddColumn: () => void
  onRemoveLastColumn: () => void
  onGateDragStart: (gateType: GateType) => void
  onGateDragEnd: () => void
  onSlotClick: (columnIndex: number, qubitIndex: number) => void
  onGateSelect: (gateId: string | null) => void
  onCircuitGateDragStart: (
    gateId: string,
    gateType: GateType,
    columnIndex: number,
    qubitIndex: number,
  ) => void
  onDragEnd: () => void
  onSlotDrop: (columnIndex: number, qubitIndex: number) => void
  onColumnInsertDrop: (insertIndex: number, qubitIndex: number) => void
  onDeleteGate: (gateId: string) => void
  onDuplicateGate: (gateId: string) => void
  onShiftGateColumn: (gateId: string, offset: -1 | 1) => void
  onImportCircuitConfig: (file: File) => Promise<string>
}

type SelectedGateInfo = {
  gate: CircuitGate
  columnIndex: number
} | null

function getSelectedGateInfo(circuit: CircuitEditorState, selectedGateId: string | null): SelectedGateInfo {
  if (!selectedGateId) {
    return null
  }

  for (const [columnIndex, column] of circuit.columns.entries()) {
    const gate = column.gates.find((candidate) => candidate.id === selectedGateId)
    if (gate) {
      return { gate, columnIndex }
    }
  }

  return null
}

function formatGateLocation(gate: CircuitGate) {
  const targetLabel = gate.targets.map((target) => `q${target}`).join(', ')
  const controlLabel = gate.controls?.map((control) => `q${control}`).join(', ')

  if ((isControlledGateType(gate.type) || isMultiControlledGateType(gate.type)) && controlLabel) {
    return `${controlLabel} -> ${targetLabel}`
  }
  if (isPairGateType(gate.type)) {
    return targetLabel
  }

  return targetLabel
}

function getGateDuration(gate: CircuitGate, gateDurationDefaults: GateDurationDefaults) {
  return gate.params?.duration_us ?? gateDurationDefaults[gate.type]
}

function formatDurationLabel(duration: number) {
  const fixed = duration < 0.01 && duration > 0 ? duration.toFixed(3) : duration.toFixed(2)
  return `${fixed.replace(/\.?0+$/, '')} us`
}

function getWorkspaceStatus({
  circuit,
  gateDurationDefaults,
  selectedGateType,
  selectedGateId,
  pendingCnotControl,
  dragPayload,
  editorHint,
}: Pick<
  CircuitWorkspaceProps,
  | 'circuit'
  | 'gateDurationDefaults'
  | 'selectedGateType'
  | 'selectedGateId'
  | 'pendingCnotControl'
  | 'dragPayload'
  | 'editorHint'
>) {
  if (dragPayload) {
    if (dragPayload.source === 'palette' && dragPayload.controlValue !== undefined) {
      return `${dragPayload.controlValue === 1 ? '制御 ●' : '反制御 ○'}をドラッグ中：繋げたい X や CNOT と同じ列へ`
    }
    /* 列と列のあいだにもドロップ先があることは、掴んでいる最中にしか伝わらない。 */
    return `${
      dragPayload.source === 'circuit' ? '' : 'パレットから'
    }${dragPayload.gateType} をドラッグ中：スロットに落とすか、列と列のあいだに落とすと新しい列が入ります`
  }

  /*
   * 制御点（● / ○）は単体では量子ゲートにならない。仮置きのまま放置されると
   * 「置いたのに動かない」ように見えるので、Xが要ることを最優先で出す。
   */
  if (!selectedGateType && pendingCnotControl) {
    const pendingQubits = [
      pendingCnotControl.qubitIndex,
      ...(pendingCnotControl.additionalQubits ?? []),
    ].map((qubit) => `q${qubit}`).join(', ')
    return (
      `制御点だけでは動きません：${pendingQubits}（列 ${pendingCnotControl.columnIndex + 1}）は仮置きです。`
      + '同じ列の別の量子ビットへ X を置くと、制御Xとして接続されます。'
    )
  }

  if (selectedGateType && isMultiQubitGateType(selectedGateType) && pendingCnotControl) {
    const selectedQubits = [
      pendingCnotControl.qubitIndex,
      ...(pendingCnotControl.additionalQubits ?? []),
    ].map((qubit) => `q${qubit}`).join(', ')
    return `${selectedQubits} に続く ${selectedGateType} の量子ビットを選択してください`
  }

  const selectedGate = getSelectedGateInfo(circuit, selectedGateId)
  if (selectedGate) {
    const duration = formatDurationLabel(getGateDuration(selectedGate.gate, gateDurationDefaults))
    return `ゲートを選択中: ${selectedGate.gate.type} ${formatGateLocation(selectedGate.gate)}、${duration}`
  }

  if (selectedGateType) {
    return `${selectedGateType} を配置中`
  }

  return editorHint === 'Ready to edit the circuit.' ? '回路を編集できます' : editorHint || '回路を編集できます'
}

export function CircuitWorkspace({
  minimumLogicalQubits = 2,
  maximumLogicalQubits,
  circuit,
  gateDurationDefaults,
  selectedGateType,
  selectedControlValue,
  selectedGateId,
  pendingCnotControl,
  dragPayload,
  editorHint,
  canUndo,
  canRedo,
  canDeleteSelected,
  canClearCircuit,
  canRemoveLastColumn,
  onSelectGateType,
  onSelectControlValue,
  onControlMarkerDragStart,
  onSelectLogicalQubits,
  onUndo,
  onRedo,
  onDeleteSelected,
  onUpdateSelectedGateTheta,
  onUpdateSelectedGateMarkedIndex,
  onReverseSelectedGateRegister,
  onClearCircuit,
  onAddColumn,
  onRemoveLastColumn,
  onGateDragStart,
  onGateDragEnd,
  onSlotClick,
  onGateSelect,
  onCircuitGateDragStart,
  onDragEnd,
  onSlotDrop,
  onColumnInsertDrop,
  onDeleteGate,
  onDuplicateGate,
  onShiftGateColumn,
  onImportCircuitConfig,
}: CircuitWorkspaceProps) {
  const [scrollToEndToken, setScrollToEndToken] = useState(0)
  const internalInfoVisible = useInternalInfoVisible()
  const [showConfigPreview, setShowConfigPreview] = useState(false)
  const [transferStatus, setTransferStatus] = useState('')
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const workspaceRef = useRef<HTMLElement | null>(null)
  const selectedGateInfo = getSelectedGateInfo(circuit, selectedGateId)
  const viewport = useCircuitViewport({
    columnCount: circuit.columns.length,
    selectedColumnIndex: selectedGateInfo?.columnIndex ?? null,
  })

  const hasPendingControlOnly = selectedGateType === null && pendingCnotControl !== null
  const statusText = getWorkspaceStatus({
    circuit,
    gateDurationDefaults,
    selectedGateType,
    selectedGateId,
    pendingCnotControl,
    dragPayload,
    editorHint,
  })

  useEffect(() => {
    function handleDeleteKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented || workspaceRef.current?.closest('[hidden]')
        || event.key !== 'Delete' || isTypingTarget(event.target) || !canDeleteSelected) {
        return
      }

      event.preventDefault()
      onDeleteSelected()
    }

    window.addEventListener('keydown', handleDeleteKeyDown)
    return () => window.removeEventListener('keydown', handleDeleteKeyDown)
  }, [canDeleteSelected, onDeleteSelected])

  function handleAddColumn() {
    setScrollToEndToken((token) => token + 1)
    onAddColumn()
  }

  function handleExportJson() {
    const json = exportCircuitConfigBundleJson(circuit)
    const blob = new Blob([json], { type: 'application/json' })
    const url = window.URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = '回路データ.json'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    window.setTimeout(() => window.URL.revokeObjectURL(url), 0)
    setTransferStatus('回路データを書き出しました。')
  }

  function openFilePicker() {
    fileInputRef.current?.click()
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) {
      return
    }

    try {
      const message = await onImportCircuitConfig(file)
      setTransferStatus(message)
    } catch (error) {
      /* 解析失敗の本文には内部のフィールド名が並ぶので、通常は伏せる。 */
      setTransferStatus(
        internalInfoVisible && error instanceof Error
          ? error.message
          : '回路データを読み込めませんでした。ファイルの形式を確認してください。',
      )
    }
  }

  return (
    <section className="circuit-workspace" aria-label="回路編集ワークスペース" ref={workspaceRef}>
      <div className="circuit-workspace__toolbar" aria-label="回路編集ツールバー">
        {/*
          ファイル操作は Word などと同じくツールバーの先頭（左上）に置く。
          折り返しレイアウトなので DOM の順番がそのまま表示順になる。
        */}
        <div
          className="circuit-workspace__tool-group circuit-workspace__tool-group--file"
          role="group"
          aria-label="ファイル"
        >
          <span className="circuit-workspace__tool-label">ファイル</span>
          <button type="button" onClick={openFilePicker}>
            インポート
          </button>
          <button type="button" onClick={handleExportJson}>
            エクスポート
          </button>
          <button
            type="button"
            aria-pressed={showConfigPreview}
            onClick={() => setShowConfigPreview((isOpen) => !isOpen)}
          >
            {showConfigPreview ? 'データを隠す' : 'データを確認'}
          </button>
          <input
            ref={fileInputRef}
            className="circuit-workspace__file-input"
            type="file"
            accept=".json,.qscope.json,application/json"
            aria-label="回路データを読み込む"
            onChange={handleFileChange}
          />
        </div>

        <CircuitZoomControls
          zoom={viewport.zoom}
          onZoomOut={viewport.zoomOut}
          onZoomIn={viewport.zoomIn}
          onResetZoom={viewport.resetZoom}
          onFitCircuit={viewport.fitCircuit}
        />
      </div>

      <div className="circuit-workspace__body">
        <GatePalette
          selectedGateType={selectedGateType}
          selectedControlValue={selectedControlValue}
          logicalQubits={circuit.logical_qubits}
          minimumLogicalQubits={minimumLogicalQubits}
          maximumLogicalQubits={maximumLogicalQubits}
          onSelectGateType={onSelectGateType}
          onSelectControlValue={onSelectControlValue}
          onControlMarkerDragStart={onControlMarkerDragStart}
          onSelectLogicalQubits={onSelectLogicalQubits}
          onGateDragStart={onGateDragStart}
          onGateDragEnd={onGateDragEnd}
        />
        <CircuitPreview
          circuit={circuit}
          gateDurationDefaults={gateDurationDefaults}
          selectedGateType={selectedGateType}
          selectedControlValue={selectedControlValue}
          selectedGateId={selectedGateId}
          pendingCnotControl={pendingCnotControl}
          dragPayload={dragPayload}
          zoom={viewport.zoom}
          scrollToEndToken={scrollToEndToken}
          viewportRef={viewport.viewportRef}
          highlightedColumnIndex={viewport.highlightedColumnIndex}
          onViewportScroll={viewport.updateVisibleRange}
          onSlotClick={onSlotClick}
          onGateSelect={onGateSelect}
          onCircuitGateDragStart={onCircuitGateDragStart}
          onDragEnd={onDragEnd}
          onSlotDrop={onSlotDrop}
          onColumnInsertDrop={onColumnInsertDrop}
          onDeleteGate={onDeleteGate}
          onDuplicateGate={onDuplicateGate}
          onShiftGateColumn={onShiftGateColumn}
        />
      </div>

      {/*
        回路そのものを書き換える操作は回路図の直下に置く。上のツールバーは
        ファイル・表示・移動といった、回路の中身を変えない操作だけにする。
      */}
      <div
        className="circuit-workspace__toolbar circuit-workspace__toolbar--edit"
        aria-label="回路の編集操作"
      >
        <div className="circuit-workspace__tool-group" role="group" aria-label="履歴">
          <span className="circuit-workspace__tool-label">履歴</span>
          <button type="button" onClick={onUndo} disabled={!canUndo}>
            元に戻す
          </button>
          <button type="button" onClick={onRedo} disabled={!canRedo}>
            やり直す
          </button>
        </div>

        <div className="circuit-workspace__tool-group" role="group" aria-label="編集操作">
          <span className="circuit-workspace__tool-label">編集</span>
          <button
            className="circuit-workspace__toolbar-danger"
            type="button"
            onClick={onDeleteSelected}
            disabled={!canDeleteSelected}
          >
            選択項目を削除
          </button>
          <button
            className="circuit-workspace__toolbar-danger"
            type="button"
            onClick={onClearCircuit}
            disabled={!canClearCircuit}
          >
            クリア
          </button>
        </div>

        <div className="circuit-workspace__tool-group" role="group" aria-label="列">
          <span className="circuit-workspace__tool-label">列</span>
          <button type="button" onClick={onRemoveLastColumn} disabled={!canRemoveLastColumn}>
            - 列
          </button>
          <span className="circuit-workspace__column-count">列 {circuit.columns.length}</span>
          <button type="button" onClick={handleAddColumn}>
            + 列
          </button>
        </div>
      </div>

      {/*
        インスペクターの開閉ボタンはツールバーから外したので、ゲートを選んだら
        そのまま出す。ORACLE のマーク状態や回転角はここでしか変えられない。
        閉じたいときはゲートの選択を外す。
      */}
      {selectedGateId ? (
        <GateInspector
          circuit={circuit}
          selectedGateId={selectedGateId}
          gateDurationDefaults={gateDurationDefaults}
          onDeleteSelected={onDeleteSelected}
          onThetaChange={onUpdateSelectedGateTheta}
          onMarkedIndexChange={onUpdateSelectedGateMarkedIndex}
          onReverseRegister={onReverseSelectedGateRegister}
          onReveal={viewport.revealSelectedColumn}
        />
      ) : null}

      <div className="circuit-workspace__status-row">
        <div className="circuit-workspace__status-block" aria-live="polite">
          <p
            className={`circuit-workspace__status${
              hasPendingControlOnly ? ' circuit-workspace__status--warning' : ''
            }`}
          >
            {statusText}
          </p>
        </div>
        <p className="circuit-workspace__shortcut-hints">
          Delete/Backspace: 選択項目を削除 | Esc: 選択を解除 | F: 回路に合わせる | Home/End: 最初/最後
        </p>
        <p className="circuit-workspace__transfer-status" aria-live="polite">
          {transferStatus || ' '}
        </p>
      </div>

      {showConfigPreview ? (
        <CircuitConfigPreview
          circuit={circuit}
          onImportCircuitConfig={onImportCircuitConfig}
          defaultOpen
          showTransferActions={false}
        />
      ) : null}
    </section>
  )
}
