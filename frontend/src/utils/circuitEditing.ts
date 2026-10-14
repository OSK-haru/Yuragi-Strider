import type {
  CircuitColumn,
  CircuitEditorState,
  CircuitGate,
  CircuitGateParams,
  ControlledGateType,
  InitialQubitState,
  GateType,
  MultiControlledGateType,
  PairGateType,
  RegisterGateType,
  SingleQubitGateType,
} from '../types/circuit'

export function isAnnotationGateType(gateType: GateType): gateType is 'RECEIVED' {
  return gateType === 'RECEIVED'
}

export function isControlledGateType(gateType: GateType): gateType is ControlledGateType {
  return gateType === 'CNOT' || gateType === 'CZ' || gateType === 'CP'
}

export function isPairGateType(gateType: GateType): gateType is PairGateType {
  return gateType === 'SWAP'
}

export function isMultiControlledGateType(
  gateType: GateType,
): gateType is MultiControlledGateType {
  return gateType === 'CCX'
}

export function isRegisterGateType(gateType: GateType): gateType is RegisterGateType {
  return gateType === 'QFT' || gateType === 'ORACLE'
}

export function isTwoQubitGateType(gateType: GateType) {
  return isControlledGateType(gateType) || isPairGateType(gateType)
}

/** Gates whose behaviour is set by a rotation angle the user types in when placing them. */
export function isThetaGateType(gateType: GateType) {
  return gateType === 'RX' || gateType === 'RY' || gateType === 'RZ' || gateType === 'CP'
}

/** Angle used when a theta gate carries no explicit params, matching gate creation. */
export const DEFAULT_THETA_RAD = Math.PI / 2

export function gateThetaRad(gate: CircuitGate): number | null {
  return isThetaGateType(gate.type)
    ? gate.params?.theta_rad ?? DEFAULT_THETA_RAD
    : null
}

/** Renders an angle as a multiple of pi when it is one, so tiles stay readable. */
export function formatThetaLabel(thetaRad: number): string {
  if (!Number.isFinite(thetaRad)) {
    return '-'
  }
  if (Math.abs(thetaRad) < 1e-9) {
    return '0'
  }

  const piRatio = thetaRad / Math.PI
  for (const denominator of [1, 2, 3, 4, 6, 8]) {
    const numerator = Math.round(piRatio * denominator)
    if (numerator !== 0 && Math.abs(piRatio * denominator - numerator) < 1e-6) {
      const sign = numerator < 0 ? '-' : ''
      const magnitude = Math.abs(numerator)
      const head = magnitude === 1 ? 'π' : `${magnitude}π`
      return denominator === 1 ? `${sign}${head}` : `${sign}${head}/${denominator}`
    }
  }

  return thetaRad.toFixed(2)
}

export function isMultiQubitGateType(gateType: GateType) {
  return (
    isTwoQubitGateType(gateType) ||
    isMultiControlledGateType(gateType) ||
    isRegisterGateType(gateType)
  )
}

/** Smallest register the editor will build for a variable-width gate. */
export const MIN_REGISTER_GATE_QUBITS = 2

export const DEFAULT_EDITOR_COLUMN_COUNT = 4
export const MIN_SUPPORTED_LOGICAL_QUBITS = 2
// The editor may describe larger ideal circuits than the noisy density-matrix
// runner can execute. The API selects statevector execution above 5 qubits
// for measurement-free ideal circuits; noisy simulation remains capped at 5.
export const MAX_SUPPORTED_LOGICAL_QUBITS = 8
// Mirrors the public API budget so locally-created circuits cannot grow into
// a request the web service must reject or spend excessive time parsing.
export const MAX_SUPPORTED_CIRCUIT_COLUMNS = 200

export function createEmptyCircuitColumn(step: number): CircuitColumn {
  return {
    step,
    gates: [],
  }
}

export function ensureCircuitColumnCount(
  circuit: CircuitEditorState,
  columnCount: number,
): CircuitEditorState {
  const columns = [...circuit.columns]
  while (columns.length < columnCount) {
    columns.push(createEmptyCircuitColumn(columns.length))
  }
  return {
    ...circuit,
    columns,
  }
}

export function appendEmptyColumn(circuit: CircuitEditorState): CircuitEditorState {
  if (circuit.columns.length >= MAX_SUPPORTED_CIRCUIT_COLUMNS) {
    return circuit
  }
  return {
    ...circuit,
    columns: [
      ...circuit.columns,
      createEmptyCircuitColumn(circuit.columns.length),
    ],
  }
}

/*
 * 列と列のあいだへのドロップを受けるための空列。Pulseタイムラインの
 * ドロップマーカーと同じで、「いまある列の間に割り込ませる」を1手で終わらせる。
 * step は書き出し時の column_index になるので、通し番号を振り直す。
 */
export function insertEmptyColumnAt(
  circuit: CircuitEditorState,
  columnIndex: number,
): CircuitEditorState {
  const insertIndex = Math.min(Math.max(columnIndex, 0), circuit.columns.length)
  const columns = [
    ...circuit.columns.slice(0, insertIndex),
    createEmptyCircuitColumn(insertIndex),
    ...circuit.columns.slice(insertIndex),
  ]

  return {
    ...circuit,
    columns: columns.map((column, index) => (
      column.step === index ? column : { ...column, step: index }
    )),
  }
}

/*
 * 複製は「同じ量子ビットのまま、すぐ右に列を作って置く」。既存の列へ入れると
 * 空きの有無で成否が変わり、Pulseの⧉（押せば必ず隣に増える）と挙動がずれる。
 */
