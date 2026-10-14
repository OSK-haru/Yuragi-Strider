# ホスト時間分離：独立設計チェック／親への引継ぎ

2026-10-04。コード読み取り専用の設計点検。本担当の唯一の書込はこの文書。AGENTS.md、DEVELOPMENT_PROTOCOL.md、PROJECT_STYLE.md、AGENT_HARNESS.md、core/AGENTS.mdと関連コードを参照。直接ファイルを点検し、並行変更を保護した。行番号は点検時点の位置であり、親は最終コードと照合すること。以下は修正前のsnapshotを含む。統合後の状態は[共通コマンドclient](../common-command-client.md)と[進捗](../progress.md)を参照。

既に合意済みの[runtime-boundaries-and-resolutions.md](../runtime-boundaries-and-resolutions.md):47–84,148を前提とする。利用者の思考・操作・準備、設定された通信／QPU時間、計算・描画・シリアライズのホスト時間を分離する。配置だけから被害者の停止、攻撃機会の消失、物理的根拠のない状態推測を作らない。ホスト遅れは非シナリオ診断・許容範囲計測とし、通常の世界一時停止／試行無効化を既定にしない。絶対ホスト時間の漏洩ゼロを保証する主張ではない。

## 本ターンの最小境界：3点の提案

1. **受付時刻・同着順序をcompile／worker待ちより先に記録する。**

   根拠：[attack_sessions.py](../../../../api/attack_sessions.py):103–138はrequestとrun_nextが同じlockを保持し、計算終了まで参加者操作を拒む。[attack_http.py](../../../../api/attack_http.py):105–116は503 HOST_BUSYへ変換する。同:143–156の回路入力変換もthreadpool投入前。[job_service.py](../../../../core/attack_simulation/job_service.py):203–239はcompile後にsubmitし、[jobs.py](../../../../core/attack_simulation/jobs.py):134–164はそこで受付連番を振る。到着時刻は未記録。

   提案：認証済みの完全な要求envelopeが窓口へ到達した境界で内部receiptを予約する。host monotonic時刻とclock domain、同着順序、主体／冪等性キーとの対応、成功／拒否／再送の結果を記録し、有効jobのadmission成功と区別する。world時刻は信頼された記録／将来のmappingが与える場合だけ別項目とし、host時刻をdevice_time_usへ代入しない。押下・送信時刻を推測で埋めず、内部連番や他主体の活動量を公開viewへ出さない。

   compileは受付・照会用mutexの外へ分離する。先行receiptの成功／拒否／再送照合を確定してから有効jobをFIFOへ投入し、compile完了順による追越しを防ぐ。容量予約、内容衝突、失敗時の予約解放、close再確認はadmission側で原子的に扱う。これは窓口順序の保存であり、世界上の同時物理イベントの優先順位を定めない。成立条件はstructured／terminal／被害者要求が共通受付を通ること。layoutは確定bindingとadmission時の再検査を使う。

   必要ファイル：api/attack_http.py、api/attack_sessions.py、core/attack_simulation/job_service.py、jobs.py。テスト：tests/test_attack_http_adapter.py、test_attack_sessions.py、test_job_application_service.pyで同着記録、compile追越し防止、拒否後の進行、同一キーの競合／容量／内容照合とterminal共通受付を検査する。

2. **短い受付・FIFO同期と、runtimeごとの単一executorを組み合わせる。**

   根拠：jobs.py:128–205はdeque／dict／statusを無同期で操作するため、session lockを外すだけではsubmit・cancel・allocate・完了・照会が競合する。[shared_qpu_runtime.py](../../../../core/attack_simulation/shared_qpu_runtime.py):196–208はallocateから計算と完了まで同期実行。[qpu_state.py](../../../../core/attack_simulation/qpu_state.py):129–207はstoreを保護するが、schedulerやruntimeの多重実行までは守らない。

   提案：sessionの生存／workspace／容量と、schedulerのqueue／冪等性／statusを短い同期で保護する。数値計算はそのmutexの外で行い、計算中も受付・認可済みpublic snapshotの照会を許す。未完了結果は既存RESULT_NOT_READYとし、計算中という理由だけのHOST_BUSYを解消する。allocate／running更新→lock外の計算→完了更新を単一executorで囲む。sessionだけのgateでは直接runtime参照を防げないため、runtime側にも実行gateが必要。照会を世界進行のトリガーにしない。

   state_storeは局所物理状態・revision・commit済みdevice時間の唯一の所有者を維持する。duration_usは宣言したReset・駆動・測定／compiled durationのみ。host処理や描画待ちを加算しない。commit内のPSD検査は現状store lock内なので、全照会の一定latencyは保証できない。処理時間・受付待ち・commit待ちは内部診断で測り、攻撃者の観測能力へ流さない。

   必要ファイル：api/attack_sessions.py、core/attack_simulation/jobs.py、shared_qpu_runtime.py、必要ならjob_service.pyのsnapshot取得境界。qpu_state.pyへ時間mappingを押し込まない。テスト：tests/test_attack_sessions.py、test_attack_http_adapter.py、test_shared_qpu_runtime.pyでkernelをEvent barrierで止め、その間の受付・status・queued取消と、二つ目のexecutorによるallocate／commit禁止を検査する。既存HTTP busy検査（test_attack_http_adapter.py:156–172）は手動lock占有であり、計算中の受付成立を検証していない。

