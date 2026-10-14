/*
 * スクリプト実行の画面側の窓口。Worker を作り、1回ずつ実行を回す。
 *
 * - 同時に動かせるのは1本だけ（ターミナルのシェルと同じ）。
 * - 停止は、まず KeyboardInterrupt を送る。3秒で終わらなければ Worker ごと止める
 *   （次の実行で Python を読み込み直す）。
 * - input() は provideInput で渡す行を待つ。
 * このファイルは実行時の import を持たない（Node の検証から直接読み込む）。
 */

import type { RunRequest, WorkerMessage } from './scriptProtocol'
import type { ChannelOutcome } from './syncChannel'

export type RunnerPhase = 'idle' | 'loading' | 'packages' | 'running' | 'stopping'

export type ScriptRunnerEvents = {
  output(stream: 'stdout' | 'stderr' | 'system', text: string): void
  phase(phase: RunnerPhase, detail?: string): void
  /** input() の待ちの始まりと終わり。prompt は input() に渡された文字列。 */
  inputRequested(waiting: boolean, prompt?: string): void
}

export type ScriptServices = {
  call(method: string, args: unknown): Promise<ChannelOutcome>
  dispose(): void
}

type WorkerLike = {
  postMessage(message: RunRequest): void
  terminate(): void
  addEventListener(type: 'message', listener: (event: MessageEvent<WorkerMessage>) => void): void
  addEventListener(type: 'error', listener: (event: Event) => void): void
}

export type ScriptRunnerOptions = {
  createWorker: () => WorkerLike
  /** Worker に組み込む SDK（相対パス → 中身）。 */
  sdk: Record<string, string>
  /** 応答を書き込む共有バッファ。createChannelBuffer を渡す。 */
  createChannel: () => SharedArrayBuffer
  /** responder を作る。syncChannel.createResponder を渡す。 */
  createResponder: (buffer: SharedArrayBuffer) => {
    received(callId: number): void
    write(callId: number, outcome: ChannelOutcome): boolean
  }
  killAfterMs?: number
}

export const EXIT_KILLED = 137

export type ScriptRunner = ReturnType<typeof createScriptRunner>

/** SharedArrayBuffer が使えない（cross-origin isolated でない）ページでは動かさない。 */
export function scriptRuntimeSupport(scope: { crossOriginIsolated?: boolean } = globalThis):
  { ok: true } | { ok: false; reason: string } {
  if (typeof SharedArrayBuffer !== 'function' || scope.crossOriginIsolated !== true) {
    return { ok: false, reason: 'このページの配信設定ではスクリプトを実行できません（cross-origin isolation が必要です）。' }
  }
  if (typeof Worker !== 'function' && !('Worker' in scope)) {
    return { ok: false, reason: 'このブラウザは Web Worker を使えません。' }
  }
  return { ok: true }
}

export function createScriptRunner(events: ScriptRunnerEvents, options: ScriptRunnerOptions) {
  const killAfterMs = options.killAfterMs ?? 3000
  let worker: WorkerLike | null = null
  let runSequence = 0
  let active: {
    runId: number
    services: ScriptServices
    responder: ReturnType<ScriptRunnerOptions['createResponder']>
    interrupt: Uint8Array
    finish: (code: number) => void
    killTimer?: ReturnType<typeof setTimeout>
  } | null = null
  let pendingInput: ((line: string | null) => void) | null = null
  let abandonedInput: ((line: string | null) => void) | null = null

  function ensureWorker(): WorkerLike {
    if (worker) return worker
    const created = options.createWorker()
    created.addEventListener('message', (event: MessageEvent<WorkerMessage>) => receive(event.data))
    created.addEventListener('error', () => {
      if (!active) return
      events.output('system', 'スクリプト実行環境が異常終了しました。')
      kill()
    })
    worker = created
    return created
  }

  function finish(code: number) {
    const run = active
    if (!run) return
    clearTimeout(run.killTimer)
    active = null
    run.services.dispose()
    if (pendingInput) { pendingInput(null); pendingInput = null; events.inputRequested(false) }
    abandonedInput?.(null)
    abandonedInput = null
    events.phase('idle')
    run.finish(code)
  }

  function kill() {
    worker?.terminate()
    worker = null
    finish(EXIT_KILLED)
  }

  function receive(message: WorkerMessage) {
    const run = active
    if (!run || message.runId !== run.runId) return
    if (message.type === 'phase') events.phase(message.phase, message.detail)
    else if (message.type === 'output') events.output(message.stream, message.text)
    else if (message.type === 'exit') finish(message.code)
    else if (message.type === 'fatal') {
      events.output('system', `スクリプトを実行できませんでした: ${message.message}`)
      finish(1)
    } else if (message.type === 'call') {
      run.responder.received(message.callId)
      void run.services.call(message.method, message.args).then((outcome) => {
        if (active === run) run.responder.write(message.callId, outcome)
      })
    }
  }

  return {
    get running() { return active !== null },
    /** 1本実行し、終了コードを返す。強制停止は EXIT_KILLED。 */
    run(request: { files: Record<string, string>; entry: string; argv: string[] },
      createServices: (readLine: (prompt: string) => Promise<string | null>) => ScriptServices): Promise<number> {
      if (active) return Promise.reject(new Error('別のスクリプトが実行中です。'))
      const target = ensureWorker()
      const runId = (runSequence += 1)
      const channel = options.createChannel()
      const interruptBuffer = new SharedArrayBuffer(1)
      const readLine = (prompt: string) => new Promise<string | null>((resolve) => {
        pendingInput = resolve
        events.inputRequested(true, prompt)
      })
      return new Promise<number>((resolve) => {
        active = { runId, services: createServices(readLine), responder: options.createResponder(channel),
          interrupt: new Uint8Array(interruptBuffer), finish: resolve }
        events.phase('loading')
        target.postMessage({ type: 'run', runId, files: request.files, entry: request.entry, argv: request.argv,
          sdk: options.sdk, channel, interrupt: interruptBuffer })
      })
    },
    provideInput(line: string | null) {
      const resolve = pendingInput
      if (!resolve) return false
      pendingInput = null
      events.inputRequested(false)
      resolve(line)
      return true
    },
    get waitingForInput() { return pendingInput !== null },
    /** Ctrl+C。終わらなければ killAfterMs 後に Worker ごと止める。 */
    interrupt() {
      const run = active
      if (!run) return
      if (run.killTimer !== undefined) { kill(); return }
      Atomics.store(run.interrupt, 0, 2)
      /* 入力待ちは返さずに打ち切る（返すと Python が先に入力の終わりと受け取る）。 */
      if (pendingInput) { abandonedInput = pendingInput; pendingInput = null; events.inputRequested(false) }
      events.phase('stopping')
      run.killTimer = setTimeout(() => {
        if (active !== run) return
        events.output('system', `${killAfterMs / 1000}秒以内に止まらなかったため、実行環境ごと停止しました。`)
        kill()
      }, killAfterMs)
    },
    dispose() {
      if (active) kill()
      worker?.terminate()
      worker = null
    },
  }
}
