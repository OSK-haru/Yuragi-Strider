import type { AttackSimulationClient, CommandIntent, PublicScenario, SettingValue } from '../types/attackSimulation'
import { MAX_CIRCUIT_IMPORT_BYTES, parseCircuitConfigJson } from './circuitConfigTransfer'

export const COMMON_COMMAND_HELP = [
  'help', 'device list', 'device show <device_id>',
  'program submit <file> --device <id> --shots <n> [--request-id <id>]',
  'job status <job_id>', 'job result <job_id>', 'job cancel <job_id>',
] as const
export const MAX_WORKSPACE_FILES = 16
export const MAX_TERMINAL_ENTRIES = 100
export const MAX_COMMAND_LENGTH = 4096

// Closed fallback: no fixtures, network calls or simulated successful execution.
async function unavailable(): Promise<never> {
  throw new Error('HTTP clientは未接続です。環境成立・ジョブ操作はまだ実行できません。')
}
export const disconnectedAttackClient: AttackSimulationClient = {
  connected: false,
  listScenarios: unavailable,
  establish: unavailable,
  discard: unavailable,
  putCircuit: unavailable,
  executeCommand: unavailable,
}

export function validateWorkspaceCircuit(name: string, json: string, names: string[]): void {
  // A symbolic actor-private name, never an OS path (same boundary as Python).
  if (!name || new TextEncoder().encode(name).length > 256
    || ['\\', ':', '\0', '\r', '\n'].some((character) => name.includes(character))
    || name.split('/').some((part) => ['', '.', '..'].includes(part))) {
    throw new Error('仮想名は256 UTF-8 bytes以内。OSパスや空・.・..の区間は使えません。')
  }
  if (new TextEncoder().encode(json).length > MAX_CIRCUIT_IMPORT_BYTES) {
    throw new Error('回路JSONは64 KiB以内にしてください。')
  }
  if (!names.includes(name) && names.length >= MAX_WORKSPACE_FILES) {
    throw new Error('workspaceの回路ファイル上限は16件です。')
  }
  parseCircuitConfigJson(json, 1)
}

export function validateCommandLine(line: string): void {
  if (!line.trim() || line.length > MAX_COMMAND_LENGTH || ['\r', '\n', '\0'].some((character) => line.includes(character))) {
    throw new Error('コマンドは空でない1行、4096文字以内にしてください。')
  }
  // Syntax and authorization belong to the common parser/application service.
}

function quoted(value: string): string {
  return `'${value.replaceAll("'", "'\"'\"'")}'`
}
export function programSubmitLine(name: string, device: string, shots: number): string {
  // Build editable grammar only. CommandRequestStore assigns identity when the
  // user sends, so preparing/reusing this line is not permanently deduplicated.
  if (!device || !Number.isSafeInteger(shots) || shots < 1) {
    throw new Error('実行先と正の整数shotsを指定してください。上限はサービスが検査します。')
  }
  const line = `program submit ${quoted(name)} --device ${quoted(device)} --shots ${shots}`
  validateCommandLine(line)
  return line
}

/** Keeps one unresolved operation, only in memory and only in its session.
 * Do not parse CLI syntax here: every terminal line gets a fallback ID, and the
 * common backend parser applies it only when needed by program submit.
 */
export class CommandRequestStore {
  private sessionId: string | null = null
  private pending: { intent: CommandIntent; workspace: string; uncertain: boolean } | null = null
  private workspaceRevision = 0

  clear(): void {
    this.sessionId = null
    this.pending = null
    this.workspaceRevision = 0
  }

  /** Invalidate before registration, even when its response may be lost. */
  workspaceChanged(): void { this.workspaceRevision += 1 }

  private snapshot(files: ReadonlyArray<{ name: string; json: string }>): string {
    // Conservative: any registered-file change blocks retry, avoiding a second
    // shlex/grammar implementation. Editor-only changes do not change these files.
    if (files.length > MAX_WORKSPACE_FILES || files.some((file) =>
      new TextEncoder().encode(file.json).length > MAX_CIRCUIT_IMPORT_BYTES
      || new TextEncoder().encode(file.name).length > 256)) {
      throw new Error('再送用の回路記録がworkspaceの保持上限を超えています。')
    }
    return JSON.stringify([this.workspaceRevision,
      files.map(({ name, json }) => [name, json]).sort(([a], [b]) => a.localeCompare(b))])
  }

