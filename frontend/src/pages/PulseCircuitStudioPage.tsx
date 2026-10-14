import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import './PulseCircuitStudioPage.css'
import { spawnGatePlacementEffect } from '../utils/gatePlacementEffect'
import { PulseBlockEditor } from '../components/PulseBlockEditor'
import type { PulseLabForm } from '../types/pulse'
import type {
  PulseCircuitLane,
  PulseCircuitState,
  PulseCircuitStep,
  PulsePrimitive,
  PulseStepParameters,
} from '../types/pulseCircuit'
import {
  createPulseCircuitStep,
  isDrivePulseStep,
  reconcileDrivePulseStep,
  normalizeFramePhase,
  pulseStepDurationUs,
  resizePulseCircuit,
} from '../utils/pulseCircuit'
import { pulseStepConstraintIssues } from '../utils/pulseConstraints'
import { matchingPulseDeviceProfile } from '../utils/pulseDeviceProfiles'

type PulseCircuitStudioPageProps = {
  circuit: PulseCircuitState
  currentForm: PulseLabForm
  onCircuitChange: (circuit: PulseCircuitState) => void
  onSelectPulseForRun: (transmonIndex: number, pulse: PulseStepParameters) => void
}

type DragPayload =
  | { kind: 'palette'; primitive: PulsePrimitive }
  | { kind: 'step'; transmonIndex: number; stepId: string }

type DropTarget = { transmonIndex: number; index: number }

/*
 * ブロック幅は駆動時間に比例させる。等幅だと「4σ Gaussian」と「長いSquare」が
 * 同じ見た目になり、タイムラインとしての情報量がゼロになるため。
 * ただし極端に短いPulseが1pxへ潰れないよう下限を設ける。
 */
const MIN_STEP_WIDTH_PX = 82
const MAX_STEP_WIDTH_PX = 232
const VIRTUAL_Z_WIDTH_PX = 62

/* 実行できるのは2〜4トランズモン。それ以上は編集専用。 */
const RUNNABLE_TRANSMON_MAX = 4

const palette: Array<{ primitive: PulsePrimitive; label: string; detail: string }> = [
  { primitive: 'x90', label: 'X/2', detail: 'X軸周りに+90°' },
  { primitive: 'x180', label: 'X', detail: 'X軸周りに+180°' },
  { primitive: 'y90', label: 'Y/2', detail: 'Y軸周りに+90°' },
  { primitive: 'y180', label: 'Y', detail: 'Y軸周りに+180°' },
  { primitive: 'custom', label: 'Custom', detail: '現在のPulse設定' },
  { primitive: 'virtual_z', label: 'VZ', detail: 'ゼロ時間の位相フレーム更新' },
]