export function duplicateGateById(
  circuit: CircuitEditorState,
  gateId: string,
  nextGateId: string,
): CircuitEditorState {
  const location = findGateLocationById(circuit, gateId)
  if (!location) {
    return circuit
  }

  const targetIndex = location.columnIndex + 1
  const inserted = insertEmptyColumnAt(circuit, targetIndex)

  return {
    ...inserted,
    columns: inserted.columns.map((column, index) => (
      index === targetIndex
        ? { ...column, gates: [{ ...cloneGate(location.gate), id: nextGateId }] }
        : column
    )),
  }
}

/*
 * ゲートを丸ごと隣の列へ動かす。隣が同じ量子ビットで埋まっていても失敗させず、
 * その隣の「向こう側」へ空列を割り込ませてすり抜けさせる。結果としてPulseの
 * ← → と同じく、押せば必ず順番がひとつ入れ替わる。
 */
export function shiftGateColumnById(
  circuit: CircuitEditorState,
  gateId: string,
  offset: -1 | 1,
): CircuitEditorState {
  const location = findGateLocationById(circuit, gateId)
  if (!location) {
    return circuit
  }

  const destination = location.columnIndex + offset
  if (destination < 0) {
    return circuit
  }

  function moveGate(
    base: CircuitEditorState,
    fromIndex: number,
    toIndex: number,
  ): CircuitEditorState {
    if (fromIndex === toIndex) {
      return circuit
    }

    return {
      ...base,
      columns: base.columns.map((column, index) => {
        if (index === fromIndex) {
          return { ...column, gates: column.gates.filter((gate) => gate.id !== gateId) }
        }
        if (index === toIndex) {
          return { ...column, gates: sortCircuitColumnGates([...column.gates, location!.gate]) }
        }
        return column
      }),
    }
  }

  /* 右端より先へ動かすときは、行き先の列そのものを先に用意する。 */
  const withDestination = destination < circuit.columns.length
    ? circuit
    : appendEmptyColumn(circuit)
  const destinationColumn = withDestination.columns[destination]
  const isBlocked =
    destinationColumn !== undefined &&
    !canPlaceGateInColumn(withDestination, destination, location.gate, gateId).valid

  if (!isBlocked) {
    return moveGate(withDestination, location.columnIndex, destination)
  }

  /*
   * 割り込ませる位置は、ふさいでいる列の向こう側。左へなら手前、右へなら奥に
   * 入れることで、ふさいでいるゲートとの前後関係がきちんと入れ替わる。
   */
  const wedgeIndex = offset < 0 ? destination : destination + 1
  const wedged = insertEmptyColumnAt(withDestination, wedgeIndex)
  const sourceIndex = location.columnIndex >= wedgeIndex
    ? location.columnIndex + 1
    : location.columnIndex

  return moveGate(wedged, sourceIndex, wedgeIndex)
}

export function canRemoveLastColumn(circuit: CircuitEditorState) {
  const lastColumn = circuit.columns[circuit.columns.length - 1]
  return circuit.columns.length > 1 && lastColumn !== undefined && lastColumn.gates.length === 0
}

export function removeLastEmptyColumn(circuit: CircuitEditorState): CircuitEditorState {
  if (!canRemoveLastColumn(circuit)) {
    return circuit
  }

  return {
    ...circuit,
    columns: circuit.columns.slice(0, -1),
  }
}

function clampLogicalQubitCount(logicalQubits: number, minimumLogicalQubits: 1 | 2) {
  const normalized = Math.trunc(logicalQubits)
  return Math.min(MAX_SUPPORTED_LOGICAL_QUBITS, Math.max(minimumLogicalQubits, normalized))
}

function isQubitIndexValid(qubitIndex: number, logicalQubits: number) {
  return Number.isInteger(qubitIndex) && qubitIndex >= 0 && qubitIndex < logicalQubits
}

function cloneGate(gate: CircuitGate): CircuitGate {
  return {
    ...gate,
    targets: [...gate.targets],
    ...(gate.controls === undefined ? {} : { controls: [...gate.controls] }),
    ...(gate.params === undefined ? {} : { params: { ...gate.params } }),
    ...(gate.source_id === undefined ? {} : { source_id: gate.source_id }),
  }
}

function isGateValidForLogicalQubits(gate: CircuitGate, logicalQubits: number) {
  if (isControlledGateType(gate.type)) {
    const controls = gate.controls ?? []
    if ((gate.type === 'CNOT' ? controls.length < 1 : controls.length !== 1) || gate.targets.length !== 1) {
      return false
    }

    const target = gate.targets[0]
    return (
      new Set(controls).size === controls.length &&
      controls.every((control) => isQubitIndexValid(control, logicalQubits)) &&
      isQubitIndexValid(target, logicalQubits) &&
      !controls.includes(target)
    )
  }

  if (isPairGateType(gate.type)) {
    return (
      (gate.controls ?? []).length === 0 &&
      gate.targets.length === 2 &&
      gate.targets[0] !== gate.targets[1] &&
      gate.targets.every((target) => isQubitIndexValid(target, logicalQubits))
    )
  }

  if (isMultiControlledGateType(gate.type)) {
    const controls = gate.controls ?? []
    const qubits = [...controls, ...gate.targets]
    return (
      controls.length === 2 &&
      gate.targets.length === 1 &&
      new Set(qubits).size === 3 &&
      qubits.every((qubit) => isQubitIndexValid(qubit, logicalQubits))
    )
  }

  if (isRegisterGateType(gate.type)) {
    return (
      (gate.controls ?? []).length === 0 &&
      gate.targets.length >= 1 &&
      new Set(gate.targets).size === gate.targets.length &&
      gate.targets.every((target) => isQubitIndexValid(target, logicalQubits))
    )
  }

  if (gate.targets.length !== 1) {
    return false
  }

  return isQubitIndexValid(gate.targets[0], logicalQubits)
}

function normalizeInitialStates(
  initialStates: InitialQubitState[],
  logicalQubits: number,
): InitialQubitState[] {
  return Array.from({ length: logicalQubits }, (_, index) =>
    initialStates[index] === 1 ? 1 : 0,
  )
}

