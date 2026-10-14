import { useEffect } from 'react'
import { CircuitWorkspace } from './CircuitWorkspace'
import { useCircuitContext } from '../context/useCircuitContext'
import type { GateDurationDefaults } from '../types/simulation'

/**
 * いま囲んでいる CircuitProvider の回路に繋いだ回路エディター。
 * 攻撃シミュレーターと通常モードのIDE画面が使う。
 */
export function ContextCircuitEditor({ gateDurationDefaults, minimumLogicalQubits = 2, maximumLogicalQubits }: {
  gateDurationDefaults: GateDurationDefaults
  minimumLogicalQubits?: 1 | 2
  maximumLogicalQubits?: number
}) {
  const circuit = useCircuitContext()
  const width = circuit.circuitState.logical_qubits
  const shrink = circuit.handleLogicalQubitsChange
  /* A circuit wider than the environment allows cannot be submitted; narrow it (undo restores it). */
  useEffect(() => {
    if (maximumLogicalQubits !== undefined && width > maximumLogicalQubits) {
      shrink(Math.max(minimumLogicalQubits, maximumLogicalQubits))
    }
  }, [maximumLogicalQubits, minimumLogicalQubits, width, shrink])
  return <CircuitWorkspace
    minimumLogicalQubits={minimumLogicalQubits}
    maximumLogicalQubits={maximumLogicalQubits}
    circuit={circuit.circuitState}
    gateDurationDefaults={gateDurationDefaults}
    selectedGateType={circuit.selectedGateType}
    selectedControlValue={circuit.selectedControlValue}
    selectedGateId={circuit.selectedGateId}
    pendingCnotControl={circuit.pendingCnotControl}
    dragPayload={circuit.dragPayload}
    editorHint={circuit.editorHint}
    canUndo={circuit.canUndoCircuit}
    canRedo={circuit.canRedoCircuit}
    canDeleteSelected={circuit.canDeleteSelected}
    canClearCircuit={circuit.canClearCircuit}
    canRemoveLastColumn={circuit.canRemoveLastCircuitColumn}
    onSelectGateType={circuit.handleSelectGateType}
    onSelectControlValue={circuit.handleSelectControlValue}
    onControlMarkerDragStart={circuit.handleControlMarkerDragStart}
    onSelectLogicalQubits={circuit.handleLogicalQubitsChange}
    onUndo={circuit.handleUndoCircuit}
    onRedo={circuit.handleRedoCircuit}
    onDeleteSelected={circuit.handleDeleteSelectedGate}
    onUpdateSelectedGateTheta={circuit.handleUpdateSelectedGateTheta}
    onUpdateSelectedGateMarkedIndex={circuit.handleUpdateSelectedGateMarkedIndex}
    onReverseSelectedGateRegister={circuit.handleReverseSelectedGateRegister}
    onClearCircuit={circuit.handleClearCircuit}
    onAddColumn={circuit.handleAddCircuitColumn}
    onRemoveLastColumn={circuit.handleRemoveLastCircuitColumn}
    onGateDragStart={circuit.handlePaletteGateDragStart}
    onGateDragEnd={circuit.handleGateDragEnd}
    onSlotClick={circuit.handleCircuitSlotClick}
    onGateSelect={circuit.handleGateSelect}
    onCircuitGateDragStart={circuit.handleCircuitGateDragStart}
    onDragEnd={circuit.handleGateDragEnd}
    onSlotDrop={circuit.handleCircuitSlotDrop}
    onColumnInsertDrop={circuit.handleCircuitColumnInsertDrop}
    onDeleteGate={circuit.handleDeleteGate}
    onDuplicateGate={circuit.handleDuplicateGate}
    onShiftGateColumn={circuit.handleShiftGateColumn}
    onImportCircuitConfig={circuit.handleImportCircuitConfig}
  />
}
