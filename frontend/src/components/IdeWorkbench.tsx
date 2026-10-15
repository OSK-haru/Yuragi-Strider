import { useRef, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'
import { KEYBOARD_STEP, LIMITS, PANE_LABELS, type IdeLayoutControls, type PaneKey } from '../hooks/useIdeLayout'

/*
 * IDE 画面のペイン配置。一般的な IDE と同じく、ナビゲーター・プロパティ・
 * メッセージの各ペインを閉じたり開いたりでき、境界線をドラッグして大きさを
 * 変えられる。状態は hooks/useIdeLayout が持つ。
 */

/** タイトル行に置く「表示」メニュー。閉じたペインはここから開き直す。 */
export function IdeViewToggles({ controls, omit = [] }: { controls: IdeLayoutControls; omit?: PaneKey[] }) {
  const { layout, labels, togglePane, reset } = controls
  return (
    <div className="ide__view-toggles" role="group" aria-label="表示">
      <span className="ide__view-label">表示</span>
      {(Object.keys(PANE_LABELS) as PaneKey[]).filter((pane) => !omit.includes(pane)).map((pane) => (
        <button key={pane} type="button" aria-pressed={layout.visible[pane]} onClick={() => togglePane(pane)}>
          {labels[pane]}
        </button>
      ))}
      <button type="button" onClick={reset} title="ペインの表示と大きさを初期状態に戻す">配置を戻す</button>
    </div>
  )
}

type SplitterProps = {
  orientation: 'vertical' | 'horizontal'
  label: string
  value: number
  sizeKey: keyof typeof LIMITS
  /* ドラッグ量に掛ける向き。右・下のペインは境界を左・上へ動かすと広がるので -1。 */
  direction: 1 | -1
  controls: IdeLayoutControls
  area: string
}

function Splitter({ orientation, label, value, sizeKey, direction, controls, area }: SplitterProps) {
  const drag = useRef<{ start: number; startValue: number } | null>(null)
  const [min, max] = LIMITS[sizeKey]
  const position = (event: PointerEvent) => orientation === 'vertical' ? event.clientX : event.clientY

  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { start: position(event), startValue: value }
  }
  function onPointerMove(event: PointerEvent<HTMLDivElement>) {
    if (!drag.current) return
    controls.resize(sizeKey, drag.current.startValue + (position(event) - drag.current.start) * direction)
  }
  function onPointerUp(event: PointerEvent<HTMLDivElement>) {
    drag.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const grow = orientation === 'vertical' ? 'ArrowRight' : 'ArrowDown'
    const shrink = orientation === 'vertical' ? 'ArrowLeft' : 'ArrowUp'
    if (event.key === grow || event.key === shrink) {
      event.preventDefault()
      const sign = (event.key === grow ? 1 : -1) * direction
      controls.resize(sizeKey, value + sign * KEYBOARD_STEP)
    } else if (event.key === 'Home') {
      event.preventDefault(); controls.resize(sizeKey, min)
    } else if (event.key === 'End') {
      event.preventDefault(); controls.resize(sizeKey, max)
    }
  }

  return (
    <div
      className={`ide__splitter ide__splitter--${orientation}`}
      style={{ gridArea: area }}
      role="separator"
      tabIndex={0}
      aria-label={label}
      aria-orientation={orientation}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      title={`${label}（ドラッグで調整、ダブルクリックで初期値）`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onDoubleClick={() => controls.resetSize(sizeKey)}
      onKeyDown={onKeyDown}
    />
  )
}

function PaneSlot({ area, pane, controls, children }: {
  area: string; pane?: PaneKey; controls: IdeLayoutControls; children: ReactNode
}) {
  return (
    <div className="ide__slot" data-area={area} style={{ gridArea: area }}>
      {children}
      {pane ? (
        <button type="button" className="ide__pane-close" aria-label={`${controls.labels[pane]}を閉じる`}
          title={`${controls.labels[pane]}を閉じる`} onClick={() => controls.setPaneVisible(pane, false)}>×</button>
      ) : null}
    </div>
  )
}

/**
 * 4つのペインを格子に並べ、境界に分割線を挟む。閉じたペインは描かない
 * （中身の状態はページ側が持つので、開き直しても失われない）。
 */
export function IdeWorkbench({ controls, navigator, editor, toolbox, inspector, messages, forceVisible = {} }: {
  controls: IdeLayoutControls
  navigator: ReactNode
  editor: ReactNode
  /* 省略したページはその欄を持たない。 */
  toolbox?: ReactNode
  inspector?: ReactNode
  /* 省略すると、その時は下の欄を出さない（伝えることが無いのに面積を取らない）。 */
  messages?: ReactNode
  /* チュートリアルが指しているペインは、閉じていても出す。保存した配置は変えない。 */
  forceVisible?: Partial<Record<PaneKey, boolean>>
}) {
  const { layout } = controls
  const visible: Record<PaneKey, boolean> = {
    navigator: layout.visible.navigator || forceVisible.navigator === true,
    toolbox: toolbox !== undefined && (layout.visible.toolbox || forceVisible.toolbox === true),
    inspector: inspector !== undefined && (layout.visible.inspector || forceVisible.inspector === true),
    messages: messages !== undefined && (layout.visible.messages || forceVisible.messages === true),
  }

  const columns = [
    ...(visible.navigator ? [`${layout.navigatorWidth}px`, '5px'] : []),
    ...(visible.toolbox ? [`${layout.toolboxWidth}px`, '5px'] : []),
    'minmax(0, 1fr)',
    ...(visible.inspector ? ['5px', `${layout.inspectorWidth}px`] : []),
  ]
  const topRow = [
    ...(visible.navigator ? ['nav', 'split-nav'] : []),
    ...(visible.toolbox ? ['toolbox', 'split-toolbox'] : []),
    'editor',
    ...(visible.inspector ? ['split-inspector', 'inspector'] : []),
  ]
  const rows = ['minmax(0, 1fr)', ...(visible.messages ? ['5px', `${layout.messagesHeight}px`] : [])]
  const areas = [
    topRow.join(' '),
    ...(visible.messages ? [topRow.map(() => 'split-messages').join(' '), topRow.map(() => 'messages').join(' ')] : []),
  ]

  return (
    <div
      className="ide__body"
      style={{
        gridTemplateColumns: columns.join(' '),
        gridTemplateRows: rows.join(' '),
        gridTemplateAreas: areas.map((row) => `"${row}"`).join(' '),
      }}
    >
      {visible.navigator ? <>
        <PaneSlot area="nav" pane="navigator" controls={controls}>{navigator}</PaneSlot>
        <Splitter orientation="vertical" label="ナビゲーターの幅" value={layout.navigatorWidth} sizeKey="navigatorWidth"
          direction={1} controls={controls} area="split-nav" />
      </> : null}
      <PaneSlot area="editor" controls={controls}>{editor}</PaneSlot>
      {visible.toolbox ? <>
        <PaneSlot area="toolbox" pane="toolbox" controls={controls}>{toolbox}</PaneSlot>
        <Splitter orientation="vertical" label="分析ツール箱の幅" value={layout.toolboxWidth} sizeKey="toolboxWidth"
          direction={1} controls={controls} area="split-toolbox" />
      </> : null}
      {visible.inspector ? <>
        <Splitter orientation="vertical" label="プロパティの幅" value={layout.inspectorWidth} sizeKey="inspectorWidth"
          direction={-1} controls={controls} area="split-inspector" />
        <PaneSlot area="inspector" pane="inspector" controls={controls}>{inspector}</PaneSlot>
      </> : null}
      {visible.messages ? <>
        <Splitter orientation="horizontal" label="メッセージの高さ" value={layout.messagesHeight} sizeKey="messagesHeight"
          direction={-1} controls={controls} area="split-messages" />
        <PaneSlot area="messages" pane="messages" controls={controls}>{messages}</PaneSlot>
      </> : null}
    </div>
  )
}
