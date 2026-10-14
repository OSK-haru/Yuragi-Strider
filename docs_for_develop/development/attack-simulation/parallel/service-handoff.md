# サービス部隊の引継ぎ／HTTP契約案

更新: 2026-10-04。**統合レビュー待ちのadapter案**。この部隊で製品のHTTP契約、管理者認証、session既定上限を確定していない。`api/main.py`にはmountしておらず、現在の公開アプリに新しい経路を追加していない。後続の統合修正でmanifest／認可／compiler／executorの一致、単一session所有、計算と参加者操作のlock分離、terminal transportの固定IDを接続した。初回検証履歴と最新の検証を区別し、[共通コマンドclient](../common-command-client.md)を参照する。

## 成果物

- `api/attack_sessions.py`: 成立済み`ScenarioEnvironment`と既存service／仮想資格情報／信頼されたexecutorのプロセス内binding。actor別`TerminalWorkspace`、受付と物理executorを分けた排他、明示的ホスト上限、終了時の失効とbinding解放。
- `api/attack_http.py`: 明示的にregistryを渡す`create_attack_router(registry)`。既存serviceのdevice／program／job操作と既存`TerminalSession`を呼ぶ。actorやReset専用の物理操作を追加しない。
- `tests/test_attack_sessions.py`、`tests/test_attack_http_adapter.py`: 実compiler・FIFO・QPU状態・結果投影までをASGI経由で検査。socket／実ネットワーク／httpxを使わず、依存を追加していない。

`core/`、`api/attack_terminal.py`、既存test、設計正本、UIは変更していない。

## HTTP候補（未採用）

prefix: `/api/attack/sessions/{session_id}`。全経路で`Authorization: Bearer <simulator-issued virtual credential>`が必要。実在serviceのAPIキーを受け入れる機能ではない。値をURL／manifest／応答／例に記載しない。参加主体は資格情報から解決し、bodyのprincipal／tenant／role／配置／seedは拒否する。

- `GET /devices` → 公開`DeviceInfo`の配列。
- `GET /devices/{device_id}` → 公開`DeviceInfo`。
- `POST /programs` → 202、`JobStatusView`。bodyは`{device_id, shots, circuit_config}`のみ。回路は現editor JSON境界で再検証する。`circuit_config`にはraw configか既存export wrapperを指定できる。
- `GET /jobs/{job_id}` → `JobStatusView`。
- `GET /jobs/{job_id}/result` → `JobResultView`。成功sampling結果は`{shots, counts}`のみ。真値・内部Reset成否・イベント履歴・他tenant情報を返さない。
- `POST /jobs/{job_id}/cancel` → `JobStatusView`。bodyなし。既存`queued_only`に従う。
- `POST /workspace/circuits` → 204。bodyは`{name, circuit_json}`。認証主体専用の記号的namespaceへeditorファイルを登録し、ホストファイルへ解決しない。
- `POST /terminal` → 共通CLIのpublic viewまたはhelp配列。bodyは`{line}`。既存構文をそのまま使用する。統合修正で任意の`Idempotency-Key` headerをfallbackとして追加。重複／不正headerを拒否し、`program submit`だけが明示`--request-id`→header→新規UUIDの順に解決する。他コマンドの意味や文法は変えない。

公開job viewは`job_id`, `status`, `error_code`、結果viewは追加の`result`を持つ。statusは既存enumの小文字値。JSON応答の具体的な版・型宣言は統合レビュー後にUI部隊と合わせる。

送信の冪等性キーは`Idempotency-Key` header。省略時に新規生成するため、通信失敗後も同一jobへ再試行したいclientは最初から固定キーを指定する。同じprincipalの同一キーは既存compiler・認可・内容照合を再通過する。端末の`--request-id`と同じscope／経路に入る。変更した内容での再利用は422で拒否する現候補であり、409等へのerror taxonomy整理はレビュー対象。

bodyは受信中に64 KiBを検査する。`extra=forbid`／strict型、重複JSON key、非有限値を拒否し、検証エラーに入力を転載しない。全HTTP envelopeの上限なので、JSON文字列として回路ファイルを二重encodingすると元ファイル64 KiB未満でもHTTP上限に達し得る。

