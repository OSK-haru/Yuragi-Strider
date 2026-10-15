import type {
  AttackEnvironmentClient,
  ConnectionState,
  CreateEnvironmentFailure,
  CreateEnvironmentResult,
  LiveAttackConnection,
  LiveReply,
  LiveRequest,
  LiveRequestKind,
  OwnedEnvironment,
  ProgramSubmission,
  SettingsCatalog,
  WaitResult,
  WaitTarget,
} from '../types/attackLive'

const API_ROOT = '/api/attack'
const CSRF_HEADER = 'X-Attack-Simulator-Request'
const CSRF_VALUE = '1'
const SOCKET_PROTOCOL = 'quanyu.attack.v1'
const MAX_MESSAGE_BYTES = 64 * 1024

type SocketMessage = Record<string, unknown>
type SocketLike = Pick<WebSocket,
  'readyState' | 'send' | 'close' | 'addEventListener' | 'removeEventListener'>
type SocketFactory = (url: string, protocols: string[]) => SocketLike

export type AttackLiveClientOptions = {
  fetcher?: typeof fetch
  socketFactory?: SocketFactory
  locationHref?: string
}

/**
 * Browser transport for the D60-D64 API. The server-issued attacker credential
 * is deliberately retained in this closure and never returned, stored, or logged.
 */
