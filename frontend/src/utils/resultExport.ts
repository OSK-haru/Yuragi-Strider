/*
 * 実行結果の書き出しと読み込み。
 *
 * 回路データ（circuitConfigTransfer.ts）が「これから実行するもの」を運ぶのに
 * 対し、こちらは「実行し終えたもの」を運ぶ。授業で配る、人に渡す、あとで
 * 読み直す、といった用途を想定している。
 *
 * 書き出しには、結果そのものだけでなく **その結果を生んだ入力一式** を必ず
 * 同梱する。入力の無い結果は、後から見ても何の条件の話か分からない。
 *
 * ■ 読み込みで守っていること
 *
 * このアプリの状態エクスプローラーは「いま編集中の回路・設定と、表示中の
 * 結果が対応している」ことを前提に図を描いている。実行後に回路を編集すると
 * 結果を隠すのは、そのためである。
 *
 * したがって結果だけを読み込むと、この前提が壊れる。図に出ている結果が
 * どの条件のものか画面から辿れなくなるからだ。そこで読み込みは
 * **結果と入力をセットで復元する**。ファイルに入っている回路と設定を
 * エディターへ書き戻したうえで結果を載せるので、読み込み後も
 * 「画面の設定 = 図の条件」が成り立ったままになる。
 *
 * そのうえで、復元した結果には origin: 'imported' を付ける。計算し直した
 * ものではないことを画面に明示するためで、この印は再実行するまで消えない。
 */

import type { PulseLabForm, PulseRunRecord } from '../types/pulse'
import type { PulseCircuitState } from '../types/pulseCircuit'
import type { GateDurationDefaults, SimulationResponse } from '../types/simulation'
import type { CircuitConfig } from './circuitConfig'

export const GATE_AWARE_RESULT_KIND = 'quantscope_gate_aware_result'
export const PULSE_RESULT_KIND = 'quantscope_pulse_result'

/*
 * 生成元を書き残しておく。結果ファイルだけが手元に残ったときに、どの
 * アプリのどの形式かが分からないと読みようがない。
 */
const GENERATOR = 'Yuragi-Strider'

/*
 * ビルド時に vite.config.ts が埋め込むコミット識別子。Vite を通さずに
 * このファイルを読む検証スクリプトでは未定義なので、typeof で確かめる。
 */
declare const __APP_BUILD__: string | undefined
function appBuild(): string {
  return typeof __APP_BUILD__ === 'string' ? __APP_BUILD__ : 'unknown'
}

export type ResultExportMeta = {
  generator: typeof GENERATOR
  /* 書き出した時刻（ISO 8601, UTC）。実行時刻とは別物。 */
  exported_at: string
  /* 書き出したアプリのコミット識別子。古いファイルには無い。 */
  build?: string
}

export type GateAwareResultBundle = {
  version: 1
  kind: typeof GATE_AWARE_RESULT_KIND
  meta: ResultExportMeta
  /* この結果を生んだ入力。これが無いと結果を読み解けない。 */
  inputs: {
    circuit_config: CircuitConfig | null
    gate_duration_defaults: GateDurationDefaults
  }
  result: SimulationResponse
}

export type PulseResultBundle = {
  version: 1
  kind: typeof PULSE_RESULT_KIND
  meta: ResultExportMeta
  inputs: {
    pulse_circuit: PulseCircuitState
    /* 実行時点のPulseラボ設定。実行後に画面で変えた値ではない。 */
    lab_form: PulseRunRecord['formAtRun']
  }
  run: {
    completed_at: string
    /*
     * 実行条件の指紋。同じ signature の結果どうしは同じ条件で計算されて
     * いる。ファイルを2つ並べたときの照合に使える。
     */
    signature: string
  }
  result: PulseRunRecord['response']
}

function createMeta(now: Date): ResultExportMeta {
  return {
    generator: GENERATOR,
    exported_at: now.toISOString(),
    build: appBuild(),
  }
}

export function buildGateAwareResultBundle(
  response: SimulationResponse,
  circuitConfig: CircuitConfig | null,
  gateDurationDefaults: GateDurationDefaults,
  now: Date = new Date(),
): GateAwareResultBundle {
  return {
    version: 1,
    kind: GATE_AWARE_RESULT_KIND,
    meta: createMeta(now),
    inputs: {
      circuit_config: circuitConfig,
      gate_duration_defaults: gateDurationDefaults,
    },
    result: response,
  }
}

