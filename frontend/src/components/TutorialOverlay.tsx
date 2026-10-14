import { useEffect, useState } from 'react'
import './TutorialOverlay.css'
import { QuantumPet } from './QuantumPet'
import { useTutorial } from '../context/useTutorial'
import type { TutorialRunSample } from '../context/TutorialContextCore'

/*
 * チュートリアル中の暗転幕と強調表示。
 *
 * 画面全体を1枚のSVGで覆い、強調したい要素の矩形だけをマスクで抜く。
 * 抜いた穴は本当に透明なので、下にある本物のボタンがそのまま見えるし
 * 押せる。幕自体は pointer-events を持たないため、案内中でも操作は
 * 一切妨げない。
 */

/* 強調枠と要素のあいだに置く余白。 */
const SPOTLIGHT_PADDING_PX = 10
/* 強調対象は動く（スクロール・ドラッグ・パネル開閉）ので測り直し続ける。 */
const MEASURE_INTERVAL_MS = 200
/* 画面移動の直後は描画待ちがあるので、少し置いてからスクロールする。 */
const REVEAL_DELAY_MS = 220

type AnchorRect = {
  key: string
  x: number
  y: number
  width: number
  height: number
}

function formatFidelity(fidelity: number | null): string {
  return fidelity === null ? '—' : `${(fidelity * 100).toFixed(2)}%`
}

/*
 * 1回目と最新の忠実度の差。パーセントポイントで言い切る。
 * 上がる条件もありうるので、下がったとは決めつけない。
 */
function fidelityDropLabel(samples: TutorialRunSample[]): string | null {
  if (samples.length < 2) {
    return null
  }

  const baseline = samples[0].fidelity
  const latest = samples[samples.length - 1].fidelity
  if (baseline === null || latest === null) {
    return null
  }

  const deltaPoints = (baseline - latest) * 100
  if (Math.abs(deltaPoints) < 0.005) {
    return '変わっていないよ。'
  }

  return deltaPoints > 0
    ? `${deltaPoints.toFixed(2)} ポイント下がったよ。`
    : `${Math.abs(deltaPoints).toFixed(2)} ポイント上がったよ。`
}

/*
 * 同じ目印が、非表示のまま常駐している画面（攻撃シミュレーター）にもある。
 * hidden の中のものは飛ばして、見えている方を使う。
 */
function findAnchor(name: string): HTMLElement | null {
  for (const element of document.querySelectorAll(`[data-tutorial-anchor="${name}"]`)) {
    if (element instanceof HTMLElement && element.closest('[hidden]') === null) return element
  }
  return null
}

/*
 * IDE のペインは中でスクロールするので、目印の要素がペインより大きいと枠が
 * ペインの外（隣のペイン）まではみ出す。スクロールする祖先の見えている範囲で
 * 切り取る。全く見えていなければ null。
 */
function visibleBounds(element: HTMLElement): { left: number; top: number; width: number; height: number } | null {
  const rect = element.getBoundingClientRect()
  let left = rect.left
  let top = rect.top
  let right = rect.right
  let bottom = rect.bottom
  for (let parent = element.parentElement; parent !== null; parent = parent.parentElement) {
    const style = getComputedStyle(parent)
    if (!/(auto|scroll|hidden)/.test(`${style.overflowX}${style.overflowY}`)) continue
    const clip = parent.getBoundingClientRect()
    left = Math.max(left, clip.left)
    top = Math.max(top, clip.top)
    right = Math.min(right, clip.right)
    bottom = Math.min(bottom, clip.bottom)
  }
  return right - left > 0 && bottom - top > 0 ? { left, top, width: right - left, height: bottom - top } : null
}

function sameRects(left: AnchorRect[], right: AnchorRect[]) {
  if (left.length !== right.length) {
    return false
  }
  return left.every((rect, index) => {
    const other = right[index]
    return rect.key === other.key
      && Math.abs(rect.x - other.x) < 0.5
      && Math.abs(rect.y - other.y) < 0.5
      && Math.abs(rect.width - other.width) < 0.5
      && Math.abs(rect.height - other.height) < 0.5
  })
}

type Measurement = {
  /* どのビートの計測結果か。台詞が変わった瞬間に前の枠を残さないための鍵。 */
  key: string
  rects: AnchorRect[]
}

