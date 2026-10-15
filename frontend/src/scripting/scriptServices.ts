/*
 * スクリプト実行の画面側。Worker の Python が呼ぶ操作を、既存のクライアントで実行する。
 *
 * 使えるのはターミナルと同じ公開操作だけ（回路送信・状態照会・結果照会・
 * 取消・課題の提出）。スクリプトは画面とは別の WebSocket 接続を 1 本持つので、
 * 照会の reply がターミナルへ流れ込まない。資格情報は
 * クライアントの中に留まり、Worker へは環境IDと公開の応答だけを返す。
 * このファイルは実行時の import を持たない（Node の検証から直接読み込む）。
 */

import type {
  LiveAttackConnection, LiveReply, LiveRequest, ProgramSubmission, SolutionFailure, SolutionResult, SolutionSubmission,
} from '../types/attackLive'
import type { ChannelOutcome } from './syncChannel'

export type ScriptServicePort = {
  environmentId: string
  connect(signal: AbortSignal): Promise<LiveAttackConnection>
  submitSolution(submission: SolutionSubmission, signal: AbortSignal):
    Promise<{ ok: true; result: SolutionResult } | { ok: false; reason: SolutionFailure }>
}

const INTERLEAVE = new Set(['victim', 'training:0', 'training:1'])
const DEFAULT_TIMEOUT_MS = 30_000
const MAX_TIMEOUT_MS = 600_000
/* 応答待ちのまま溜め込む reply の上限。スクリプトが投げっぱなしにしても膨らまない。 */
const MAX_BUFFERED_REPLIES = 512

class ServiceError extends Error {
  readonly code: string
  constructor(code: string, message: string) { super(message); this.code = code }
}

/**
 * 1回のスクリプト実行ぶんの操作。dispose で接続を閉じ、待っている呼出しを打ち切る。
 * readLine は input() の行を返す（入力の終わりは null）。prompt は input() に渡された文字列。
 */
