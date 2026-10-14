import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type Dispatch,
  type KeyboardEvent,
  type PointerEvent,
  type SetStateAction,
} from 'react'
import './HomeModeDrum.css'
import { useAnimationSettings } from '../context/useAnimationSettings'
import { usePrefersReducedMotion } from '../hooks/usePrefersReducedMotion'
import { documentationLinks } from '../utils/documentationLinks'
import {
  gaussianPulsePath,
  HOME_MODE_COUNT,
  MODE_ENTRIES,
  type ModeEntry,
} from '../utils/homeModes'

/*
 * ホームの主動線。通常モード / PULSEモード / 攻撃シミュレーター / 公式ドキュメント
 * の4つを、四角柱のドラムが回るように切り替える。
 *
 * 面の間隔は 360° / 面の数 = 90°。回転半径は面の高さ h に対して
 * r = h / (2·tan 45°) = h·0.5 で決まるので、高さだけ決めれば辻褄が合う。
 * CSS 側でこの係数を持っている。面の数を変えるときは、ここの角度と
 * CSS の係数の両方を直す。
 *
 * 自動送りはするが、ホバー・フォーカス中は止める。読んでいる最中に
 * 面が回るのはただの妨害なので。
 */

const ROTATION_INTERVAL_MS = 5000

const PULSE_SIGNATURE = gaussianPulsePath()

function ModeSignature({ kind }: { kind: ModeEntry['signature'] }) {
  if (kind === 'pulse') {
    return (
      <svg className="mode-drum__signature" viewBox="0 0 96 32" aria-hidden="true">
        <line x1="0" y1="16" x2="96" y2="16" className="mode-drum__signature-rule" />
        <path d={PULSE_SIGNATURE} className="mode-drum__signature-trace" />
      </svg>
    )
  }

  if (kind === 'attack') {
    return (
      <svg className="mode-drum__signature" viewBox="0 0 96 32" aria-hidden="true">
        <line x1="0" y1="16" x2="96" y2="16" className="mode-drum__signature-rule" />
        <line x1="48" y1="0" x2="48" y2="32" className="mode-drum__signature-rule" />
        <circle cx="48" cy="16" r="11" className="mode-drum__signature-trace" />
        <circle cx="48" cy="16" r="3" className="mode-drum__signature-node" />
        <line x1="30" y1="16" x2="38" y2="16" className="mode-drum__signature-accent" />
        <line x1="58" y1="16" x2="66" y2="16" className="mode-drum__signature-accent" />
      </svg>
    )
  }

  if (kind === 'docs') {
    return (
      <svg className="mode-drum__signature" viewBox="0 0 96 32" aria-hidden="true">
        <path d="M6 4 L2 4 L2 28 L6 28" className="mode-drum__signature-trace" />
        <path d="M90 4 L94 4 L94 28 L90 28" className="mode-drum__signature-trace" />
        <line x1="14" y1="10" x2="82" y2="10" className="mode-drum__signature-rule" />
        <line x1="14" y1="16" x2="66" y2="16" className="mode-drum__signature-rule" />
        <line x1="14" y1="22" x2="74" y2="22" className="mode-drum__signature-accent" />
      </svg>
    )
  }

  return (
    <svg className="mode-drum__signature" viewBox="0 0 96 32" aria-hidden="true">
      <line x1="0" y1="9" x2="96" y2="9" className="mode-drum__signature-rule" />
      <line x1="0" y1="24" x2="96" y2="24" className="mode-drum__signature-rule" />
      <rect x="14" y="3" width="12" height="12" className="mode-drum__signature-trace" />
      <line x1="52" y1="9" x2="52" y2="24" className="mode-drum__signature-accent" />
      <circle cx="52" cy="9" r="3" className="mode-drum__signature-node" />
      <rect x="46" y="18" width="12" height="12" className="mode-drum__signature-accent" />
      <rect x="74" y="3" width="12" height="12" className="mode-drum__signature-trace" />
    </svg>
  )
}

type HomeModeDrumProps = {
  /*
   * index ではなく単調増加の turn を持つ。index を 2 → 0 に戻すと、ドラムが
   * 240° 逆走して「戻った」ように見えてしまう。turn を進め続ければ、
   * 見た目は常に同じ向きに回る。
   *
   * 状態を親に預けているのは、背景のゆらぎを同じ turn で駆動するため。
   * ここで持ってコールバックで知らせると、真の値が2箇所に分かれる。
   */
  turn: number
  onTurnChange: Dispatch<SetStateAction<number>>
  onStartSimulation: () => void
  onOpenPulseLab: () => void
  onOpenAttackSimulator: () => void
}

/* 1面ぶんの回転角。CSS の --drum-step と同じ値。 */
const FACE_ANGLE_DEG = 360 / HOME_MODE_COUNT