function useAnchorRects(anchorKey: string): AnchorRect[] {
  const [measurement, setMeasurement] = useState<Measurement>({ key: '', rects: [] })

  useEffect(() => {
    const names = anchorKey === '' ? [] : anchorKey.split('|')
    if (names.length === 0) {
      return
    }

    function measure() {
      const next: AnchorRect[] = []
      for (const name of names) {
        const element = findAnchor(name)
        if (element === null) {
          continue
        }

        const bounds = visibleBounds(element)
        if (bounds === null) {
          continue
        }

        next.push({
          key: name,
          x: bounds.left - SPOTLIGHT_PADDING_PX,
          y: bounds.top - SPOTLIGHT_PADDING_PX,
          width: bounds.width + SPOTLIGHT_PADDING_PX * 2,
          height: bounds.height + SPOTLIGHT_PADDING_PX * 2,
        })
      }

      setMeasurement((current) =>
        current.key === anchorKey && sameRects(current.rects, next)
          ? current
          : { key: anchorKey, rects: next },
      )
    }

    /* 初回は描画が終わってから測る。 */
    const frameId = window.requestAnimationFrame(measure)
    const intervalId = window.setInterval(measure, MEASURE_INTERVAL_MS)
    window.addEventListener('resize', measure)
    /* パネル内部のスクロールも拾いたいのでキャプチャ段階で受ける。 */
    window.addEventListener('scroll', measure, true)

    return () => {
      window.cancelAnimationFrame(frameId)
      window.clearInterval(intervalId)
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure, true)
    }
  }, [anchorKey])

  return measurement.key === anchorKey ? measurement.rects : []
}