export function createAttackEnvironmentClient(
  options: AttackLiveClientOptions = {},
): AttackEnvironmentClient {
  const fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis)
  const socketFactory = options.socketFactory ?? ((url, protocols) => new WebSocket(url, protocols))
  const credentials = new Map<string, string>()
  const connections = new Map<string, Set<LiveAttackConnection>>()
  let commandSequence = 0

  const client: AttackEnvironmentClient = {
    async getCatalog(signal) {
      const response = await fetcher(`${API_ROOT}/settings/catalog`, {
        method: 'GET', credentials: 'same-origin', signal,
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) throw new Error('攻撃シミュレーターの設定目録を取得できません。')
      const catalog: unknown = await response.json()
      if (!isRecord(catalog) || catalog.schema_version !== 'attack-settings-catalog-v1') {
        throw new Error('設定目録の形式が不正です。')
      }
      return catalog as SettingsCatalog
    },

    async listOwnedEnvironments(signal): Promise<OwnedEnvironment[]> {
      const response = await fetcher(`${API_ROOT}/environments/owned`, {
        method: 'POST', credentials: 'same-origin', signal,
        headers: { Accept: 'application/json', [CSRF_HEADER]: CSRF_VALUE },
      })
      if (!response.ok) throw new Error('所有環境を確認できませんでした。')
      const body: unknown = await response.json()
      if (!isRecord(body) || !Array.isArray(body.environments)) {
        throw new Error('所有環境の応答が不正です。')
      }
      if (body.environments.length > 64) throw new Error('所有環境の件数が上限を超えています。')
      return body.environments.map((item: unknown) => {
        if (!isRecord(item) || !isEnvironmentId(item.environment_id)
            || (item.mode !== 'free' && item.mode !== 'challenge')
            || typeof item.created_at !== 'string' || item.created_at.length > 64) {
          throw new Error('所有環境の応答が不正です。')
        }
        return { environmentId: item.environment_id, mode: item.mode,
          createdAt: item.created_at }
      })
    },

    async resumeEnvironment(environment, signal) {
      if (!isEnvironmentId(environment.environmentId)) return false
      const response = await fetcher(`${API_ROOT}/environments/${encodeURIComponent(environment.environmentId)}/attacker-credential`, {
        method: 'POST', credentials: 'same-origin', signal,
        headers: { Accept: 'application/json', [CSRF_HEADER]: CSRF_VALUE },
      })
      if (response.status === 404) return false
      if (!response.ok) throw new Error('環境の認証を回復できませんでした。')
      const body: unknown = await response.json()
      if (!isRecord(body) || typeof body.attacker_credential !== 'string'
          || body.attacker_credential.length === 0) {
        throw new Error('環境の認証応答が不正です。')
      }
      credentials.set(environment.environmentId, body.attacker_credential)
      return true
    },

    async createEnvironment(request, signal): Promise<CreateEnvironmentResult> {
      let response: Response
      try {
        response = await fetcher(`${API_ROOT}/environments`, {
          method: 'POST', credentials: 'same-origin', signal,
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            [CSRF_HEADER]: CSRF_VALUE,
          },
          body: JSON.stringify(request),
        })
      } catch {
        return { ok: false, reason: 'unavailable' }
      }
      if (!response.ok) return { ok: false, reason: await mapCreateFailure(response) }
      try {
        const body: unknown = await response.json()
        if (!isRecord(body) || !isEnvironmentId(body.environment_id)
            || typeof body.attacker_credential !== 'string' || body.attacker_credential.length === 0) {
          return { ok: false, reason: 'unavailable' }
        }
        credentials.set(body.environment_id, body.attacker_credential)
        return {
          ok: true,
          environment: { environmentId: body.environment_id, mode: request.mode },
        }
      } catch {
        return { ok: false, reason: 'unavailable' }
      }
    },

    async submitSolution(environmentId, submission, signal) {
      let response: Response
      try {
        response = await fetcher(`${API_ROOT}/environments/${encodeURIComponent(environmentId)}/submission`, {
          method: 'POST', credentials: 'same-origin', signal,
          headers: { Accept: 'application/json', 'Content-Type': 'application/json', [CSRF_HEADER]: CSRF_VALUE },
          body: JSON.stringify('message' in submission ? { message: submission.message }
            : { threshold: submission.threshold, direction: submission.direction }),
        })
      } catch {
        return { ok: false, reason: 'unavailable' }
      }
      if (response.status === 422) return { ok: false, reason: 'invalid' }
      if (response.status === 409) return { ok: false, reason: 'exhausted' }
      if (!response.ok) return { ok: false, reason: 'unavailable' }
      try {
        const body: unknown = await response.json()
        const count = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 0
        const aggregates = ['correct', 'total', 'required'] as const
        if (!isRecord(body) || typeof body.passed !== 'boolean'
            || !count(body.attempt) || !count(body.attempts_remaining)) {
          return { ok: false, reason: 'unavailable' }
        }
        /* Hidden-case aggregates come all together or not at all (exact-match grading). */
        const present = aggregates.filter((key) => key in body)
        if (present.length && (present.length !== aggregates.length
            || aggregates.some((key) => !count(body[key])))) {
          return { ok: false, reason: 'unavailable' }
        }
        return { ok: true, result: {
          attempt: body.attempt as number, attemptsRemaining: body.attempts_remaining as number,
          ...(present.length ? { correct: body.correct as number, total: body.total as number,
            required: body.required as number } : {}),
          passed: body.passed,
        } }
      } catch {
        return { ok: false, reason: 'unavailable' }
      }
    },

    async discardEnvironment(environmentId, signal) {
      const response = await fetcher(`${API_ROOT}/environments/${encodeURIComponent(environmentId)}`, {
        method: 'DELETE', credentials: 'same-origin', signal,
        headers: { [CSRF_HEADER]: CSRF_VALUE },
      })
      if (!response.ok) throw new Error('環境を破棄できません。')
      credentials.delete(environmentId)
      for (const connection of connections.get(environmentId) ?? []) connection.close()
      connections.delete(environmentId)
    },

    async connect(environment, signal) {
      if (signal.aborted) throw new Error('WebSocket接続が中断されました。')
      const credential = credentials.get(environment.environmentId)
      if (!credential) throw new Error('この画面では環境を認証できません。')
      const href = options.locationHref ?? globalThis.location?.href
      if (!href) throw new Error('WebSocket接続先を決定できません。')
      const url = new URL(
        `${API_ROOT}/live/${encodeURIComponent(environment.environmentId)}/socket`, href,
      )
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'

      let socket: SocketLike
      try {
        socket = socketFactory(url.toString(), [SOCKET_PROTOCOL])
      } catch {
        throw new Error('WebSocketを開始できません。')
      }
      const live = createLiveConnection(socket, credential, () => {
        const owned = connections.get(environment.environmentId)
        owned?.delete(live)
        if (owned?.size === 0) connections.delete(environment.environmentId)
      }, () => `${Date.now().toString(36)}-${(++commandSequence).toString(36)}`)
      let owned = connections.get(environment.environmentId)
      if (!owned) {
        owned = new Set()
        connections.set(environment.environmentId, owned)
      }
      owned.add(live)
      const abort = () => live.close()
      signal.addEventListener('abort', abort, { once: true })
      try {
        await live.connected
        if (signal.aborted) {
          live.close()
          throw new Error('WebSocket接続が中断されました。')
        }
        return live
      } catch (error) {
        live.close()
        throw error
      } finally {
        signal.removeEventListener('abort', abort)
      }
    },
  }
  return client
}

