# 有限宣言列の物理確定・結果公開境界

2026-10-04。前段は[宣言workloadの共通受付](declared-workload-handoff.md)。要求・残件は[時間境界](../runtime-boundaries-and-resolutions.md)、証拠は[進捗](../progress.md)を正本とする。

## 分担

- store担当: `qpu_state.py`、`jobs.py`、新規`test_declared_transaction_stores.py`のみ。発行元・世代付き準備tokenと共有transaction gate。
- 親: 新規`declared_execution.py`／対応試験、`world_event_frontier.py`、runtime／service／session／workload接続、文書と最終検証。
- 独立監査: 上記の時刻・状態・認可・取消・破棄・有限資源と一般性を読み取り専用で点検。修正は親／元担当が行う。

## 実装の保証範囲

`DeclaredQpuExecutor`は新鮮な共通runtimeへ一度だけ組み立てる内部adapter。既存の複数qubit Program・物理layout・準備／継承・tenant交代Resetを使う。単一局所領域のFIFO排他に限定し、旧isolated再割当・legacy role固有operationはこのadapterで対応しない。未対応を共通型の永久制限にせず、通常の旧runtimeは保全する。

現adapterはdevice累積durationの初期値0だけに対応する。非ゼロ初期値のworld対応offsetは未実装なので、所有gateの変更前に拒否する。これは全製品・将来の復旧状態が必ず0であるという仮定ではない。

trusted ownerは受理済みjobへ`JobExecutionWindow(arrival,start,finish,release)`を登録し、有限sourceの網羅済み位置を明示sealする。host clock・空のpending・worker完了からsealや公開時刻を推定しない。finish-startはcompiler／runtimeの実際のサービスevent durationと照合する。job windowは再送で変更できず、FIFO head未宣言、順序逆行・重複・物理区間重複は拒否する。宣言arrivalとhost receiptは別記録のまま。

START: 正の待機区間は明示された`IndependentIdleModel`で正本外に計算し、候補を検証してから待機状態・RUNNING・開始frontierを共通gateで確定する。モデル未指定で待機作用を黙って省かない。モデル係数は内部adapterへの明示入力であり、製品presetの数値を新規採用した意味ではない。world µsとdevice累積durationの対応には宣言した待機だけを含め、host計算待ちを含めない。

FINISH: 数値計算はdetached store上で実行し、Reset／shot順、共同密度行列、countsを既存kernelで生成する。PSD検証・結果copyは正本gateの外、publishは準備tokenの真性・現snapshot／job世代を再検査して、物理event batch・job success・終了frontierを同じgateで確定する。共通store／scheduler／frontierの個々のreadもこのgateを通る。複数readから一時点の組合せを得たい内部ownerはgateを保持する必要があり、lockなしの連続readが一つのsnapshotになるとは主張しない。

START／FINISHで後のowner更新に例外が起きた場合、共有gateを保持したまま発行済みtoken／正確なjob世代の最新journalから未公開のsuffixだけを補償する。queue全体や全履歴をコピーせず、QPU batchとjob/startの一つの直前journalを保持する。readerへ見せた確定済み状態の巻戻し、server異常終了の復旧保証ではない。正常の状態遷移後や別世代への補償は拒否する。

数値中断は確定済みの待機／STARTを保持し、未確定shotと結果を公開しない。jobはRUNNINGのまま基盤側の明示retry／復旧へ渡し、架空の物理INTERNAL_ERRORへ変換しない。queued取消は開始前だけ、計算が遅いから開始後もqueued取消できる例外を設けない。session破棄はexecutorをfenceして計算終了をdrainし、確定済み状態をReset・巻戻ししない。

公開: common `get_result`は物理successだけでなく宣言release到達を要求する。workloadが使う公開時刻は登録済みreleaseと一致する必要があり、callback時刻や都合のよい過去時刻へ変更できない。releaseは内部supervisor証拠で、参加者への新しい時刻公開fieldではない。statusとresultの公開条件は別とする。

physical window登録前にqueued取消されたjobは、`register_cancelled_release()`で制御応答の公開位置だけ登録できる。実行しなかったQPU window／物理event／frontier進行を捏造しない。登録後の取消も既存releaseを保持し、共通result・workload進行へ同じ認可で返す。取消releaseも同じ有限履歴上限へ数える。

host証拠上限は明示`max_jobs`・`max_events_per_job`・`max_unresolved`。event batch超過／未対応モデルは開始前に拒否し、履歴を黙って破棄しない。複数qubitのmatrix計算に加えdetached候補・準備batch・一つの未公開補償journalを保持するため、メモリを無料と扱わない。製品上限・FFIを含むRust判断・長時間RSS受入は後続。

## 未接続と次の作業

これは有限のtrusted宣言列の実QPU／共通認証session接続であって、live網羅性の自動証明ではない。workload予約／自己結果barrierと実HTTP到着から安全sealを生成するowner、宣言job作用の自動展開・service処理／公開durationの生成、clock targetの自律同期は残る。結果barrierを物理ticketへ無条件に入れず、自身の結果生成を止めない。

service profileへの正式field／根拠表示、ZZ等を含む待機モデル選択、動的領域・同時job干渉、整合checkpoint／復旧、実Web所有者・lifecycle・UI、旧Reset課題の共通Program／予算移行は未完了。最初の被害者列・通信／待機の数値と追加仮定を勝手に採用しない。追加依存・外部ネットワーク・実秘密処理・Git履歴変更・commit／push／deployなし。

独立監査で非ゼロ初期device時刻の対応漏れと、window登録前取消の永久公開待ちを再現し、上記の開始前拒否／取消releaseの別登録で修正した。START／FINISH途中のowner更新への故障注入、stageの数値中断、破棄、ホスト遅延不変性、非ゼロ待機channel、両主体の継承／交代Resetを検査する。最終件数・commandは進捗正本を参照。