export function TutorialOverlay() {
  const tutorial = useTutorial()
  const beat = tutorial.beat
  const anchorKey = beat?.anchors?.join('|') ?? ''
  const rects = useAnchorRects(anchorKey)
  const beatId = beat?.id ?? ''
  /* 手を動かしてもらう場面かどうか。待機中は勝手に先へ進めない。 */
  const isWaiting = beat !== null && beat.waitFor !== undefined && !tutorial.isBeatSatisfied

  /* 強調対象が画面の外にあるなら、まず見える位置まで運ぶ。 */
  useEffect(() => {
    if (anchorKey === '') {
      return
    }

    const timeoutId = window.setTimeout(() => {
      const element = findAnchor(anchorKey.split('|')[0])
      element?.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' })
    }, REVEAL_DELAY_MS)

    return () => window.clearTimeout(timeoutId)
  }, [anchorKey, beatId])

  /* キーボードでも会話を進められるようにする。 */
  useEffect(() => {
    if (beat === null) {
      return
    }

    function handleKeyDown(event: KeyboardEvent) {
      const target = event.target
      if (target instanceof HTMLElement) {
        const tagName = target.tagName.toLowerCase()
        /* ボタンを外すのは、Enterで二重に進めないため。 */
        if (
          tagName === 'input'
          || tagName === 'textarea'
          || tagName === 'select'
          || tagName === 'button'
          || target.isContentEditable
        ) {
          return
        }
      }

      if (event.key === 'ArrowRight' || event.key === 'Enter') {
        if (isWaiting) {
          /* 待機中の飛ばしは、意思表示（「とばす」）でだけ受け付ける。 */
          return
        }

        event.preventDefault()
        tutorial.next()
        return
      }

      if (event.key === 'ArrowLeft' && tutorial.canGoBack) {
        event.preventDefault()
        tutorial.back()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [beat, isWaiting, tutorial])

  if (beat === null) {
    return null
  }

  const labelRect = rects[0] ?? null
  const progressPercent = Math.round(((tutorial.beatIndex + 1) / tutorial.beatCount) * 100)
  /*
   * 強調対象が指定されているのに、いまの画面には見つからないことがある
   * （例: 回路スタジオへ移動してしまった）。その場合は「強調ゼロ＝全暗転」
   * ではなく、幕そのものを外して通常表示に戻す。
   */
  const anchorsMissing = anchorKey !== '' && rects.length === 0

  return (
    <>
      <div className="tutorial-overlay">
        <svg className="tutorial-overlay__scrim" aria-hidden="true">
          {anchorsMissing ? null : (
            <>
              <defs>
                <mask id="tutorial-spotlight-mask">
                  <rect x="0" y="0" width="100%" height="100%" fill="#ffffff" />
                  {rects.map((rect) => (
                    <rect
                      key={rect.key}
                      x={rect.x}
                      y={rect.y}
                      width={rect.width}
                      height={rect.height}
                      fill="#000000"
                    />
                  ))}
                </mask>
              </defs>

              <rect
                className="tutorial-overlay__veil"
                x="0"
                y="0"
                width="100%"
                height="100%"
                mask="url(#tutorial-spotlight-mask)"
              />
            </>
          )}

          {rects.map((rect) => (
            <rect
              key={rect.key}
              className="tutorial-overlay__frame"
              x={rect.x}
              y={rect.y}
              width={rect.width}
              height={rect.height}
            />
          ))}
        </svg>

        {/* 強調枠が何を指しているのかを、枠の肩に書き添える。 */}
        {labelRect !== null && beat.anchorLabel ? (
          <span
            className="tutorial-overlay__tag"
            style={{
              left: `${Math.max(8, labelRect.x)}px`,
              top: `${Math.max(8, labelRect.y - 26)}px`,
            }}
          >
            {beat.anchorLabel}
          </span>
        ) : null}

        {/*
          実行の記録。ペットは右下にいるので、比較表は左下に置いて重ねない。
          「変えたのはどれで、結果はどう動いたか」を1枚で読み切れるようにする。
        */}
        {beat.showRunSamples && tutorial.runSamples.length > 0 ? (
          <aside className="tutorial-overlay__samples" aria-label="実行の記録">
            <span className="tutorial-overlay__samples-title">実行の記録</span>

            <table className="tutorial-overlay__samples-table">
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">T1</th>
                  <th scope="col">時間</th>
                  <th scope="col">忠実度</th>
                </tr>
              </thead>
              <tbody>
                {tutorial.runSamples.map((sample, index) => (
                  <tr
                    key={sample.id}
                    className={
                      index === tutorial.runSamples.length - 1
                        ? 'tutorial-overlay__samples-row--latest'
                        : undefined
                    }
                  >
                    <th scope="row">{sample.id}</th>
                    <td>{sample.t1Us.toFixed(0)}μs</td>
                    <td>{sample.durationUs.toFixed(1)}μs</td>
                    <td>{formatFidelity(sample.fidelity)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {fidelityDropLabel(tutorial.runSamples) !== null ? (
              <p className="tutorial-overlay__samples-delta">
                1回目より {fidelityDropLabel(tutorial.runSamples)}
              </p>
            ) : null}
          </aside>
        ) : null}

        <div className="tutorial-overlay__hud">
          <div className="tutorial-overlay__hud-text">
            <span className="tutorial-overlay__hud-eyebrow">
              {tutorial.course?.title} {beat.chapter} / {tutorial.chapterCount}
            </span>
            <strong className="tutorial-overlay__hud-title">{beat.chapterTitle}</strong>
          </div>

          <div className="tutorial-overlay__progress" aria-hidden="true">
            <span style={{ width: `${progressPercent}%` }} />
          </div>

          <button
            className="tutorial-overlay__quit"
            type="button"
            onClick={tutorial.exit}
          >
            終了
          </button>
        </div>
      </div>

      <QuantumPet
        role="tutorial"
        phase="idle"
        mood={beat.mood}
        eyebrow={`ナビペット / ${beat.chapterTitle}`}
        message={beat.text}
        actions={
          <>
            {tutorial.canGoBack ? (
              <button
                className="quantum-pet__action quantum-pet__action--quiet"
                type="button"
                onClick={tutorial.back}
              >
                もどる
              </button>
            ) : null}

            {isWaiting ? (
              <>
                <span className="quantum-pet__waiting">
                  {beat.waitingHint ?? 'できたら次へ進むよ。'}
                </span>
                <button
                  className="quantum-pet__action quantum-pet__action--quiet"
                  type="button"
                  onClick={tutorial.next}
                >
                  とばす
                </button>
              </>
            ) : (
              <button
                className="quantum-pet__action"
                type="button"
                onClick={tutorial.next}
              >
                {tutorial.isLastBeat ? 'おわり' : 'つぎへ'}
              </button>
            )}
          </>
        }
      />
    </>
  )
}