type InternalConnection = LiveAttackConnection & { connected: Promise<void> }

function createLiveConnection(
  socket: SocketLike,
  credential: string,
  onClose: () => void,
  nextCommandId: () => string,
): InternalConnection {
  let state: ConnectionState = 'connecting'
  let worldTimeUs = 0
  let clockMode: 'explicit_wait' | 'host_clock' = 'explicit_wait'
  let pauseWhileCatchingUp = false
  let maxProgramQubits: number | null = null
  let victimActivityAvailable = false
  let closeRequested = false
  let readySettled = false
  let resolveReady: () => void = () => {}
  let rejectReady: (error: Error) => void = () => {}
  const ready = new Promise<void>((resolve, reject) => {
    resolveReady = resolve
    rejectReady = reject
  })
  const replyListeners = new Set<(reply: LiveReply) => void>()
  const clockListeners = new Set<(worldTimeUs: number) => void>()
  const stateListeners = new Set<(state: ConnectionState) => void>()
  const pending = new Map<string, {
    kind: 'accepted' | 'wait' | 'pending-list'
    resolve: (message: SocketMessage) => void
    reject: (error: Error) => void
  }>()

  const transition = (next: ConnectionState) => {
    if (state === next) return
    state = next
    for (const listener of stateListeners) {
      try { listener(next) } catch { /* Listener errors never reach console/logs. */ }
    }
  }

  const failPending = (message: string) => {
    for (const item of pending.values()) item.reject(new Error(message))
    pending.clear()
  }

  const settleReadyFailure = (message: string) => {
    if (readySettled) return
    readySettled = true
    rejectReady(new Error(message))
  }

  const onMessage = (event: MessageEvent) => {
    if (typeof event.data !== 'string') return
    let message: unknown
    try { message = JSON.parse(event.data) } catch { return }
    if (!isRecord(message) || typeof message.type !== 'string') return

    if (message.type === 'ready') {
      if (typeof message.world_time_us !== 'number' || !Number.isFinite(message.world_time_us)) {
        transition('failed')
        settleReadyFailure('WebSocket応答の形式が不正です。')
        return
      }
      const limit = message.max_program_qubits
      if ((message.clock_mode !== 'host_clock' && message.clock_mode !== 'explicit_wait')
          || typeof message.pause_while_catching_up !== 'boolean'
          || (limit !== undefined && limit !== null && !(Number.isSafeInteger(limit) && (limit as number) >= 1))) {
        transition('failed')
        settleReadyFailure('WebSocket応答の形式が不正です。')
        return
      }
      clockMode = message.clock_mode
      pauseWhileCatchingUp = message.pause_while_catching_up
      maxProgramQubits = typeof limit === 'number' ? limit : null
      victimActivityAvailable = message.victim_activity === true
      worldTimeUs = message.world_time_us
      transition('open')
      if (!readySettled) {
        readySettled = true
        resolveReady()
      }
      return
    }
    if (message.type === 'clock' && typeof message.world_time_us === 'number'
        && Number.isFinite(message.world_time_us) && message.world_time_us >= worldTimeUs) {
      worldTimeUs = message.world_time_us
      for (const listener of clockListeners) {
        try { listener(worldTimeUs) } catch { /* Listener errors stay outside transport logs. */ }
      }
      return
    }
    if (message.type === 'reply' && typeof message.request_id === 'string') {
      const reply: LiveReply = {
        requestId: message.request_id,
        value: message.value,
        errorCode: typeof message.error_code === 'string' ? message.error_code : null,
      }
      for (const listener of replyListeners) {
        try { listener(reply) } catch { /* Do not log application data or credentials. */ }
      }
      return
    }
    if ((message.type === 'accepted' || message.type === 'wait.completed' || message.type === 'pending.list')
        && typeof message.command_id === 'string') {
      const operation = pending.get(message.command_id)
      if (!operation) return
      if (message.type === 'accepted' && operation.kind === 'accepted'
          && typeof message.request_id === 'string'
          && typeof message.issued_at_world_us === 'number'
          && Number.isFinite(message.issued_at_world_us)) {
        pending.delete(message.command_id)
        operation.resolve(message)
      } else if (message.type === 'wait.completed' && operation.kind === 'wait'
          && isWaitOutcome(message.outcome) && typeof message.world_time_us === 'number'
          && Number.isFinite(message.world_time_us)) {
        worldTimeUs = message.world_time_us
        pending.delete(message.command_id)
        operation.resolve(message)
      } else if (message.type === 'pending.list' && operation.kind === 'pending-list') {
        pending.delete(message.command_id)
        operation.resolve(message)
      }
      return
    }
    if (message.type === 'error') {
      const operation = typeof message.command_id === 'string'
        ? pending.get(message.command_id) : undefined
      const code = typeof message.code === 'string' ? message.code : 'UNKNOWN'
      const error = new Error(`攻撃シミュレーター要求に失敗しました (${code})。`)
      if (operation && typeof message.command_id === 'string') {
        pending.delete(message.command_id)
        operation.reject(error)
      } else if (state === 'connecting') {
        transition('failed')
        settleReadyFailure('WebSocket認証に失敗しました。')
      }
    }
  }
  const onOpen = () => {
    try {
      socket.send(JSON.stringify({ type: 'authenticate', credential }))
    } catch {
      transition('failed')
      settleReadyFailure('WebSocket認証を開始できません。')
    }
  }
  const onError = () => {
    transition('failed')
    settleReadyFailure('WebSocket接続に失敗しました。')
    failPending('WebSocket接続に失敗しました。')
  }
  const onCloseEvent = () => {
    transition(closeRequested ? 'closed' : state === 'failed' ? 'failed' : 'closed')
    settleReadyFailure('WebSocketが接続前に終了しました。')
    failPending('WebSocketが切断されました。')
    onClose()
  }
  socket.addEventListener('open', onOpen)
  socket.addEventListener('message', onMessage as EventListener)
  socket.addEventListener('error', onError)
  socket.addEventListener('close', onCloseEvent)

  function issue(operation: string, payload: Record<string, unknown>, kind: 'accepted' | 'wait' | 'pending-list') {
    if (state !== 'open' || socket.readyState !== 1) {
      return Promise.reject(new Error('WebSocketは接続されていません。'))
    }
    const commandId = nextCommandId()
    const frame = JSON.stringify({ type: 'command', command_id: commandId, operation, payload })
    if (new TextEncoder().encode(frame).byteLength > MAX_MESSAGE_BYTES) {
      return Promise.reject(new Error('送信内容が上限を超えています。'))
    }
    return new Promise<SocketMessage>((resolve, reject) => {
      pending.set(commandId, { kind, resolve, reject })
      try { socket.send(frame) } catch {
        pending.delete(commandId)
        reject(new Error('WebSocketへ送信できません。'))
      }
    })
  }

  const accepted = async (
    operation: string, kind: LiveRequestKind, payload: Record<string, unknown>,
  ): Promise<LiveRequest> => {
    const message = await issue(operation, payload, 'accepted')
    return { requestId: message.request_id as string, kind,
      issuedAtWorldUs: message.issued_at_world_us as number }
  }

  const connection: InternalConnection = {
    get state() { return state },
    get worldTimeUs() { return worldTimeUs },
    get clockMode() { return clockMode },
    get pauseWhileCatchingUp() { return pauseWhileCatchingUp },
    get maxProgramQubits() { return maxProgramQubits },
    get victimActivityAvailable() { return victimActivityAvailable },
    connected: ready,
    async submit(program: ProgramSubmission) {
      const payload: Record<string, unknown> = {
        device_id: program.deviceId,
        shots: program.shots,
        circuit_config: program.circuitConfig,
      }
      if (program.initialization !== undefined) payload.initialization = program.initialization
      if (program.interleave !== undefined) payload.interleave = program.interleave
      return accepted('program.submit', 'submit', payload)
    },
    cancel(jobId) { return accepted('job.cancel', 'cancel', { job_id: jobId }) },
    status(jobId) { return accepted('job.status.read', 'status', { job_id: jobId }) },
    result(jobId) { return accepted('job.result.read', 'result', { job_id: jobId }) },
    victimActivity(jobId) { return accepted('job.victim_activity.read', 'victim_activity', { job_id: jobId }) },
    async pendingRequests() {
      const message = await issue('request.pending.list', {}, 'pending-list')
      if (!Array.isArray(message.requests) || message.requests.length > 4096) {
        throw new Error('未完了要求一覧の応答が不正です。')
      }
      return message.requests.map((item: unknown): LiveRequest => {
        if (!isRecord(item) || typeof item.request_id !== 'string'
            || !/^[\x21-\x7e]{1,128}$/.test(item.request_id)
            || !['submit', 'cancel', 'status', 'result', 'victim_activity'].includes(String(item.kind))
            || !(item.job_id === null || typeof item.job_id === 'string'
              && /^[\x21-\x7e]{1,128}$/.test(item.job_id))
            || typeof item.issued_at_world_us !== 'number'
            || !Number.isFinite(item.issued_at_world_us) || item.issued_at_world_us < 0) {
          throw new Error('未完了要求一覧の応答が不正です。')
        }
        return { requestId: item.request_id, kind: item.kind as LiveRequestKind,
          issuedAtWorldUs: item.issued_at_world_us,
          ...(item.job_id === null ? {} : { jobId: item.job_id }) }
      })
    },
    async wait(target: WaitTarget): Promise<WaitResult> {
      const selected = waitOperation(target)
      const message = await issue(selected.operation, selected.payload, 'wait')
      return {
        outcome: message.outcome as WaitResult['outcome'],
        worldTimeUs: message.world_time_us as number,
      }
    },
    onReply(listener) {
      replyListeners.add(listener)
      return () => replyListeners.delete(listener)
    },
    onClockChange(listener) {
      clockListeners.add(listener)
      return () => clockListeners.delete(listener)
    },
    onStateChange(listener) {
      stateListeners.add(listener)
      return () => stateListeners.delete(listener)
    },
    close() {
      if (closeRequested || state === 'closed') return
      closeRequested = true
      transition('closed')
      failPending('WebSocketが閉じられました。')
      try { socket.close() } catch { /* Closing is best-effort and idempotent. */ }
      onClose()
    },
  }
  return connection
}

