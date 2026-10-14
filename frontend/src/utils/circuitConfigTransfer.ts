import type {
  CircuitColumn,
  CircuitEditorState,
  CircuitGate,
  GateType,
  InitialQubitState,
} from '../types/circuit'
import { circuitEditorStateToConfig } from './circuitConfig'
import {
  findColumnCollision,
  MAX_SUPPORTED_CIRCUIT_COLUMNS,
  MAX_SUPPORTED_LOGICAL_QUBITS,
  MIN_SUPPORTED_LOGICAL_QUBITS,
} from './circuitEditing'

export const MAX_CIRCUIT_IMPORT_BYTES = 64 * 1024
const MAX_CIRCUIT_GATES_PER_COLUMN = 16
const MAX_CIRCUIT_GATES = 800

export type CircuitConfigGate = {
  type: GateType
  targets: number[]
  controls?: number[]
  params?: Record<string, number | undefined>
  classical_targets?: number[]
  condition?: { bit: number; value: 0 | 1 } | null
  conditions?: Array<{ bit: number; value: 0 | 1 }>
}

export type CircuitConfigColumn = {
  step: number
  gates: CircuitConfigGate[]
}

export type CircuitConfig = {
  logical_qubits: number
  initial_states: Array<number | '+' | '-'>
  classical_bits?: number
  columns: CircuitConfigColumn[]
  annotations?: CircuitAnnotationConfig[]
}

export type CircuitConfigBundle = {
  version: 1
  kind: 'quantscope_circuit_config'
  circuit_config: CircuitConfig
}

const WRAPPER_KIND = 'quantscope_circuit_config'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isFiniteInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value)
}

function isFiniteNonNegativeNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