function normalizeCircuitColumns(
  columns: CircuitEditorState['columns'],
  logicalQubits: number,
) {
  return columns.map((column) => {
    const gates = column.gates
      .filter((gate) => isGateValidForLogicalQubits(gate, logicalQubits))
      .map((gate) => cloneGate(gate))

    return {
      ...column,
      gates: sortCircuitColumnGates(gates),
    }
  })
}

export function resizeCircuitEditorState(
  circuit: CircuitEditorState,
  logicalQubits: number,
  minimumLogicalQubits: 1 | 2 = MIN_SUPPORTED_LOGICAL_QUBITS,
): CircuitEditorState {
  const nextLogicalQubits = clampLogicalQubitCount(logicalQubits, minimumLogicalQubits)
  const nextInitialStates = normalizeInitialStates(
    circuit.initial_states,
    nextLogicalQubits,
  )
  const nextColumns = normalizeCircuitColumns(circuit.columns, nextLogicalQubits)

  const hasChanged =
    circuit.logical_qubits !== nextLogicalQubits ||
    circuit.initial_states.length !== nextLogicalQubits ||
    circuit.initial_states.some((state, index) => nextInitialStates[index] !== state) ||
    circuit.columns.some((column, columnIndex) => {
      const nextColumn = nextColumns[columnIndex]
      if (nextColumn === undefined) {
        return true
      }

      if (column.gates.length !== nextColumn.gates.length) {
        return true
      }

      return column.gates.some((gate, gateIndex) => gate.id !== nextColumn.gates[gateIndex]?.id)
    })

  if (!hasChanged) {
    return circuit
  }

  return {
    ...circuit,
    logical_qubits: nextLogicalQubits,
    initial_states: nextInitialStates,
    columns: nextColumns,
  }
}

export function createPlacedGate(
  gateType: SingleQubitGateType,
  qubitIndex: number,
  durationUs: number,
  gateId: string,
): CircuitGate {
  return {
    id: gateId,
    type: gateType,
    targets: [qubitIndex],
      params: {
      duration_us: durationUs,
      ...(
        gateType === 'RX' || gateType === 'RY' || gateType === 'RZ'
          ? { theta_rad: Math.PI / 2 }
          : gateType === 'MESSAGE'
            ? { animation_parameter_t: 0 }
            : {}
      ),
    },
  }
}

export function createCnotGate(
  controlQubit: number | number[],
  targetQubit: number,
  durationUs: number,
  gateId: string,
  gateType: ControlledGateType = 'CNOT',
  controlValue: 0 | 1 | Array<0 | 1> = 1,
): CircuitGate {
  const controls = Array.isArray(controlQubit) ? [...controlQubit] : [controlQubit]
  const controlValues = Array.isArray(controlValue)
    ? [...controlValue]
    : Array.from({ length: controls.length }, () => controlValue)
  const controlState = controlValues.reduce<number>((state, value) => (state << 1) | value, 0)
  return {
    id: gateId,
    type: gateType,
    controls,
    targets: [targetQubit],
    params: {
      duration_us: durationUs,
      ...(gateType === 'CNOT' && controls.length === 1 && controlValues[0] === 0
        ? { control_value: 0 as const }
        : {}),
      ...(gateType === 'CNOT' && controls.length > 1 ? { control_state: controlState } : {}),
      ...(gateType === 'CP' ? { theta_rad: Math.PI / 2 } : {}),
    },
  }
}

export function controlValuesForGate(gate: CircuitGate): Array<0 | 1> {
  const controls = gate.controls ?? []
  const state = controls.length > 1
    ? gate.params?.control_state ?? (2 ** controls.length - 1)
    : gate.params?.control_value ?? 1
  return controls.map((_, index) => (
    ((state >> (controls.length - 1 - index)) & 1) as 0 | 1
  ))
}

/** Replace the sole plain X in a column with a connected controlled-X. */
export function connectControlToXInCircuit(
  circuit: CircuitEditorState,
  columnIndex: number,
  controlQubit: number | number[],
  controlValue: 0 | 1 | Array<0 | 1>,
  durationUs: number,
  gateId: string,
): CircuitEditorState {
  const column = circuit.columns[columnIndex]
  if (!column) return circuit
  const controls = Array.isArray(controlQubit) ? controlQubit : [controlQubit]
  const targets = column.gates.filter((gate) =>
    gate.type === 'X' && gate.targets.length === 1 && !controls.includes(gate.targets[0]),
  )
  if (targets.length !== 1) return circuit

  const targetGate = targets[0]
  const candidate = createCnotGate(
    controls,
    targetGate.targets[0],
    durationUs,
    gateId,
    'CNOT',
    controlValue,
  )
  const remainingGates = column.gates.filter((gate) => gate.id !== targetGate.id)
  if (findColumnCollision({ ...column, gates: remainingGates }, candidate) !== null) {
    return circuit
  }

  return {
    ...circuit,
    columns: circuit.columns.map((item, index) => index === columnIndex
      ? { ...item, gates: sortCircuitColumnGates([...remainingGates, candidate]) }
      : item),
  }
}

