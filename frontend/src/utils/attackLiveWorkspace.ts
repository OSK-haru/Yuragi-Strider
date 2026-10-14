import type {
  SolutionSubmission,
  AttackEnvironmentClient, ConnectionState, CreateEnvironmentRequest, EnvironmentHandle,
  LiveAttackConnection, LiveReply, LiveRequest, OwnedEnvironment, ProgramSubmission, SettingsCatalog, WaitTarget,
} from '../types/attackLive'
import type { ScriptServicePort } from '../scripting/scriptServices'
import { assertSupportedCatalog, settleRequest } from './attackLiveSettings'

export type LiveWorkspaceState = {
  catalog: SettingsCatalog | null
  ownedEnvironments: OwnedEnvironment[]
  environment: EnvironmentHandle | null
  connectionState: ConnectionState | 'idle'
  clockMode: 'explicit_wait' | 'host_clock' | null
  pauseWhileCatchingUp: boolean
  /** Upper bound for the circuit editor; null until a connection reports it. */
  maxProgramQubits: number | null
  worldTimeUs: number
  pending: LiveRequest[]
  pendingUnknown: boolean
  replies: LiveReply[]
  busy: string | null
  notice: string
  replacement: CreateEnvironmentRequest | null
}

const MAX_REPLIES = 100
const MAX_PENDING = 128
const FAILURE_MESSAGES = {
  browser_limit: 'このブラウザで持てる環境数の上限です。',
  server_full: 'サーバーの環境数の上限です。時間を置いてください。',
  invalid_settings: '設定がサービスに拒否されました。値と組合せを確認してください。',
  too_large: '設定の送信内容が大きすぎます。',
  unavailable: '環境の成立を確認できませんでした。通信とサービスの状態を確認してください。',
}

/** Only public handles/projections enter the observable state. Transport and
 * credentials remain in the existing client, outside React state. */