type CircuitAnnotationConfig = {
  kind: 'MESSAGE' | 'RECEIVED'
  id?: string | null
  source_id?: string | null
  column_index: number
  qubits: number[]
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function validateGateParams(params: unknown, gateLabel: string) {
  if (params === undefined) {
    return
  }
  if (!isRecord(params)) {
    throw new Error(`Import failed: ${gateLabel} params must be an object.`)
  }

  if ('duration_us' in params && !isFiniteNonNegativeNumber(params.duration_us)) {
    throw new Error(
      `Import failed: ${gateLabel} params.duration_us must be a finite number greater than or equal to 0.`,
    )
  }
  if ('theta_rad' in params && !isFiniteNumber(params.theta_rad)) {
    throw new Error(`Import failed: ${gateLabel} params.theta_rad must be finite.`)
  }
  if ('control_value' in params && params.control_value !== 0 && params.control_value !== 1) {
    throw new Error(`Import failed: ${gateLabel} params.control_value must be 0 or 1.`)
  }
  if ('control_state' in params && (!isFiniteInteger(params.control_state) || params.control_state < 0)) {
    throw new Error(`Import failed: ${gateLabel} params.control_state must be a non-negative integer.`)
  }
}

function validateGateShape(gate: Record<string, unknown>, logicalQubits: number, step: number, index: number) {
  const gateType = gate.type
  if (
    gateType !== 'H' &&
    gateType !== 'X' &&
    gateType !== 'Y' &&
    gateType !== 'Z' &&
    gateType !== 'S' &&
    gateType !== 'T' &&
    gateType !== 'RX' &&
    gateType !== 'RY' &&
    gateType !== 'RZ' &&
    gateType !== 'CNOT' &&
    gateType !== 'CZ' &&
    gateType !== 'CP' &&
    gateType !== 'CCX' &&
    gateType !== 'SWAP' &&
    gateType !== 'QFT' &&
    gateType !== 'ORACLE' &&
    gateType !== 'MEASURE'
  ) {
    throw new Error(`Import failed: unsupported gate type at step ${step}.`)
  }

  if (!Array.isArray(gate.targets)) {
    throw new Error(`Import failed: gate targets must be an array at step ${step}.`)
  }

  if (gateType === 'CCX') {
    if (!Array.isArray(gate.controls) || gate.controls.length !== 2) {
      throw new Error('Import failed: CCX requires exactly two control qubits.')
    }
    if (gate.targets.length !== 1) {
      throw new Error('Import failed: CCX requires exactly one target qubit.')
    }
    if (new Set([...gate.controls, gate.targets[0]]).size !== 3) {
      throw new Error('Import failed: CCX controls and target must differ.')
    }
  } else if (gateType === 'CNOT') {
    if (!Array.isArray(gate.controls)) {
      throw new Error(`Import failed: ${gateType} controls must be an array at step ${step}.`)
    }
    if (gate.controls.length < 1) {
      throw new Error('Import failed: CNOT requires at least one control qubit.')
    }
    if (gate.targets.length !== 1) {
      throw new Error('Import failed: CNOT requires exactly one target qubit.')
    }
    if (new Set(gate.controls).size !== gate.controls.length || gate.controls.includes(gate.targets[0])) {
      throw new Error('Import failed: CNOT controls and target must all be different.')
    }
    if (isRecord(gate.params) && gate.params.control_state !== undefined) {
      const state = gate.params.control_state
      if (!isFiniteInteger(state) || state < 0 || state >= 2 ** gate.controls.length) {
        throw new Error('Import failed: CNOT control_state is outside the control-bit range.')
      }
    }
  } else if (gateType === 'CZ' || gateType === 'CP') {
    if (!Array.isArray(gate.controls) || gate.controls.length !== 1) {
      throw new Error(`Import failed: ${gateType} requires exactly one control qubit.`)
    }
    if (gate.targets.length !== 1) {
      throw new Error(`Import failed: ${gateType} requires exactly one target qubit.`)
    }
  } else if (gateType === 'SWAP') {
    if (gate.targets.length !== 2 || gate.targets[0] === gate.targets[1]) {
      throw new Error('Import failed: SWAP requires two different target qubits.')
    }
    if (gate.controls !== undefined && (!Array.isArray(gate.controls) || gate.controls.length > 0)) {
      throw new Error('Import failed: SWAP does not accept control qubits.')
    }
  } else if (gateType === 'QFT' || gateType === 'ORACLE') {
    if (gate.targets.length < 1) {
      throw new Error(`Import failed: ${gateType} requires at least one target qubit.`)
    }
    if (new Set(gate.targets).size !== gate.targets.length) {
      throw new Error(`Import failed: ${gateType} target qubits must all be different.`)
    }
    if (gate.controls !== undefined && (!Array.isArray(gate.controls) || gate.controls.length > 0)) {
      throw new Error(`Import failed: ${gateType} does not accept control qubits.`)
    }
    if (gateType === 'ORACLE' && isRecord(gate.params)) {
      const marked = gate.params.marked_index
      if (
        marked !== undefined
        && (!isFiniteInteger(marked) || marked < 0 || marked >= 2 ** gate.targets.length)
      ) {
        throw new Error(
          'Import failed: ORACLE marked_index must sit inside the register range.',
        )
      }
    }
  } else {
    if (gate.targets.length !== 1) {
      throw new Error(`Import failed: ${gateType} requires exactly one target qubit.`)
    }
    if (gate.controls !== undefined && (!Array.isArray(gate.controls) || gate.controls.length > 0)) {
      throw new Error(`Import failed: ${gateType} does not accept control qubits.`)
    }
  }

  const targets = gate.targets
  const controls = Array.isArray(gate.controls) ? gate.controls : []

  for (const target of targets) {
    if (!isFiniteInteger(target) || target < 0 || target >= logicalQubits) {
      throw new Error('Import failed: gate target is outside the logical qubit range.')
    }
  }

  for (const control of controls) {
    if (!isFiniteInteger(control) || control < 0 || control >= logicalQubits) {
      throw new Error('Import failed: gate control is outside the logical qubit range.')
    }
  }

  if ((gateType === 'CNOT' || gateType === 'CZ' || gateType === 'CP') && controls.includes(targets[0])) {
    throw new Error(`Import failed: ${gateType} control and target must differ.`)
  }

  validateGateParams(gate.params, `gate at step ${step}, index ${index}`)
}

function normalizeCircuitConfigShape(value: unknown, minimumLogicalQubits: 1 | 2): CircuitConfig {
  if (!isRecord(value)) {
    throw new Error('Import failed: invalid JSON file.')
  }

  const candidate = value.version !== undefined || value.kind !== undefined || value.circuit_config !== undefined
    ? value.circuit_config
    : value

  if (!isRecord(candidate)) {
    throw new Error('Import failed: invalid circuit_config payload.')
  }

  if (value.circuit_config !== undefined) {
    if (value.version !== 1) {
      throw new Error('Import failed: unsupported circuit file version.')
    }
    if (value.kind !== WRAPPER_KIND) {
      throw new Error('Import failed: unsupported circuit file kind.')
    }
  }

  const logicalQubits = candidate.logical_qubits
  if (
    !isFiniteInteger(logicalQubits) ||
    logicalQubits < minimumLogicalQubits ||
    logicalQubits > MAX_SUPPORTED_LOGICAL_QUBITS
  ) {
    throw new Error(
      `Import failed: logical_qubits must be an integer from ${minimumLogicalQubits} to ${MAX_SUPPORTED_LOGICAL_QUBITS}.`,
    )
  }

  const initialStates = candidate.initial_states
  if (!Array.isArray(initialStates)) {
    throw new Error('Import failed: initial_states must be an array.')
  }
  if (initialStates.length !== logicalQubits) {
    throw new Error('Import failed: initial_states must match logical_qubits.')
  }

  initialStates.forEach((state, index) => {
    if (!(isFiniteInteger(state) && (state === 0 || state === 1)) && state !== '+' && state !== '-') {
      throw new Error(
        `Import failed: initial_states[${index}] must be one of 0, 1, +, or -.`,
      )
    }
  })

  const columns = candidate.columns
  if (!Array.isArray(columns)) {
    throw new Error('Import failed: columns must be an array.')
  }
  if (columns.length < 1) {
    throw new Error('Import failed: columns must include at least one column.')
  }
  if (columns.length > MAX_SUPPORTED_CIRCUIT_COLUMNS) {
    throw new Error(
      `Import failed: circuits may contain at most ${MAX_SUPPORTED_CIRCUIT_COLUMNS} columns.`,
    )
  }

  let gateCount = 0

  const normalizedColumns: CircuitConfigColumn[] = columns.map((column, columnIndex) => {
    if (!isRecord(column)) {
      throw new Error(`Import failed: column ${columnIndex} must be an object.`)
    }
    if (!isFiniteInteger(column.step) || column.step < 0) {
      throw new Error(`Import failed: column ${columnIndex} step must be a non-negative integer.`)
    }
    if (!Array.isArray(column.gates)) {
      throw new Error(`Import failed: column ${columnIndex} gates must be an array.`)
    }
    if (column.gates.length > MAX_CIRCUIT_GATES_PER_COLUMN) {
      throw new Error(
        `Import failed: column ${columnIndex} contains too many gates.`,
      )
    }
    gateCount += column.gates.length
    if (gateCount > MAX_CIRCUIT_GATES) {
      throw new Error(`Import failed: circuits may contain at most ${MAX_CIRCUIT_GATES} gates.`)
    }

    const step = column.step as number
    const normalizedGates = column.gates.map((gate, gateIndex) => {
      if (!isRecord(gate)) {
        throw new Error(
          `Import failed: gate ${gateIndex} in column ${columnIndex} must be an object.`,
        )
      }

      const gateRecord = gate as Record<string, unknown>
      validateGateShape(gateRecord, logicalQubits, step, gateIndex)

      const targets = gateRecord.targets as number[]
      const controls = Array.isArray(gateRecord.controls)
        ? (gateRecord.controls as number[])
        : []
      const params =
        gateRecord.params === undefined
          ? undefined
          : (gateRecord.params as Record<string, number | undefined>)
      const classicalTargets = Array.isArray(gateRecord.classical_targets)
        ? (gateRecord.classical_targets as number[])
        : undefined
      const condition = isRecord(gateRecord.condition)
        ? gateRecord.condition as { bit: number; value: 0 | 1 }
        : undefined
      const conditions = Array.isArray(gateRecord.conditions)
        ? gateRecord.conditions as Array<{ bit: number; value: 0 | 1 }>
        : undefined

      return {
        type: gateRecord.type as GateType,
        targets: [...targets],
        ...(Array.isArray(gateRecord.controls) ? { controls: [...controls] } : {}),
        ...(params === undefined ? {} : { params: { ...params } }),
        ...(classicalTargets === undefined ? {} : { classical_targets: [...classicalTargets] }),
        ...(condition === undefined ? {} : { condition }),
        ...(conditions === undefined ? {} : { conditions: [...conditions] }),
      }
    })

    return {
      step,
      gates: normalizedGates,
    }
  })

  return {
    logical_qubits: logicalQubits,
    initial_states: initialStates as InitialQubitState[],
    ...(typeof candidate.classical_bits === 'number' ? { classical_bits: candidate.classical_bits } : {}),
    columns: normalizedColumns,
    annotations: Array.isArray(candidate.annotations) ? candidate.annotations.filter((annotation): annotation is CircuitAnnotationConfig =>
      isRecord(annotation) && (annotation.kind === 'MESSAGE' || annotation.kind === 'RECEIVED') && isFiniteInteger(annotation.column_index) && Array.isArray(annotation.qubits),
    ) : [],
  }
}

function createImportedGateId(
  gateType: GateType,
  step: number,
  gateIndex: number,
  targets: number[],
  controls: number[],
) {
  const targetPart = targets.length > 0 ? targets.join('-') : 'none'
  const controlPart = controls.length > 0 ? controls.join('-') : 'none'
  return `imported-${gateType.toLowerCase()}-s${step}-g${gateIndex}-t${targetPart}-c${controlPart}`
}

export function circuitConfigToEditorState(config: CircuitConfig): CircuitEditorState {
  const editorState = {
    logical_qubits: config.logical_qubits,
    initial_states: [...config.initial_states] as InitialQubitState[],
    ...(config.classical_bits === undefined ? {} : { classical_bits: config.classical_bits }),
    columns: config.columns.map((column): CircuitColumn => ({
      step: column.step,
      gates: column.gates.map((gate, gateIndex): CircuitGate => ({
        id: createImportedGateId(
          gate.type,
          column.step,
          gateIndex,
          gate.targets,
          gate.controls ?? [],
        ),
        type: gate.type,
        targets: [...gate.targets],
        ...(gate.controls === undefined ? {} : { controls: [...gate.controls] }),
        ...(gate.params === undefined ? {} : { params: { ...gate.params } }),
        ...(gate.classical_targets === undefined ? {} : { classical_targets: [...gate.classical_targets] }),
        ...(gate.condition === undefined ? {} : { condition: gate.condition }),
        ...(gate.conditions === undefined ? {} : { conditions: [...gate.conditions] }),
      })),
    })),
  }

  for (const annotation of config.annotations ?? []) {
    const column = editorState.columns[annotation.column_index]
    if (!column) continue
    for (const qubit of annotation.qubits) {
      column.gates.push({
        id: annotation.id ?? `annotation-${annotation.kind.toLowerCase()}-${annotation.column_index}-${qubit}`,
        type: annotation.kind,
        targets: [qubit],
        source_id: annotation.source_id,
      })
    }
  }

  for (const column of editorState.columns) {
    const acceptedGates: CircuitGate[] = []
    for (const gate of column.gates) {
      const collision = findColumnCollision({ ...column, gates: acceptedGates }, gate)
      if (collision !== null) {
        throw new Error(
          `Import failed: ${gate.type} collides with another gate at step ${column.step}.`,
        )
      }
      acceptedGates.push(gate)
    }
  }

  return editorState
}

export function parseCircuitConfigJson(text: string, minimumLogicalQubits: 1 | 2 = MIN_SUPPORTED_LOGICAL_QUBITS): CircuitEditorState {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('Import failed: invalid JSON file.')
  }

  const config = normalizeCircuitConfigShape(parsed, minimumLogicalQubits)
  return circuitConfigToEditorState(config)
}

export function exportCircuitConfigBundleJson(circuit: CircuitEditorState) {
  const bundle: CircuitConfigBundle = {
    version: 1,
    kind: WRAPPER_KIND,
    circuit_config: circuitEditorStateToConfig(circuit),
  }

  return JSON.stringify(bundle, null, 2)
}