/** Add a newly placed marker to the sole controlled-X already in a column. */
export function appendControlToCnotInCircuit(
  circuit: CircuitEditorState,
  columnIndex: number,
  controlQubit: number,
  controlValue: 0 | 1,
): CircuitEditorState {
  const column = circuit.columns[columnIndex]
  if (!column) return circuit
  const candidates = column.gates.filter((gate) =>
    gate.type === 'CNOT' && gate.targets.length === 1 && !(gate.controls ?? []).includes(controlQubit),
  )
  if (candidates.length !== 1) return circuit
  const source = candidates[0]
  const controls = [...(source.controls ?? []), controlQubit]
  if (source.targets[0] === controlQubit || new Set(controls).size !== controls.length) return circuit
  const next = createCnotGate(
    controls,
    source.targets[0],
    source.params?.duration_us ?? durationUsForGate(source),
    source.id,
    'CNOT',
    [...controlValuesForGate(source), controlValue],
  )
  const remainingGates = column.gates.filter((gate) => gate.id !== source.id)
  if (findColumnCollision({ ...column, gates: remainingGates }, next) !== null) return circuit
  return {
    ...circuit,
    columns: circuit.columns.map((item, index) => index === columnIndex
      ? { ...item, gates: sortCircuitColumnGates([...remainingGates, next]) }
      : item),
  }
}

function durationUsForGate(gate: CircuitGate) {
  return gate.params?.duration_us ?? 0.2
}

export function createPairGate(
  firstQubit: number,
  secondQubit: number,
  durationUs: number,
  gateId: string,
  gateType: PairGateType = 'SWAP',
): CircuitGate {
  return {
    id: gateId,
    type: gateType,
    targets: [firstQubit, secondQubit],
    params: { duration_us: durationUs },
  }
}

export function createCcxGate(
  controlA: number,
  controlB: number,
  targetQubit: number,
  durationUs: number,
  gateId: string,
  gateType: MultiControlledGateType = 'CCX',
): CircuitGate {
  return {
    id: gateId,
    type: gateType,
    controls: [controlA, controlB],
    targets: [targetQubit],
    params: { duration_us: durationUs },
  }
}

// QFT declares its whole register in `targets`, ordered most significant bit
// first, so the qubit order is part of the gate rather than a drawing detail.
export function createRegisterGate(
  registerQubits: number[],
  durationUs: number,
  gateId: string,
  gateType: RegisterGateType = 'QFT',
): CircuitGate {
  return {
    id: gateId,
    type: gateType,
    targets: [...registerQubits],
    params: {
      duration_us: durationUs,
      // ORACLE needs a state to mark; 0 is the predictable starting point and
      // the inspector edits it afterwards.
      ...(gateType === 'ORACLE' ? { marked_index: 0 } : {}),
    },
  }
}

/** Register-gate durations scale with the width, so defaults are per qubit. */
export function registerDurationUs(perQubitDurationUs: number, registerSize: number) {
  return perQubitDurationUs * Math.max(1, registerSize)
}

/** Highest basis-state index an ``m``-qubit register can mark. */
export function maxMarkedIndex(registerSize: number) {
  return 2 ** Math.max(1, registerSize) - 1
}

/** The marked value's bit on ``targets[position]`` (targets are MSB-first). */
export function markedBitAt(
  markedIndex: number,
  position: number,
  registerSize: number,
) {
  return (markedIndex >> (registerSize - 1 - position)) & 1
}

// The editor enters a register LSB-first: the qubit picked first carries bit 0,
// so a top-to-bottom pick makes the top wire the least significant bit. That is
// the Quirk and Qiskit operand order, which keeps side-by-side comparisons
// honest. `targets` itself stays MSB-first, which is the core's contract.
export function registerTargetsFromEntryOrder(entryOrder: number[]): number[] {
  return [...entryOrder].reverse()
}

/** Bit weight k (the qubit carries 2**k) held by `targets[position]`. */
export function registerBitWeight(position: number, registerSize: number) {
  return registerSize - 1 - position
}

// Dropping QFT claims the drop row and everything below it, so one drag gives a
// register wide enough to be useful. Arbitrary subsets come from click placement.
export function resolveRegisterDropPlacement(qubitIndex: number, logicalQubits: number) {
  if (logicalQubits < MIN_REGISTER_GATE_QUBITS) {
    return []
  }

  const start = Math.min(
    Math.max(0, qubitIndex),
    logicalQubits - MIN_REGISTER_GATE_QUBITS,
  )
  const topToBottom = Array.from(
    { length: logicalQubits - start },
    (_, offset) => start + offset,
  )
  return registerTargetsFromEntryOrder(topToBottom)
}

// Keeps the register's shape and order while it is dragged, shifting the whole
// span by the drag delta and clamping so no qubit runs off either edge.
export function resolveMovedRegisterPlacement(
  registerQubits: number[],
  grabbedQubit: number,
  droppedQubit: number,
  logicalQubits: number,
) {
  if (registerQubits.length === 0) {
    return []
  }

  const lowest = Math.min(...registerQubits)
  const highest = Math.max(...registerQubits)
  const delta = Math.min(
    Math.max(droppedQubit - grabbedQubit, -lowest),
    logicalQubits - 1 - highest,
  )
  return registerQubits.map((qubit) => qubit + delta)
}

export function resolveCnotDropPlacement(qubitIndex: number, logicalQubits: number) {
  if (logicalQubits < 2) {
    return { controlQubit: 0, targetQubit: 0 }
  }

  if (qubitIndex < logicalQubits - 1) {
    return { controlQubit: qubitIndex, targetQubit: qubitIndex + 1 }
  }

  if (qubitIndex > 0) {
    return { controlQubit: qubitIndex - 1, targetQubit: qubitIndex }
  }

  return { controlQubit: 0, targetQubit: 1 }
}

// Preserves the qubit span of an existing two-qubit gate while it is being
// dragged, instead of collapsing control/target back to adjacent qubits.
// `grabbedQubit` is the endpoint the drag started from; `otherQubit` is the
// gate's other endpoint. The whole span is shifted (not re-anchored) if it
// would otherwise run off either edge of the register.
export function resolveMovedTwoQubitPlacement(
  droppedQubit: number,
  grabbedQubit: number,
  otherQubit: number,
  logicalQubits: number,
) {
  const offset = otherQubit - grabbedQubit
  let nextGrabbed = droppedQubit
  let nextOther = droppedQubit + offset

  if (nextOther < 0) {
    nextGrabbed -= nextOther
    nextOther = 0
  } else if (nextOther > logicalQubits - 1) {
    nextGrabbed -= nextOther - (logicalQubits - 1)
    nextOther = logicalQubits - 1
  }

  nextGrabbed = Math.min(logicalQubits - 1, Math.max(0, nextGrabbed))

  return { grabbedQubit: nextGrabbed, otherQubit: nextOther }
}

