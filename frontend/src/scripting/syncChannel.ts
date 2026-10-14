/*
 * スクリプト実行用 Worker と画面の間の同期チャネル。
 *
 * Worker の Python は同期で書かれる（job.result() で待つ）。待っている間 Worker の
 * イベントループは回らないので、応答を message イベントで受け取れない。そこで、
 * 要求だけを postMessage で送り、応答は SharedArrayBuffer に書いてもらい、
 * Worker は Atomics.wait で止まって待つ。
 *
 * 配置: Int32 の制御領域 [応答した呼出番号, バイト長] と、その後ろの JSON 本体。
 * Worker は自分の呼出番号が書かれるまで待つ。画面側は、中断で見捨てられた古い
 * 呼出しの応答を、新しい呼出しを受け取った後には書かない（createResponder）。
 * このファイルは実行時の import を持たない（Node の検証から直接読み込む）。
 */

const CALL_ID = 0
const LENGTH = 1
const CONTROL_BYTES = 8

export const DEFAULT_CHANNEL_BYTES = 1024 * 1024
/* 「応答が大きすぎる」という誤りの応答そのものは必ず入る大きさ。 */
const MIN_CHANNEL_BYTES = 1024
/* 中断の確認と同じ間隔で目を覚ます。短すぎると Worker が無駄に回る。 */
export const WAIT_SLICE_MS = 50

export type ChannelOutcome = { value: unknown } | { error: { code: string; message: string } }

export function createChannelBuffer(bytes = DEFAULT_CHANNEL_BYTES): SharedArrayBuffer {
  return new SharedArrayBuffer(CONTROL_BYTES + Math.max(bytes, MIN_CHANNEL_BYTES))
}

/**
 * 画面側の書き手。呼出番号は Worker が 1 から順に振る。受け取った最新の呼出しより
 * 古い応答は捨てる（Worker はもう待っていない）。
 */
export function createResponder(buffer: SharedArrayBuffer) {
  const control = new Int32Array(buffer, 0, CONTROL_BYTES / 4)
  const body = new Uint8Array(buffer, CONTROL_BYTES)
  let latest = 0
  return {
    received(callId: number) { latest = Math.max(latest, callId) },
    write(callId: number, outcome: ChannelOutcome): boolean {
      if (callId < latest) return false
      let encoded = new TextEncoder().encode(JSON.stringify(outcome))
      if (encoded.byteLength > body.byteLength) {
        encoded = new TextEncoder().encode(JSON.stringify({
          error: { code: 'RESPONSE_TOO_LARGE', message: '応答がスクリプト用の受け渡し領域に入りません。' },
        }))
      }
      // TextEncoder は共有メモリへ直接書けないため、通常の配列から写す。
      body.set(encoded)
      Atomics.store(control, LENGTH, encoded.byteLength)
      Atomics.store(control, CALL_ID, callId)
      Atomics.notify(control, CALL_ID)
      return true
    },
  }
}

/** Worker 側: 指定した呼出しの応答が書かれるまで止まって待つ。onSlice は中断の確認に使う。 */
export function waitForResponse(
  buffer: SharedArrayBuffer,
  callId: number,
  onSlice: () => void,
  timeoutMs = Number.POSITIVE_INFINITY,
): ChannelOutcome {
  const control = new Int32Array(buffer, 0, CONTROL_BYTES / 4)
  const body = new Uint8Array(buffer, CONTROL_BYTES)
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const written = Atomics.load(control, CALL_ID)
    if (written === callId) {
      const length = Atomics.load(control, LENGTH)
      // TextDecoder も共有メモリを直接読めないため、写してから読む。
      return JSON.parse(new TextDecoder().decode(body.slice(0, length))) as ChannelOutcome
    }
    onSlice()
    const remaining = deadline - Date.now()
    if (remaining <= 0) {
      return { error: { code: 'TIMEOUT', message: '画面からの応答が時間内に届きませんでした。' } }
    }
    Atomics.wait(control, CALL_ID, written, Math.min(WAIT_SLICE_MS, remaining))
  }
}

/** Worker 側: 中断を確かめながら止まる（time.sleep の代わり）。 */
export function blockingSleep(sleeper: Int32Array, milliseconds: number, onSlice: () => void): void {
  const deadline = Date.now() + Math.max(0, milliseconds)
  for (;;) {
    onSlice()
    const remaining = deadline - Date.now()
    if (remaining <= 0) return
    Atomics.wait(sleeper, 0, 0, Math.min(WAIT_SLICE_MS, remaining))
  }
}
