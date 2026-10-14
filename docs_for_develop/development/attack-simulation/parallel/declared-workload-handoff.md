# 宣言workloadと共通受付の接続

2026-10-04。D57/D58の後続実装。[進捗正本](../progress.md)、[時間境界](../runtime-boundaries-and-resolutions.md)、[共通client](../common-command-client.md)を優先する。

## 分担とコード境界

- 受付担当: 新規`core/attack_simulation/declared_ingress.py`と`tests/test_declared_ingress.py`のみ。有限予約・barrier・同着順序・再送履歴を管理。
- 設定担当: 新規`core/attack_simulation/workload_config.py`と`tests/test_workload_config.py`のみ。typed v1とJSON／凍結manifestの往復を管理。その後、他担当の成果を読み取り専用監査。
- 親: `api/attack_workloads.py`、`api/attack_sessions.py`、`core/attack_simulation/job_service.py`、session fixture・実接続試験、文書・統合検証。既存変更と未追跡fileを保全。

## 組立と操作

成立前にactor設定の`workload`へ`serialize_workload_plan()`の出力、experimentへ明示`world_clock={clock_domain, rate}`を置く。v1は全fieldを必須とし、prepare／durationを暗黙補完しない。最大byte／node／depthと有限collectionをコピー前に検査する。認証・秘密乱数・物理配置の決定をJSON入力へ委譲しない。

`DeclaredWorkloadRunner`は凍結manifestだけを読み、actor／role／device／layout／service能力を検査し、全予定をbookへ公開してから新鮮なsessionを専有する。権限ゼロを開始拒否へ変えず、共通操作時に拒否する。`start()`だけがoriginを取得し、組立待ちをworldの経過へ混ぜない。

`dispatch_next()`はbookの一件grantを得て、短いstate lock外で認証済み共通sessionへProgram送信する。callback例外ではDISPATCHINGと予約を保持し、明示再送は共通冪等性で同じjobを得る。成功／grant拒否の退役とWAITING／barrier公開は原子的metadata更新。host容量不足・期限切れ・世界破棄は模擬DENIEDを生成しない。

同着順序は宣言position、ASCII producer ID、event IDの順で、host登録・callback完了順に依存しない。barrierの例外は同じproducerかつ同じeventだけ。未解決予約省略、退役順への遡及挿入、foreign token、source差替えを拒否する。予約／barrierは`max_pending`、非破棄の再送履歴は`max_history`で制限し、履歴満杯時はcallback前に止める。自動retry／無限loop／sleepを追加しない。

`complete_result()`は実際の共通result投影を読み、trusted ownerの明示公開時刻から後続を生成する。現段階ではその時刻と物理commitとの整合を証明できない。state／book snapshotはsupervisor証拠で、参加者routeへ渡さない。`close()`は認証を先に失効させ、実行要求をdrainして証拠を保持し、設定解凍・架空Resetを行わない。

独立監査で、jobなしの拒否結果からsession廃棄後も後続を生成できる問題と、事前compileがsession制御lockを保持して廃棄を遅らせる問題を再現した。親が拒否経路にもlive認証確認を追加し、最後のstate／book更新を短いsession lockで廃棄と順序付けた。これは物理transactionではない。事前compileはlock外へ移し、終了後とowner claim時にbindingを再確認する。廃棄・認証切れ・結果取得と公開の間の廃棄競合・compile保留中のregistry廃棄を回帰試験に追加した。

## 保証範囲と次の作業

宣言runnerがclaimしたsessionはraw HTTP／raw実行を拒否する。通常のsessionとHTTP候補の文法は変えない。未対応のlive要求を宣言列へ勝手に混ぜない安全な限定であり、全体のHTTP接続完了ではない。

実session試験は複数qubit、両主体、prepare／inherit、共通queue・認可・取消・応答喪失・host容量・認証失効・compile中clock進行・破棄を含む。trusted経路でのuntimed QPU smokeも行うが、物理時刻と状態の原子的確定の証拠ではない。件数とcommandは進捗正本へ記録する。

次は宣言到達に対応するjob作用の記録、網羅的seal、物理実行／結果公開／frontierの原子的確定を接続する。結果barrierを物理ticketへ無条件に入れず、service Reset／初期化と明示待機作用を維持する。その後にlive HTTP、実Web所有・成立／破棄・同一process復旧、UI、旧Reset課題の共通Program／予算移行を進める。物理係数・サービスlatency・未合意のgrantを推測して埋めない。

追加依存・外部ネットワーク・実秘密処理・Git履歴変更・commit／push／deploy・自動継続の再開なし。