  begin(sessionId: string, line: string, files: ReadonlyArray<{ name: string; json: string }>): CommandIntent {
    validateCommandLine(line)
    if (this.sessionId !== sessionId) this.clear()
    const workspace = this.snapshot(files)
    const intent = Object.freeze({ line, requestId: crypto.randomUUID() })
    this.sessionId = sessionId
    this.pending = { intent, workspace, uncertain: false }
    return intent
  }

  unresolved(sessionId: string): CommandIntent | null {
    if (this.sessionId !== sessionId) this.clear()
    return this.pending?.uncertain ? this.pending.intent : null
  }

  retry(sessionId: string, line: string, files: ReadonlyArray<{ name: string; json: string }>): CommandIntent {
    const intent = this.unresolved(sessionId)
    if (!intent) throw new Error('この環境に再送できる未確定要求はありません。')
    if (line !== intent.line || this.snapshot(files) !== this.pending?.workspace) {
      throw new Error('コマンドまたは登録回路が変わっています。同一IDでは再送できません。新規要求として送信してください。')
    }
    return intent
  }

  confirmed(intent: CommandIntent): void {
    if (this.pending?.intent === intent) this.pending = null
  }

  failed(intent: CommandIntent, uncertain: boolean): void {
    if (this.pending?.intent !== intent) return
    // A rejected retry cannot disprove an earlier acceptance whose reply was lost.
    if (uncertain || this.pending.uncertain) this.pending.uncertain = true
    else this.pending = null
  }
}

export async function sendCommandIntent(
  client: AttackSimulationClient, sessionId: string, intent: CommandIntent,
  requests: CommandRequestStore, timeoutMs = 10000,
): Promise<string[]> {
  try {
    const reply = await boundedClientCall((signal) => client.executeCommand(sessionId, intent, signal), timeoutMs, true)
    if (reply?.outcome !== 'confirmed' || !Array.isArray(reply.lines)
      || !reply.lines.every((line) => typeof line === 'string')) {
      throw new AttackClientFailure('応答から受付を確定できません。同一要求の再送、または新規要求の送信を選んでください。', true)
    }
    requests.confirmed(intent)
    return reply.lines
  } catch (error) {
    requests.failed(intent, !(error instanceof AttackClientFailure) || error.uncertain)
    throw error
  }
}

export function validateSetupOverrides(scenario: PublicScenario, overrides: Record<string, SettingValue>): void {
  for (const [id, value] of Object.entries(overrides)) {
    const setting = scenario.settings.find((candidate) => candidate.id === id)
    if (scenario.mode !== 'free' || !setting?.editable) {
      throw new Error('この設定は変更できません。')
    }
    if (setting.kind === 'choice'
      && (typeof value !== 'string' || !setting.choices.some((choice) => choice.value === value))) {
      throw new Error(`${setting.label}: 未対応の選択です。`)
    }
    if (setting.kind === 'boolean' && typeof value !== 'boolean') {
      throw new Error(`${setting.label}: 真偽値を指定してください。`)
    }
    if (setting.kind === 'number') {
      if (typeof value !== 'number' || !Number.isFinite(value)
        || value < setting.min || value > setting.max || !Number.isFinite(setting.step) || setting.step <= 0) {
        throw new Error(`${setting.label}: 許容範囲外です。`)
      }
      const steps = (value - setting.min) / setting.step
      if (Math.abs(steps - Math.round(steps)) > 1e-9) throw new Error(`${setting.label}: 設定の刻み幅に合わせてください。`)
    }
  }
}

export class AttackClientFailure extends Error {
  readonly uncertain: boolean
  constructor(publicMessage: string, uncertain = false) {
    super(publicMessage)
    this.uncertain = uncertain
  }
}

/** Wall-clock UI timeout, not virtual QPU time and not job cancellation.
 * Adapter failures must be sanitized into AttackClientFailure, not raw errors.
 */
export async function boundedClientCall<T>(
  call: (signal: AbortSignal) => Promise<T>, timeoutMs = 10000,
  failureMayHaveCommitted = false,
): Promise<T> {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      call(controller.signal),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          controller.abort()
          reject(new AttackClientFailure('応答待ちが期限を超えました。処理の成立・取消は確定していません。', true))
        }, timeoutMs)
      }),
    ])
  } catch (error) {
    if (error instanceof AttackClientFailure) throw error
    // Without a classified server rejection, a failed lifecycle response cannot
    // prove that the mutation did not happen. Preserve the setup lock.
    throw new AttackClientFailure('サービス操作に失敗しました。詳細な内部エラーは表示しません。', failureMayHaveCommitted)
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}