3. **close／取消と完了shotを守り、同じ記録列で遅延不変性を検査する。**

   根拠：attack_sessions.py:140–152は現在のatomic操作終了後に失効・解放し、巻戻さない。jobs.py:172–185の取消はqueued_only。shared_qpu_runtime.py:316–357はshot計算後にprogram_shotをcommitし、その後countsへ加算する。後続例外（:204–205）はinternal_errorとなり、完了shotは残る。[test_common_command_pipeline.py](../../../../tests/test_common_command_pipeline.py):207–218に正常shot数0／1の後の不正Resetでrevision／eventsを保持する既存検査がある。

   提案：close開始を短いlifecycle同期で確定し、新規受付／admissionと次のexecutor開始を止める。走行中のrun_next完了を受付mutexなしで待ち、失効・binding解放する。shot途中中断、queued取消への読み替え、物理Resetを加えない。cancel／allocateは同じscheduler同期点で確定しqueued_onlyを維持する。shot内の未検証状態を先にpublishせず、完了shot・service_reset等のcommit済みイベントを消さない。親が追加中のbinding整合／所有権検査（attack_sessions.py:91–93、job_service.py:134–174）も保護する。

   遅延検査：同じmanifest・初期snapshot・compiled sampling seed・入力・記録受付時刻／同着順序列を二つのrunへ与える。列に被害者、攻撃者、同着、同一キー再送を含め、片方だけ数値kernel／compileをEvent barrierで遅延させる。有限traceの先行未解決receiptを解決して順に投入し、停止中にも残りの受付が成立することを別に確認する。解放後、有効job順序、イベント種別／順序、device開始・終了／duration、最終device_time_us、countsを一致比較。UUID job IDはreceipt／冪等性キー対応で正規化し、host処理時間・応答時刻は除外する。同じRNG消費順・実行環境が成立条件。

   seed注意：job_service.py:131,213–219はservice生成ごとの内部saltからseedを導出する。独立fixtureへ同じrequestを送るだけではseedが揃わない。trusted test compilerで固定seedを与え、実認可・compile・admissionを通す。salt／資格情報を出力せず、参加者へseed指定を許さない。close中の受付拒否／drain、cancel／allocateの両順序、1 shot完了後の次shot例外もEvent barrierで検査し、全wait／join／ASGI呼出にtimeoutを置く。無限retryやsleepの偶然へ依存しない。

   必要ファイル：api/attack_sessions.py、core/attack_simulation/shared_qpu_runtime.py、jobs.py。テスト：tests/test_attack_sessions.py、test_attack_http_adapter.py、test_shared_qpu_runtime.py、test_common_command_pipeline.py。親が追加検査と焦点回帰を実行する。本点検のruntimeテストは未実行で、設計を実装済み／検証済みとは扱わない。

## 限界と親への引継ぎ

今回の提案は単一QPU・一局所領域・on-demand FIFOの受付／実行分離。被害者イベントの自律的な連続進行、同時物理実行、駆動クロストーク・ZZ・相関Readoutのサービス接続の証拠にはならない（runtime-boundaries-and-resolutions.md:139–141、shared_qpu_runtime.py:113–116,370）。

wall-clock/world-clock mapping、設定通信遅延との合成、liveでcommit済み世界時刻より過去へ届くlate event、commit horizon、世界上の同時イベント優先順位、公開観測の利用可能時点、負荷閾値／復旧方式は未決定。記録列再生の検査でlive時間契約を確定しない。実時刻のHTTP応答や完了statusは観測可能なので、この最小修正だけでホスト由来の側情報が全て消えるとも主張しない。

親が実行境界のコード修正・検証を担当する。本担当は文書のみ変更し、新依存・ネットワーク・Git操作・秘密値の取得／記録・他チャット送信なし。点検中の親側binding検査追加を再読し、上書きしていない。
