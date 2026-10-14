/*
 * スクリプト実行用 Worker。利用者の Python を Pyodide（WebAssembly）で動かす。
 *
 * 画面のスレッドとは別に動き、画面の DOM・cookie・資格情報には触れない。サービスの
 * 操作は `_attack_bridge` 経由で画面に頼み、応答は syncChannel で同期に受け取る。
 * Pyodide は一度読み込んだら使い回し、実行ごとに作業ディレクトリと利用者の
 * モジュールを入れ替える。
 */

import type { PyodideAPI } from 'pyodide'
import type { RunRequest, WorkerMessage } from './scriptProtocol'
import { blockingSleep, waitForResponse } from './syncChannel'

declare const __PYODIDE_VERSION__: string

type WorkerScope = {
  postMessage(message: WorkerMessage): void
  addEventListener(type: 'message', listener: (event: MessageEvent<RunRequest>) => void): void
  location: { origin: string }
}
const scope = self as unknown as WorkerScope

/* 本体（pyodide.mjs・wasm・標準ライブラリ）はこのアプリが配信し、numpy 等の追加
 * パッケージだけを公式CDNから読む。各パッケージは同梱の lock ファイルの SHA-256 で検査される。 */
const INDEX_URL = new URL(`${import.meta.env.BASE_URL}assets/python-runtime/`, scope.location.origin).href
const PACKAGE_BASE_URL = `https://cdn.jsdelivr.net/pyodide/v${__PYODIDE_VERSION__}/full/`
const SDK_ROOT = '/home/pyodide/sdk'
const WORKSPACE = '/home/pyodide/workspace'
const INTERRUPTED = JSON.stringify({ error: { code: 'INTERRUPTED', message: 'KeyboardInterrupt' } })

const RUNNER = `
import builtins, json, os, runpy, sys, traceback
import _attack_bridge


def _attack_input(prompt=""):
    """input() を画面のターミナルにつなぐ。待っている間の Ctrl+C は KeyboardInterrupt。"""
    sys.stdout.flush()
    outcome = json.loads(_attack_bridge.call("stdin", json.dumps({"prompt": str(prompt)})))
    error = outcome.get("error")
    if error and error.get("code") == "INTERRUPTED":
        raise KeyboardInterrupt
    line = (outcome.get("value") or {}).get("line")
    if error or line is None:
        raise EOFError("EOF when reading a line")
    return line


builtins.input = _attack_input


def _attack_main(path, workspace):
    try:
        runpy.run_path(path, run_name="__main__")
        return 0
    except SystemExit as error:
        if error.code is None:
            return 0
        if isinstance(error.code, int):
            return error.code
        print(error.code, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        frames = traceback.extract_tb(error.__traceback__)
        # 実行の仕組み（runpy）の行は見せず、利用者のファイルからの行だけを残す。
        start = next((i for i, f in enumerate(frames) if f.filename.startswith(workspace + "/")), 0)
        print("Traceback (most recent call last):", file=sys.stderr)
        print("".join(traceback.format_list(frames[start:])), end="", file=sys.stderr)
        print("".join(traceback.format_exception_only(error)), end="", file=sys.stderr)
        return 1


def _attack_run(path, argv, workspace):
    for name, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None) or ""
        if origin.startswith(workspace + "/"):
            del sys.modules[name]
    os.chdir(workspace)
    sys.argv = [path, *argv]
    if workspace not in sys.path:
        sys.path.insert(0, workspace)
    # 例外の表示中に届いた Ctrl+C もここで受ける。
    try:
        return _attack_main(path, workspace)
    except KeyboardInterrupt:
        print("KeyboardInterrupt", file=sys.stderr)
        return 130
`

let runtime: Promise<PyodideAPI> | null = null
let current: { runId: number; callId: number; channel: SharedArrayBuffer; interrupt: Uint8Array } | null = null
const sleeper = new Int32Array(new SharedArrayBuffer(4))

function post(message: WorkerMessage) { scope.postMessage(message) }

/* 中断を見つけたら、こちらで KeyboardInterrupt を起こすので Pyodide 側の要求は消す。 */
function interrupted(): boolean {
  if (!current || current.interrupt[0] === 0) return false
  current.interrupt[0] = 0
  return true
}

