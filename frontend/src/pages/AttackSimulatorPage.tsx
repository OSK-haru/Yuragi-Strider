import { useEffect, useRef, useState } from 'react'
import { AttackCircuitEditor } from '../components/AttackCircuitEditor'
import { useCircuitContext } from '../context/useCircuitContext'
import type { GateDurationDefaults } from '../types/simulation'
import type {
  AssumptionGroup, AttackMode, AttackSimulationClient, CommandIntent, PublicAttackSession,
  PublicScenario, PublicSetting, SettingValue,
} from '../types/attackSimulation'
import { exportCircuitConfigBundleJson } from '../utils/circuitConfigTransfer'
import {
  AttackClientFailure, boundedClientCall, COMMON_COMMAND_HELP, disconnectedAttackClient,
  CommandRequestStore, MAX_TERMINAL_ENTRIES, programSubmitLine, sendCommandIntent, validateCommandLine,
  validateSetupOverrides, validateWorkspaceCircuit,
} from '../utils/attackWorkspace'
import './AttackSimulatorPage.css'

const settingGroups: Array<{ id: AssumptionGroup; label: string; empty: string }> = [
  { id: 'attacker', label: '攻撃者', empty: '接続方式・操作権限はサービスの公開設定から取得します。' },
  { id: 'victim', label: '被害者', empty: '課題の秘密や非公開設定は攻撃側に渡しません。' },
  { id: 'qpu_service', label: 'QPUサービス', empty: '実行方式・提供操作・公開範囲を成立前に確定します。' },
  { id: 'relation', label: '共有資源・経路', empty: '主体間の配置・共有経路は公開された条件だけを扱います。' },
  { id: 'experiment', label: '実験条件', empty: '反復・観測条件は公開設定から取得し、非公開の判定情報は渡しません。' },
]
type Entry = { id: number; origin: 'client' | 'service' | 'error'; text: string }

