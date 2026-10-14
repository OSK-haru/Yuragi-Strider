/*
 * 位相の色分け。
 *
 * 位相は -π と +π が同じ値になる循環量なので、直線のランプで塗ると
 * ほぼ同じ位相（3.1 と -3.1 など）が両端の色に分かれてしまう。そこで
 * 一周して元の色へ戻る循環ランプを使う。
 *
 *   0 = インク（文字色）, +π/2 = 位相ゲート色（橙）, ±π = アクセント（赤）,
 *   -π/2 = 回転ゲート色（青）
 *
 * 8 段に量子化し、±π は同じ帯に入るよう帯の中心を 0, π/4, ... に置く。
 * 色の割り当てと根拠は公式ドキュメント（ui-design）に載せ、画面には出さない。
 */

export const PHASE_BAND_COUNT = 8

/* 帯 k の色は、アンカー floor(k/2) と次のアンカーの中点（偶数帯ならアンカーそのもの）。 */
export const PHASE_ANCHOR_ORDER = ['ink', 'positive', 'accent', 'negative'] as const

export function phaseBand(phase: number): number {
  if (!Number.isFinite(phase)) {
    return 0
  }
  const k = Math.round((phase / (2 * Math.PI)) * PHASE_BAND_COUNT)
  return ((k % PHASE_BAND_COUNT) + PHASE_BAND_COUNT) % PHASE_BAND_COUNT
}

export type Rgb = [number, number, number]

export function phaseBandRgb(band: number, anchors: readonly [Rgb, Rgb, Rgb, Rgb]): Rgb {
  const segment = band / 2
  const from = Math.floor(segment) % anchors.length
  const to = (from + 1) % anchors.length
  const t = segment - Math.floor(segment)
  return [
    anchors[from][0] + (anchors[to][0] - anchors[from][0]) * t,
    anchors[from][1] + (anchors[to][1] - anchors[from][1]) * t,
    anchors[from][2] + (anchors[to][2] - anchors[from][2]) * t,
  ]
}