export function buildPulseResultBundle(
  record: PulseRunRecord,
  circuit: PulseCircuitState,
  now: Date = new Date(),
): PulseResultBundle {
  return {
    version: 1,
    kind: PULSE_RESULT_KIND,
    meta: createMeta(now),
    inputs: {
      pulse_circuit: circuit,
      lab_form: record.formAtRun,
    },
    run: {
      completed_at: record.completedAt,
      signature: record.signature,
    },
    result: record.response,
  }
}

/*
 * ファイル名に実行時刻を入れる。条件を変えて何度も書き出す使い方なので、
 * 「(1)」「(2)」が並ぶより、いつの実行かが名前で分かるほうがよい。
 * コロンはWindowsのファイル名に使えないため、日付と時刻を数字だけにする。
 */
export function resultFileName(prefix: string, timestamp: string): string {
  const parsed = new Date(timestamp)
  const stamp = Number.isNaN(parsed.getTime())
    ? 'unknown'
    : [
        parsed.getFullYear(),
        String(parsed.getMonth() + 1).padStart(2, '0'),
        String(parsed.getDate()).padStart(2, '0'),
        '-',
        String(parsed.getHours()).padStart(2, '0'),
        String(parsed.getMinutes()).padStart(2, '0'),
        String(parsed.getSeconds()).padStart(2, '0'),
      ].join('')
  return `${prefix}_${stamp}.json`
}

/*
 * Blob を作ってダウンロードさせるところまで。回路データの書き出しと同じ
 * 手順だが、こちらはファイル名を呼び出し側から渡す。
 */
export function downloadJson(json: string, fileName: string) {
  const blob = new Blob([json], { type: 'application/json' })
  const url = window.URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  /*
   * click() が非同期に処理されることがあるので、revoke は次のタスクへ回す。
   * 同期的に呼ぶと、ダウンロードが始まる前にURLが無効になる場合がある。
   */
  window.setTimeout(() => window.URL.revokeObjectURL(url), 0)
}

/* ---------------------------------------------------------------- 読み込み */

/*
 * 結果ファイルは軌跡や密度行列を含むので、回路データより桁が大きい。
 * それでも上限は要る。壊れた巨大ファイルを掴んでブラウザを固めないため。
 */
export const MAX_RESULT_IMPORT_BYTES = 24 * 1024 * 1024

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/*
 * 検証で見るのは「このファイルを載せて画面が壊れないか」まで。
 *
 * 結果の中身（軌跡の物理的な妥当性など）までは検査しない。それは
 * シミュレーターが計算して返したものであり、ここで再計算して確かめる
 * ようなものではないからである。逆に、画面が必ず触る形——配列であるべき
 * ものが配列か、といったところ——は落ちる前に弾く。
 */
function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new Error(`Import failed: ${label} must be an object.`)
  }
  return value
}

function requireArray(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new Error(`Import failed: ${label} must be an array.`)
  }
  return value
}

/*
 * 封筒（version / kind）を先に照合する。ここで弾いておかないと、
 * Gate-awareの画面へPulseの結果を載せる、といった取り違えが起きる。
 */
function openEnvelope(text: string, expectedKind: string): Record<string, unknown> {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('Import failed: invalid JSON file.')
  }

  const bundle = requireRecord(parsed, 'result file')

  if (bundle.kind !== expectedKind) {
    /*
     * もう一方のモードの結果だと分かる場合は、そう言う。「壊れています」
     * より「こちらはPulseの結果です」のほうが、次にやることが分かる。
     */
    const otherKind = expectedKind === GATE_AWARE_RESULT_KIND
      ? PULSE_RESULT_KIND
      : GATE_AWARE_RESULT_KIND
    if (bundle.kind === otherKind) {
      throw new Error(
        expectedKind === GATE_AWARE_RESULT_KIND
          ? 'Import failed: this is a Pulse result. Open it from the Pulse state explorer.'
          : 'Import failed: this is a Gate-aware result. Open it from the Gate-aware state explorer.',
      )
    }
    throw new Error('Import failed: this file is not a Yuragi-Strider result.')
  }

  if (bundle.version !== 1) {
    throw new Error('Import failed: unsupported result file version.')
  }

  return bundle
}

