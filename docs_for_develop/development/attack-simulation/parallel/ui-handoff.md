# UI部隊の引継ぎ

更新: 2026-10-04。状態: **画面・ローカル回路編集・型付きclient境界を実装／HTTP本接続はレビュー待ち**。製品のHTTP schema、設定既定値、管理者認証を確定していない。

## 成果物

- `/attack-simulator` とホームの共通メニューに導線を追加。環境設定と攻撃者workspaceを同じ画面内で切り替える。
- 自由／課題は同じ画面・clientを使う。三設定群の入力はサービスが公開するfield descriptorから生成する。課題のoverrideを拒否し、環境成立後は方式・preset・仮定を固定する。presetの実物や秘密入りmanifestをfrontendへ埋め込んでいない。
- 既存 `CircuitProvider` / `CircuitWorkspace` / Circuit Studio JSONを再利用。攻撃側エディターはGate-awareと別のstateを持つ。最大8量子ビット互換、仮想名256 UTF-8 bytes・16ファイル・回路64 KiBを既存境界に合わせて検査する。記号的なfolder名は許すがOSパスには解決しない。
- エディターの送信準備は共通 `program submit` を端末へ載せる。構文解析・認可・実行は共通backendへ委ねる。独自shell、攻撃者専用物理コマンド、UIの数値シミュレーションは追加しない。
- 初期clientは必ず未接続。画面確認は環境成立と区別し、ローカル下書きを登録済みjobとして見せない。課題判定、被害者、QPU、queue、countsを捏造しない。
- 成立とactor接続済みを分ける。`actorConnection=unconfigured`なら登録・コマンド送信を許さず、仮想資格情報の設定画面は接続待ちと明記する。実APIキー入力は用意していない。
- 画面移動ではsessionを失わないよう常駐する。隠れたeditorのDelete／Backspace／viewport hotkeyが現在の画面へ干渉しないよう既存keyboard listenerに可視性の境界を追加した。
- 10秒のclient応答上限はホストの待機打切りであり装置時間や取消ではない。自動再試行なし。成立／棄却の応答待ち切れは状態不明として操作を止め、失敗だけで設定unlockしない。端末表示は100 entry・各8192文字／応答50行までで、省略を表示する。

## 変更ファイル

新規:

- `frontend/src/pages/AttackSimulatorPage.tsx` / `.css`
- `frontend/src/components/AttackCircuitEditor.tsx`
- `frontend/src/types/attackSimulation.ts`
- `frontend/src/utils/attackWorkspace.ts`
- `frontend/src/validation/attackWorkspace.test.mjs`

既存の変更:

- `frontend/src/App.tsx`
- `frontend/src/components/AppNavigation.tsx`
- `frontend/src/components/CircuitWorkspace.tsx`
- `frontend/src/context/CircuitContext.tsx`
- `frontend/src/hooks/useCircuitViewport.ts`

この引継ぎ以外の設計正本、core、api、既存testは変更していない。依存追加、server起動、ネットワーク有効化、Git履歴・commit・pushも行っていない。並列のサービス部隊の変更は本部隊の成果物に含めない。

## 焦点検証

すべて `frontend/` から実行、2026-10-04。

- `npm run lint`: PASS。
- `npm run build`: PASS。687.03 kBのJS chunkに対する500 kB超警告あり。bundle分割・性能の受入判定は未実施。
- `node src/validation/attackWorkspace.test.mjs`: **14件PASS**。既存JSON往復、8 qubit入力、仮想名／UTF-8／件数／byte上限、入力拒否、Python `shlex`との引用互換、自由設定の型・範囲・刻み幅・課題override拒否、実React初期render、未接続拒否、内部エラー秘匿、timeoutとabortを確認した。
- `npm run validate:result-export`: PASS。
- `npm run validate:pulse-lab`: PASS。
- `git diff --check -- frontend`: PASS。

React renderはNodeのstatic markup検査であり、ブラウザの見た目・クリック・keyboard動作を検証した証拠ではない。今回の `.mjs` 焦点検証は個別コマンドで実行する（`npm run lint`の既存configはこの場所のplain JSを対象にしない）。実HTTP認証、ブラウザ一連操作、復旧、全suite、課題攻略、数値精度や最大RSSは未検証。

## 統合待ち

サービス部隊の `service-handoff.md` を読んだが、HTTP契約は採用していない。現在のHTTP候補は成立済みsessionへのactor操作のみで、catalog／環境成立／棄却／credential受渡し／提出の管理者bootstrapは未実装である。そのため単にfetchを足しても、画面から環境開始まで完結しない。

`AttackSimulationClient` はfrontendの**intent／public projectionの接続口案**でありHTTP schemaではない。統合担当がHTTP案をレビューした後、以下をadapterで対応させる必要がある。

1. 環境作成者の公開catalog・設定検証・成立／棄却と、攻撃者の認証済み操作を別の権限contextへ接続する。管理者権限をterminalへ流さない。
2. `putCircuit`をactor私有namespace、`executeCommand`を既存共通terminalへ接続する。HTTPのstructured public viewを表示用 `lines`へ変換する際にも内部情報を追加しない。未知のbackend例外は `AttackClientFailure` へ安全に投影する。
3. manual仮想資格情報、認証失効・期限、作成／棄却の冪等性と状態不明からの復旧を決める。現UIのtimeoutはserver処理を取り消さず、自動再試行もしない。表示の接続待ちを実装済みと扱わない。
4. 課題固有提出・公開reference workload・local analysis・観測来歴を共通基盤へ接続する。現端末表示は全結果を永続保存する分析機能ではない。表示省略を分析用dataの省略と同一視しない。
5. 本接続後にブラウザで成立→設定固定→回路登録→送信→状態／結果→取消／棄却を確認し、既存Gate-aware/Pulseと隠れたeditorのkeyboard回帰も実操作で確認する。

入力shotsの共通既定値は作っていない。Resetの32 shots/case等を一般基盤へ持ち込まず、採用profile／課題設定で解決する。認可・固定・秘匿の正本はbackendであり、frontendのdisabled表示を安全境界の証明とは扱わない。
