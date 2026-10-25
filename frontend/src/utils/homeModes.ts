/*
 * ホームのドラムに載せる3つの行き先。
 *
 * データを HomeModeDrum から出してあるのは、背景のゆらぎ（
 * QuantumFluctuationField）が同じ ModeId で相を切り替えるため。
 * 背景のゆらぎも同じ定義から現在の面を受け取る。
 */

export const HOME_MODE_COUNT = 3

export type ModeId = 'attack' | 'physics' | 'docs'

export type ModeEntry = {
  id: ModeId
  unit: string
  title: string
  tagline: string
  detail: string
  callToAction: string
  readouts: { label: string; value: string }[]
  signature: 'gate' | 'pulse' | 'attack' | 'docs'
}

export const MODE_ENTRIES: ModeEntry[] = [
  {
    id: 'attack',
    unit: 'MODE 01 / ATTACK SIMULATOR',
    title: '攻撃シミュレーター',
    tagline: '量子サービスの攻撃と防御を試す',
    detail:
      '仮想QPUを用意し、攻撃者として操作します。共有された量子計算環境で起こる干渉や情報漏れを調べます。',
    callToAction: '攻撃シミュレーターを開始',
    readouts: [
      { label: 'WORKSPACE', value: 'VIRTUAL QPU' },
      { label: 'ROLE', value: 'ATTACKER' },
      { label: 'STATUS', value: 'EXPERIMENTAL' },
    ],
    signature: 'attack',
  },
  {
    id: 'physics',
    unit: 'MODE 02 / PHYSICS ENGINE',
    title: '物理エンジンを試す',
    tagline: '量子状態が変化する仕組みを観察する',
    detail:
      '回路ゲート単位の Gate-aware と、制御波形を扱う Pulse をここにまとめました。',
    callToAction: '物理エンジンを選ぶ',
    readouts: [
      { label: 'ENGINE', value: 'GATE / PULSE' },
      { label: 'FOCUS', value: 'QUANTUM PHYSICS' },
      { label: 'STATUS', value: 'RESEARCH' },
    ],
    signature: 'gate',
  },
  {
    id: 'docs',
    unit: 'REF 03 / DOCUMENTATION',
    title: '公式ドキュメント',
    tagline: '何を計算しているのかを確かめる',
    detail:
      'ハミルトニアンの定義、散逸項の入れ方、数値解法の刻み幅まで。'
      + '画面の使い方だけでなく、その裏で解いている式そのものを公開しています。',
    callToAction: 'ドキュメントを開く',
    readouts: [
      { label: 'SECTIONS', value: 'PHYSICS / TUTORIAL' },
      { label: 'FORMAT', value: 'WEB' },
      { label: 'STATUS', value: 'LIVE' },
    ],
    signature: 'docs',
  },
]

/*
 * turn（単調増加の回転数）から今どの面が正面にいるかを出す。
 * turn は負にもなりうるので、剰余を1度たたんでから引く。
 */
export function modeAtTurn(turn: number): ModeId {
  return MODE_ENTRIES[((turn % HOME_MODE_COUNT) + HOME_MODE_COUNT) % HOME_MODE_COUNT].id
}

/*
 * PULSE 面の署名グラフ。ガウス包絡 × 搬送波、つまり実際に PULSE モードで
 * 撃っているパルスそのものの形。手描きのベジェで近似せず、式から出す。
 * viewBox は 96 × 32。
 */
export function gaussianPulsePath(): string {
  const points: string[] = []
  for (let step = 0; step <= 72; step += 1) {
    const t = (step / 72) * 2 - 1
    const envelope = Math.exp(-(t * t) / 0.17)
    const y = 16 - envelope * Math.cos(t * 9.2) * 12.5
    points.push(`${((step / 72) * 96).toFixed(2)},${y.toFixed(2)}`)
  }
  return `M${points.join(' L')}`
}