export function findGateLocationById(circuit: CircuitEditorState, gateId: string) {
  for (let columnIndex = 0; columnIndex < circuit.columns.length; columnIndex += 1) {
    const gate = circuit.columns[columnIndex].gates.find((candidate) => candidate.id === gateId)
    if (gate) {
      return {
        columnIndex,
        gate,
      }
    }
  }

  return null
}

export function getOccupiedQubits(gate: CircuitGate): number[] {
  if (isControlledGateType(gate.type) || isMultiControlledGateType(gate.type)) {
    return [...(gate.controls ?? []), ...gate.targets]
  }

  return [...gate.targets]
}

export function findColumnCollision(
  column: CircuitColumn,
  candidate: CircuitGate,
  ignoredGateId?: string,
) {
  const occupiedQubits = new Set(getOccupiedQubits(candidate))
  return column.gates.find((gate) => {
    if (gate.id === ignoredGateId) {
      return false
    }
    if (isAnnotationGateType(gate.type) || isAnnotationGateType(candidate.type)) {
      return false
    }

    return getOccupiedQubits(gate).some((qubit) => occupiedQubits.has(qubit))
  }) ?? null
}

export function formatColumnCollisionMessage(
  columnIndex: number,
  candidate: CircuitGate,
  collision: CircuitGate | null,
) {
  if (collision === null) {
    return null
  }

  const candidateQubits = getOccupiedQubits(candidate)
  const collisionQubits = new Set(getOccupiedQubits(collision))
  const collidedQubit = candidateQubits.find((qubit) => collisionQubits.has(qubit))
  const qubitLabel = collidedQubit === undefined ? 'A qubit' : `q${collidedQubit}`
  return `${qubitLabel} is already occupied in column ${columnIndex + 1}.`
}

export function canPlaceGateInColumn(
  circuit: CircuitEditorState,
  columnIndex: number,
  candidate: CircuitGate,
  ignoredGateId?: string,
) {
  const column = circuit.columns[columnIndex]
  if (!column) {
    return {
      valid: true,
      message: null,
    }
  }

  const collision = findColumnCollision(column, candidate, ignoredGateId)
  return {
    valid: collision === null,
    message: formatColumnCollisionMessage(columnIndex, candidate, collision),
  }
}

export function sortCircuitColumnGates(gates: CircuitGate[]) {
  return [...gates].sort((left, right) => {
    const leftPriority = isMultiQubitGateType(left.type) ? 1 : 0
    const rightPriority = isMultiQubitGateType(right.type) ? 1 : 0
    if (leftPriority !== rightPriority) {
      return leftPriority - rightPriority
    }

    const leftQubit = Math.min(...getOccupiedQubits(left))
    const rightQubit = Math.min(...getOccupiedQubits(right))
    return leftQubit - rightQubit || left.type.localeCompare(right.type)
  })
}

export function placeSingleGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  columnIndex: number,
  qubitIndex: number,
  gateType: SingleQubitGateType,
  durationUs: number,
  gateId: string,
): CircuitEditorState {
  if (!isQubitIndexValid(qubitIndex, circuit.logical_qubits)) {
    return circuit
  }

  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, columnIndex + 1),
  )

  const candidate = createPlacedGate(gateType, qubitIndex, durationUs, gateId)
  const placement = canPlaceGateInColumn(normalizedCircuit, columnIndex, candidate)
  if (!placement.valid) {
    return circuit
  }

  const nextColumns = normalizedCircuit.columns.map((column, index) => {
    if (index !== columnIndex) {
      return column
    }

    const remainingGates = [...column.gates, candidate]

    return {
      ...column,
      gates: sortCircuitColumnGates(remainingGates),
    }
  })

  return {
    ...normalizedCircuit,
    columns: nextColumns,
  }
}

export function moveSingleGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  gateId: string,
  targetColumnIndex: number,
  targetQubitIndex: number,
): CircuitEditorState {
  if (!isQubitIndexValid(targetQubitIndex, circuit.logical_qubits)) {
    return circuit
  }

  const sourceGate = circuit.columns
    .flatMap((column) => column.gates)
    .find((gate) => gate.id === gateId && !isMultiQubitGateType(gate.type))

  if (!sourceGate || isMultiQubitGateType(sourceGate.type)) {
    return circuit
  }

  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, targetColumnIndex + 1),
  )

  const movedGate: CircuitGate = {
    ...sourceGate,
    targets: [targetQubitIndex],
    ...(sourceGate.controls === undefined ? {} : { controls: [] }),
    params: sourceGate.params === undefined ? undefined : { ...sourceGate.params },
  }
  const placement = canPlaceGateInColumn(
    normalizedCircuit,
    targetColumnIndex,
    movedGate,
    gateId,
  )
  if (!placement.valid) {
    return circuit
  }

  const nextColumns = normalizedCircuit.columns.map((column, index) => {
    const remainingGates = column.gates.filter((gate) => {
      return gate.id !== gateId
    })

    if (index === targetColumnIndex) {
      remainingGates.push(movedGate)
    }

    return {
      ...column,
      gates: sortCircuitColumnGates(remainingGates),
    }
  })

  return {
    ...normalizedCircuit,
    columns: nextColumns,
  }
}

