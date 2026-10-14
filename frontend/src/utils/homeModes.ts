/*
 * ホームのドラムに載せる4つの行き先。
 *
 * データを HomeModeDrum から出してあるのは、背景のゆらぎ（
 * QuantumFluctuationField）が同じ ModeId で相を切り替えるため。
 * 両方が1つの定義を見るようにしておく。
 */

export const HOME_MODE_COUNT = 4

export type ModeId = 'gate-aware' | 'pulse' | 'attack' | 'docs'

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
    id: 'gate-aware',
    unit: 'MODE 01 / GATE-AWARE',
    title: '通常モード',
    tagline: '量子ゲートで回路を組む',
    detail:
      '理想的なゲート操作でも、現実の量子ビットはノイズで少しずつ状態がずれていきます。'
      + '回路を組んでノイズの強さを変えるだけで、その“ズレ”がどれだけ・なぜ起きるのかを追えます。まずはこちら。',
    callToAction: '通常モードを開始',
    readouts: [
      { label: 'SOLVER', value: 'LINDBLAD / RK4' },
      { label: 'LAYER', value: 'GATE' },
      { label: 'STATUS', value: 'STABLE' },
    ],
    signature: 'gate',
  },
  {
    id: 'pulse',
    unit: 'MODE 02 / PULSE',
    title: 'PULSEモード',
    tagline: 'ゲートより下の層へ',
    detail:
      'ゲートは、本当はマイクロ波パルスの列です。包絡線・離調・振幅を直接いじって、'
      + '波形そのものから量子ビットの応答を追いかけます。実験的な層。',
    callToAction: 'PULSEモードを開始',
    readouts: [
      { label: 'SOLVER', value: 'TIME-DEPENDENT' },
      { label: 'LAYER', value: 'MICROWAVE' },
      { label: 'STATUS', value: 'EXPERIMENTAL' },
    ],
    signature: 'pulse',
  },
  {
    id: 'attack',
    unit: 'MODE 03 / ATTACK',
    title: '攻撃シミュレーター',
    tagline: '量子コンピューターを攻撃する側に立つ',
    detail:
      '攻撃の環境を用意して、コマンドターミナルから操作します。'
      + '攻撃がどの層に、どれだけ効くのかを、守る側の指標と並べて観察します。実験的な層。',
    callToAction: '攻撃シミュレーターを開始',
    readouts: [
      { label: 'CONSOLE', value: 'TERMINAL' },
      { label: 'LAYER', value: 'ATTACK' },
      { label: 'STATUS', value: 'EXPERIMENTAL' },
    ],
    signature: 'attack',
  },
  {
    id: 'docs',
    unit: 'REF 04 / DOCUMENTATION',
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