/* 画面への同期呼出し。中断の確認を挟みながら止まって待つ。 */
function call(method: string, argsJson: string): string {
  const run = current
  if (!run) return JSON.stringify({ error: { code: 'CLOSED', message: '実行中ではありません。' } })
  let args: unknown
  try { args = JSON.parse(argsJson) } catch {
    return JSON.stringify({ error: { code: 'INVALID', message: '引数を読めません。' } })
  }
  run.callId += 1
  post({ type: 'call', runId: run.runId, callId: run.callId, method, args })
  const outcome = waitForResponse(run.channel, run.callId, () => {
    if (interrupted()) throw new Error('interrupted')
  })
  return JSON.stringify(outcome)
}

/* 待っている間に中断されたら、Python 側で KeyboardInterrupt にする印を返す。 */
function callSafely(method: string, argsJson: string): string {
  try { return call(method, argsJson) } catch { return INTERRUPTED }
}

function sleep(milliseconds: number): boolean {
  try {
    blockingSleep(sleeper, milliseconds, () => { if (interrupted()) throw new Error('interrupted') })
    return false
  } catch { return true }
}

async function load(runId: number): Promise<PyodideAPI> {
  post({ type: 'phase', runId, phase: 'loading', detail: 'Python を読み込んでいます…' })
  const module = await import(/* @vite-ignore */ `${INDEX_URL}pyodide.mjs`) as typeof import('pyodide')
  const pyodide = await module.loadPyodide({ indexURL: INDEX_URL, packageBaseUrl: PACKAGE_BASE_URL })
  pyodide.registerJsModule('_attack_bridge', {
    call: callSafely,
    sleep: (milliseconds: number) => sleep(milliseconds),
  })
  pyodide.runPython(RUNNER)
  return pyodide
}

function resetDirectory(pyodide: PyodideAPI, root: string, files: Record<string, string>) {
  const fs = pyodide.FS
  // 前の実行の作業ディレクトリにいたままだと消せないので、先に出る。
  pyodide.runPython(`import shutil, os\nos.chdir("/")\nshutil.rmtree(${JSON.stringify(root)}, ignore_errors=True)\nos.makedirs(${JSON.stringify(root)}, exist_ok=True)`)
  for (const [name, text] of Object.entries(files)) {
    const parts = name.split('/')
    let directory = root
    for (const part of parts.slice(0, -1)) {
      directory = `${directory}/${part}`
      if (!fs.analyzePath(directory).exists) fs.mkdir(directory)
    }
    fs.writeFile(`${root}/${name}`, text)
  }
}

scope.addEventListener('message', (event) => {
  const request = event.data
  if (request?.type !== 'run') return
  void run(request)
})

async function run(request: RunRequest) {
  const { runId } = request
  try {
    const firstLoad = runtime === null
    runtime ??= load(runId)
    const pyodide = await runtime
    if (firstLoad) {
      resetDirectory(pyodide, SDK_ROOT, request.sdk)
      pyodide.runPython(`import sys\nsys.path.insert(0, ${JSON.stringify(SDK_ROOT)})`)
    }
    const interrupt = new Uint8Array(request.interrupt)
    current = { runId, callId: 0, channel: request.channel, interrupt }
    pyodide.setInterruptBuffer(interrupt)
    pyodide.setStdout({ batched: (text: string) => post({ type: 'output', runId, stream: 'stdout', text }) })
    pyodide.setStderr({ batched: (text: string) => post({ type: 'output', runId, stream: 'stderr', text }) })
    pyodide.setStdin({ stdin: () => {
      const reply = callSafely('stdin', '{}')
      // input() 中の Ctrl+C は、入力の終わりではなく KeyboardInterrupt にする。
      if (reply === INTERRUPTED) { interrupt[0] = 2; return null }
      const line = (JSON.parse(reply) as { value?: { line?: string | null } }).value?.line
      return typeof line === 'string' ? `${line}\n` : null
    } })
    resetDirectory(pyodide, WORKSPACE, request.files)

    post({ type: 'phase', runId, phase: 'packages' })
    await pyodide.loadPackagesFromImports(Object.values(request.files).join('\n'), {
      messageCallback: (message: string) => post({ type: 'phase', runId, phase: 'packages', detail: message }),
      errorCallback: (message: string) => post({ type: 'output', runId, stream: 'stderr', text: message }),
    })
    post({ type: 'phase', runId, phase: 'running' })
    const runner = pyodide.globals.get('_attack_run')
    const code = runner(`${WORKSPACE}/${request.entry}`, pyodide.toPy(request.argv), WORKSPACE) as number
    runner.destroy()
    post({ type: 'exit', runId, code })
  } catch (error) {
    if (runtime && !(await runtime.then(() => true, () => false))) runtime = null
    post({ type: 'fatal', runId, message: error instanceof Error ? error.message : String(error) })
  } finally {
    current = null
  }
}