export function placeCnotGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  columnIndex: number,
  controlQubit: number | number[],
  targetQubit: number,
  durationUs: number,
  gateId: string,
  gateType: ControlledGateType = 'CNOT',
  controlValue: 0 | 1 | Array<0 | 1> = 1,
): CircuitEditorState {
  const controls = Array.isArray(controlQubit) ? controlQubit : [controlQubit]
  if (
    controls.length < 1 ||
    !controls.every((control) => isQubitIndexValid(control, circuit.logical_qubits)) ||
    new Set(controls).size !== controls.length ||
    !isQubitIndexValid(targetQubit, circuit.logical_qubits) ||
    controls.includes(targetQubit)
  ) {
    return circuit
  }

  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, columnIndex + 1),
  )

  const candidate = createCnotGate(
    controls, targetQubit, durationUs, gateId, gateType, controlValue,
  )
  const placement = canPlaceGateInColumn(normalizedCircuit, columnIndex, candidate)
  if (!placement.valid) {
    return circuit
  }

  const nextColumns = normalizedCircuit.columns.map((column, index) => {
    if (index !== columnIndex) {
      return column
    }

    const remainingGates = [...column.gates, candidate]

    return {
      ...column,
      gates: sortCircuitColumnGates(remainingGates),
    }
  })

  return {
    ...normalizedCircuit,
    columns: nextColumns,
  }
}

export function placeCnotGateFromDropInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  columnIndex: number,
  qubitIndex: number,
  durationUs: number,
  gateId: string,
  gateType: ControlledGateType = 'CNOT',
): CircuitEditorState {
  if (!isQubitIndexValid(qubitIndex, circuit.logical_qubits)) {
    return circuit
  }

  const placement = resolveCnotDropPlacement(qubitIndex, circuit.logical_qubits)
  return placeCnotGateInCircuit(
    circuit,
    columnCount,
    columnIndex,
    placement.controlQubit,
    placement.targetQubit,
    durationUs,
    gateId,
    gateType,
  )
}

export function resolveCcxDropPlacement(qubitIndex: number, logicalQubits: number) {
  if (logicalQubits < 3) {
    return { controlA: 0, controlB: 0, targetQubit: 0 }
  }
  const start = Math.min(Math.max(0, qubitIndex), logicalQubits - 3)
  return { controlA: start, controlB: start + 1, targetQubit: start + 2 }
}

export function placePairGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  columnIndex: number,
  firstQubit: number,
  secondQubit: number,
  durationUs: number,
  gateId: string,
  gateType: PairGateType = 'SWAP',
): CircuitEditorState {
  if (
    !isQubitIndexValid(firstQubit, circuit.logical_qubits) ||
    !isQubitIndexValid(secondQubit, circuit.logical_qubits) ||
    firstQubit === secondQubit
  ) {
    return circuit
  }
  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, columnIndex + 1),
  )
  const candidate = createPairGate(firstQubit, secondQubit, durationUs, gateId, gateType)
  if (!canPlaceGateInColumn(normalizedCircuit, columnIndex, candidate).valid) {
    return circuit
  }
  return {
    ...normalizedCircuit,
    columns: normalizedCircuit.columns.map((column, index) =>
      index === columnIndex
        ? { ...column, gates: sortCircuitColumnGates([...column.gates, candidate]) }
        : column,
    ),
  }
}

export function placePairGateFromDropInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  columnIndex: number,
  qubitIndex: number,
  durationUs: number,
  gateId: string,
  gateType: PairGateType = 'SWAP',
): CircuitEditorState {
  const placement = resolveCnotDropPlacement(qubitIndex, circuit.logical_qubits)
  return placePairGateInCircuit(
    circuit,
    columnCount,
    columnIndex,
    placement.controlQubit,
    placement.targetQubit,
    durationUs,
    gateId,
    gateType,
  )
}

export function movePairGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  gateId: string,
  targetColumnIndex: number,
  targetQubitIndex: number,
  fromQubitIndex: number,
): CircuitEditorState {
  if (!isQubitIndexValid(targetQubitIndex, circuit.logical_qubits)) {
    return circuit
  }
  const sourceLocation = findGateLocationById(circuit, gateId)
  if (sourceLocation === null || !isPairGateType(sourceLocation.gate.type)) {
    return circuit
  }
  const sourceTargets = sourceLocation.gate.targets
  if (sourceTargets.length !== 2) {
    return circuit
  }
  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, targetColumnIndex + 1),
  )
  const grabbedWasFirst = fromQubitIndex === sourceTargets[0]
  const otherQubit = grabbedWasFirst ? sourceTargets[1] : sourceTargets[0]
  const movedPlacement = resolveMovedTwoQubitPlacement(
    targetQubitIndex,
    fromQubitIndex,
    otherQubit,
    normalizedCircuit.logical_qubits,
  )
  const movedFirst = grabbedWasFirst ? movedPlacement.grabbedQubit : movedPlacement.otherQubit
  const movedSecond = grabbedWasFirst ? movedPlacement.otherQubit : movedPlacement.grabbedQubit
  const movedGate: CircuitGate = {
    ...sourceLocation.gate,
    controls: [],
    targets: [movedFirst, movedSecond],
    params: sourceLocation.gate.params === undefined
      ? undefined
      : { ...sourceLocation.gate.params },
  }
  if (!canPlaceGateInColumn(
    normalizedCircuit,
    targetColumnIndex,
    movedGate,
    gateId,
  ).valid) {
    return circuit
  }
  if (
    sourceLocation.columnIndex === targetColumnIndex &&
    sourceLocation.gate.targets[0] === movedGate.targets[0] &&
    sourceLocation.gate.targets[1] === movedGate.targets[1]
  ) {
    return circuit
  }
  return {
    ...normalizedCircuit,
    columns: normalizedCircuit.columns.map((column, index) => {
      const gates = column.gates.filter((gate) => gate.id !== gateId)
      if (index === targetColumnIndex) {
        gates.push(movedGate)
      }
      return { ...column, gates: sortCircuitColumnGates(gates) }
    }),
  }
}

