import type { VictimActivity, VictimActivityStep } from '../types/attackLive'
import { describeVictimActivity, referenceLabel, stepLabel } from '../utils/victimActivity'

/*
 * 自由モードの抽象コマンド「被害者の動き」。サービスが記録した順番と世界時刻を、
 * 3つの帯（被害者・サービス・攻撃者）に並べる。幅は所要時間に比例（見えるよう最小幅あり）で、
 * 正確な時刻は各ブロックの title と、下の文章にある。色だけでなく、帯と文字でも区別する。
 */

const LANES = ['victim', 'service', 'attacker'] as const
const LANE_LABELS: Record<(typeof LANES)[number], string> = { victim: '被害者', service: 'サービス', attacker: '攻撃者' }
const LANE_HEIGHT = 26
const TOP = 22
const LEFT = 62
const MIN_BLOCK = 30
const MAX_BLOCK = 84
const PX_PER_US = 12

function laneOf(step: VictimActivityStep): (typeof LANES)[number] {
  return step.actor === 'victim' || step.actor === 'attacker' ? step.actor : 'service'
}

function shortLabel(step: VictimActivityStep): string {
  if (step.kind === 'victim_run') return '実行'
  if (step.kind === 'attacker_measure') return '測定'
  if (step.kind === 'service_reset') return 'Reset'
  return step.kind === 'idle' ? '待機' : '…'
}

type Block = { step: VictimActivityStep; shot: number; x: number; width: number }

function layout(activity: VictimActivity): { blocks: Block[]; marks: Array<{ shot: number; x: number }>; width: number } {
  const blocks: Block[] = []
  const marks: Array<{ shot: number; x: number }> = []
  let x = LEFT
  for (const shot of activity.shots) {
    marks.push({ shot: shot.index, x })
    for (const step of shot.steps) {
      /* 待機は秒に達することがある。図が伸び切らないよう、幅には上限を付ける（正確な時間は title）。 */
      const width = Math.min(MAX_BLOCK, Math.max(MIN_BLOCK, (step.end_us - step.start_us) * PX_PER_US))
      blocks.push({ step, shot: shot.index, x, width })
      x += width + 2
    }
    x += 6
  }
  return { blocks, marks, width: x + 8 }
}

export function VictimActivityPanel({ activity, jobId, busy, onRead }: {
  activity: VictimActivity | null
  jobId: string
  busy: boolean
  onRead: () => void
}) {
  const shown = activity && activity.reference !== null ? layout(activity) : null
  return <section className="ide__group attack-live__activity" aria-label="被害者の動き">
    <div className="attack-live__activity-explain">
    <h3>被害者の動き</h3>
    <small>自由モードで「抽象コマンド: 被害者の動きの表示」をオンにした環境だけの機能です（現実のサービスには無い操作）。
      交互観測のジョブは、shotごとに次の順番で動きます。</small>
    <ol className="attack-live__activity-steps">
      <li><strong>被害者</strong>が、共有qubitに自分のbitを準備して測定する</li>
      <li><strong>サービス</strong>が、次の利用者のためにResetする（不完全なので、少し残る）</li>
      <li><strong>攻撃者</strong>が測定する（残りが観測に出る）</li>
      <li><strong>サービス</strong>が、次のshotの被害者のためにResetする</li>
    </ol>
    <button type="button" disabled={busy || !jobId} onClick={onRead}>
      {jobId ? 'このジョブの動きを読む' : 'ジョブを送ると読めます'}</button>
    </div>
    <div className="attack-live__activity-view">
    {!activity && <p className="attack-live__muted">ジョブを送ってから「このジョブの動きを読む」を押す（または <code className="ide__inline-code">activity</code>）と、ここに shot ごとの順番が出ます。</p>}
    {activity && <>
      <p className="attack-live__activity-summary">
        <code className="ide__inline-code">{activity.job_id}</code> · {referenceLabel(activity.reference)}
        {activity.victim_prepared_bit !== null && <> · 被害者が準備したbit <strong>{activity.victim_prepared_bit}</strong></>}
        {activity.reference !== null && <> · {activity.shots_total}shot中、先頭{activity.shots_shown}shot</>}
      </p>
      {shown
        ? <div className="attack-live__activity-scroll">
          <svg role="img" width={shown.width} height={TOP + LANES.length * LANE_HEIGHT + 4}
            aria-label={`被害者の動き。${describeVictimActivity(activity, 1).join(' ')}`}>
            {LANES.map((lane, index) => <text key={lane} x={4} y={TOP + index * LANE_HEIGHT + 17} className="attack-live__activity-lane">
              {LANE_LABELS[lane]}</text>)}
            {shown.marks.map((mark) => <text key={mark.shot} x={mark.x} y={14} className="attack-live__activity-shot">shot {mark.shot}</text>)}
            {shown.blocks.map((block, index) => {
              const lane = laneOf(block.step)
              return <g key={index}>
                <title>{`shot ${block.shot} · ${stepLabel(block.step)} · ${block.step.start_us.toFixed(2)}–${block.step.end_us.toFixed(2)}µs`}</title>
                <rect x={block.x} y={TOP + LANES.indexOf(lane) * LANE_HEIGHT} width={block.width} height={LANE_HEIGHT - 4}
                  className={`attack-live__activity-block attack-live__activity-block--${lane}${block.step.after ? ` is-after-${block.step.after}` : ''}`} />
                <text x={block.x + block.width / 2} y={TOP + LANES.indexOf(lane) * LANE_HEIGHT + 15} textAnchor="middle"
                  className="attack-live__activity-text">{shortLabel(block.step)}</text>
              </g>
            })}
          </svg>
          <small className="attack-live__activity-legend">幅は所要時間に比例します（見えるよう最小幅と最大幅あり）。太い枠のResetは、被害者の直後のReset（漏れの経路）です。</small>
        </div>
        : <p className="attack-live__muted">このジョブには被害者の実行が挟まっていません。<code className="ide__inline-code">submit --interleave ref:0</code> のような交互観測で送ると、動きが出ます。</p>}
      {activity.reference !== null && <details>
        <summary>文章で読む（正確な時刻）</summary>
        <pre className="attack-live__activity-text-lines">{describeVictimActivity(activity, activity.shots.length).join('\n')}</pre>
      </details>}
    </>}
    </div>
  </section>
}
