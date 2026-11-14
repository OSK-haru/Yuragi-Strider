/*
 * チュートリアルの「できたかどうか」を回路そのものから読む。
 *
 * 操作の履歴ではなく、いまの回路の形だけを見る。だから元に戻したり
 * 置き直したりしても判定はいつも正しいし、途中で寄り道してもよい。
 */

import type { CircuitEditorState } from '../types/circuit'
import type { SimulateRequestParameters } from '../types/simulation'

/*
 * 「ゆらぎ実験」コースの合格ライン。
 *
 * 既定は T1 = 100 μs / 総時間 = 2 μs。既定のままでは減衰が数%しか出ず、
 * 変化を見た気になれない。T1 を1桁下げ、時間を数倍にすると、
 * 忠実度の差が誰の目にもはっきり出る。台本の文面もこの定数を読む。
 */
export const TUTORIAL_SHORT_T1_US = 20
export const TUTORIAL_LONG_DURATION_US = 5

export function hasShortT1(parameters: SimulateRequestParameters): boolean {
  return parameters.t1_zero_temperature_us <= TUTORIAL_SHORT_T1_US
}

export function hasExtendedDuration(parameters: SimulateRequestParameters): boolean {
  return parameters.duration_us >= TUTORIAL_LONG_DURATION_US
}

export function countCircuitGates(circuit: CircuitEditorState): number {
  return circuit.columns.reduce((total, column) => total + column.gates.length, 0)
}

export type BellProgress = {
  /* どこかに H が置かれている。 */
  hasHadamard: boolean
  /*
   * H を置いた量子ビットを制御とする CNOT が、その右の列にある。
   * つまり Bell 回路の形になっている。
   */
  hasBellPair: boolean
}

export function inspectBellProgress(circuit: CircuitEditorState): BellProgress {
  /* H を置いた量子ビットと、その列。 */
  const hadamards: { columnIndex: number; qubitIndex: number }[] = []

  for (const [columnIndex, column] of circuit.columns.entries()) {
    for (const gate of column.gates) {
      if (gate.type === 'H') {
        for (const target of gate.targets) {
          hadamards.push({ columnIndex, qubitIndex: target })
        }
      }
    }
  }

  if (hadamards.length === 0) {
    return { hasHadamard: false, hasBellPair: false }
  }

  const hasBellPair = circuit.columns.some((column, columnIndex) =>
    column.gates.some((gate) => {
      if (gate.type !== 'CNOT') {
        return false
      }

      const controls = gate.controls ?? []
      return hadamards.some((hadamard) =>
        columnIndex > hadamard.columnIndex
        && controls.includes(hadamard.qubitIndex)
        && gate.targets.some((target) => target !== hadamard.qubitIndex),
      )
    }),
  )

  return { hasHadamard: true, hasBellPair }
}