export function HomeModeDrum({
  turn,
  onTurnChange,
  onStartSimulation,
  onOpenPulseLab,
  onOpenAttackSimulator,
}: HomeModeDrumProps) {
  const [paused, setPaused] = useState(false)
  const [latched, setLatched] = useState<string | null>(null)
  const { animationsEnabled } = useAnimationSettings()
  const prefersReducedMotion = usePrefersReducedMotion()
  const latchTimer = useRef<number | undefined>(undefined)

  /*
   * レバーを掴んでいる間の段。null なら掴んでいない。
   * ドラッグ中は turn を毎フレーム書き換えず、ここに置いた段だけを動かす。
   * turn を直接動かすとドラムが指の動きに合わせて何度も回り直してしまう。
   */
  const [gripIndex, setGripIndex] = useState<number | null>(null)
  const slotRef = useRef<HTMLDivElement>(null)

  const activeIndex = ((turn % HOME_MODE_COUNT) + HOME_MODE_COUNT) % HOME_MODE_COUNT
  /* 掴んでいる間は自動送りを止める。指の下で面が回るのはただの妨害。 */
  const autoRotates = animationsEnabled && !prefersReducedMotion && !paused
    && gripIndex === null

  useEffect(() => {
    if (!autoRotates) {
      return
    }
    const timer = window.setTimeout(
      () => onTurnChange((current) => current + 1),
      ROTATION_INTERVAL_MS,
    )
    return () => window.clearTimeout(timer)
  }, [autoRotates, turn, onTurnChange])

  useEffect(() => () => window.clearTimeout(latchTimer.current), [])

  /* 目的の面へ、前方向に回して到達する最短の turn を選ぶ。 */
  function goTo(index: number) {
    onTurnChange((current) => {
      const currentIndex = ((current % HOME_MODE_COUNT) + HOME_MODE_COUNT) % HOME_MODE_COUNT
      const forward = (index - currentIndex + HOME_MODE_COUNT) % HOME_MODE_COUNT
      return current + forward
    })
  }

  /* 溝の中の y 座標を、いちばん近い段の番号に直す。 */
  function indexFromPointer(clientY: number): number {
    const slot = slotRef.current
    if (slot === null) {
      return activeIndex
    }
    const bounds = slot.getBoundingClientRect()
    if (bounds.height <= 0) {
      return activeIndex
    }
    const ratio = (clientY - bounds.top) / bounds.height
    const stop = Math.round(ratio * HOME_MODE_COUNT - 0.5)
    return Math.min(HOME_MODE_COUNT - 1, Math.max(0, stop))
  }

  function handleSlotPointerDown(event: PointerEvent<HTMLDivElement>) {
    /* 主ボタン以外は無視。右クリックでレバーが動くのは事故のもと。 */
    if (event.button !== 0) {
      return
    }
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    setGripIndex(indexFromPointer(event.clientY))
  }

  function handleSlotPointerMove(event: PointerEvent<HTMLDivElement>) {
    if (gripIndex === null) {
      return
    }
    setGripIndex(indexFromPointer(event.clientY))
  }

  /* 指を離した段に確定させる。掴んだまま同じ段で離せば、ただの選択になる。 */
  function handleSlotPointerUp(event: PointerEvent<HTMLDivElement>) {
    if (gripIndex === null) {
      return
    }
    const target = indexFromPointer(event.clientY)
    setGripIndex(null)
    goTo(target)
  }

  /* 掴みが取り消されたら、ノブは元の段へ戻す。 */
  function handleSlotPointerCancel() {
    setGripIndex(null)
  }

  function activate(entry: ModeEntry, callback: () => void) {
    setLatched(entry.id)
    latchTimer.current = window.setTimeout(callback, 180)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault()
      onTurnChange((current) => current + 1)
      return
    }
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault()
      onTurnChange((current) => current - 1)
    }
  }

  return (
    <section
      className="mode-drum"
      aria-roledescription="carousel"
      aria-label="モードの選択"
      data-tutorial-anchor="home-modes"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      onKeyDown={handleKeyDown}
    >
      <div className="mode-drum__frame">
        <div
          className="mode-drum__stage"
          style={{ '--drum-angle': `${-FACE_ANGLE_DEG * turn}deg` } as CSSProperties}
        >
          <div className="mode-drum__barrel">
            {MODE_ENTRIES.map((entry, index) => {
              const isActive = index === activeIndex
              return (
                <article
                  className={`mode-drum__face mode-drum__face--${entry.id}${
                    isActive ? ' mode-drum__face--active' : ''
                  }`}
                  key={entry.id}
                  style={{ '--face-index': index } as CSSProperties}
                  aria-hidden={!isActive}
                  inert={!isActive}
                >
                  <div className="mode-drum__face-head">
                    <span className="mode-drum__unit">{entry.unit}</span>
                    <ModeSignature kind={entry.signature} />
                  </div>

                  <h2 className="mode-drum__title">{entry.title}</h2>
                  <p className="mode-drum__tagline">{entry.tagline}</p>
                  <p className="mode-drum__detail">{entry.detail}</p>

                  <dl className="mode-drum__readouts">
                    {entry.readouts.map((readout) => (
                      <div key={readout.label}>
                        <dt>{readout.label}</dt>
                        <dd>{readout.value}</dd>
                      </div>
                    ))}
                  </dl>

                  {entry.id === 'docs' ? (
                    <a
                      className="mode-drum__cta"
                      href={documentationLinks.home}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <span className="mode-drum__cta-mark" aria-hidden="true">
                        &gt;&gt;&gt;
                      </span>
                      {entry.callToAction}
                    </a>
                  ) : (
                    <button
                      className={`mode-drum__cta${
                        latched === entry.id ? ' mode-drum__cta--latched' : ''
                      }`}
                      type="button"
                      onClick={() =>
                        activate(
                          entry,
                          entry.id === 'pulse'
                            ? onOpenPulseLab
                            : entry.id === 'attack'
                              ? onOpenAttackSimulator
                              : onStartSimulation,
                        )
                      }
                    >
                      <span className="mode-drum__cta-mark" aria-hidden="true">
                        &gt;&gt;&gt;
                      </span>
                      {entry.callToAction}
                    </button>
                  )}
                </article>
              )
            })}
          </div>
        </div>

        {/*
          切り替えレバー。今どの面にいるかと、直接の行き先を兼ねる。
          溝（スロット）の上をノブが滑って止まる形にして、
          「掴んで動かせるもの」だと一目で分かるようにしている。
          位置は active な段の番号から算出するので、段数が変わっても追従する。

          レバー自体も掴んで動かせる。見た目が「動かせるもの」なのに
          横の番号を押すしかない、という食い違いを無くすため。
          ドラッグ中は掴んでいる段を、離していれば現在の段を指す。
        */}
        <div
          className="mode-drum__rail"
          style={{
            '--rail-stops': HOME_MODE_COUNT,
            '--rail-index': gripIndex ?? activeIndex,
          } as CSSProperties}
        >
          <div
            className={`mode-drum__rail-slot${
              gripIndex === null ? '' : ' mode-drum__rail-slot--held'
            }`}
            ref={slotRef}
            /*
             * レバー本体はスライダーとして扱う。目盛りは横のボタンが
             * 受け持つので、こちらは掴んで動かす操作だけを担う。
             */
            role="slider"
            tabIndex={0}
            aria-label="モードの切り替えレバー"
            aria-orientation="vertical"
            aria-valuemin={1}
            aria-valuemax={HOME_MODE_COUNT}
            aria-valuenow={(gripIndex ?? activeIndex) + 1}
            aria-valuetext={MODE_ENTRIES[gripIndex ?? activeIndex].title}
            onPointerDown={handleSlotPointerDown}
            onPointerMove={handleSlotPointerMove}
            onPointerUp={handleSlotPointerUp}
            onPointerCancel={handleSlotPointerCancel}
          >
            <span className="mode-drum__rail-knob">
              <span className="mode-drum__rail-knob-grip" />
            </span>
          </div>

          <ol className="mode-drum__rail-stops">
            {MODE_ENTRIES.map((entry, index) => (
              <li key={entry.id}>
                <button
                  className={`mode-drum__detent${
                    index === activeIndex ? ' mode-drum__detent--active' : ''
                  }`}
                  type="button"
                  aria-current={index === activeIndex}
                  aria-label={`${entry.title}へ`}
                  onClick={() => goTo(index)}
                >
                  <span className="mode-drum__detent-tick" aria-hidden="true" />
                  <span className="mode-drum__detent-index" aria-hidden="true">
                    {String(index + 1).padStart(2, '0')}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </div>
      </div>

      <div className="mode-drum__transport">
        <button
          className="mode-drum__step"
          type="button"
          aria-label="前の項目"
          onClick={() => onTurnChange((current) => current - 1)}
        >
          &lt;
        </button>

        <div className="mode-drum__gauge">
          <span
            className={`mode-drum__gauge-fill${
              autoRotates ? '' : ' mode-drum__gauge-fill--held'
            }`}
            key={turn}
            style={{ '--dwell': `${ROTATION_INTERVAL_MS}ms` } as CSSProperties}
          />
        </div>

        <span className="mode-drum__count">
          {String(activeIndex + 1).padStart(2, '0')} / {String(HOME_MODE_COUNT).padStart(2, '0')}
        </span>

        <button
          className="mode-drum__step"
          type="button"
          aria-label="次の項目"
          onClick={() => onTurnChange((current) => current + 1)}
        >
          &gt;
        </button>
      </div>
    </section>
  )
}
