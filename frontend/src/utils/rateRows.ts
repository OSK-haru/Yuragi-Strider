import type { SimulationRates } from '../types/simulation'

export type RateRow = {
  label: string
  value: string
}

export function formatMicroseconds(value: unknown) {
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) {
    return '利用できません'
  }
  return `${number.toFixed(3)} us`
}

export function formatRate(value: unknown) {
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) {
    return '利用できません'
  }
  return `${number.toExponential(3)} 1/us`
}

/*
 * 緩和時間（T1・T2）は誰にでも意味のある物理量なので通常モードでも出す。
 * γ 系の生レートは内部表現に近いので、詳細モードでだけ添える。
 */
export function coherenceTimeRows(rates: SimulationRates): RateRow[] {
  return [
    { label: 'T1（0 K）', value: rates.t1_zero_temperature_us },
    { label: '実効 T1', value: rates.t1_effective_us },
    { label: 'Tφ', value: rates.tphi_us },
    { label: '実効 T2', value: rates.t2_effective_us },
  ]
    .filter((row) => row.value !== null)
    .map((row) => ({ label: row.label, value: formatMicroseconds(row.value) }))
}

export function decayRateRows(rates: SimulationRates): RateRow[] {
  return [
    { label: '下降レート', value: rates.gamma_down_per_us },
    { label: '上昇レート', value: rates.gamma_up_per_us },
    { label: '占有数緩和レート', value: rates.gamma_population_relaxation_per_us },
    { label: '純位相緩和レート', value: rates.gamma_phi_per_us },
  ]
    .filter((row) => row.value !== null)
    .map((row) => ({ label: row.label, value: formatRate(row.value) }))
}