export function placeCcxGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  columnIndex: number,
  controlA: number,
  controlB: number,
  targetQubit: number,
  durationUs: number,
  gateId: string,
  gateType: MultiControlledGateType = 'CCX',
): CircuitEditorState {
  const qubits = [controlA, controlB, targetQubit]
  if (
    new Set(qubits).size !== 3 ||
    qubits.some((qubit) => !isQubitIndexValid(qubit, circuit.logical_qubits))
  ) {
    return circuit
  }
  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, columnIndex + 1),
  )
  const candidate = createCcxGate(
    controlA, controlB, targetQubit, durationUs, gateId, gateType,
  )
  if (!canPlaceGateInColumn(normalizedCircuit, columnIndex, candidate).valid) {
    return circuit
  }
  return {
    ...normalizedCircuit,
    columns: normalizedCircuit.columns.map((column, index) =>
      index === columnIndex
        ? { ...column, gates: sortCircuitColumnGates([...column.gates, candidate]) }
        : column,
    ),
  }
}

export function placeCcxGateFromDropInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  columnIndex: number,
  qubitIndex: number,
  durationUs: number,
  gateId: string,
): CircuitEditorState {
  const placement = resolveCcxDropPlacement(qubitIndex, circuit.logical_qubits)
  return placeCcxGateInCircuit(
    circuit,
    columnCount,
    columnIndex,
    placement.controlA,
    placement.controlB,
    placement.targetQubit,
    durationUs,
    gateId,
  )
}

export function moveCcxGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  gateId: string,
  targetColumnIndex: number,
  targetQubitIndex: number,
): CircuitEditorState {
  const sourceLocation = findGateLocationById(circuit, gateId)
  if (sourceLocation === null || !isMultiControlledGateType(sourceLocation.gate.type)) {
    return circuit
  }
  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, targetColumnIndex + 1),
  )
  const placement = resolveCcxDropPlacement(
    targetQubitIndex,
    normalizedCircuit.logical_qubits,
  )
  if (new Set([placement.controlA, placement.controlB, placement.targetQubit]).size !== 3) {
    return circuit
  }
  const movedGate: CircuitGate = {
    ...sourceLocation.gate,
    controls: [placement.controlA, placement.controlB],
    targets: [placement.targetQubit],
    params: sourceLocation.gate.params === undefined
      ? undefined
      : { ...sourceLocation.gate.params },
  }
  if (!canPlaceGateInColumn(
    normalizedCircuit,
    targetColumnIndex,
    movedGate,
    gateId,
  ).valid) {
    return circuit
  }
  const sourceQubits = [
    ...(sourceLocation.gate.controls ?? []),
    ...sourceLocation.gate.targets,
  ]
  const movedQubits = [...movedGate.controls ?? [], ...movedGate.targets]
  if (
    sourceLocation.columnIndex === targetColumnIndex &&
    sourceQubits.every((qubit, index) => qubit === movedQubits[index])
  ) {
    return circuit
  }
  return {
    ...normalizedCircuit,
    columns: normalizedCircuit.columns.map((column, index) => {
      const gates = column.gates.filter((gate) => gate.id !== gateId)
      if (index === targetColumnIndex) {
        gates.push(movedGate)
      }
      return { ...column, gates: sortCircuitColumnGates(gates) }
    }),
  }
}

export function placeRegisterGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  columnIndex: number,
  registerQubits: number[],
  durationUs: number,
  gateId: string,
  gateType: RegisterGateType = 'QFT',
): CircuitEditorState {
  if (
    registerQubits.length < MIN_REGISTER_GATE_QUBITS ||
    new Set(registerQubits).size !== registerQubits.length ||
    registerQubits.some((qubit) => !isQubitIndexValid(qubit, circuit.logical_qubits))
  ) {
    return circuit
  }
  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, columnIndex + 1),
  )
  const candidate = createRegisterGate(registerQubits, durationUs, gateId, gateType)
  if (!canPlaceGateInColumn(normalizedCircuit, columnIndex, candidate).valid) {
    return circuit
  }
  return {
    ...normalizedCircuit,
    columns: normalizedCircuit.columns.map((column, index) =>
      index === columnIndex
        ? { ...column, gates: sortCircuitColumnGates([...column.gates, candidate]) }
        : column,
    ),
  }
}

export function placeRegisterGateFromDropInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  columnIndex: number,
  qubitIndex: number,
  perQubitDurationUs: number,
  gateId: string,
  gateType: RegisterGateType = 'QFT',
): CircuitEditorState {
  const registerQubits = resolveRegisterDropPlacement(qubitIndex, circuit.logical_qubits)
  return placeRegisterGateInCircuit(
    circuit,
    columnCount,
    columnIndex,
    registerQubits,
    registerDurationUs(perQubitDurationUs, registerQubits.length),
    gateId,
    gateType,
  )
}

export function moveRegisterGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  gateId: string,
  targetColumnIndex: number,
  targetQubitIndex: number,
  fromQubitIndex: number,
): CircuitEditorState {
  if (!isQubitIndexValid(targetQubitIndex, circuit.logical_qubits)) {
    return circuit
  }
  const sourceLocation = findGateLocationById(circuit, gateId)
  if (sourceLocation === null || !isRegisterGateType(sourceLocation.gate.type)) {
    return circuit
  }
  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, targetColumnIndex + 1),
  )
  const movedTargets = resolveMovedRegisterPlacement(
    sourceLocation.gate.targets,
    fromQubitIndex,
    targetQubitIndex,
    normalizedCircuit.logical_qubits,
  )
  const movedGate: CircuitGate = {
    ...sourceLocation.gate,
    controls: [],
    targets: movedTargets,
    params: sourceLocation.gate.params === undefined
      ? undefined
      : { ...sourceLocation.gate.params },
  }
  if (!canPlaceGateInColumn(
    normalizedCircuit,
    targetColumnIndex,
    movedGate,
    gateId,
  ).valid) {
    return circuit
  }
  if (
    sourceLocation.columnIndex === targetColumnIndex &&
    sourceLocation.gate.targets.every((qubit, index) => qubit === movedTargets[index])
  ) {
    return circuit
  }
  return {
    ...normalizedCircuit,
    columns: normalizedCircuit.columns.map((column, index) => {
      const gates = column.gates.filter((gate) => gate.id !== gateId)
      if (index === targetColumnIndex) {
        gates.push(movedGate)
      }
      return { ...column, gates: sortCircuitColumnGates(gates) }
    }),
  }
}