function SetupField({ setting, value, disabled, onChange }: {
  setting: PublicSetting; value: SettingValue; disabled: boolean
  onChange: (value: SettingValue) => void
}) {
  if (setting.kind === 'choice') {
    return <label>{setting.label}<select value={String(value)} disabled={disabled}
      onChange={(event) => onChange(event.target.value)}>
      {setting.choices.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
    </select></label>
  }
  if (setting.kind === 'boolean') {
    return <label className="attack-page__check"><input type="checkbox" checked={value === true}
      disabled={disabled} onChange={(event) => onChange(event.target.checked)} />{setting.label}</label>
  }
  return <label>{setting.label}<input type="number" value={typeof value === 'number' && !Number.isFinite(value) ? '' : String(value)} disabled={disabled}
    min={setting.min} max={setting.max} step={setting.step}
    onChange={(event) => onChange(event.target.valueAsNumber)} /></label>
}

export function AttackSimulatorPage({ gateDurationDefaults, client = disconnectedAttackClient }: {
  gateDurationDefaults: GateDurationDefaults; client?: AttackSimulationClient
}) {
  const editor = useCircuitContext()
  const [panel, setPanel] = useState<'setup' | 'workspace'>('setup')
  const [mode, setMode] = useState<AttackMode>('free')
  const [scenarios, setScenarios] = useState<PublicScenario[]>([])
  const [scenarioId, setScenarioId] = useState('')
  const [overrides, setOverrides] = useState<Record<string, SettingValue>>({})
  const [session, setSession] = useState<PublicAttackSession | null>(null)
  const [lifecycleUncertain, setLifecycleUncertain] = useState(false)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [notice, setNotice] = useState('')
  const [files, setFiles] = useState<Array<{ name: string; json: string }>>([])
  const [fileName, setFileName] = useState('circuit.json')
  const [device, setDevice] = useState('')
  const [shots, setShots] = useState('')
  const [line, setLine] = useState('help')
  const [entries, setEntries] = useState<Entry[]>([])
  const [requests] = useState(() => new CommandRequestStore())
  const [pendingCommand, setPendingCommand] = useState<CommandIntent | null>(null)
  const nextEntry = useRef(0)
  const outputRef = useRef<HTMLDivElement>(null)
  const scenario = scenarios.find((candidate) => candidate.id === scenarioId && candidate.mode === mode)
  const locked = session !== null || busy || lifecycleUncertain
  const actorReady = session?.actorConnection === 'ready'

  useEffect(() => () => requests.clear(), [requests])

  useEffect(() => {
    function warnBeforeUnload(event: BeforeUnloadEvent) {
      if (!session && !lifecycleUncertain && !busy) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warnBeforeUnload)
    return () => window.removeEventListener('beforeunload', warnBeforeUnload)
  }, [session, lifecycleUncertain, busy])
  useEffect(() => {
    outputRef.current?.scrollTo({ top: outputRef.current.scrollHeight })
  }, [entries])

  function record(origin: Entry['origin'], text: string) {
    const entry = { id: ++nextEntry.current, origin,
      text: text.length <= 8192 ? text : `${text.slice(0, 8192)}\n[表示上限のため末尾を省略]` }
    setEntries((current) => [...current, entry].slice(-MAX_TERMINAL_ENTRIES))
  }
  async function perform(action: () => Promise<void>, lifecycle = false) {
    if (busyRef.current || lifecycleUncertain) return
    busyRef.current = true
    setBusy(true)
    setNotice('')
    try {
      await action()
    } catch (error) {
      if (lifecycle && error instanceof AttackClientFailure && error.uncertain) setLifecycleUncertain(true)
      // Transport failures are sanitized by boundedClientCall. Local validation is safe.
      const message = error instanceof Error ? error.message : '操作に失敗しました。'
      setNotice(message.slice(0, 1024))
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }
  async function loadScenarios() {
    await perform(async () => {
      const catalog = await boundedClientCall((signal) => client.listScenarios(signal))
      setScenarios(catalog)
      setScenarioId('')
      setOverrides({})
    })
  }
  async function establish() {
    if (!scenario || session || !client.connected || lifecycleUncertain) return
    await perform(async () => {
      validateSetupOverrides(scenario, overrides)
      const established = await boundedClientCall((signal) => client.establish({
        scenarioId: scenario.id, mode: scenario.mode, overrides,
      }, signal), undefined, true)
      setSession(established)
      requests.clear()
      setPendingCommand(null)
      setFiles([])
      setEntries([])
      setPanel('workspace')
    }, true)
  }
  async function discard() {
    if (!session || lifecycleUncertain) return
    await perform(async () => {
      requests.clear()
      setPendingCommand(null)
      await boundedClientCall((signal) => client.discard(session.id, signal), undefined, true)
      setSession(null)
      setFiles([])
      setEntries([])
      setPanel('setup')
      setNotice('サービスが環境の棄却を確認しました。回路エディターはローカルの下書きとして残ります。')
    }, true)
  }
  async function storeCircuit() {
    await perform(async () => {
      const json = exportCircuitConfigBundleJson(editor.circuitState)
      validateWorkspaceCircuit(fileName, json, files.map((file) => file.name))
      if (session) {
        if (!actorReady) throw new Error('仮想接続プロファイルが未設定です。workspaceへの登録はできません。')
        requests.workspaceChanged()
        await boundedClientCall((signal) => client.putCircuit(session.id, fileName, json, signal))
      }
      setFiles((current) => [...current.filter((file) => file.name !== fileName), { name: fileName, json }])
      setNotice(session ? '攻撃者workspaceへの登録を確認しました。' : 'ローカル下書きに保存しました。サービスへの登録・送信はしていません。')
    })
  }
  async function execute(command: string, retry = false) {
    await perform(async () => {
      validateCommandLine(command)
      record('client', `$ ${command}`)
      if (!session || !actorReady) {
        record('error', '環境未成立、または仮想接続が未設定です。コマンドはサービスへ送信されていません。')
        return
      }
      const intent = retry ? requests.retry(session.id, command, files) : requests.begin(session.id, command, files)
      try {
        const lines = await sendCommandIntent(client, session.id, intent, requests)
        // The adapter must supply only the authenticated public projection.
        record('service', lines.slice(0, 50).join('\n') + (lines.length > 50 ? '\n[表示上限のため残りの行を省略]' : ''))
      } finally {
        setPendingCommand(requests.unresolved(session.id))
      }
    })
  }

  return <main className="attack-page">
    <header className="attack-page__header">
      <div><p className="attack-page__eyebrow">SHARED SERVICE / ATTACK WORKSPACE</p>
        <h1>攻撃シミュレーター</h1>
        <p>{session ? session.publicNotice : '設定は成立前に確定。操作は攻撃側の共通クライアントから行います。'}</p>
      </div>
      <span className="attack-page__status" role="status">{session ? actorReady ? '環境成立・設定固定' : '環境成立・仮想接続待ち' : client.connected ? '環境未成立' : 'HTTP未接続'}</span>
    </header>
    {!client.connected && <p className="attack-page__warning">画面・回路編集のみ確認できます。仮想環境、ジョブ実行、課題判定はまだ接続されていません。</p>}
    {lifecycleUncertain && <p className="attack-page__warning" role="alert">環境の状態確認が必要です。重複作成や再設定を防ぐため操作を止めています。復旧用のサービス契約は接続待ちです。</p>}
    {session && !actorReady && <p className="attack-page__warning">仮想資格情報の設定操作は接続待ちです。実在のAPIキーや秘密をここへ入力しないでください。</p>}
    <div className="attack-page__tabs" aria-label="攻撃シミュレーターの画面">
      <button type="button" disabled={busy} aria-pressed={panel === 'setup'} onClick={() => setPanel('setup')}>環境設定</button>
      <button type="button" disabled={busy} aria-pressed={panel === 'workspace'} onClick={() => setPanel('workspace')}>
        {session ? '攻撃側ワークスペース' : 'ワークスペースを確認（未成立）'}
      </button>
    </div>
    {notice && <p className="attack-page__notice" role="status">{notice}</p>}
    <section hidden={panel !== 'setup'} aria-label="環境成立前の設定">
      <div className="attack-page__panel attack-page__setup-header">
        <label>方式<select value={mode} disabled={locked} onChange={(event) => {
          setMode(event.target.value as AttackMode); setScenarioId(''); setOverrides({})
        }}><option value="free">自由モード</option><option value="graded">課題モード</option></select></label>
        <label>設定プリセット<select value={scenarioId} disabled={locked || !client.connected} onChange={(event) => {
          setScenarioId(event.target.value); setOverrides({})
        }}><option value="">{scenarios.length ? '選択してください' : 'サービスから未取得'}</option>
          {scenarios.filter((candidate) => candidate.mode === mode).map((candidate) =>
            <option key={candidate.id} value={candidate.id}>{candidate.title}</option>)}
        </select></label>
        <button type="button" disabled={locked || !client.connected} onClick={() => void loadScenarios()}>公開設定を取得</button>
      </div>
      {scenario && <p>{scenario.description}</p>}
      <div className="attack-page__settings">
        {settingGroups.map((group) => <fieldset className="attack-page__panel" key={group.id} disabled={locked}>
          <legend>{group.label}</legend>
          {scenario?.settings.some((setting) => setting.group === group.id)
            ? scenario.settings.filter((setting) => setting.group === group.id).map((setting) => <SetupField
              key={setting.id} setting={setting} value={overrides[setting.id] ?? setting.value}
              disabled={locked || mode === 'graded' || !setting.editable}
              onChange={(value) => setOverrides((current) => ({ ...current, [setting.id]: value }))} />)
            : <p>{group.empty}</p>}
        </fieldset>)}
      </div>
      <p className="attack-page__subtle">課題はプリセットで固定。共有経路・実験条件は対応する公開設定とともにサービス側で検査します。</p>
      {session ? <button type="button" disabled={busy || lifecycleUncertain} onClick={() => void discard()}>環境を棄却して再設定</button>
        : <button type="button" disabled={locked || !scenario || !client.connected} onClick={() => void establish()}>検査して環境を成立</button>}
    </section>
    <section hidden={panel !== 'workspace'} aria-label="攻撃者ワークスペース">
      <div className="attack-page__workspace-header">
        <h2>{session ? session.title : '回路・端末の準備'}</h2>
        <p>{session ? '設定は固定されています。ページ移動だけでは環境を棄却しません。' : '未成立：被害者、QPU、キューは動作していません。'}</p>
      </div>
      <div id="attack-circuit-editor"><AttackCircuitEditor gateDurationDefaults={gateDurationDefaults} /></div>
      <div className="attack-page__panel attack-page__filebar">
        <label>仮想ファイル名<input value={fileName} maxLength={256} disabled={busy} onChange={(event) => setFileName(event.target.value)} /></label>
        <button type="button" disabled={busy || lifecycleUncertain || (!!session && !actorReady)} onClick={() => void storeCircuit()}>{session ? 'workspaceに登録' : '下書きに保存'}</button>
        <p>JSONは既存Circuit Studio形式。編集内容は保存した回路へ自動反映されません。</p>
        <ul aria-label="この攻撃者の回路ファイル">{files.map((file) => <li key={file.name}>
          <button type="button" disabled={busy} onClick={() => setFileName(file.name)}>{file.name}</button>
          <small>{session ? '登録済み' : 'ローカル下書き'} · {new TextEncoder().encode(file.json).length} bytes</small>
        </li>)}</ul>
      </div>
      <div className="attack-page__panel attack-page__submit">
        <label>実行先<input value={device} disabled={busy} placeholder="device list で照会" onChange={(event) => setDevice(event.target.value)} /></label>
        <label>shots<input type="number" min={1} step={1} value={shots} disabled={busy} onChange={(event) => setShots(event.target.value)} /></label>
        <button type="button" disabled={busy || lifecycleUncertain || !actorReady || !files.some((file) => file.name === fileName)} onClick={() => {
          try { setLine(programSubmitLine(fileName, device, Number(shots))) }
          catch (error) { setNotice(error instanceof Error ? error.message : '送信要求を作れません。') }
        }}>共通送信コマンドを端末へ</button>
      </div>
      <section className="attack-page__panel attack-page__terminal" aria-label="共通コマンドターミナル">
        <div className="attack-page__terminal-heading"><h2>ターミナル</h2><span>利用者クライアント · OS shellではありません</span></div>
        <details><summary>共通コマンド</summary><pre>{COMMON_COMMAND_HELP.join('\n')}</pre></details>
        <div className="attack-page__output" role="log" aria-label="コマンドの応答" ref={outputRef}>
          {entries.length ? entries.map((entry) => <pre key={entry.id} className={`attack-page__entry--${entry.origin}`}>{entry.text}</pre>)
            : <p>観測結果はありません。密度行列・内部Reset成否・被害者秘密はここへ表示しません。</p>}
        </div>
        {pendingCommand && <p role="status">前の要求は受付未確定です。新規要求は別ジョブになり得ます。明示した --request-id は新規送信でも維持されます。</p>}
        <form onSubmit={(event) => { event.preventDefault(); void execute(line) }}>
          <label className="attack-page__command">コマンド<input value={line} maxLength={4096} disabled={busy}
            autoComplete="off" spellCheck={false} onChange={(event) => setLine(event.target.value)} /></label>
          <button type="submit" disabled={busy || lifecycleUncertain || (!!session && !actorReady)}>{session ? pendingCommand ? '新規要求として送信' : '送信' : '未成立の状態を確認'}</button>
          {pendingCommand && <button type="button" disabled={busy || lifecycleUncertain || !actorReady}
            onClick={() => void execute(line, true)}>同一要求を再送</button>}
        </form>
      </section>
      {session?.mode === 'graded' && <section className="attack-page__panel"><h2>提出</h2><p>課題固有の公開提出契約は接続待ちです。判定結果は生成しません。</p></section>}
    </section>
  </main>
}