error候補は`{detail: {code}}`。未認証／未知環境／終了済み環境は401 `UNAUTHENTICATED`。認可拒否403 `OPERATION_DENIED`。不可視／不存在jobは同じ404 `JOB_NOT_FOUND`。未完了結果409 `RESULT_NOT_READY`。入力や未対応操作422。上限413 `REQUEST_TOO_LARGE`、ホスト収容上限429 `HOST_CAPACITY_REACHED`、計算中のadapter503 `HOST_BUSY`、予期しない内部例外500 `INTERNAL_ERROR`。本文や内部例外を返さない。

## 環境・実行管理の境界

1. 信頼された設定管理が準備中の`ScenarioEnvironment`へ許された自由設定を適用する。課題は固定presetを使う。
2. 設定検証と`establish()`でmanifestを確定する。
3. 信頼されたassemblerがそのmanifestから一致したservice／authority／runtime／executorを作り、`AttackSession`とregistryへ登録する。sessionはserviceのmanifest ID、認可／compilerの解決結果、executorのruntime identityを検査する。service／runtime／authorityのsession重複利用と未知principalへの発行を拒否する。未宣言の物理設定をmanifestから証明する検査ではなく、このadapter自体はgeneric RuntimePlanを組み立てない。
4. 仮想資格情報の発行・preconfigured／manual受渡しは既存の仮定境界に従う。**HTTPのbootstrapや設定管理者の認証は未実装**。被害者資格情報を攻撃側へ渡さない。
5. 参加者HTTPは共通操作だけを行う。照会も送信も実行を進めず、信頼されたsupervisorだけが`AttackSession.run_next()`を呼ぶ。`run_next`のHTTP／terminalコマンドは存在しない。
6. 信頼された管理者がregistryの`discard()`を呼ぶ。新規参加者操作を止めて双方の資格情報を失効し、現在のexecutor終了を受付lockなしで待ってworkspace／受付ledger／service／executorへのbindingを解放する。戻せるmanifestに変更したり、実行済みshotを巻き戻したり、失敗時に理想Resetを足したりしない。

環境のcreate／設定変更／start／stop／credential発行／grader提出の公開routeは未決定の管理者認証と統合を待つ。この候補を、UIで成立から終了まで完結するシステムと扱わない。

## 上限・負荷・未接続事項

- registryの`max_sessions`、`SessionHostLimits(max_jobs, max_workspaces)`は信頼された呼出元の**必須引数**。製品既定値を定義していない。max_jobsはsession生涯の成功受付キー数で、取消・完了しても枠を戻さず保持量を限定する。同じキーの再送は上限到達後も既存照合を通す。失敗受付は枠を消費しない。
- これはホスト保護であり`ObservationBudget`の代替ではない。課題固有予算の消費やhidden判定に結びつけていない。既存Reset課題のcompilerを有効化して漏洩経路や予算を迂回することは禁止する。
- 参加者操作と受付ledgerはcontrol lock、物理実行は別のexecutor gate、FIFO記録は独自の短いlockで保護する。数値計算中という理由だけではHTTPを503へ変換しない。受付lockには現在compileも含まれるため、compile前receipt、compile完了順に依存しない受付順序、正確なリアルタイム観測とlatency保証は未達。503 `HOST_BUSY`の候補変換は残るが通常の参加者要求を数値計算中に拒否する動作は除去した。ホスト時間を仮想durationへ加えない。
- callbackによる実行を受け取るだけなので、assemblerは全参加者の受付を同じadapterへ通し、他経路の内部workloadにも整合した総量管理を適用する必要がある。これを外して直接service/runtimeを呼ぶ内部コードの量をこの上限では管理できない。
- 同一manifest／QPU／runtime／資格情報の整合を確認するfactory、session管理者の認証、session全体のshots／event／RSS上限、expiry、永続化、checkpoint、終了中の取消・復旧、複数worker／processのstate所有は未接続。呼出元が持つ別のruntime参照をこのadapterだけで解放できるわけではない。
- protocolに用意した境界は自由／課題共通だが、Reset提出／training／hidden、動的配置、同時物理干渉、任意コードはこの成果物に含まれない。OS sandboxや実QPU service互換を主張しない。

