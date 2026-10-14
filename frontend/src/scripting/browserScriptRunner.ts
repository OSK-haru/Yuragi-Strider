import { ATTACK_CLIENT_SDK } from './pythonSdk'
import { createScriptRunner, type ScriptRunnerEvents } from './scriptRunner'
import { createChannelBuffer, createResponder } from './syncChannel'

export { STARTER_FILES } from './pythonSdk'

/*
 * このページで使う実行窓口。Worker と SDK を結び付けるだけで、判断は scriptRunner に置く。
 * import.meta（Worker の URL と SDK の取り込み）を使うのはここと pythonSdk だけで、
 * 画面はこのモジュールを後から動的に読む。
 */
export function createBrowserScriptRunner(events: ScriptRunnerEvents) {
  return createScriptRunner(events, {
    createWorker: () => new Worker(new URL('./pythonWorker.ts', import.meta.url), { type: 'module', name: 'python-runtime' }),
    sdk: ATTACK_CLIENT_SDK,
    createChannel: () => createChannelBuffer(),
    createResponder,
  })
}
