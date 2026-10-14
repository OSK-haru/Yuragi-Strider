/* 画面とスクリプト実行用 Worker の間でやり取りする message の形。 */

export type RunRequest = {
  type: 'run'
  /** 実行ごとに変わる番号。前の実行の遅れた message を取り違えないため。 */
  runId: number
  /** 作業ディレクトリに置くファイル（名前 → 中身）。 */
  files: Record<string, string>
  entry: string
  argv: string[]
  /** Worker に組み込む SDK（相対パス → 中身）。 */
  sdk: Record<string, string>
  channel: SharedArrayBuffer
  /** Pyodide の中断バッファ。2 を書くと KeyboardInterrupt。 */
  interrupt: SharedArrayBuffer
}

export type WorkerMessage =
  | { type: 'phase'; runId: number; phase: 'loading' | 'packages' | 'running'; detail?: string }
  | { type: 'output'; runId: number; stream: 'stdout' | 'stderr'; text: string }
  | { type: 'call'; runId: number; callId: number; method: string; args: unknown }
  | { type: 'exit'; runId: number; code: number }
  | { type: 'fatal'; runId: number; message: string }