export function createScriptServices(port: ScriptServicePort, readLine: (prompt: string) => Promise<string | null>) {
  const abort = new AbortController()
  let connection: LiveAttackConnection | null = null
  let opening: Promise<LiveAttackConnection> | null = null
  let detachReply: (() => void) | null = null
  const replies = new Map<string, LiveReply>()
  const waiters = new Map<string, (reply: LiveReply) => void>()

  function remember(reply: LiveReply) {
    const waiter = waiters.get(reply.requestId)
    if (waiter) { waiters.delete(reply.requestId); waiter(reply); return }
    replies.set(reply.requestId, reply)
    if (replies.size > MAX_BUFFERED_REPLIES) replies.delete(replies.keys().next().value as string)
  }

  async function open(): Promise<LiveAttackConnection> {
    if (connection && connection.state === 'open') return connection
    if (abort.signal.aborted) throw new ServiceError('CLOSED', 'スクリプトの接続は閉じられています。')
    opening ??= port.connect(abort.signal).then((opened) => {
      connection = opened
      detachReply = opened.onReply(remember)
      return opened
    }).finally(() => { opening = null })
    try { return await opening } catch {
      throw new ServiceError('CONNECT', 'サービスへ接続できませんでした（接続数の上限か、環境の期限切れ）。')
    }
  }

  function replyFor(requestId: string, timeoutMs: number): Promise<LiveReply> {
    const ready = replies.get(requestId)
    if (ready) { replies.delete(requestId); return Promise.resolve(ready) }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        waiters.delete(requestId)
        reject(new ServiceError('TIMEOUT', 'サービスの reply が時間内に届きませんでした。'))
      }, timeoutMs)
      const onAbort = () => { clearTimeout(timer); waiters.delete(requestId); reject(new ServiceError('CLOSED', 'スクリプトの接続は閉じられました。')) }
      abort.signal.addEventListener('abort', onAbort, { once: true })
      waiters.set(requestId, (reply) => {
        clearTimeout(timer)
        abort.signal.removeEventListener('abort', onAbort)
        resolve(reply)
      })
    })
  }

  /* このスクリプトが出した要求の ID。返事を待てるのはここにある要求だけ。 */
  const issued = new Set<string>()

  async function send(args: Record<string, unknown>) {
    const live = await open()
    const operation = args.operation
    const payload = isRecord(args.payload) ? args.payload : {}
    let accepted: LiveRequest
    try {
      accepted = await issue(live, operation, payload)
    } catch (error) {
      if (error instanceof ServiceError) throw error
      throw new ServiceError('REFUSED', error instanceof Error ? error.message : '要求を送れませんでした。')
    }
    issued.add(accepted.requestId)
    if (issued.size > MAX_BUFFERED_REPLIES) issued.delete(issued.values().next().value as string)
    return { live, requestId: accepted.requestId }
  }

  async function awaitReply(live: LiveAttackConnection, requestId: string, timeoutMs: number) {
    const reply = await replyFor(requestId, timeoutMs)
    issued.delete(requestId)
    return { value: reply.value ?? null, error_code: reply.errorCode, world_time_us: live.worldTimeUs }
  }

  async function request(args: Record<string, unknown>) {
    const { live, requestId } = await send(args)
    return awaitReply(live, requestId, timeoutOf(args.timeout_ms))
  }

  /* 送るだけ。受理された要求の ID を返し、返事は reply で待つ（複数の要求を並べて送れる）。 */
  async function issueOnly(args: Record<string, unknown>) {
    const { requestId } = await send(args)
    return { request_id: requestId }
  }

  async function reply(args: Record<string, unknown>) {
    const requestId = args.request_id
    if (typeof requestId !== 'string' || !issued.has(requestId)) {
      throw new ServiceError('UNKNOWN_REQUEST', 'このスクリプトが出した要求ではありません。')
    }
    return awaitReply(await open(), requestId, timeoutOf(args.timeout_ms))
  }

  async function answer(args: Record<string, unknown>) {
    let submission: SolutionSubmission
    if ('message' in args) {
      if (typeof args.message !== 'string' || Object.keys(args).length !== 1) {
        throw new ServiceError('INVALID', 'message は文字列にしてください。')
      }
      submission = { message: args.message }
    } else {
      const threshold = args.threshold
      const direction = args.direction
      if (typeof threshold !== 'number' || !Number.isFinite(threshold) || (direction !== 'ge' && direction !== 'le')) {
        throw new ServiceError('INVALID', 'threshold は数、direction は ge か le にしてください。')
      }
      submission = { threshold, direction }
    }
    const outcome = await port.submitSolution(submission, abort.signal)
    if (!outcome.ok) {
      throw new ServiceError(outcome.reason.toUpperCase(), outcome.reason === 'invalid'
        ? '提出内容が正しくありません。この課題の解答の形式と合っているか確かめてください。提出回数は減りません。'
        : outcome.reason === 'exhausted' ? '提出できる回数を使い切りました。'
          : '提出の結果を確認できませんでした。自動再送は行いません。')
    }
    const { result } = outcome
    return { attempt: result.attempt, attempts_remaining: result.attemptsRemaining,
      ...(result.total === undefined ? {} : { correct: result.correct, total: result.total, required: result.required }),
      passed: result.passed }
  }

  return {
    async call(method: string, args: unknown): Promise<ChannelOutcome> {
      const input = isRecord(args) ? args : {}
      try {
        if (method === 'open') {
          const live = await open()
          return { value: { environment_id: port.environmentId, world_time_us: live.worldTimeUs } }
        }
        if (method === 'request') return { value: await request(input) }
        if (method === 'issue') return { value: await issueOnly(input) }
        if (method === 'reply') return { value: await reply(input) }
        if (method === 'answer') return { value: await answer(input) }
        if (method === 'close') { closeConnection(); return { value: {} } }
        if (method === 'stdin') {
          const prompt = typeof input.prompt === 'string' ? input.prompt.slice(0, 200) : ''
          return { value: { line: await readLine(prompt) } }
        }
        return { error: { code: 'UNKNOWN_METHOD', message: `未知の操作です: ${method}` } }
      } catch (error) {
        if (error instanceof ServiceError) return { error: { code: error.code, message: error.message } }
        return { error: { code: 'SERVICE_ERROR', message: '操作を完了できませんでした。' } }
      }
    },
    dispose() {
      abort.abort()
      closeConnection()
    },
  }

  function closeConnection() {
    detachReply?.()
    detachReply = null
    connection?.close()
    connection = null
    replies.clear()
  }
}

/* Python 側は配信形式（snake_case）で送る。既存の接続の型へ写し、未知の操作は通さない。 */
function issue(live: LiveAttackConnection, operation: unknown, payload: Record<string, unknown>): Promise<LiveRequest> {
  if (operation === 'program.submit') {
    const shots = payload.shots
    if (typeof payload.device_id !== 'string' || typeof shots !== 'number' || !Number.isSafeInteger(shots)) {
      throw new ServiceError('INVALID', 'device_id と shots（整数）が必要です。')
    }
    const program: ProgramSubmission = { deviceId: payload.device_id, shots, circuitConfig: payload.circuit_config }
    if (payload.initialization !== undefined) {
      if (payload.initialization !== 'prepare' && payload.initialization !== 'inherit') {
        throw new ServiceError('INVALID', 'initialization は prepare か inherit です。')
      }
      program.initialization = payload.initialization
    }
    if (payload.interleave !== undefined) {
      if (typeof payload.interleave !== 'string' || !INTERLEAVE.has(payload.interleave)) {
        throw new ServiceError('INVALID', 'interleave の参照先が正しくありません。')
      }
      program.interleave = payload.interleave as ProgramSubmission['interleave']
    }
    return live.submit(program)
  }
  const jobId = payload.job_id
  if (typeof jobId !== 'string' || !/^[\x21-\x7e]{1,128}$/.test(jobId)) {
    throw new ServiceError('INVALID', 'ジョブIDの形式が正しくありません。')
  }
  if (operation === 'job.status.read') return live.status(jobId)
  if (operation === 'job.result.read') return live.result(jobId)
  if (operation === 'job.cancel') return live.cancel(jobId)
  throw new ServiceError('OPERATION_DENIED', 'スクリプトから使えない操作です。')
}

function timeoutOf(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? Math.min(value, MAX_TIMEOUT_MS) : DEFAULT_TIMEOUT_MS
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
