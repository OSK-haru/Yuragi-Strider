/*
 * スクリプトのファイル置き場。このブラウザの localStorage にだけ保存する。
 * 保存できない（プライベートウィンドウ等）ときも、画面を開いている間は使える。
 * サーバーへは送らない。
 * このファイルは実行時の import を持たない（Node の検証から直接読み込む）。
 */

export type ScriptFiles = { files: Record<string, string>; active: string }

const STORAGE_KEY = 'attack-scripts-v1'
export const MAX_FILES = 32
export const MAX_TOTAL_CHARS = 512 * 1024
const FILE_NAME = /^[A-Za-z_][A-Za-z0-9_-]{0,47}\.py$/

export function fileNameProblem(name: string, existing: Record<string, string>): string | null {
  if (!FILE_NAME.test(name)) return 'ファイル名は英字か _ で始め、英数字・_・- で書いて .py で終えてください（例: attack.py）。'
  if (name in existing) return `${name} はもうあります。`
  if (Object.keys(existing).length >= MAX_FILES) return `ファイルは${MAX_FILES}個までです。`
  return null
}

export function totalChars(files: Record<string, string>): number {
  return Object.values(files).reduce((sum, text) => sum + text.length, 0)
}

/** 最初に置くファイル。保存が無いときに使う。 */
export function defaultScriptFiles(starter: Record<string, string>): ScriptFiles {
  return { files: { ...starter }, active: Object.keys(starter)[0] ?? 'main.py' }
}

/** 保存したファイル。無い・読めない・壊れているときは null。 */
export function loadScriptFiles(storage: Pick<Storage, 'getItem'> | null): ScriptFiles | null {
  try {
    const raw = storage?.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return null
    const { files, active } = parsed as { files?: unknown; active?: unknown }
    if (typeof files !== 'object' || files === null) return null
    const valid = Object.fromEntries(Object.entries(files as Record<string, unknown>)
      .filter((entry): entry is [string, string] => FILE_NAME.test(entry[0]) && typeof entry[1] === 'string')
      .slice(0, MAX_FILES))
    const names = Object.keys(valid)
    if (!names.length || totalChars(valid) > MAX_TOTAL_CHARS) return null
    return { files: valid, active: typeof active === 'string' && active in valid ? active : names[0] }
  } catch {
    return null
  }
}

/** 保存できたかを返す。失敗しても画面上のファイルは残る。 */
export function saveScriptFiles(storage: Pick<Storage, 'setItem'> | null, value: ScriptFiles): boolean {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(value))
    return storage !== null
  } catch {
    return false
  }
}

export function browserStorage(): Storage | null {
  try { return globalThis.localStorage ?? null } catch { return null }
}
