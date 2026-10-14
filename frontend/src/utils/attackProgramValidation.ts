import type { CircuitConfigBundle } from './circuitConfigTransfer'

export type AttackProgramIssue = { code: string; message: string }

/** Mirror the common Program compiler's final-measurement constraints before
 * submit. The server remains authoritative and repeats all validation. */
export function validateAttackProgramShape(bundle: CircuitConfigBundle): AttackProgramIssue[] {
  const circuit = bundle.circuit_config
  const columns = [...circuit.columns].sort((left, right) => left.step - right.step)
  const orderedGates = columns.flatMap((column) =>
    column.gates.map((gate) => ({ step: column.step, gate })))
  const measurements = orderedGates.filter(({ gate }) => gate.type === 'MEASURE')
  if (!measurements.length) {
    return [{ code: 'measurement_missing', message: '回路に測定がありません。最後に測定を置いてください。' }]
  }

  const issues: AttackProgramIssue[] = []
  let measurementStarted = false
  const measuredQubits = new Set<number>()
  const writtenBits = new Set<number>()
  const classicalBits = circuit.classical_bits ?? 0

  for (const { step, gate } of orderedGates) {
    if (gate.type !== 'MEASURE') {
      if (measurementStarted) {
        issues.push({
          code: 'gate_after_measurement',
          message: `step ${step} の ${gate.type} は測定の後にあります。測定は回路の最後にまとめてください。`,
        })
      }
      continue
    }

    measurementStarted = true
    const targets = gate.classical_targets
    if (!targets || targets.length === 0) {
      issues.push({
        code: 'measurement_classical_target_missing',
        message: `step ${step} の測定（qubit ${gate.targets.join(', ')}）に測定先の古典bitが指定されていません。`,
      })
      continue
    }
    if (targets.length !== gate.targets.length) {
      issues.push({
        code: 'measurement_classical_target_count',
        message: `step ${step} の測定はqubit ${gate.targets.length}個に対して、古典bitを同じ数だけ指定してください。`,
      })
    }
    for (const qubit of gate.targets) {
      if (measuredQubits.has(qubit)) {
        issues.push({ code: 'measurement_qubit_repeated',
          message: `qubit ${qubit} が複数回測定されています。測定対象は重複できません。` })
      }
      measuredQubits.add(qubit)
    }
    for (const bit of targets) {
      if (writtenBits.has(bit)) {
        issues.push({ code: 'measurement_classical_target_repeated',
          message: `古典bit ${bit} が複数回使われています。測定先は重複できません。` })
      }
      if (bit >= classicalBits) {
        issues.push({ code: 'measurement_classical_target_outside_register',
          message: `古典bit ${bit} は古典レジスタ幅 ${classicalBits} の範囲外です。` })
      }
      writtenBits.add(bit)
    }
  }
  return issues
}