export function PulseCircuitStudioPage({
  circuit,
  currentForm,
  onCircuitChange,
  onSelectPulseForRun,
}: PulseCircuitStudioPageProps) {
  const firstStep = circuit.lanes.flatMap((lane) => lane.steps)[0] ?? null
  const [selectedTransmonIndex, setSelectedTransmonIndex] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(firstStep?.id ?? null)
  const [draftPulse, setDraftPulse] = useState<PulseStepParameters | null>(
    firstStep && isDrivePulseStep(firstStep) ? { ...firstStep.pulse } : null,
  )
  const [draftVirtualZAngle, setDraftVirtualZAngle] = useState<number | null>(
    firstStep?.operation === 'virtual_z' ? firstStep.angleRad : null,
  )
  const [draftDirty, setDraftDirty] = useState(false)
  const [dragPayload, setDragPayload] = useState<DragPayload | null>(null)
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null)
  const [clearArmed, setClearArmed] = useState(false)
  const idCounter = useRef(circuit.lanes.reduce((count, lane) => count + lane.steps.length, 0))
  const editorRef = useRef<HTMLDivElement | null>(null)
  const clearTimer = useRef<number | null>(null)
  /* onDrop が有効なレーンで受けたら true。onDragEnd で「枠外に放した＝削除」を判定する。 */
  const droppedOnLaneRef = useRef(false)

  const selectedStep = findStep(circuit, selectedId)
  const totalPulseCount = circuit.lanes.reduce((count, lane) => count + lane.steps.length, 0)
  const transmonCount = circuit.transmons.length
  const deviceProfile = matchingPulseDeviceProfile(circuit.executionConstraints)
  const exceedsRunnableScope = transmonCount > RUNNABLE_TRANSMON_MAX

  /*
   * 制約判定は1ブロックあたり129点の波形を評価するので、
   * ドラッグ中の再描画で毎回走らないよう回路と共通設定でメモ化する。
   */
  const issuesByStepId = useMemo(() => {
    const table = new Map<string, string[]>()
    circuit.lanes.forEach((lane) => {
      lane.steps.forEach((step) => {
        const issues = pulseStepConstraintIssues(step, currentForm, circuit.executionConstraints)
        if (issues.length > 0) {
          table.set(step.id, issues)
        }
      })
    })
    return table
  }, [circuit.lanes, circuit.executionConstraints, currentForm])

  const schedules = useMemo(
    () => new Map(circuit.lanes.map((lane) => [
      lane.transmonIndex,
      laneSchedule(lane, circuit.executionConstraints.interPulseGapUs),
    ])),
    [circuit.lanes, circuit.executionConstraints.interPulseGapUs],
  )
  const longestStepUs = Math.max(
    0,
    ...circuit.lanes.flatMap((lane) => lane.steps.filter(isDrivePulseStep).map(
      (step) => finiteOrZero(pulseStepDurationUs(step.pulse)),
    )),
  )
  const pxPerUs = longestStepUs > 0 ? MAX_STEP_WIDTH_PX / longestStepUs : 0
  const violationCount = [...issuesByStepId.values()].reduce((total, issues) => total + issues.length, 0)
  const editorOpen = draftPulse !== null || draftVirtualZAngle !== null

  useEffect(() => {
    if (editorOpen) {
      editorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [selectedId, editorOpen])

  useEffect(() => () => {
    if (clearTimer.current !== null) {
      window.clearTimeout(clearTimer.current)
    }
  }, [])

  /*
   * 配置エフェクトは、置いたブロックそのものの上で焚く。
   *
   * Pulseブロックは長さ（時間）に応じて幅が変わり、ドロップの当たり判定も
   * ブロックの左右どちら寄りかで挿入位置が決まるため、カーソル位置と実際に
   * 入った場所はよくずれる。置けた step.id を控えて、描画後にその要素を
   * 探して中心から出す。パレットのクリック追加のように、そもそもカーソルが
   * 配置先の近くにない経路もある。
   */
  const pendingPlacementEffectRef = useRef<string | null>(null)

  function armPlacementEffect(stepId: string) {
    pendingPlacementEffectRef.current = stepId
  }

  useEffect(() => {
    const stepId = pendingPlacementEffectRef.current
    pendingPlacementEffectRef.current = null
    if (stepId === null) {
      return
    }

    const element = document.querySelector(
      `.pulse-circuit-studio__step[data-step-id="${CSS.escape(stepId)}"]`,
    )
    if (!element) {
      return
    }

    const bounds = element.getBoundingClientRect()
    spawnGatePlacementEffect(
      bounds.left + bounds.width / 2,
      bounds.top + bounds.height / 2,
      'pulse',
    )
  }, [circuit])

  /*
   * 未保存の下書きを持ったまま別ブロックへ移ると、編集内容が黙って消えていた。
   * 回路を書き換える操作はすべてこれを通し、直前の下書きを確定させてから進む。
   */
  function commitDraft(base: PulseCircuitState): PulseCircuitState {
    if (!selectedStep || !draftDirty) {
      return base
    }
    const lane = base.lanes[selectedStep.transmonIndex]
    if (!lane || !lane.steps.some((step) => step.id === selectedStep.step.id)) {
      return base
    }
    if (selectedStep.step.operation === 'virtual_z') {
      if (draftVirtualZAngle === null || !Number.isFinite(draftVirtualZAngle)) {
        return base
      }
      return withLane(base, selectedStep.transmonIndex, lane.steps.map((step) => (
        step.id === selectedStep.step.id && step.operation === 'virtual_z'
          ? { ...step, angleRad: normalizeFramePhase(draftVirtualZAngle) }
          : step
      )))
    }
    if (!draftPulse) {
      return base
    }
    return withLane(base, selectedStep.transmonIndex, lane.steps.map((step) => (
      step.id === selectedStep.step.id && step.operation === 'drive'
        ? reconcileDrivePulseStep(step, { ...draftPulse })
        : step
    )))
  }

  /* 選択状態だけを動かす。回路本体には触れない。 */
  function focusStep(transmonIndex: number, step: PulseCircuitStep) {
    setSelectedTransmonIndex(transmonIndex)
    setSelectedId(step.id)
    setDraftDirty(false)
    if (isDrivePulseStep(step)) {
      setDraftPulse({ ...step.pulse })
      setDraftVirtualZAngle(null)
      onSelectPulseForRun(transmonIndex, step.pulse)
    } else {
      setDraftPulse(null)
      setDraftVirtualZAngle(step.angleRad)
    }
  }

  function selectStep(transmonIndex: number, step: PulseCircuitStep) {
    const base = commitDraft(circuit)
    if (base !== circuit) {
      onCircuitChange(base)
    }
    focusStep(transmonIndex, findStep(base, step.id)?.step ?? step)
  }

  function mutateLane(transmonIndex: number, mutate: (steps: PulseCircuitStep[]) => PulseCircuitStep[]) {
    const base = commitDraft(circuit)
    const lane = base.lanes[transmonIndex]
    if (!lane) {
      return
    }
    setDraftDirty(false)
    onCircuitChange(withLane(base, transmonIndex, mutate(lane.steps)))
  }

  /*
   * 配置エフェクトを出す位置（ビューポート座標）。実際に置けた／動かせた分岐でだけ
   * 焚きたいので、早期 return のある関数の内側まで持ち回る。
   */
  function insertStep(
    primitive: PulsePrimitive,
    transmonIndex: number,
    index: number,
  ) {
    const base = commitDraft(circuit)
    const lane = base.lanes[transmonIndex]
    if (!lane) {
      return
    }
    idCounter.current += 1
    const step = createPulseCircuitStep(
      primitive,
      currentForm,
      `pulse-q${transmonIndex}-step-${idCounter.current}`,
    )
    const steps = [...lane.steps]
    steps.splice(clampIndex(index, steps.length), 0, step)
    onCircuitChange(withLane(base, transmonIndex, steps))
    focusStep(transmonIndex, step)
    armPlacementEffect(step.id)
  }

  function duplicateStep(transmonIndex: number, index: number) {
    const base = commitDraft(circuit)
    const lane = base.lanes[transmonIndex]
    const source = lane?.steps[index]
    if (!lane || !source) {
      return
    }
    idCounter.current += 1
    const id = `pulse-q${transmonIndex}-step-${idCounter.current}`
    const copy: PulseCircuitStep = source.operation === 'drive'
      ? { ...source, id, pulse: { ...source.pulse } }
      : { ...source, id }
    const steps = [...lane.steps]
    steps.splice(index + 1, 0, copy)
    onCircuitChange(withLane(base, transmonIndex, steps))
    focusStep(transmonIndex, copy)
  }

  function removeStep(transmonIndex: number, id: string) {
    const base = commitDraft(circuit)
    const lane = base.lanes[transmonIndex]
    if (!lane) {
      return
    }
    const index = lane.steps.findIndex((step) => step.id === id)
    const next = lane.steps.filter((step) => step.id !== id)
    setDraftDirty(false)
    onCircuitChange(withLane(base, transmonIndex, next))
    if (selectedId !== id) {
      return
    }
    const nextSelected = next[Math.min(index, Math.max(next.length - 1, 0))] ?? null
    if (nextSelected) {
      focusStep(transmonIndex, nextSelected)
    } else {
      setSelectedId(null)
      setDraftPulse(null)
      setDraftVirtualZAngle(null)
    }
  }

  function moveStep(transmonIndex: number, index: number, offset: number) {
    const destination = index + offset
    const lane = circuit.lanes[transmonIndex]
    if (!lane || destination < 0 || destination >= lane.steps.length) {
      return
    }
    mutateLane(transmonIndex, (steps) => {
      const next = [...steps]
      const [step] = next.splice(index, 1)
      next.splice(destination, 0, step)
      return next
    })
  }

  function moveStepTo(
    payload: Extract<DragPayload, { kind: 'step' }>,
    transmonIndex: number,
    index: number,
  ) {
    const base = commitDraft(circuit)
    const source = base.lanes[payload.transmonIndex]
    const target = base.lanes[transmonIndex]
    if (!source || !target) {
      return
    }
    const sourceIndex = source.steps.findIndex((step) => step.id === payload.stepId)
    if (sourceIndex < 0) {
      return
    }
    const step = source.steps[sourceIndex]
    setDraftDirty(false)

    if (payload.transmonIndex === transmonIndex) {
      const destination = index > sourceIndex ? index - 1 : index
      if (destination === sourceIndex) {
        /* 位置が変わらないドロップでも、確定済みの下書きは書き戻す。 */
        if (base !== circuit) {
          onCircuitChange(base)
        }
        return
      }
      const steps = [...source.steps]
      steps.splice(sourceIndex, 1)
      steps.splice(clampIndex(destination, steps.length), 0, step)
      onCircuitChange(withLane(base, transmonIndex, steps))
      armPlacementEffect(step.id)
      return
    }

    const targetSteps = [...target.steps]
    targetSteps.splice(clampIndex(index, targetSteps.length), 0, step)
    onCircuitChange(withLane(
      withLane(base, payload.transmonIndex, source.steps.filter((candidate) => candidate.id !== payload.stepId)),
      transmonIndex,
      targetSteps,
    ))
    focusStep(transmonIndex, step)
    armPlacementEffect(step.id)
  }

  function saveDraft() {
    const base = commitDraft(circuit)
    if (base !== circuit) {
      onCircuitChange(base)
    }
    setDraftDirty(false)
    if (draftVirtualZAngle !== null && Number.isFinite(draftVirtualZAngle)) {
      setDraftVirtualZAngle(normalizeFramePhase(draftVirtualZAngle))
    }
    if (selectedStep && draftPulse) {
      onSelectPulseForRun(selectedStep.transmonIndex, draftPulse)
    }
  }

  /* 閉じる=破棄。保存済みの値に戻す。 */
  function closeEditor() {
    setDraftPulse(null)
    setDraftVirtualZAngle(null)
    setDraftDirty(false)
  }

  function changeTransmonCount(count: number) {
    const next = resizePulseCircuit(commitDraft(circuit), count)
    setDraftDirty(false)
    onCircuitChange(next)
    setSelectedTransmonIndex(Math.min(selectedTransmonIndex, next.transmons.length - 1))
    if (selectedStep && selectedStep.transmonIndex >= next.transmons.length) {
      setSelectedId(null)
      setDraftPulse(null)
      setDraftVirtualZAngle(null)
    }
  }

  /* 全消去は取り消せないので二段階クリックで確認する。 */
  function requestClearAll() {
    if (clearTimer.current !== null) {
      window.clearTimeout(clearTimer.current)
    }
    if (!clearArmed) {
      setClearArmed(true)
      clearTimer.current = window.setTimeout(() => setClearArmed(false), 4000)
      return
    }
    setClearArmed(false)
    setDraftDirty(false)
    onCircuitChange({ ...circuit, lanes: circuit.lanes.map((lane) => ({ ...lane, steps: [] })) })
    setSelectedId(null)
    setDraftPulse(null)
    setDraftVirtualZAngle(null)
  }

  function acceptDrag(event: React.DragEvent, target: DropTarget) {
    if (!dragPayload) {
      return
    }
    event.preventDefault()
    event.dataTransfer.dropEffect = dragPayload.kind === 'palette' ? 'copy' : 'move'
    setDropTarget((current) => (
      current && current.transmonIndex === target.transmonIndex && current.index === target.index
        ? current
        : target
    ))
  }

  function handleDrop(transmonIndex: number, index: number) {
    const payload = dragPayload
    droppedOnLaneRef.current = true
    setDragPayload(null)
    setDropTarget(null)
    if (!payload) {
      return
    }
    if (payload.kind === 'palette') {
      insertStep(payload.primitive, transmonIndex, index)
      return
    }
    moveStepTo(payload, transmonIndex, index)
  }

  /*
   * onDragEnd。onDrop の後に必ず発火する。回路レーンの外で放した step ドラッグは
   * そのPulseを削除する（Gate-aware回路の「枠外へドラッグして消す」と同じ操作感）。
   * パレットからのドラッグ（kind === 'palette'）は枠外で放しても何もしない。
   */
  function endDrag() {
    const payload = dragPayload
    const droppedOnLane = droppedOnLaneRef.current
    droppedOnLaneRef.current = false
    setDragPayload(null)
    setDropTarget(null)
    if (!droppedOnLane && payload?.kind === 'step') {
      removeStep(payload.transmonIndex, payload.stepId)
    }
  }

  function dropMarker(transmonIndex: number, index: number) {
    const active = dropTarget?.transmonIndex === transmonIndex && dropTarget.index === index
    return (
      <div
        className="pulse-circuit-studio__drop-marker"
        data-active={active}
        data-dragging={dragPayload !== null}
        onDragOver={(event) => acceptDrag(event, { transmonIndex, index })}
        onDrop={(event) => {
          event.preventDefault()
          handleDrop(transmonIndex, index)
        }}
      />
    )
  }

  const editorFramePhaseRad = selectedStep
    ? framePhaseBefore(circuit.lanes[selectedStep.transmonIndex]?.steps ?? [], selectedStep.step.id)
    : 0

  return (
    <div className="pulse-circuit-studio">
      <aside className="pulse-circuit-studio__notice" data-tone={exceedsRunnableScope ? 'alert' : 'info'}>
        {exceedsRunnableScope
          ? `現在 ${transmonCount} トランズモン。密度行列の計算量上限により、5台以上は回路編集のみで実行できません。実行するには4台以下にしてください。`
          : '2〜4台ではツールバーから全レーンを同時実行できます（交換結合ありのネットワーク）。5〜8台は回路編集のみ対応です。'}
      </aside>

      <section className="pulse-circuit-studio__environment" aria-label="Pulse回路の環境設定">
        <div>
          <span>システム全体</span>
          <strong>回路全体の構成</strong>
        </div>
        <label className="pulse-circuit-studio__transmon-count">
          トランズモン数
          <select value={transmonCount} onChange={(event) => changeTransmonCount(Number(event.target.value))}>
            {[1, 2, 3, 4, 5, 6, 7, 8].map((count) => (
              <option value={count} key={count}>
                {count}{count > RUNNABLE_TRANSMON_MAX ? '（編集のみ）' : ''}
              </option>
            ))}
          </select>
        </label>
        <dl>
          <div><dt>デバイス</dt><dd>{deviceProfile?.name ?? 'カスタム'}</dd></div>
          <div><dt>温度</dt><dd>{currentForm.temperatureMk} mK</dd></div>
          <div><dt>デバイス品質</dt><dd>{currentForm.deviceQuality.toFixed(2)}</dd></div>
          <div><dt>発展方式</dt><dd>{currentForm.evolutionMethod === 'explicit_cptp' ? 'Explicit CPTP' : 'RK4'}</dd></div>
          <div><dt>最大駆動</dt><dd>{circuit.executionConstraints.maximumDriveAmplitudeRadPerUs} rad/us</dd></div>
          <div><dt>AWG刻み</dt><dd>{circuit.executionConstraints.awgSamplePeriodUs} us</dd></div>
          <div><dt>パルス間隔</dt><dd>{circuit.executionConstraints.interPulseGapUs} us</dd></div>
        </dl>
      </section>

      <section className="pulse-circuit-studio__workspace">
        <aside className="pulse-circuit-studio__palette" aria-label="Pulseパレット">
          <div>
            <span>プリミティブ</span>
            <h2>Pulseを追加</h2>
            <p className="pulse-circuit-studio__palette-hint">
              クリックで <strong>q{selectedTransmonIndex}</strong> の末尾へ追加。ドラッグすれば任意のレーンの任意の位置に挿入できます。
            </p>
          </div>
          {palette.map((item) => (
            <button
              type="button"
              key={item.primitive}
              data-operation={item.primitive === 'virtual_z' ? 'virtual_z' : 'drive'}
              draggable
              onDragStart={(event) => {
                event.dataTransfer.effectAllowed = 'copy'
                event.dataTransfer.setData('text/plain', `palette:${item.primitive}`)
                setDragPayload({ kind: 'palette', primitive: item.primitive })
              }}
              onDragEnd={endDrag}
              onClick={() => insertStep(
                item.primitive,
                selectedTransmonIndex,
                circuit.lanes[selectedTransmonIndex]?.steps.length ?? 0,
              )}
            >
              <strong>{item.label}</strong>
              <small>{item.detail}</small>
            </button>
          ))}
        </aside>

        <section className="pulse-circuit-studio__sequence" aria-labelledby="pulse-sequence-title">
          <div className="pulse-circuit-studio__sequence-heading">
            <div>
              <span>複数トランズモンシーケンス</span>
              <h2 id="pulse-sequence-title">Pulseタイムライン</h2>
            </div>
            <button
              type="button"
              className="pulse-circuit-studio__clear"
              data-armed={clearArmed}
              disabled={totalPulseCount === 0}
              onBlur={() => setClearArmed(false)}
              onClick={requestClearAll}
            >
              {clearArmed ? `本当に${totalPulseCount}個削除？` : 'すべて削除'}
            </button>
          </div>

          <p className="pulse-circuit-studio__scale-note">
            ブロック幅は駆動時間に比例（最長 {formatDurationUs(longestStepUs)} が基準。可読性のため最小幅あり）。
            Virtual Zは時間を消費しないため固定幅で表示します。
          </p>

          <div className="pulse-circuit-studio__lanes">
            {circuit.lanes.map((lane) => {
              const schedule = schedules.get(lane.transmonIndex)
              const isActive = lane.transmonIndex === selectedTransmonIndex
              return (
                <div
                  className="pulse-circuit-studio__track"
                  data-active={isActive}
                  data-drop={dropTarget?.transmonIndex === lane.transmonIndex}
                  key={lane.transmonIndex}
                >
                  <button
                    type="button"
                    className="pulse-circuit-studio__line-label"
                    aria-pressed={isActive}
                    onClick={() => setSelectedTransmonIndex(lane.transmonIndex)}
                  >
                    <strong>{circuit.transmons[lane.transmonIndex]?.label ?? `q${lane.transmonIndex}`}</strong>
                    <small>|0⟩ / 駆動</small>
                    <small className="pulse-circuit-studio__line-total">
                      {schedule ? formatDurationUs(schedule.totalUs) : '—'}
                    </small>
                  </button>
                  {lane.steps.length === 0 ? (
                    <div
                      className="pulse-circuit-studio__lane-empty"
                      data-drop={dropTarget?.transmonIndex === lane.transmonIndex}
                      onDragOver={(event) => acceptDrag(event, { transmonIndex: lane.transmonIndex, index: 0 })}
                      onDrop={(event) => {
                        event.preventDefault()
                        handleDrop(lane.transmonIndex, 0)
                      }}
                    >
                      <button type="button" onClick={() => setSelectedTransmonIndex(lane.transmonIndex)}>
                        {isActive
                          ? 'パレットをクリック、またはここへドラッグ'
                          : `q${lane.transmonIndex} を選択してPulseを追加`}
                      </button>
                    </div>
                  ) : (
                    <div className="pulse-circuit-studio__steps">
                      {lane.steps.map((step, index) => {
                        const issues = issuesByStepId.get(step.id) ?? []
                        const isSelected = step.id === selectedId
                        const isDragged = dragPayload?.kind === 'step' && dragPayload.stepId === step.id
                        return (
                          <Fragment key={step.id}>
                            {dropMarker(lane.transmonIndex, index)}
                            <article
                              className="pulse-circuit-studio__step"
                              data-step-id={step.id}
                              style={{ ['--step-width' as string]: `${stepWidthPx(step, pxPerUs)}px` }}
                              data-operation={step.operation}
                              data-selected={isSelected}
                              data-invalid={issues.length > 0}
                              data-dragging={isDragged}
                              data-dirty={isSelected && draftDirty}
                              draggable
                              onDragStart={(event) => {
                                event.dataTransfer.effectAllowed = 'move'
                                event.dataTransfer.setData('text/plain', `step:${step.id}`)
                                setDragPayload({ kind: 'step', transmonIndex: lane.transmonIndex, stepId: step.id })
                              }}
                              onDragEnd={endDrag}
                              onDragOver={(event) => {
                                if (!dragPayload) {
                                  return
                                }
                                const bounds = event.currentTarget.getBoundingClientRect()
                                const after = event.clientX > bounds.left + bounds.width / 2
                                acceptDrag(event, {
                                  transmonIndex: lane.transmonIndex,
                                  index: index + (after ? 1 : 0),
                                })
                              }}
                              onDrop={(event) => {
                                event.preventDefault()
                                handleDrop(lane.transmonIndex, dropTarget?.index ?? index)
                              }}
                            >
                              <button
                                type="button"
                                className="pulse-circuit-studio__step-main"
                                aria-pressed={isSelected}
                                title={issues.length > 0 ? issues.join('\n') : undefined}
                                onClick={() => selectStep(lane.transmonIndex, step)}
                              >
                                <span className="pulse-circuit-studio__step-index">
                                  {index + 1}
                                  {issues.length > 0 ? (
                                    <em
                                      className="pulse-circuit-studio__step-flag"
                                      aria-label={`ハード制約違反 ${issues.length} 件`}
                                    >
                                      !
                                    </em>
                                  ) : null}
                                </span>
                                <strong>{step.label}</strong>
                                {isDrivePulseStep(step) ? (
                                  <>
                                    <small>{formatDurationUs(pulseStepDurationUs(step.pulse))}</small>
                                    <small className="pulse-circuit-studio__step-time">
                                      t {formatDurationUs(schedule?.startUs[index] ?? 0)}
                                    </small>
                                  </>
                                ) : (
                                  <>
                                    <small>{formatPhase(step.angleRad)}</small>
                                    <small className="pulse-circuit-studio__step-time">
                                      frame {normalizeFramePhase(framePhaseAtStep(lane.steps, index)).toFixed(3)}
                                    </small>
                                  </>
                                )}
                              </button>
                              <div className="pulse-circuit-studio__step-actions">
                                <button
                                  type="button"
                                  aria-label={`${step.label}を左へ移動`}
                                  disabled={index === 0}
                                  onClick={() => moveStep(lane.transmonIndex, index, -1)}
                                >
                                  ←
                                </button>
                                <button
                                  type="button"
                                  aria-label={`${step.label}を右へ移動`}
                                  disabled={index === lane.steps.length - 1}
                                  onClick={() => moveStep(lane.transmonIndex, index, 1)}
                                >
                                  →
                                </button>
                                <button
                                  type="button"
                                  aria-label={`${step.label}を複製`}
                                  onClick={() => duplicateStep(lane.transmonIndex, index)}
                                >
                                  ⧉
                                </button>
                                <button
                                  type="button"
                                  aria-label={`${step.label}を削除`}
                                  onClick={() => removeStep(lane.transmonIndex, step.id)}
                                >
                                  ×
                                </button>
                              </div>
                            </article>
                          </Fragment>
                        )
                      })}
                      {dropMarker(lane.transmonIndex, lane.steps.length)}
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          {violationCount > 0 ? (
            <aside className="pulse-circuit-studio__violations" aria-label="ハードウェア制約の違反">
              <strong>
                {deviceProfile?.name ?? 'カスタムデバイス'} の制約に {violationCount} 件違反しています（このままでは実行できません）
              </strong>
              <ul>
                {circuit.lanes.flatMap((lane) => lane.steps.flatMap((step, index) => (
                  (issuesByStepId.get(step.id) ?? []).map((message, issueIndex) => (
                    <li key={`${step.id}-${issueIndex}`}>
                      <button type="button" onClick={() => selectStep(lane.transmonIndex, step)}>
                        q{lane.transmonIndex} #{index + 1} {step.label}
                      </button>
                      <span>{message}</span>
                    </li>
                  ))
                )))}
              </ul>
            </aside>
          ) : null}

          <div ref={editorRef}>
            {selectedStep?.step.operation === 'drive' && draftPulse ? (
              <PulseBlockEditor
                label={`q${selectedStep.transmonIndex} / ${selectedStep.step.label}`}
                pulse={draftPulse}
                globalForm={currentForm}
                constraints={circuit.executionConstraints}
                framePhaseRad={editorFramePhaseRad}
                dirty={draftDirty}
                onChange={(pulse) => {
                  setDraftPulse(pulse)
                  setDraftDirty(true)
                }}
                onSave={saveDraft}
                onClose={closeEditor}
              />
            ) : null}
            {selectedStep?.step.operation === 'virtual_z' && draftVirtualZAngle !== null ? (
              <aside className="pulse-circuit-studio__virtual-z-editor">
                <div>
                  <span>PHASE FRAME / q{selectedStep.transmonIndex}</span>
                  <h2>Virtual Zを編集{draftDirty ? '（未保存）' : ''}</h2>
                  <p>
                    時間を進めず、以後の論理位相フレームを更新します。
                    直前までの累積フレームは {editorFramePhaseRad.toFixed(3)} rad です。
                  </p>
                </div>
                <label>
                  回転角 λ [rad]
                  <input
                    type="number"
                    step="0.05"
                    value={draftVirtualZAngle}
                    onChange={(event) => {
                      setDraftVirtualZAngle(Number(event.target.value))
                      setDraftDirty(true)
                    }}
                  />
                </label>
                <div className="pulse-circuit-studio__virtual-z-presets">
                  {[Math.PI / 4, Math.PI / 2, Math.PI, -Math.PI / 2].map((angle) => (
                    <button
                      type="button"
                      key={angle}
                      onClick={() => {
                        setDraftVirtualZAngle(angle)
                        setDraftDirty(true)
                      }}
                    >
                      {formatPhase(angle)}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  className="pulse-circuit-studio__virtual-z-save"
                  disabled={!Number.isFinite(draftVirtualZAngle) || !draftDirty}
                  onClick={saveDraft}
                >
                  {draftDirty ? 'フレーム更新を保存' : '保存済み'}
                </button>
              </aside>
            ) : null}
          </div>
        </section>
      </section>
    </div>
  )
}

function withLane(
  circuit: PulseCircuitState,
  transmonIndex: number,
  steps: PulseCircuitStep[],
): PulseCircuitState {
  return {
    ...circuit,
    lanes: circuit.lanes.map((lane) => (
      lane.transmonIndex === transmonIndex ? { ...lane, steps } : lane
    )),
  }
}

function clampIndex(index: number, length: number): number {
  return Math.min(Math.max(index, 0), length)
}

function finiteOrZero(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 0
}

function stepWidthPx(step: PulseCircuitStep, pxPerUs: number): number {
  if (!isDrivePulseStep(step)) {
    return VIRTUAL_Z_WIDTH_PX
  }
  return Math.max(MIN_STEP_WIDTH_PX, Math.round(pxPerUs * finiteOrZero(pulseStepDurationUs(step.pulse))))
}

/* PulseLabPage の transmonNetworkDurationUs と同じ並べ方で開始時刻を数える。 */
function laneSchedule(
  lane: PulseCircuitLane,
  interPulseGapUs: number,
): { startUs: number[]; totalUs: number } {
  let cursorUs = 0
  let driveCount = 0
  const startUs = lane.steps.map((step) => {
    if (!isDrivePulseStep(step)) {
      return cursorUs
    }
    if (driveCount > 0) {
      cursorUs += interPulseGapUs
    }
    const start = cursorUs
    cursorUs += finiteOrZero(pulseStepDurationUs(step.pulse))
    driveCount += 1
    return start
  })
  return { startUs, totalUs: cursorUs }
}

function framePhaseAtStep(steps: PulseCircuitStep[], inclusiveIndex: number): number {
  return steps.slice(0, inclusiveIndex + 1).reduce(
    (phase, step) => phase + (step.operation === 'virtual_z' ? step.angleRad : 0),
    0,
  )
}

function framePhaseBefore(steps: PulseCircuitStep[], stepId: string): number {
  const index = steps.findIndex((step) => step.id === stepId)
  return index <= 0 ? 0 : normalizeFramePhase(framePhaseAtStep(steps, index - 1))
}

function formatDurationUs(durationUs: number): string {
  if (!Number.isFinite(durationUs)) {
    return '— us'
  }
  if (durationUs === 0) {
    return '0 us'
  }
  return durationUs < 0.001
    ? `${(durationUs * 1000).toPrecision(3)} ns`
    : `${durationUs.toPrecision(3)} us`
}

function formatPhase(angleRad: number): string {
  const ratio = angleRad / Math.PI
  if (ratio === 1) return 'π'
  if (ratio === 0.5) return 'π/2'
  if (ratio === 0.25) return 'π/4'
  if (ratio === -0.5) return '-π/2'
  return `${angleRad.toFixed(3)} rad`
}

function findStep(
  circuit: PulseCircuitState,
  stepId: string | null,
): { transmonIndex: number; step: PulseCircuitStep } | null {
  if (!stepId) {
    return null
  }
  for (const lane of circuit.lanes) {
    const step = lane.steps.find((candidate) => candidate.id === stepId)
    if (step) {
      return { transmonIndex: lane.transmonIndex, step }
    }
  }
  return null
}