export function createLiveWorkspace(client: AttackEnvironmentClient, timeoutMs = 30_000) {
  let state: LiveWorkspaceState = {
    catalog: null, ownedEnvironments: [], environment: null, connectionState: 'idle',
    clockMode: null, pauseWhileCatchingUp: false, maxProgramQubits: null, worldTimeUs: 0,
    pending: [], pendingUnknown: false, replies: [], busy: null, notice: '', replacement: null,
  }
  let connection: LiveAttackConnection | null = null
  let subscriptions: Array<() => void> = []
  let activeAbort: AbortController | null = null
  let generation = 0
  const listeners = new Set<() => void>()
  function update(values: Partial<LiveWorkspaceState>) {
    state = { ...state, ...values }
    for (const listener of listeners) listener()
  }
  function detach() {
    for (const unsubscribe of subscriptions) unsubscribe()
    subscriptions = []
    connection?.close()
    connection = null
  }
  async function perform(label: string, action: (signal: AbortSignal, current: () => boolean) => Promise<void>) {
    if (state.busy !== null) return
    const revision = generation
    const abort = new AbortController()
    activeAbort = abort
    update({ busy: label, notice: '' })
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      await Promise.race([
        action(abort.signal, () => revision === generation && !abort.signal.aborted),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => {
            abort.abort()
            detach()
            if (revision === generation) update({ connectionState: 'failed' })
            reject(new Error('timeout'))
          }, timeoutMs)
        }),
      ])
    } catch {
      if (revision === generation) update({ notice: '操作の完了を確認できませんでした。自動再送は行いません。' })
    } finally {
      clearTimeout(timer)
      if (revision === generation) { activeAbort = null; update({ busy: null }) }
    }
  }
  async function attach(signal: AbortSignal, current: () => boolean) {
    const environment = state.environment
    if (!environment) return
    detach()
    update({ connectionState: 'connecting' })
    let opened: LiveAttackConnection
    try { opened = await client.connect(environment, signal) }
    catch (error) {
      if (current()) update({ connectionState: 'failed' })
      throw error
    }
    if (!current()) { opened.close(); return }
    connection = opened
    update({ connectionState: opened.state, worldTimeUs: opened.worldTimeUs,
      clockMode: opened.clockMode, pauseWhileCatchingUp: opened.pauseWhileCatchingUp,
      maxProgramQubits: opened.maxProgramQubits })
    subscriptions = [
      opened.onStateChange((connectionState) => update({ connectionState })),
      opened.onClockChange((worldTimeUs) => update({ worldTimeUs })),
      opened.onReply((incoming) => {
        const reply = publicReply(incoming)
        update({
          pending: settleRequest(state.pending, reply),
          replies: [...state.replies.filter((item) => item.requestId !== reply.requestId), reply].slice(-MAX_REPLIES),
          worldTimeUs: opened.worldTimeUs,
        })
      }),
    ]
  }
  async function restorePending(current: () => boolean) {
    if (!connection || !current()) return
    try {
      const pending = await connection.pendingRequests()
      if (current()) update({ pending, pendingUnknown: false,
        notice: pending.length ? '未完了の要求一覧を復元しました。' : '未完了の要求はありません。' })
    } catch {
      if (current()) update({ notice: '未完了の要求一覧を取得できません。再接続して確認してください。' })
    }
  }
  async function create(request: CreateEnvironmentRequest, signal: AbortSignal, current: () => boolean) {
    const result = await client.createEnvironment(request, signal)
    if (!current()) return
    if (!result.ok) {
      update({ notice: FAILURE_MESSAGES[result.reason],
        replacement: result.reason === 'browser_limit' ? request : null })
      return
    }
    update({ environment: { environmentId: result.environment.environmentId, mode: result.environment.mode },
      pending: [], pendingUnknown: false, replies: [], worldTimeUs: 0, replacement: null })
    await attach(signal, current)
  }
  async function discard(signal: AbortSignal, current: () => boolean) {
    const environment = state.environment
    if (!environment) return
    // Keep the handle until the server confirms destruction, including a
    // failed request. A transport error is not confirmation of disposal.
    await client.discardEnvironment(environment.environmentId, signal)
    if (!current()) return
    detach()
    update({ environment: null, ownedEnvironments: state.ownedEnvironments.filter((item) =>
      item.environmentId !== environment.environmentId), connectionState: 'idle', pending: [], pendingUnknown: false, replies: [],
      worldTimeUs: 0, clockMode: null, pauseWhileCatchingUp: false, maxProgramQubits: null,
      replacement: null, notice: '環境を破棄しました。設定を変更できます。' })
  }
  async function issue(label: string, action: (live: LiveAttackConnection) => Promise<LiveRequest>) {
    if (!connection || connection.state !== 'open') return
    if (state.pending.length >= MAX_PENDING) {
      update({ notice: '未完了の要求が多すぎます。待機してreplyを受け取ってください。' }); return
    }
    const live = connection
    await perform(label, async (_signal, current) => {
      const request = await action(live)
      if (!current()) return
      // Reply delivery may precede the accepted Promise continuation.
      if (state.replies.some((reply) => reply.requestId === request.requestId)) return
      update({ pending: [...state.pending, {
        requestId: request.requestId, kind: request.kind, issuedAtWorldUs: request.issuedAtWorldUs,
        ...(request.jobId === undefined ? {} : { jobId: request.jobId }),
      }] })
    })
  }
  return {
    getSnapshot: () => state,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } },
    loadCatalog: () => perform('catalog', async (signal, current) => {
      const catalog = await client.getCatalog(signal)
      assertSupportedCatalog(catalog)
      if (current()) update({ catalog })
    }),
    loadOwnedEnvironments: () => perform('owned', async (signal, current) => {
      const ownedEnvironments = await client.listOwnedEnvironments(signal)
      if (current()) update({ ownedEnvironments })
    }),
    resume: (environment: OwnedEnvironment) => perform('resume', async (signal, current) => {
      if (state.environment) return
      const available = await client.resumeEnvironment(environment, signal)
      if (!current()) return
      if (!available) {
        update({ ownedEnvironments: state.ownedEnvironments.filter((item) =>
          item.environmentId !== environment.environmentId), notice: '環境は見つかりません。期限切れまたは破棄済みです。' })
        return
      }
      update({ environment: { environmentId: environment.environmentId, mode: environment.mode },
        pending: [], pendingUnknown: true, replies: [], replacement: null,
        notice: '未完了の要求一覧を取得しています。' })
      await attach(signal, current)
      await restorePending(current)
    }),
    establish: (request: CreateEnvironmentRequest) => perform('create', async (signal, current) => {
      // An existing world stays frozen. This entry also allows an explicit
      // request which reaches the browser limit, offering replacement safely.
      if (state.environment) {
        update({ replacement: request, notice: '今の環境を破棄して作り直しますか？' }); return
      }
      await create(request, signal, current)
    }),
    discard: () => perform('discard', discard),
    cancelReplacement: () => update({ replacement: null }),
    replace: (environmentId?: string) => perform('replace', async (signal, current) => {
      const request = state.replacement
      if (!request) return
      if (state.environment) await discard(signal, current)
      else {
        if (!environmentId || !/^[a-f0-9]{32}$/.test(environmentId)) {
          update({ notice: '破棄対象の環境IDを、その環境の画面で確認してください。' }); return
        }
        await client.discardEnvironment(environmentId, signal)
      }
      if (current()) await create(request, signal, current)
    }),
    connect: () => perform('connect', async (signal, current) => {
      await attach(signal, current)
      if (state.pendingUnknown) await restorePending(current)
    }),
    submit: (program: ProgramSubmission) => issue('submit', (live) => live.submit(program)),
    submitAnswer: (submission: SolutionSubmission) => perform('answer', async (signal, current) => {
      const environment = state.environment
      if (!environment || environment.mode !== 'challenge') {
        update({ notice: '提出できるのは課題モードの環境だけです。' }); return
      }
      const outcome = await client.submitSolution(environment.environmentId, submission, signal)
      if (!current()) return
      if (!outcome.ok) {
        update({ notice: outcome.reason === 'invalid' ? ('message' in submission
          ? '提出内容が正しくありません。メッセージは ASCII 文字で 256 文字以内です。この課題の解答の形式と合っているかも確かめてください。提出回数は減りません。'
          : '提出内容が正しくありません。しきい値は0〜1、向きは ge か le です。この課題の解答の形式と合っているかも確かめてください。提出回数は減りません。')
          : outcome.reason === 'exhausted' ? '提出できる回数を使い切りました。'
            : '提出の結果を確認できませんでした。自動再送は行いません。' })
        return
      }
      const { result } = outcome
      const detail = result.total === undefined
        ? `メッセージは${result.passed ? '一致しました' : '一致しません'}`
        : `見えないケース ${result.total} 件中 ${result.correct} 件正解（合格は ${result.required} 件以上）`
      update({ notice: `提出 ${result.attempt}回目: ${detail} → ${result.passed ? '合格' : '不合格'}。残り ${result.attemptsRemaining} 回。` })
    }),
    /**
     * Port for the in-browser script runner. A script opens its own connection,
     * so its polling never enters this state.
     * The credential stays inside the client.
     */
    scriptPort(): ScriptServicePort | null {
      const environment = state.environment
      if (!environment) return null
      return {
        environmentId: environment.environmentId,
        connect: (signal) => client.connect(environment, signal),
        submitSolution: (estimator, signal) => client.submitSolution(environment.environmentId, estimator, signal),
      }
    },
    job: (kind: 'status' | 'result' | 'cancel', jobId: string) => issue(kind, (live) => live[kind](jobId)),
    wait: (target: WaitTarget) => perform('wait', async (_signal, current) => {
      if (!connection || connection.state !== 'open') return
      if (target.kind === 'request' && !state.pending.some((request) => request.requestId === target.requestId)) {
        update({ notice: 'この環境の未完了の要求を選んでください。' }); return
      }
      const result = await connection.wait(target)
      if (current()) update({ worldTimeUs: result.worldTimeUs,
        pendingUnknown: result.outcome === 'nothing_pending' ? false : state.pendingUnknown,
        notice: result.outcome === 'limited'
        ? '待機の上限に達しました。未完了の要求は残っています。'
        : result.outcome === 'nothing_pending' ? '待機するreplyはありません。' : '待機が完了しました。' })
    }),
    disconnect() {
      generation += 1
      activeAbort?.abort()
      activeAbort = null
      detach()
      update({ busy: null, connectionState: state.environment ? 'closed' : 'idle' })
    },
  }
}

