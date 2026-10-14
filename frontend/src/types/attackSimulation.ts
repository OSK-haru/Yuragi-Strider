/** UI intent/projection types, not an approved HTTP wire schema.
 * Adapters must return actor-visible data only, never a resolved manifest.
 */
export type AttackMode = 'free' | 'graded'
export type AssumptionGroup = 'attacker' | 'victim' | 'qpu_service' | 'relation' | 'experiment'
export type SettingValue = string | number | boolean
export type PublicSetting = {
  id: string
  label: string
  group: AssumptionGroup
  value: SettingValue
  editable: boolean
} & (
  | { kind: 'choice'; choices: Array<{ value: string; label: string }> }
  | { kind: 'number'; min: number; max: number; step: number }
  | { kind: 'boolean' }
)
export type PublicScenario = {
  id: string
  title: string
  description: string
  mode: AttackMode
  /** Only safe, public setup fields. Graded victim secrets must not arrive here. */
  settings: PublicSetting[]
}
export type AttackSetupIntent = {
  scenarioId: string
  mode: AttackMode
  overrides: Record<string, SettingValue>
}
export type PublicAttackSession = {
  id: string
  title: string
  mode: AttackMode
  /** Human-readable public policy, not private actor grants or victim parameters. */
  publicNotice: string
  /** Establishment alone is not authentication. No credential value belongs here. */
  actorConnection: 'ready' | 'unconfigured'
}
/** One user operation. requestId is a fallback for program submit without an
 * explicit --request-id; the common parser must give that explicit ID priority.
 * This is transport intent, not an approved terminal header/body schema.
 */
export type CommandIntent = Readonly<{ line: string; requestId: string }>
export type CommandReply =
  | { outcome: 'confirmed'; lines: string[] }
  | { outcome: 'unknown' }

/** UI port, not a claim that one credential authorizes all these operations.
 * listScenarios/establish/discard use the environment-owner boundary;
 * putCircuit/executeCommand use the scoped actor connection. Never forward
 * owner authority into the attacker terminal. Editor and terminal share the
 * same actor Application API. Transport/auth/lifecycle require review.
 */
export interface AttackSimulationClient {
  readonly connected: boolean
  listScenarios(signal: AbortSignal): Promise<PublicScenario[]>
  establish(intent: AttackSetupIntent, signal: AbortSignal): Promise<PublicAttackSession>
  discard(sessionId: string, signal: AbortSignal): Promise<void>
  putCircuit(sessionId: string, name: string, json: string, signal: AbortSignal): Promise<void>
  /** Must preserve intent.requestId end-to-end. Never silently discard it. */
  executeCommand(sessionId: string, intent: CommandIntent, signal: AbortSignal): Promise<CommandReply>
}