export function moveCnotGateInCircuit(
  circuit: CircuitEditorState,
  columnCount: number,
  gateId: string,
  targetColumnIndex: number,
  targetQubitIndex: number,
  fromQubitIndex: number,
): CircuitEditorState {
  if (!isQubitIndexValid(targetQubitIndex, circuit.logical_qubits)) {
    return circuit
  }

  const sourceLocation = findGateLocationById(circuit, gateId)
  if (sourceLocation === null || !isControlledGateType(sourceLocation.gate.type)) {
    return circuit
  }

  const sourceControls = sourceLocation.gate.controls ?? []
  const sourceTargets = sourceLocation.gate.targets
  if (sourceControls.length !== 1 || sourceTargets.length !== 1) {
    return circuit
  }

  const normalizedCircuit = ensureCircuitColumnCount(
    circuit,
    Math.max(columnCount, targetColumnIndex + 1),
  )

  const grabbedWasControl = fromQubitIndex === sourceControls[0]
  const otherQubit = grabbedWasControl ? sourceTargets[0] : sourceControls[0]
  const movedPlacement = resolveMovedTwoQubitPlacement(
    targetQubitIndex,
    fromQubitIndex,
    otherQubit,
    normalizedCircuit.logical_qubits,
  )
  const movedControl = grabbedWasControl ? movedPlacement.grabbedQubit : movedPlacement.otherQubit
  const movedTarget = grabbedWasControl ? movedPlacement.otherQubit : movedPlacement.grabbedQubit
  const movedGate: CircuitGate = {
    ...sourceLocation.gate,
    controls: [movedControl],
    targets: [movedTarget],
    params:
      sourceLocation.gate.params === undefined
        ? undefined
        : { ...sourceLocation.gate.params },
  }
  const placement = canPlaceGateInColumn(
    normalizedCircuit,
    targetColumnIndex,
    movedGate,
    gateId,
  )
  if (!placement.valid) {
    return circuit
  }

  const nextColumns = normalizedCircuit.columns.map((column, index) => {
    const remainingGates = column.gates.filter((gate) => {
      return gate.id !== gateId
    })

    if (index === targetColumnIndex) {
      remainingGates.push(movedGate)
    }

    return {
      ...column,
      gates: sortCircuitColumnGates(remainingGates),
    }
  })

  return {
    ...normalizedCircuit,
    columns: nextColumns,
  }
}

export function removeGateById(
  circuit: CircuitEditorState,
  gateId: string,
): CircuitEditorState {
  return {
    ...circuit,
    columns: circuit.columns.map((column) => ({
      ...column,
      gates: column.gates.filter((gate) => gate.id !== gateId),
    })),
  }
}

export function updateGateParamsById(
  circuit: CircuitEditorState,
  gateId: string,
  params: CircuitGateParams,
): CircuitEditorState {
  let changed = false
  const columns = circuit.columns.map((column) => ({
    ...column,
    gates: column.gates.map((gate) => {
      if (gate.id !== gateId) {
        return gate
      }
      changed = true
      return {
        ...gate,
        params: { ...(gate.params ?? {}), ...params },
      }
    }),
  }))
  return changed ? { ...circuit, columns } : circuit
}

// Reversing the register turns our MSB-first operand order into the LSB-first
// order used by Quirk and Qiskit, so a circuit can be lined up against an
// external simulator without rebuilding it. The occupied qubits do not change,
// so this never collides with other gates in the column.
export function reverseRegisterOrderById(
  circuit: CircuitEditorState,
  gateId: string,
): CircuitEditorState {
  let changed = false
  const columns = circuit.columns.map((column) => ({
    ...column,
    gates: column.gates.map((gate) => {
      if (
        gate.id !== gateId ||
        !isRegisterGateType(gate.type) ||
        gate.targets.length < 2
      ) {
        return gate
      }
      changed = true
      return { ...gate, targets: [...gate.targets].reverse() }
    }),
  }))
  return changed ? { ...circuit, columns } : circuit
}

export function clearCircuit(
  circuit: CircuitEditorState,
  columnCount = Math.max(1, circuit.columns.length),
): CircuitEditorState {
  return {
    ...circuit,
    columns: Array.from({ length: columnCount }, (_, step) => createEmptyCircuitColumn(step)),
  }
}

export function isCircuitEmpty(circuit: CircuitEditorState) {
  return circuit.columns.every((column) => column.gates.length === 0)
}

export function getGateIdAtSlot(
  circuit: CircuitEditorState,
  columnIndex: number,
  qubitIndex: number,
) {
  const column = circuit.columns[columnIndex]
  if (!column) {
    return null
  }

  const singleGate = column.gates.find(
    (gate) => !isMultiQubitGateType(gate.type) && gate.targets.includes(qubitIndex),
  )
  if (singleGate) {
    return singleGate.id
  }

  const cnotGate = column.gates.find(
    (gate) => isMultiQubitGateType(gate.type) && (gate.controls ?? []).concat(gate.targets).includes(qubitIndex),
  )
  return cnotGate ? cnotGate.id : null
}
