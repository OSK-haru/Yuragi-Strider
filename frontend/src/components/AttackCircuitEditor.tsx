import { ContextCircuitEditor } from './ContextCircuitEditor'
import type { GateDurationDefaults } from '../types/simulation'

/** Reuses the existing circuit editor; this component never simulates a QPU. */
export function AttackCircuitEditor({ gateDurationDefaults, maximumLogicalQubits }: {
  gateDurationDefaults: GateDurationDefaults
  /** The connected environment's program-qubit limit; the editor default when absent. */
  maximumLogicalQubits?: number
}) {
  return <ContextCircuitEditor gateDurationDefaults={gateDurationDefaults} minimumLogicalQubits={1}
    maximumLogicalQubits={maximumLogicalQubits} />
}
