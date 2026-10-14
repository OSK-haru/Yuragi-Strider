import { useRef, useState } from 'react'
import { fileNameProblem, MAX_TOTAL_CHARS, totalChars, type ScriptFiles } from '../scripting/scriptFiles'

/*
 * スクリプトのファイルを書く場所。実行はターミナルの python コマンドで行う。
 * 実行ボタンは、そのコマンドをターミナルへ打つのと同じ。
 */
export function AttackScriptEditor({ value, onChange, running, available, onRun, onStop, saved }: {
  value: ScriptFiles
  onChange: (next: ScriptFiles) => void
  running: boolean
  /** 実行できない理由。null なら実行できる。 */
  available: string | null
  onRun: (file: string) => void
  onStop: () => void
  saved: boolean
}) {
  const [draftName, setDraftName] = useState<string | null>(null)
  const [problem, setProblem] = useState('')
  const releaseTab = useRef(false)
  const names = Object.keys(value.files)
  const text = value.files[value.active] ?? ''

  function edit(next: string) {
    const files = { ...value.files, [value.active]: next }
    if (totalChars(files) > MAX_TOTAL_CHARS) { setProblem('ファイルの合計が上限（512K文字）を超えます。'); return }
    setProblem('')
    onChange({ ...value, files })
  }
  function create() {
    const name = (draftName ?? '').trim()
    const issue = fileNameProblem(name, value.files)
    if (issue) { setProblem(issue); return }
    setProblem(''); setDraftName(null)
    onChange({ files: { ...value.files, [name]: '' }, active: name })
  }
  function remove() {
    if (names.length <= 1) { setProblem('最後のファイルは消せません。'); return }
    if (!window.confirm(`${value.active} を削除しますか？`)) return
    const files = { ...value.files }
    delete files[value.active]
    onChange({ files, active: Object.keys(files)[0] })
  }

  return <section className="attack-script" aria-label="スクリプト">
    <div className="attack-script__bar" role="toolbar" aria-label="スクリプトの操作">
      <div className="attack-script__files" role="tablist" aria-label="ファイル">
        {names.map((name) => <button key={name} type="button" role="tab" aria-selected={name === value.active}
          onClick={() => onChange({ ...value, active: name })}>{name}</button>)}
      </div>
      {draftName === null
        ? <button type="button" onClick={() => { setDraftName(''); setProblem('') }}>新しいファイル</button>
        : <form className="attack-script__new" onSubmit={(event) => { event.preventDefault(); create() }}>
          <input aria-label="新しいファイル名" value={draftName} placeholder="attack.py" autoFocus spellCheck={false}
            onChange={(event) => setDraftName(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Escape') { setDraftName(null); setProblem('') } }} />
          <button type="submit">作成</button>
          <button type="button" onClick={() => { setDraftName(null); setProblem('') }}>取消</button>
        </form>}
      <button type="button" onClick={remove} disabled={running}>削除</button>
      <span className="attack-script__spacer" />
      {running
        ? <button type="button" className="ide__danger" onClick={onStop}>停止（Ctrl+C）</button>
        : <button type="button" disabled={available !== null} onClick={() => onRun(value.active)}
          title={available ?? `ターミナルで python ${value.active} を実行`}>実行</button>}
    </div>
    {(problem || available) && <p className="attack-script__note" role="status">{problem || available}</p>}
    <textarea className="attack-script__text" aria-label={`${value.active} の内容`} value={text} spellCheck={false}
      autoCapitalize="off" autoComplete="off" wrap="off"
      onChange={(event) => edit(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === 'Escape') { releaseTab.current = true; return }
        const release = releaseTab.current
        releaseTab.current = false
        if (event.key !== 'Tab' || release || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return
        // Tab はフォーカス移動ではなく字下げ。抜けるときは Esc の後に Tab。
        event.preventDefault()
        const target = event.currentTarget
        const { selectionStart, selectionEnd } = target
        edit(`${text.slice(0, selectionStart)}    ${text.slice(selectionEnd)}`)
        requestAnimationFrame(() => target.setSelectionRange(selectionStart + 4, selectionStart + 4))
      }} />
    <small className="attack-script__footer">{saved ? 'このブラウザに保存しています。' : 'このブラウザに保存できません。画面を閉じると消えます。'}
      {' '}スクリプトはこのPCのブラウザの中で動き、使えるのはターミナルと同じ公開操作だけです。</small>
  </section>
}