export type ImportedGateAwareResult = {
  response: SimulationResponse
  circuitConfig: CircuitConfig
  gateDurationDefaults: GateDurationDefaults | null
  exportedAt: string | null
}

/*
 * Gate-awareの結果を読む。
 *
 * circuit_config が無いファイルは受け付けない。回路が無ければエディターへ
 * 書き戻せず、「画面の設定 = 図の条件」を保てないからである。書き出し側は
 * 回路が無いとき null を入れるので、その場合もここで弾かれる。
 */
export function parseGateAwareResultJson(text: string): ImportedGateAwareResult {
  const bundle = openEnvelope(text, GATE_AWARE_RESULT_KIND)
  const inputs = requireRecord(bundle.inputs, 'inputs')
  const response = requireRecord(bundle.result, 'result')

  if (inputs.circuit_config === null || inputs.circuit_config === undefined) {
    throw new Error(
      'Import failed: this result has no circuit, so it cannot be restored onto the editor.',
    )
  }
  const circuitConfig = requireRecord(inputs.circuit_config, 'inputs.circuit_config')

  /* 画面が必ず読む配列。欠けていると描画側で落ちる。 */
  requireArray(response.timeline, 'result.timeline')
  requireArray(response.state_snapshots, 'result.state_snapshots')
  requireRecord(response.parameters, 'result.parameters')
  requireRecord(response.output_probabilities, 'result.output_probabilities')

  const meta = isRecord(bundle.meta) ? bundle.meta : null

  return {
    response: response as unknown as SimulationResponse,
    circuitConfig: circuitConfig as unknown as CircuitConfig,
    gateDurationDefaults: isRecord(inputs.gate_duration_defaults)
      ? (inputs.gate_duration_defaults as unknown as GateDurationDefaults)
      : null,
    exportedAt: typeof meta?.exported_at === 'string' ? meta.exported_at : null,
  }
}

export type ImportedPulseResult = {
  record: PulseRunRecord
  circuit: PulseCircuitState
  exportedAt: string | null
}

/*
 * Pulseの結果を読む。
 *
 * 復元した記録の signature は、ファイルに入っていた実行時のものをそのまま
 * 使う。読み込み側で作り直すと、書き出した実行と同じ条件かどうかを
 * 照合できなくなる。呼び出し側は、この signature が現在の設定の指紋と
 * 一致するかを見て「古い結果」の判定をする。
 */
export function parsePulseResultJson(text: string): ImportedPulseResult {
  const bundle = openEnvelope(text, PULSE_RESULT_KIND)
  const inputs = requireRecord(bundle.inputs, 'inputs')
  const run = requireRecord(bundle.run, 'run')
  const response = requireRecord(bundle.result, 'result')

  const circuit = requireRecord(inputs.pulse_circuit, 'inputs.pulse_circuit')
  const labForm = requireRecord(inputs.lab_form, 'inputs.lab_form')

  requireArray(circuit.lanes, 'inputs.pulse_circuit.lanes')
  requireArray(response.trajectory, 'result.trajectory')
  requireRecord(response.model, 'result.model')

  if (typeof run.signature !== 'string' || run.signature.length === 0) {
    throw new Error('Import failed: run.signature is missing.')
  }
  if (typeof run.completed_at !== 'string') {
    throw new Error('Import failed: run.completed_at is missing.')
  }

  const meta = isRecord(bundle.meta) ? bundle.meta : null

  return {
    record: {
      response: response as unknown as PulseRunRecord['response'],
      formAtRun: labForm as unknown as PulseLabForm,
      completedAt: run.completed_at,
      signature: run.signature,
    },
    circuit: circuit as unknown as PulseCircuitState,
    exportedAt: typeof meta?.exported_at === 'string' ? meta.exported_at : null,
  }
}

/*
 * ファイルを読んで文字列にする。サイズ上限の判定はここでまとめて行う。
 */
export async function readResultFile(file: File): Promise<string> {
  if (file.size > MAX_RESULT_IMPORT_BYTES) {
    throw new Error(
      `Import failed: result files must be ${Math.floor(MAX_RESULT_IMPORT_BYTES / (1024 * 1024))} MiB or smaller.`,
    )
  }
  return file.text()
}