## 検証

初回焦点検証: `tests.test_attack_sessions tests.test_attack_http_adapter`の11件成功、0.424秒。

回帰を含む最終検証: 下記38件成功、0.406秒（2026-10-04）。内部例外の秘匿を含む。先行回帰は37件成功、0.444秒。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_attack_sessions tests.test_attack_http_adapter tests.test_common_command_pipeline tests.test_attack_terminal tests.test_job_application_service tests.test_scenario_credential_lifecycle
```

確認範囲: 2量子ビットBell共同counts、非同期受付、照会無副作用、端末／structured送信の共通冪等性、別tenantの不可視job、主体body拒否、actor別workspace、queued取消、設定凍結、終了失効、明示上限、busy、strict型／分割受信上限／重複認証／重複JSON key、認可拒否。

未検証: 本番mount、実ネットワーク、ブラウザ、複数process、総RSS／latency、全test suite、実機／新しい物理精度。

## 後続統合: 内部receipt記録

親側で`RequestReceiptLedger`をsession／HTTP候補へ追加した。threadpoolと回路変換の前に内部host時刻・順序を記録し、pending／history上限を`SessionHostLimits.receipt_limits`として必須化した。上記のcompile前receipt未実装という記載はこの追加前のsnapshot。到着順の記録とFIFO受理順の強制は別で、後者とcompileのcontrol lock外への分離は残る。正本は[共通コマンドclient](../common-command-client.md#内部受付記録2026-10-04)。復旧と管理者／サービス発行の責務はD51/D52に従う。

## 後続統合: 受付順の受理とcompile分離

上記のreceipt順／compile lock未実装は以前のsnapshot。親側で`request_async()`のworker前FIFO gate、短いcontrol同期とlock外prepare／再認可admit、jobの最初のreceipt保持、close時の準備drainを追加した。送信／workspace更新は順序を守り、照会／queued取消はcompileを待たない。async窓口を内部clientでも使えるが、被害者runner自律進行・Reset予算移行は未接続。gate容量はpending receiptに含み、host時刻をQPU時間へ読み替えない。同期`request()`を有限worker poolへ逆順投入する使い方は避ける。最新の証拠・未完了・次作業は[進捗](../progress.md)、境界は[共通client](../common-command-client.md#受付順の受理compile分離2026-10-04後続統合)を参照。

## 残る統合判断

HTTP候補のpath／body／error taxonomyをレビューする。次に管理者／参加者のWeb認証とbootstrap、明示ホスト上限の採用値、RuntimePlanのbinding整合／資源管理を確定し、初めてmainへのmountとUI本接続を行う。未決定をこの部隊で製品仕様へ昇格させていない。

後続でmanifest成立を原子的に同期し、read-onlyなauthoring session、成立処理中の編集／再entrant成立拒否、失敗後の再編集を追加した。`event_commit_boundary`は明示world時刻列だけを検査する純粋部品で、host receiptのmappingやlive horizonを生成しない。world目標と確定位置の分離／追付き方式はD53で採用し、ホスト性能による架空現象を防ぐ条件と遅延注入の受入項目を記録した。live接続・検証は未完了。原点・対応倍率・通信境界・遅れ／未処理量の許容値、被害者周期・待機物理条件、公開時刻は判断待ち。詳細は[時間境界 §4.3](../runtime-boundaries-and-resolutions.md#43-時間接続の前提検査と判断待ち2026-10-04)。成立後に設定を解凍して接続する代替は取らない。

その後の[時刻境界の並列引継ぎ](time-frontier-handoff.md)で、world時刻metadata管理、並行sessionの二重所有／生成失敗時のclaim rollback、読み取り投影のcontrol lock分離を追加した。旧snapshotの生成順・同期範囲ではなく、現コードと[進捗](../progress.md)の最新検証を参照する。QPU transaction／live時計／HTTP本mountは依然未接続。
