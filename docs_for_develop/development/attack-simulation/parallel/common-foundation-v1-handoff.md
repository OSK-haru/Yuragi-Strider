# 共通基盤v1の実装分担

2026-10-04。D57の五暫定契約とD58の局所化防止に従う。[実装契約](../common-foundation-v1-contract-proposal.md)、[進捗正本](../progress.md)を優先する。

## 今回の書込み境界

- 親: 全体トポロジー／局所state binding、`qpu_topology.py`、`qpu_state.py`、対応試験、共通pipeline fixture、文書、最終統合。
- 初期化担当: `program_request.py`、`circuit_program.py`、`shared_qpu_runtime.py`、`job_service.py`、`api/attack_terminal.py`、初期化試験。共通準備／継承を設定・三policy・principalで制限し、交代Reset／Readoutは維持する。
- 時間／workload担当: 新規`declared_world_clock.py`、`victim_workload.py`と対応試験。成立原点・内部clock対応、有限操作列と結果依存を扱い、host callbackを模擬投入時刻にしない。
- 独立監査: 実装差分が揃った後に読み取り専用で認可・時刻・状態・資源・一般性を点検。修正は親／元の担当が実施する。

既存の変更・未追跡ファイルを保全する。新規依存・外部通信・実秘密情報・別チャット・commit／push／deploy・automation再開を行わない。任意コード実行・公開サイト認証の秘密処理はこの分担に含めない。

## 接続と未接続を分ける

トポロジーは量子ビットinventory、ゲート接続、ZZ候補、制御線／伝達、Readout共有を保持する。候補関係を保存しただけで物理作用を有効にせず、係数・時間窓・active physics planは別所有とする。全体metadataと局所tensorを対応させるstate bindingは、領域外のstate relabelを拒否する。動的割当・gate routing・近似の必要相関判定を完成扱いにしない。

clock／workloadの内部部品と、live HTTP受付の網羅性・watermark・物理commitの原子性は別の検証対象。新しいclockを既存`device_time_us`へ読み替えない。結果依存runnerの論理到達と、人間がブラウザで実際に結果を見る時刻を区別する。

準備／継承は共通Program経路で扱い、旧Reset課題のrole固有operationへ縮約しない。継承でもservice handover Resetと宣言Readout誤差を黙って除去しない。旧課題の数値profile／予算／grader移行は、event列照合と受入が済むまで未完了。

## 最初の実装結果

- 全体metadata: `QpuTopology`は版付き有限schema、五種類の疎関係と独立host上限を保持する。`RetainedRegionBinding`で局所共同状態の順序付き物理IDを固定し、store commitとsession manifestの一致を検査する。fixtureは300qubit・527ゲートedge、分岐／閉路／非接続域と遠隔control／Readoutを持つ。入力と出力は独立copyで、未知field／重複／不在endpoint／上限超過は拒否する。
- 準備／継承: `ProgramSubmitRequest.initialization=prepare|inherit`、profileの`circuit_execution.supported_initializations`、追加権限`program.state.inherit`を実装。既定はprepare。共通compiler・service・runtime・terminal・HTTP候補を接続した。`register_readout.py`はResetを呼ばないreadout問合せで、対応外modelは拒否する。既存Reset preset／予算／graderの移行とUIは未完了。
- 時計／workload: `DeclaredWorldClock`は構築時にoriginを取らず、明示ready後に一度だけ開始する。targetは固定倍率と非減少monotonic入力から求め、device durationと別に持つ。`WorkloadPlan`は内部typed v1で、Program送信、自己counts分岐、独立群、有限反復、明示durationと共通prepare／inheritを扱う。未知操作・参照・版・無限列は拒否する。純粋stateをtrusted ownerが保持し、未投入予約と因果barrierを同時公開する。countsは最大256項目、planは最大4096event等の明示的な内部対応範囲を持ち、製品全体の不変上限ではない。

独立監査の初回で二つの矛盾を再現し、親が修正した。topology付き局所adapterはroutingを実装するまでunitaryの全operand pairに直接gate edgeを要求し、受付／実行前に検査する。高arity gateがcliqueで物理nativeになる保証ではなく、serviceの対応gate検査も必要な保守的な対応制限。chain経由の暗黙routingは拒否する。固定regionと旧isolated再割当の組合せも組立時に拒否し、必ず失敗するjobを受付しない。旧unbound隔離対照は保全する。

参考計測は300qubit／527edge metadataのconfig再parseのみ。100回×5反復の中央値0.609902 ms、JSON 9106 bytes。8qubit complex128密度行列の要素だけは1 MiBで、metadataも含む実RSS／runtime／HTTP／最悪遅延の保証ではない。host上限はfixtureで明示した値で、製品既定を採用した意味ではない。

## 次の接続順

後続では下記1の宣言runner側を[受付接続](declared-workload-handoff.md)まで実装した。live HTTP混在・物理transactionはまだ残る。以下は初回区切り時点の接続順。

1. 認可済み模擬runnerの宣言到達adapter、clock開始owner、workload予約／producer依存とlive ingressの全体同着順序をcoordinatorへ統合する。既存frontier ticketへ因果barrierをそのまま入れない。
2. 網羅的seal／結果公開時刻／QPU transactionと宣言待機を接続し、ホスト遅延注入で同じ入力列の物理結果を比較する。CPU待ちをservice／物理durationへ加算しない。
3. 公開Webの実所有者・環境成立／破棄・同一process復旧・UIを接続し、Resetの共通Program物理列／予算を照合して課題を移行する。

最終commandと検証証拠は[進捗正本](../progress.md)へ記録する。上記内部部品と共通送信の試験を、live自律進行・実所有者認証・復旧・体験完成の証拠にしない。