/** Defensive display projection for the common job API, not a new authority
 * boundary. Unknown fields (including accidentally supplied credentials) are
 * never copied into page state or rendered. */
function publicReply(reply: LiveReply): LiveReply {
  const value: Record<string, unknown> = {}
  if (isRecord(reply.value)) {
    if (typeof reply.value.job_id === 'string' && /^job-[a-f0-9-]{32,36}$/.test(reply.value.job_id)) value.job_id = reply.value.job_id
    const statuses = ['submitted', 'validating', 'queued', 'allocated', 'running', 'cancel_requested',
      'succeeded', 'failed', 'cancelled', 'rejected', 'internal_error']
    if (typeof reply.value.status === 'string' && statuses.includes(reply.value.status)) value.status = reply.value.status
    const result = isRecord(reply.value.result) ? reply.value.result : reply.value
    if (typeof reply.value.reason === 'string' && reply.value.reason.length <= 200) value.reason = reply.value.reason
    if (typeof result.shots === 'number' && Number.isSafeInteger(result.shots) && result.shots >= 0) value.shots = result.shots
    if (isRecord(result.counts)) {
      const counts = Object.fromEntries(Object.entries(result.counts).filter(([bits, count]) =>
        /^[01]{1,32}$/.test(bits) && typeof count === 'number' && Number.isSafeInteger(count) && count >= 0).slice(0, 256))
      value.counts = counts
    }
  }
  return { requestId: reply.requestId, value, errorCode: reply.errorCode === null ? null
    : /^[A-Z_]{1,64}$/.test(reply.errorCode) ? reply.errorCode : 'SERVICE_ERROR' }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