function waitOperation(target: WaitTarget): { operation: string; payload: Record<string, unknown> } {
  if (target.kind === 'duration') {
    if (!Number.isFinite(target.durationUs) || target.durationUs <= 0) {
      throw new Error('待機時間は正の有限値にしてください。')
    }
    return { operation: 'wait.duration', payload: { duration_us: target.durationUs } }
  }
  if (target.kind === 'request') {
    return { operation: 'wait.request', payload: { request_id: target.requestId } }
  }
  return { operation: 'wait.any', payload: {} }
}

async function mapCreateFailure(response: Response): Promise<CreateEnvironmentFailure> {
  if (response.status === 429) return 'browser_limit'
  if (response.status === 413) return 'too_large'
  let code = ''
  try {
    const body: unknown = await response.json()
    if (isRecord(body) && isRecord(body.detail) && typeof body.detail.code === 'string') {
      code = body.detail.code
    }
  } catch { /* Status alone is enough to fail closed. */ }
  if (response.status === 422 && code === 'INVALID_SETTINGS') return 'invalid_settings'
  if (response.status === 503 && code === 'ENVIRONMENT_CAPACITY') return 'server_full'
  return 'unavailable'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isEnvironmentId(value: unknown): value is string {
  return typeof value === 'string' && /^[a-f0-9]{32}$/.test(value)
}

function isWaitOutcome(value: unknown): value is WaitResult['outcome'] {
  return value === 'reached' || value === 'limited' || value === 'nothing_pending'
}
