import {
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import type {
  CircuitEditorState,
  DragGatePayload,
  GateType,
  RegisterGateType,
  SingleQubitGateType,
} from '../types/circuit'
import type { GateDurationDefaults } from '../types/simulation'
import { createDefaultCircuit } from '../utils/circuitDefaults'
import {
  MAX_CIRCUIT_IMPORT_BYTES,
  circuitConfigToEditorState,
  parseCircuitConfigJson,
  type CircuitConfig,
} from '../utils/circuitConfigTransfer'
import {
  appendEmptyColumn,
  appendControlToCnotInCircuit,
  canRemoveLastColumn,
  canPlaceGateInColumn,
  clearCircuit,
  connectControlToXInCircuit,
  createCnotGate,
  createCcxGate,
  createPairGate,
  createPlacedGate,
  createRegisterGate,
  duplicateGateById,
  findGateLocationById,
  insertEmptyColumnAt,
  isCircuitEmpty,
  moveCnotGateInCircuit,
  moveCcxGateInCircuit,
  movePairGateInCircuit,
  moveRegisterGateInCircuit,
  moveSingleGateInCircuit,
  placeCnotGateFromDropInCircuit,
  placeCnotGateInCircuit,
  placeCcxGateFromDropInCircuit,
  placePairGateFromDropInCircuit,
  placePairGateInCircuit,
  placeRegisterGateFromDropInCircuit,
  placeRegisterGateInCircuit,
  placeSingleGateInCircuit,
  registerDurationUs,
  registerTargetsFromEntryOrder,
  removeGateById,
  reverseRegisterOrderById,
  updateGateParamsById,
  removeLastEmptyColumn,
  resizeCircuitEditorState,
  resolveCnotDropPlacement,
  resolveCcxDropPlacement,
  resolveRegisterDropPlacement,
  shiftGateColumnById,
  isControlledGateType,
  isPairGateType,
  isMultiControlledGateType,
  isRegisterGateType,
  isThetaGateType,
  maxMarkedIndex,
  MIN_REGISTER_GATE_QUBITS,
  MAX_SUPPORTED_CIRCUIT_COLUMNS,
} from '../utils/circuitEditing'
import {
  canRedo,
  canUndo,
  commitCircuitChange,
  createCircuitHistory,
  redoCircuitChange,
  undoCircuitChange,
  type CircuitHistoryState,
} from '../utils/circuitHistory'
import { CircuitContext, type CircuitContextValue, type CircuitPresetKey, type PendingCnotControl } from './CircuitContextCore'

type ActiveDragSession = {
  source: 'palette' | 'circuit'
  gateId?: string
  gateType: GateType
  committed: boolean
}

type CircuitProviderProps = {
  gateDurationDefaults: GateDurationDefaults
  children: ReactNode
  keyboardEnabled?: boolean
  keyboardScopeId?: string
  minimumLogicalQubits?: 1 | 2
}

const DEFAULT_EDITOR_HINT = 'パレットのゲートを選んでスロットをクリック、またはドラッグして配置できます。列と列のあいだに落とすと新しい列が入ります。'

export function CircuitProvider({ gateDurationDefaults, children, keyboardEnabled = true, keyboardScopeId, minimumLogicalQubits = 2 }: CircuitProviderProps) {
  const [circuitHistory, setCircuitHistory] = useState<CircuitHistoryState>(() =>
    createCircuitHistory(createDefaultCircuit()),
  )
  const [selectedGateType, setSelectedGateType] = useState<GateType | null>(null)
  const [selectedControlValue, setSelectedControlValue] = useState<0 | 1 | null>(null)
  const [selectedGateId, setSelectedGateId] = useState<string | null>(null)
  const [dragPayload, setDragPayload] = useState<DragGatePayload | null>(null)
  const [pendingCnotControl, setPendingCnotControl] = useState<PendingCnotControl | null>(null)
  const [editorHint, setEditorHint] = useState<string>(DEFAULT_EDITOR_HINT)
  const gateIdCounterRef = useRef(0)
  const activeDragRef = useRef<ActiveDragSession | null>(null)
  const circuitState = circuitHistory.present

  function finalizeCircuitEdit(nextCircuit: CircuitEditorState) {
    setCircuitHistory((currentHistory) => commitCircuitChange(currentHistory, nextCircuit))
    setSelectedGateId(null)
    setPendingCnotControl(null)
  }

  function handleSelectGateType(gateType: GateType | null) {
    setSelectedGateType(gateType)
    setSelectedControlValue(null)
    setSelectedGateId(null)
    setPendingCnotControl(null)

    if (gateType === null) {
      setEditorHint(DEFAULT_EDITOR_HINT)
      return
    }

    if (isRegisterGateType(gateType)) {
      setEditorHint(`${gateType}: 量子ビットをビット0(最下位)から順にクリックしてレジスタを作り、選択済みの量子ビットをもう一度クリックすると確定します。`)
      return
    }

    if (isControlledGateType(gateType) || isPairGateType(gateType)) {
      setEditorHint(`${gateType}: 量子ビットをクリックしてから、接続先の量子ビットをクリックしてください。`)
      return
    }

    setEditorHint(`${gateType}: スロットをクリックするか、パレットからドラッグして配置してください。`)
  }

  function handleSelectControlValue(controlValue: 0 | 1 | null) {
    setSelectedControlValue(controlValue)
    setSelectedGateType(null)
    setSelectedGateId(null)
    setPendingCnotControl(null)
    setEditorHint(controlValue === null
      ? DEFAULT_EDITOR_HINT
      : `${controlValue === 1 ? '制御 ●' : '反制御 ○'}: X と同じ列の量子ビットをクリックしてください。`)
  }

  function handleGateSelect(gateId: string | null) {
    setSelectedGateId(gateId)
    setPendingCnotControl(null)
    setEditorHint(gateId ? 'ゲートを選択中です。Deleteキーで削除できます。' : DEFAULT_EDITOR_HINT)
  }

  function handleLogicalQubitsChange(nextLogicalQubits: number) {
    if (nextLogicalQubits === circuitState.logical_qubits) {
      return
    }

    finalizeCircuitEdit(resizeCircuitEditorState(circuitState, nextLogicalQubits, minimumLogicalQubits))
    setEditorHint(`Circuit resized to ${nextLogicalQubits} qubits.`)
  }

  // Builds a register gate one click at a time, LSB-first: the qubit picked
  // first carries bit 0. Picking top to bottom therefore makes the top wire the
  // least significant bit, matching Quirk and Qiskit. The qubits need be
  // neither adjacent nor ascending. Clicking a qubit already in the register
  // commits it.
  function handleRegisterSlotClick(
    gateType: RegisterGateType,
    columnIndex: number,
    qubitIndex: number,
  ) {
    const pending =
      pendingCnotControl !== null && pendingCnotControl.columnIndex === columnIndex
        ? pendingCnotControl
        : null

    if (pending === null) {
      setPendingCnotControl({ columnIndex, qubitIndex, additionalQubits: [] })
      setEditorHint(`${gateType}: q${qubitIndex} をビット0(最下位)にしました。続けて上位ビットにする量子ビットをクリックしてください。`)
      return
    }

    const entryOrder = [pending.qubitIndex, ...(pending.additionalQubits ?? [])]
    if (!entryOrder.includes(qubitIndex)) {
      setPendingCnotControl({
        ...pending,
        additionalQubits: [...(pending.additionalQubits ?? []), qubitIndex],
      })
      setEditorHint(
        `${gateType}: ビット0から順に [${[...entryOrder, qubitIndex].map((qubit) => `q${qubit}`).join(', ')}]。`
        + '選択済みの量子ビットをもう一度クリックすると確定します。',
      )
      return
    }

    if (entryOrder.length < MIN_REGISTER_GATE_QUBITS) {
      setPendingCnotControl(null)
      setEditorHint(`${gateType}: 選択を解除しました。開始する量子ビットをクリックしてください。`)
      return
    }

    const registerQubits = registerTargetsFromEntryOrder(entryOrder)
    const gateId = `${gateType.toLowerCase()}-${columnIndex}-${registerQubits.join('-')}-${gateIdCounterRef.current}`
    const durationUs = registerDurationUs(
      gateDurationDefaults[gateType],
      registerQubits.length,
    )
    const candidate = createRegisterGate(registerQubits, durationUs, gateId, gateType)
    const placement = canPlaceGateInColumn(circuitState, columnIndex, candidate)
    if (!placement.valid) {
      setEditorHint(placement.message ?? `${gateType} cannot be placed in that occupied slot.`)
      return
    }

    gateIdCounterRef.current += 1
    finalizeCircuitEdit(placeRegisterGateInCircuit(
      circuitState,
      circuitState.columns.length,
      columnIndex,
      registerQubits,
      durationUs,
      gateId,
      gateType,
    ))
    setEditorHint(
      `${gateType} を配置しました。ビット0から順に [${entryOrder.map((qubit) => `q${qubit}`).join(', ')}]。`
      + (gateType === 'ORACLE' ? 'インスペクタでマークする状態を設定してください。' : ''),
    )
  }

  // Only CNOT/CZ/CP/SWAP and the register gates support click placement.
  // CNOT/CZ/CP/SWAP use a two-click "stretch" between any two qubits in a
  // column; register gates accumulate an ordered register (see above). Every
  // other gate type is drag-only (from the palette), and selecting an existing
  // gate happens by clicking it directly (see CircuitPreview's slot click
  // wiring), not through here.
  function placeControlMarker(
    columnIndex: number,
    qubitIndex: number,
    controlValue: 0 | 1,
  ) {
      const pendingControls = pendingCnotControl?.columnIndex === columnIndex
        ? [pendingCnotControl.qubitIndex, ...(pendingCnotControl.additionalQubits ?? [])]
        : []
      const pendingValues = pendingCnotControl?.columnIndex === columnIndex
        ? [pendingCnotControl.controlValue ?? 1, ...(pendingCnotControl.additionalControlValues ?? [])]
        : []
      if (pendingControls.includes(qubitIndex)) {
        const retained = pendingControls
          .map((control, index) => ({ control, value: pendingValues[index] }))
          .filter((item) => item.control !== qubitIndex)
        setPendingCnotControl(retained.length === 0 ? null : {
          columnIndex,
          qubitIndex: retained[0].control,
          controlValue: retained[0].value,
          additionalQubits: retained.slice(1).map((item) => item.control),
          additionalControlValues: retained.slice(1).map((item) => item.value),
        })
        setEditorHint('制御点の配置を解除しました。')
        return
      }

      const xTargets = circuitState.columns[columnIndex]?.gates.filter((gate) =>
        gate.type === 'X' && ![...pendingControls, qubitIndex].includes(gate.targets[0]),
      ) ?? []
      if (xTargets.length === 1) {
        const controls = [...pendingControls, qubitIndex]
        const values = [...pendingValues, controlValue]
        const gateId = `cnot-${columnIndex}-${controls.join('-')}-${xTargets[0].targets[0]}-${gateIdCounterRef.current}`
        const nextCircuit = connectControlToXInCircuit(
          circuitState,
          columnIndex,
          controls,
          values,
          gateDurationDefaults.CNOT,
          gateId,
        )
        if (nextCircuit !== circuitState) {
          gateIdCounterRef.current += 1
          setPendingCnotControl(null)
          finalizeCircuitEdit(nextCircuit)
          setEditorHint(`${controlValue === 1 ? '制御' : '反制御'}と X を自動接続しました。`)
          return
        }
      }

      const nextCnotCircuit = appendControlToCnotInCircuit(
        circuitState, columnIndex, qubitIndex, controlValue,
      )
      if (nextCnotCircuit !== circuitState) {
        setPendingCnotControl(null)
        finalizeCircuitEdit(nextCnotCircuit)
        setEditorHint(`${controlValue === 1 ? '制御' : '反制御'}を既存の制御Xへ追加しました。`)
        return
      }

      const controls = [...pendingControls, qubitIndex]
      const values = [...pendingValues, controlValue]
      setPendingCnotControl({
        columnIndex,
        qubitIndex: controls[0],
        controlValue: values[0],
        additionalQubits: controls.slice(1),
        additionalControlValues: values.slice(1),
      })
      setEditorHint(`${controls.length} 個の制御点を仮置きしました。同じ列へ X をドロップしてください。`)
  }

  function placeSingleGateByClick(
    columnIndex: number,
    qubitIndex: number,
    gateType: SingleQubitGateType,
  ) {
    const gateId = `${gateType.toLowerCase()}-${columnIndex}-${qubitIndex}-${gateIdCounterRef.current}`
    const candidate = createPlacedGate(
      gateType,
      qubitIndex,
      gateDurationDefaults[gateType],
      gateId,
    )
    const placement = canPlaceGateInColumn(circuitState, columnIndex, candidate)
    if (!placement.valid) {
      setEditorHint(`q${qubitIndex} は列 ${columnIndex + 1} で既に埋まっています。`)
      return
    }

    gateIdCounterRef.current += 1
    const nextCircuit = placeSingleGateInCircuit(
      circuitState,
      circuitState.columns.length,
      columnIndex,
      qubitIndex,
      gateType,
      gateDurationDefaults[gateType],
      gateId,
    )
    finalizeCircuitEdit(nextCircuit)
    if (isThetaGateType(gateType)) {
      setSelectedGateId(gateId)
      setEditorHint(`${gateType} を配置しました。インスペクターで角度 θ を指定してください。`)
      return
    }
    setEditorHint(`${gateType} を配置しました。続けてクリックすると同じゲートを置けます。`)
  }

  function placeCcxByClick(columnIndex: number, qubitIndex: number) {
    const gateId = `ccx-${columnIndex}-${qubitIndex}-${gateIdCounterRef.current}`
    const placement = resolveCcxDropPlacement(qubitIndex, circuitState.logical_qubits)
    const candidate = createCcxGate(
      placement.controlA,
      placement.controlB,
      placement.targetQubit,
      gateDurationDefaults.CCX,
      gateId,
    )
    const columnPlacement = canPlaceGateInColumn(circuitState, columnIndex, candidate)
    if (!columnPlacement.valid) {
      setEditorHint(columnPlacement.message ?? `CCX は列 ${columnIndex + 1} に置けません。`)
      return
    }

    gateIdCounterRef.current += 1
    finalizeCircuitEdit(
      placeCcxGateFromDropInCircuit(
        circuitState,
        circuitState.columns.length,
        columnIndex,
        qubitIndex,
        gateDurationDefaults.CCX,
        gateId,
      ),
    )
    setEditorHint('CCX を配置しました。')
  }

  function handleCircuitSlotClick(columnIndex: number, qubitIndex: number) {
    if (selectedControlValue !== null) {
      placeControlMarker(columnIndex, qubitIndex, selectedControlValue)
      return
    }

    if (selectedGateType && isRegisterGateType(selectedGateType)) {
      handleRegisterSlotClick(selectedGateType, columnIndex, qubitIndex)
      return
    }

    if (!selectedGateType) {
      return
    }

    /*
     * 1量子ビットゲートとCCXは、以前はドラッグでしか置けなかった。パレットで選んで
     * スロットをクリックしても何も起きないのが「扱いづらい」の主因だったので、
     * Pulseのパレット（クリックで置ける）に合わせてクリック配置を足す。
     * 落とす位置の解決はドロップとまったく同じ関数を使う。
     */
    if (!isControlledGateType(selectedGateType) && !isPairGateType(selectedGateType)) {
      if (isMultiControlledGateType(selectedGateType)) {
        placeCcxByClick(columnIndex, qubitIndex)
        return
      }
      placeSingleGateByClick(columnIndex, qubitIndex, selectedGateType)
      return
    }

    const gateType = selectedGateType

    if (pendingCnotControl === null || pendingCnotControl.columnIndex !== columnIndex) {
      setPendingCnotControl({ columnIndex, qubitIndex })
      setEditorHint(`${gateType}: 列 ${columnIndex + 1} で接続先の量子ビットをクリックしてください。`)
      return
    }

    if (pendingCnotControl.qubitIndex === qubitIndex) {
      setPendingCnotControl(null)
      setEditorHint(`${gateType}: 選択を解除しました。開始する量子ビットをクリックしてください。`)
      return
    }

    const gateId = `${gateType.toLowerCase()}-${columnIndex}-${pendingCnotControl.qubitIndex}-${qubitIndex}-${gateIdCounterRef.current}`
    const candidate = isPairGateType(gateType)
      ? createPairGate(pendingCnotControl.qubitIndex, qubitIndex, gateDurationDefaults[gateType], gateId, gateType)
      : createCnotGate(pendingCnotControl.qubitIndex, qubitIndex, gateDurationDefaults[gateType], gateId, gateType)
    const placement = canPlaceGateInColumn(circuitState, columnIndex, candidate)
    if (!placement.valid) {
      setEditorHint(placement.message ?? `${gateType} cannot be placed in that occupied slot.`)
      return
    }

    gateIdCounterRef.current += 1
    finalizeCircuitEdit(
      isPairGateType(gateType)
        ? placePairGateInCircuit(
            circuitState,
            circuitState.columns.length,
            columnIndex,
            pendingCnotControl.qubitIndex,
            qubitIndex,
            gateDurationDefaults[gateType],
            gateId,
            gateType,
          )
        : placeCnotGateInCircuit(
            circuitState,
            circuitState.columns.length,
            columnIndex,
            pendingCnotControl.qubitIndex,
            qubitIndex,
            gateDurationDefaults[gateType],
            gateId,
            gateType,
          ),
    )
    if (isThetaGateType(gateType)) {
      setSelectedGateId(gateId)
      setEditorHint(`${gateType}を配置しました。インスペクターで角度 θ を指定してください。`)
    } else {
      setEditorHint(`${gateType}を配置しました。`)
    }
  }

  function handleDeleteSelectedGate() {
    if (!selectedGateId) {
      return
    }

    handleDeleteGate(selectedGateId)
  }

  /*
   * ゲート脇の ×、右クリックメニュー、そして「回路ブロックの外へドラッグして離す」から呼ぶ。
   * 選択中かどうかに関わらず、そのIDのゲートを消す。
   * ドラッグ経由のときは進行中セッションを畳んでおき、直後の onDragEnd が
   * 「移動を取りやめました」で上書きしないようにする。
   */
  function handleDeleteGate(gateId: string) {
    const location = findGateLocationById(circuitState, gateId)
    if (!location) {
      return
    }

    if (activeDragRef.current?.gateId === gateId) {
      activeDragRef.current = null
    }
    finalizeCircuitEdit(removeGateById(circuitState, gateId))
    setEditorHint(`${location.gate.type} を削除しました。元に戻すで復元できます。`)
  }

  function handleLoadCircuitPreset(preset: CircuitPresetKey) {
    const duration = (type: GateType) => gateDurationDefaults[type]
    const gate = (
      id: string,
      type: GateType,
      targets: number[],
      controls: number[] = [],
      extra: Partial<CircuitEditorState['columns'][number]['gates'][number]> = {},
    ) => ({
      id,
      type,
      targets,
      controls,
      params: { duration_us: duration(type) },
      ...extra,
    })

    let columns: CircuitEditorState['columns']
    let logicalQubits: number
    let classicalBits: number
    let initialStates: CircuitEditorState['initial_states']
    let hint: string

    if (preset === 'teleportation') {
      columns = [
        { step: 0, gates: [gate('tele-h-q1', 'H', [1])] },
        { step: 1, gates: [gate('tele-cnot-a', 'CNOT', [2], [1])] },
        { step: 2, gates: [gate('tele-cnot-b', 'CNOT', [1], [0])] },
        { step: 3, gates: [gate('tele-h-q0', 'H', [0])] },
        { step: 4, gates: [gate('tele-m-q0', 'MEASURE', [0], [], { classical_targets: [0] })] },
        { step: 5, gates: [gate('tele-m-q1', 'MEASURE', [1], [], { classical_targets: [1] })] },
        { step: 6, gates: [gate('tele-x-q2', 'X', [2], [], { condition: { bit: 1, value: 1 }, conditions: [{ bit: 1, value: 1 }] })] },
        { step: 7, gates: [gate('tele-z-q2', 'Z', [2], [], { condition: { bit: 0, value: 1 }, conditions: [{ bit: 0, value: 1 }] })] },
      ]
      logicalQubits = 3
      classicalBits = 2
      initialStates = ['+', 0, 0]
      hint = '量子テレポーテーション回路を読み込みました。'
    } else if (preset === 'bit_flip_repetition') {
      columns = [
        { step: 0, gates: [gate('flip-enc-1', 'CNOT', [1], [0])] },
        { step: 1, gates: [gate('flip-enc-2', 'CNOT', [2], [0])] },
        { step: 2, gates: [gate('flip-error', 'X', [1])] },
        { step: 3, gates: [gate('flip-syndrome-a0', 'CNOT', [3], [0])] },
        { step: 4, gates: [gate('flip-syndrome-a1', 'CNOT', [3], [1])] },
        { step: 5, gates: [gate('flip-syndrome-b1', 'CNOT', [4], [1])] },
        { step: 6, gates: [gate('flip-syndrome-b2', 'CNOT', [4], [2])] },
        { step: 7, gates: [gate('flip-measure-a', 'MEASURE', [3], [], { classical_targets: [0] })] },
        { step: 8, gates: [gate('flip-measure-b', 'MEASURE', [4], [], { classical_targets: [1] })] },
        { step: 9, gates: [gate('flip-correct-q0', 'X', [0], [], { conditions: [{ bit: 0, value: 1 }, { bit: 1, value: 0 }] })] },
        { step: 10, gates: [gate('flip-correct-q1', 'X', [1], [], { conditions: [{ bit: 0, value: 1 }, { bit: 1, value: 1 }] })] },
        { step: 11, gates: [gate('flip-correct-q2', 'X', [2], [], { conditions: [{ bit: 0, value: 0 }, { bit: 1, value: 1 }] })] },
      ]
      logicalQubits = 5
      classicalBits = 2
      initialStates = [1, 0, 0, 0, 0]
      hint = '3量子ビット反復符号を読み込みました。X故障を注入済みです。'
    } else if (preset === 'magic_state') {
      // Prepare the magic state |T> = (|0> + e^{i pi/4}|1>)/sqrt(2) on q0 with H
      // then T.  Both gates carry zero declared duration, so the whole simulation
      // window becomes the post-circuit idle segment and the Bloch vector visibly
      // contracts under the environment.  q1 holds the Clifford state |+> (H only)
      // as a side-by-side reference: both Bloch vectors shrink at the same rate
      // here, so the contrast to look at is geometric -- |+> sits on an octahedron
      // vertex while |T> points 45 degrees off it, outside the Clifford polytope.
      columns = [
        { step: 0, gates: [gate('magic-h-q0', 'H', [0]), gate('magic-h-q1', 'H', [1])] },
        { step: 1, gates: [gate('magic-t-q0', 'T', [0])] },
      ]
      logicalQubits = 2
      classicalBits = 0
      initialStates = [0, 0]
      hint = 'マジック状態 |T> を読み込みました。q0が|T>、q1が参照用の|+>です。State ExplorerのBloch球で向きの違いを確認してください。'
    } else if (preset === 'grover_4qubit') {
      // Search a 16-item register for |0100>. The optimal integer Grover
      // iteration count is three. Each diffuser is H^4 X^4 MCZ X^4 H^4,
      // with MCZ represented as H(q3) · MCX(q0,q1,q2 -> q3) · H(q3).
      const allQubits = [0, 1, 2, 3]
      const allSingleQubitGates = (idPrefix: string, type: 'H' | 'X') =>
        allQubits.map((qubit) => gate(`${idPrefix}-q${qubit}`, type, [qubit]))
      const oracleGate = (iteration: number) => gate(
        `grover4-oracle-${iteration}`,
        'ORACLE',
        allQubits,
        [],
        { params: { duration_us: gateDurationDefaults.ORACLE * allQubits.length, marked_index: 4 } },
      )
      const diffuser = (iteration: number, step: number) => [
        { step, gates: allSingleQubitGates(`grover4-${iteration}-diff-h-a`, 'H') },
        { step: step + 1, gates: allSingleQubitGates(`grover4-${iteration}-diff-x-a`, 'X') },
        { step: step + 2, gates: [gate(`grover4-${iteration}-diff-h-target-a`, 'H', [3])] },
        {
          step: step + 3,
          gates: [gate(
            `grover4-${iteration}-diff-mcx`,
            'CNOT',
            [3],
            [0, 1, 2],
            { params: { duration_us: gateDurationDefaults.CNOT, control_state: 7 } },
          )],
        },
        { step: step + 4, gates: [gate(`grover4-${iteration}-diff-h-target-b`, 'H', [3])] },
        { step: step + 5, gates: allSingleQubitGates(`grover4-${iteration}-diff-x-b`, 'X') },
        { step: step + 6, gates: allSingleQubitGates(`grover4-${iteration}-diff-h-b`, 'H') },
      ]
      columns = [
        { step: 0, gates: allSingleQubitGates('grover4-init-h', 'H') },
        { step: 1, gates: [oracleGate(1)] },
        ...diffuser(1, 2),
        { step: 9, gates: [oracleGate(2)] },
        ...diffuser(2, 10),
        { step: 17, gates: [oracleGate(3)] },
        ...diffuser(3, 18),
      ]
      logicalQubits = 4
      classicalBits = 0
      initialStates = [0, 0, 0, 0]
      hint = '4量子ビットGrover探索回路を読み込みました。|0100>を3回の反復で振幅増幅します。'
    } else {
      // Marks |11> with a CZ oracle, then amplifies it with one Grover diffusion round.
      columns = [
        { step: 0, gates: [gate('grover-h0-a', 'H', [0])] },
        { step: 1, gates: [gate('grover-h1-a', 'H', [1])] },
        { step: 2, gates: [gate('grover-oracle', 'CZ', [1], [0])] },
        { step: 3, gates: [gate('grover-h0-b', 'H', [0])] },
        { step: 4, gates: [gate('grover-h1-b', 'H', [1])] },
        { step: 5, gates: [gate('grover-x0-a', 'X', [0])] },
        { step: 6, gates: [gate('grover-x1-a', 'X', [1])] },
        { step: 7, gates: [gate('grover-diffuser', 'CZ', [1], [0])] },
        { step: 8, gates: [gate('grover-x0-b', 'X', [0])] },
        { step: 9, gates: [gate('grover-x1-b', 'X', [1])] },
        { step: 10, gates: [gate('grover-h0-c', 'H', [0])] },
        { step: 11, gates: [gate('grover-h1-c', 'H', [1])] },
      ]
      logicalQubits = 2
      classicalBits = 0
      initialStates = [0, 0]
      hint = 'Grover探索回路を読み込みました。|11>を1回の反復で振幅増幅します。'
    }

    const nextCircuit: CircuitEditorState = {
      logical_qubits: logicalQubits,
      classical_bits: classicalBits,
      initial_states: initialStates,
      columns,
    }
    finalizeCircuitEdit(nextCircuit)
    setSelectedGateType(null)
    setEditorHint(hint)
  }

  function handleUpdateSelectedGateTheta(thetaRad: number) {
    if (!selectedGateId || !Number.isFinite(thetaRad)) {
      return
    }
    finalizeCircuitEdit(updateGateParamsById(
      circuitState,
      selectedGateId,
      { theta_rad: thetaRad },
    ))
    setSelectedGateId(selectedGateId)
    setEditorHint(`角度を ${thetaRad.toFixed(4)} rad に更新しました。`)
  }

  function handleUpdateSelectedGateMarkedIndex(markedIndex: number) {
    if (!selectedGateId || !Number.isInteger(markedIndex) || markedIndex < 0) {
      return
    }
    const gate = circuitState.columns
      .flatMap((column) => column.gates)
      .find((candidate) => candidate.id === selectedGateId)
    if (!gate || gate.type !== 'ORACLE') {
      return
    }
    const highest = maxMarkedIndex(gate.targets.length)
    if (markedIndex > highest) {
      setEditorHint(`ORACLE: マークできるのは 0〜${highest} です。`)
      return
    }
    finalizeCircuitEdit(updateGateParamsById(
      circuitState,
      selectedGateId,
      { marked_index: markedIndex },
    ))
    setSelectedGateId(selectedGateId)
    const label = markedIndex.toString(2).padStart(gate.targets.length, '0')
    setEditorHint(`ORACLE: |${label}> をマークするようにしました。`)
  }

  function handleReverseSelectedGateRegister() {
    if (!selectedGateId) {
      return
    }
    const nextCircuit = reverseRegisterOrderById(circuitState, selectedGateId)
    if (nextCircuit === circuitState) {
      return
    }
    finalizeCircuitEdit(nextCircuit)
    setSelectedGateId(selectedGateId)
    setEditorHint('レジスタ順を反転しました。上から順のワイヤ列を下位ビット先頭として読む外部シミュレータと突き合わせるときに使えます。')
  }

  function handleClearCircuit() {
    finalizeCircuitEdit(clearCircuit(circuitState))
    setEditorHint('Circuit cleared.')
  }

  function handleAddCircuitColumn() {
    const nextCircuit = appendEmptyColumn(circuitState)
    if (nextCircuit === circuitState) {
      setEditorHint(`Circuit limit reached (${MAX_SUPPORTED_CIRCUIT_COLUMNS} columns).`)
      return
    }
    finalizeCircuitEdit(nextCircuit)
    setSelectedGateId(null)
    setPendingCnotControl(null)
    setEditorHint(`Added column ${circuitState.columns.length + 1}.`)
  }

  function handleRemoveLastCircuitColumn() {
    if (!canRemoveLastColumn(circuitState)) {
      setEditorHint('Remove last column is available only when the final column is empty.')
      return
    }

    finalizeCircuitEdit(removeLastEmptyColumn(circuitState))
    setSelectedGateId(null)
    setPendingCnotControl(null)
    setEditorHint(`Removed column ${circuitState.columns.length}.`)
  }

  function handleUndoCircuit() {
    setCircuitHistory((currentHistory) => undoCircuitChange(currentHistory))
    setSelectedGateId(null)
    setPendingCnotControl(null)
    setEditorHint('Undid last circuit edit.')
  }

  function handleRedoCircuit() {
    setCircuitHistory((currentHistory) => redoCircuitChange(currentHistory))
    setSelectedGateId(null)
    setPendingCnotControl(null)
    setEditorHint('Redid circuit edit.')
  }

  function handlePaletteGateDragStart(gateType: GateType) {
    activeDragRef.current = { source: 'palette', gateType, committed: false }
    setDragPayload({ source: 'palette', gateType })
    setSelectedGateType(null)
    setSelectedGateId(null)
    if (gateType !== 'X' || pendingCnotControl === null) {
      setPendingCnotControl(null)
    }
    setEditorHint(`${gateType} をドラッグ中です。回路スロットにドロップしてください。`)
  }

  function handleControlMarkerDragStart(controlValue: 0 | 1) {
    activeDragRef.current = { source: 'palette', gateType: 'CNOT', committed: false }
    setDragPayload({ source: 'palette', gateType: 'CNOT', controlValue })
    setSelectedGateType(null)
    setSelectedGateId(null)
    setEditorHint(`${controlValue === 1 ? '制御 ●' : '反制御 ○'}をドラッグ中です。回路スロットにドロップしてください。`)
  }

  function handleCircuitGateDragStart(
    gateId: string,
    gateType: GateType,
    fromColumn: number,
    fromQubit: number,
  ) {
    activeDragRef.current = { source: 'circuit', gateId, gateType, committed: false }
    setDragPayload({ source: 'circuit', gateId, gateType, fromColumn, fromQubit })
    setSelectedGateType(null)
    setSelectedGateId(gateId)
    setPendingCnotControl(null)
    setEditorHint(`${gateType} を移動中です。回路スロットにドロップ、回路の外へ出して離すと削除。`)
  }

  /*
   * ドロップも削除もされずに終わったドラッグ（Esc・pointercancel・回路のすぐ内側で指を離した等）は
   * 「取りやめ」。回路ブロックの外で離した場合は handleUp が onDeleteGate を呼び、そこで
   * activeDragRef が畳まれるので、ここには来ない。
   * 以前は列内へのドロップでも消えて事故が多かったため、削除は「明確に枠外」に限定している。
   */
  function handleGateDragEnd() {
    const dragSession = activeDragRef.current
    if (dragSession?.source === 'circuit' && dragSession.committed === false) {
      setEditorHint(`${dragSession.gateType} の移動を取りやめました。Deleteキーでも削除できます。`)
    }

    activeDragRef.current = null
    setDragPayload(null)
  }

  /* Pulseタイムラインの ⧉ と同じで、押せば必ず隣の新しい列に増える。 */
  function handleDuplicateGate(gateId: string) {
    const location = findGateLocationById(circuitState, gateId)
    if (!location) {
      return
    }

    const nextGateId = `${location.gate.type.toLowerCase()}-copy-${gateIdCounterRef.current}`
    gateIdCounterRef.current += 1
    finalizeCircuitEdit(duplicateGateById(circuitState, gateId, nextGateId))
    setSelectedGateId(nextGateId)
    setEditorHint(`${location.gate.type} を右隣の新しい列へ複製しました。`)
  }

  /* Pulseタイムラインの ← → と同じで、隣が埋まっていても列を割り込ませて必ず動く。 */
  function handleShiftGateColumn(gateId: string, offset: -1 | 1) {
    const location = findGateLocationById(circuitState, gateId)
    if (!location) {
      return
    }

    const nextCircuit = shiftGateColumnById(circuitState, gateId, offset)
    if (nextCircuit === circuitState) {
      setEditorHint(`${location.gate.type} はこれ以上${offset < 0 ? '左' : '右'}へ動かせません。`)
      return
    }

    finalizeCircuitEdit(nextCircuit)
    setSelectedGateId(gateId)
    setEditorHint(`${location.gate.type} を${offset < 0 ? '左' : '右'}の列へ移動しました。`)
  }

  /* 列と列のあいだのドロップマーカーで受ける。新しい列を割り込ませてそこへ置く。 */
  function handleCircuitColumnInsertDrop(insertIndex: number, qubitIndex: number) {
    handleCircuitSlotDrop(insertIndex, qubitIndex, true)
  }

  /*
   * ドロップの受け口。insertColumn=true のときは、いま指している位置に空の列を
   * 割り込ませてからそこへ置く（列と列のあいだへのドロップ）。判定も配置も
   * baseCircuit に対して行うので、失敗したときは割り込んだ列ごと捨てられる。
   */
  function handleCircuitSlotDrop(
    columnIndex: number,
    qubitIndex: number,
    insertColumn = false,
  ) {
    if (insertColumn && circuitState.columns.length >= MAX_SUPPORTED_CIRCUIT_COLUMNS) {
      setEditorHint(`Circuit limit reached (${MAX_SUPPORTED_CIRCUIT_COLUMNS} columns).`)
      setDragPayload(null)
      return
    }
    const baseCircuit = insertColumn
      ? insertEmptyColumnAt(circuitState, columnIndex)
      : circuitState
    if (!dragPayload) {
      return
    }

    if (dragPayload.source === 'palette') {
      if (dragPayload.controlValue !== undefined) {
        /* 制御点は同じ列の X / CNOT に繋げるための記号なので、新しい空列には置けない。 */
        if (insertColumn) {
          setEditorHint('制御点は、繋げたい X や CNOT と同じ列のスロットへ落としてください。')
          if (activeDragRef.current) activeDragRef.current.committed = true
          setDragPayload(null)
          return
        }
        placeControlMarker(columnIndex, qubitIndex, dragPayload.controlValue)
        if (activeDragRef.current) activeDragRef.current.committed = true
        setDragPayload(null)
        return
      }
      const gateId = `${dragPayload.gateType.toLowerCase()}-${columnIndex}-${qubitIndex}-${gateIdCounterRef.current}`
      if (
        !insertColumn &&
        dragPayload.gateType === 'X' &&
        pendingCnotControl?.columnIndex === columnIndex &&
        ![pendingCnotControl.qubitIndex, ...(pendingCnotControl.additionalQubits ?? [])].includes(qubitIndex)
      ) {
        const controls = [pendingCnotControl.qubitIndex, ...(pendingCnotControl.additionalQubits ?? [])]
        const controlValues = [pendingCnotControl.controlValue ?? 1, ...(pendingCnotControl.additionalControlValues ?? [])]
        const controlledGateId = `cnot-${columnIndex}-${controls.join('-')}-${qubitIndex}-${gateIdCounterRef.current}`
        const candidate = createCnotGate(
          controls,
          qubitIndex,
          gateDurationDefaults.CNOT,
          controlledGateId,
          'CNOT',
          controlValues,
        )
        const placement = canPlaceGateInColumn(baseCircuit, columnIndex, candidate)
        if (placement.valid) {
          gateIdCounterRef.current += 1
          const nextCircuit = placeCnotGateInCircuit(
            baseCircuit,
            baseCircuit.columns.length,
            columnIndex,
            controls,
            qubitIndex,
            gateDurationDefaults.CNOT,
            controlledGateId,
            'CNOT',
            controlValues,
          )
          if (activeDragRef.current) activeDragRef.current.committed = true
          setPendingCnotControl(null)
          finalizeCircuitEdit(nextCircuit)
          setEditorHint(`${controls.length} 個の制御点と X を自動接続しました。`)
          setDragPayload(null)
          return
        }
      }
      const candidate =
        isRegisterGateType(dragPayload.gateType)
          ? (() => {
              const registerQubits = resolveRegisterDropPlacement(
                qubitIndex,
                baseCircuit.logical_qubits,
              )
              return createRegisterGate(
                registerQubits,
                registerDurationUs(
                  gateDurationDefaults[dragPayload.gateType],
                  registerQubits.length,
                ),
                gateId,
                dragPayload.gateType,
              )
            })()
          : isMultiControlledGateType(dragPayload.gateType)
          ? (() => {
              const placement = resolveCcxDropPlacement(qubitIndex, baseCircuit.logical_qubits)
              return createCcxGate(
                placement.controlA,
                placement.controlB,
                placement.targetQubit,
                gateDurationDefaults.CCX,
                gateId,
              )
            })()
          : isControlledGateType(dragPayload.gateType)
          ? (() => {
              const placement = resolveCnotDropPlacement(qubitIndex, baseCircuit.logical_qubits)
              return createCnotGate(
                placement.controlQubit,
                placement.targetQubit,
                gateDurationDefaults[dragPayload.gateType],
                gateId,
                dragPayload.gateType,
              )
            })()
          : isPairGateType(dragPayload.gateType)
            ? (() => {
                const placement = resolveCnotDropPlacement(qubitIndex, baseCircuit.logical_qubits)
                return createPairGate(
                  placement.controlQubit,
                  placement.targetQubit,
                  gateDurationDefaults[dragPayload.gateType],
                  gateId,
                  dragPayload.gateType,
                )
              })()
          : createPlacedGate(
              dragPayload.gateType,
              qubitIndex,
              gateDurationDefaults[dragPayload.gateType],
              gateId,
            )
      const placement = canPlaceGateInColumn(baseCircuit, columnIndex, candidate)
      if (!placement.valid) {
        if (activeDragRef.current) {
          activeDragRef.current.committed = true
        }
        setEditorHint(placement.message ?? `${dragPayload.gateType} cannot be placed in that occupied slot.`)
        setDragPayload(null)
        return
      }

      gateIdCounterRef.current += 1
      const nextCircuit =
        isRegisterGateType(dragPayload.gateType)
          ? placeRegisterGateFromDropInCircuit(
              baseCircuit,
              baseCircuit.columns.length,
              columnIndex,
              qubitIndex,
              gateDurationDefaults[dragPayload.gateType],
              gateId,
              dragPayload.gateType,
            )
          : isMultiControlledGateType(dragPayload.gateType)
          ? placeCcxGateFromDropInCircuit(
              baseCircuit,
              baseCircuit.columns.length,
              columnIndex,
              qubitIndex,
              gateDurationDefaults.CCX,
              gateId,
            )
          : isControlledGateType(dragPayload.gateType)
          ? placeCnotGateFromDropInCircuit(
              baseCircuit,
              baseCircuit.columns.length,
              columnIndex,
              qubitIndex,
              gateDurationDefaults[dragPayload.gateType],
              gateId,
              dragPayload.gateType,
            )
          : isPairGateType(dragPayload.gateType)
            ? placePairGateFromDropInCircuit(
                baseCircuit,
                baseCircuit.columns.length,
                columnIndex,
                qubitIndex,
                gateDurationDefaults[dragPayload.gateType],
                gateId,
                dragPayload.gateType,
              )
          : placeSingleGateInCircuit(
              baseCircuit,
              baseCircuit.columns.length,
              columnIndex,
              qubitIndex,
              dragPayload.gateType,
              gateDurationDefaults[dragPayload.gateType],
              gateId,
            )

      if (activeDragRef.current) {
        activeDragRef.current.committed = true
      }

      finalizeCircuitEdit(nextCircuit)
      if (isThetaGateType(dragPayload.gateType)) {
        // Select the new gate so the inspector opens on its angle field right away.
        setSelectedGateId(gateId)
        setEditorHint(`${dragPayload.gateType} を配置しました。インスペクターで角度 θ を指定してください。`)
      } else {
        setEditorHint(`${dragPayload.gateType} placed by drag-and-drop.`)
      }
      setDragPayload(null)
      return
    }

    if (
      !insertColumn &&
      dragPayload.fromColumn === columnIndex &&
      dragPayload.fromQubit === qubitIndex
    ) {
      if (activeDragRef.current) {
        activeDragRef.current.committed = true
      }
      setEditorHint(`${dragPayload.gateType} stayed in place.`)
      setDragPayload(null)
      return
    }

    const nextCircuit =
      isRegisterGateType(dragPayload.gateType)
        ? moveRegisterGateInCircuit(
            baseCircuit,
            baseCircuit.columns.length,
            dragPayload.gateId,
            columnIndex,
            qubitIndex,
            dragPayload.fromQubit,
          )
        : isMultiControlledGateType(dragPayload.gateType)
        ? moveCcxGateInCircuit(
            baseCircuit,
            baseCircuit.columns.length,
            dragPayload.gateId,
            columnIndex,
            qubitIndex,
          )
        : isControlledGateType(dragPayload.gateType)
        ? moveCnotGateInCircuit(
            baseCircuit,
            baseCircuit.columns.length,
            dragPayload.gateId,
            columnIndex,
            qubitIndex,
            dragPayload.fromQubit,
          )
        : isPairGateType(dragPayload.gateType)
          ? movePairGateInCircuit(
              baseCircuit,
              baseCircuit.columns.length,
              dragPayload.gateId,
              columnIndex,
              qubitIndex,
              dragPayload.fromQubit,
            )
        : moveSingleGateInCircuit(
            baseCircuit,
            baseCircuit.columns.length,
            dragPayload.gateId,
            columnIndex,
            qubitIndex,
          )

    if (activeDragRef.current) {
      activeDragRef.current.committed = true
    }

    /*
     * 移動できなかったときは baseCircuit がそのまま返る。insertColumn で割り込ませた
     * 空の列も baseCircuit 側にあるので、ここで確定させると空列だけが残ってしまう。
     */
    if (nextCircuit === baseCircuit) {
      setEditorHint(`${dragPayload.gateType} は、その位置には移動できません（スロットが埋まっています）。`)
      setDragPayload(null)
      return
    }

    finalizeCircuitEdit(nextCircuit)
    setEditorHint(`${dragPayload.gateType} をドラッグ＆ドロップで移動しました。`)
    setDragPayload(null)
  }

  async function handleImportCircuitConfig(file: File) {
    if (file.size > MAX_CIRCUIT_IMPORT_BYTES) {
      throw new Error(
        `Import failed: circuit files must be ${Math.floor(MAX_CIRCUIT_IMPORT_BYTES / 1024)} KiB or smaller.`,
      )
    }
    const text = await file.text()
    const importedCircuit = parseCircuitConfigJson(text, minimumLogicalQubits)

    finalizeCircuitEdit(importedCircuit)
    setSelectedGateType(null)
    setSelectedGateId(null)
    setPendingCnotControl(null)
    setEditorHint('回路をインポートしました。')

    return '回路をインポートしました。'
  }

  /*
   * 解析済みの回路をそのまま載せる。結果ファイルの読み込みから呼ばれる。
   *
   * ファイルの検証は呼び出し側（resultExport）が済ませているが、回路そのものの
   * 妥当性——ゲートの重なりや範囲外の量子ビット——はここで通す必要がある。
   * そのため circuitConfigToEditorState を経由させ、不正なら例外を出す。
   */
  function handleReplaceCircuitConfig(config: CircuitConfig) {
    const nextCircuit = circuitConfigToEditorState(config)
    finalizeCircuitEdit(nextCircuit)
    setSelectedGateType(null)
    setSelectedGateId(null)
    setPendingCnotControl(null)
    setEditorHint('読み込んだ結果の回路を復元しました。')
  }

  useEffect(() => {
    if (!keyboardEnabled) return
    function handleWindowKeyDown(event: KeyboardEvent) {
      if (keyboardScopeId) {
        const scope = document.getElementById(keyboardScopeId)
        if (!scope || scope.closest('[hidden]')) return
      }
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) {
        return
      }

      const target = event.target
      if (target instanceof HTMLElement) {
        const tagName = target.tagName.toLowerCase()
        if (
          tagName === 'input' ||
          tagName === 'textarea' ||
          tagName === 'select' ||
          target.isContentEditable
        ) {
          return
        }
      }

      if ((event.key === 'Delete' || event.key === 'Backspace') && selectedGateId !== null) {
        event.preventDefault()
        handleDeleteSelectedGate()
        return
      }

      /*
       * ドラッグを取りやめても、選んだゲート種別や途中まで作った制御点は残る。
       * 手を止めたいときの逃げ道として Escape で全部戻せるようにしておく。
       */
      if (event.key === 'Escape') {
        if (selectedGateType === null && selectedControlValue === null && selectedGateId === null) {
          return
        }
        event.preventDefault()
        setSelectedGateType(null)
        setSelectedControlValue(null)
        setSelectedGateId(null)
        setPendingCnotControl(null)
        setEditorHint(DEFAULT_EDITOR_HINT)
      }
    }

    window.addEventListener('keydown', handleWindowKeyDown)
    return () => window.removeEventListener('keydown', handleWindowKeyDown)
  })

  const value: CircuitContextValue = {
    circuitState,
    selectedGateType,
    selectedControlValue,
    selectedGateId,
    dragPayload,
    pendingCnotControl,
    editorHint,
    canUndoCircuit: canUndo(circuitHistory),
    canRedoCircuit: canRedo(circuitHistory),
    canDeleteSelected: selectedGateId !== null,
    canClearCircuit: !isCircuitEmpty(circuitState),
    canRemoveLastCircuitColumn: canRemoveLastColumn(circuitState),
    handleSelectGateType,
    handleSelectControlValue,
    handleGateSelect,
    handleLoadCircuitPreset,
    handleLogicalQubitsChange,
    handleCircuitSlotClick,
    handleDeleteSelectedGate,
    handleUpdateSelectedGateTheta,
    handleUpdateSelectedGateMarkedIndex,
    handleReverseSelectedGateRegister,
    handleClearCircuit,
    handleAddCircuitColumn,
    handleRemoveLastCircuitColumn,
    handleUndoCircuit,
    handleRedoCircuit,
    handlePaletteGateDragStart,
    handleControlMarkerDragStart,
    handleCircuitGateDragStart,
    handleGateDragEnd,
    handleCircuitSlotDrop,
    handleCircuitColumnInsertDrop,
    handleDeleteGate,
    handleDuplicateGate,
    handleShiftGateColumn,
    handleImportCircuitConfig,
    handleReplaceCircuitConfig,
  }

  return <CircuitContext.Provider value={value}>{children}</CircuitContext.Provider>
}
