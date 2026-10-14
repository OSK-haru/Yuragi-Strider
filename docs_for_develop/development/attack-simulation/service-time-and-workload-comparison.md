# 通信・サービス時間・被害者進行の実サービス比較

確認日: 2026-10-04。公開一次資料と現コードの比較であり、実サービスへジョブを送った計測ではない。IBM/AWS互換実装や現在のサービスでの攻撃成立を保証しない。

## 1. 今回の判断

- 実通信、QPUアクセス、実計算の抽象化がサービスへどこまで対応するかを確認して時計・投入規則を具体化する。D57の初期値1倍は履歴として残り、live実行方式はD60の`explicit_wait`／`host_clock`選択へ更新された。どちらも実サービスlatencyの一致を意味しない。
- 固定周期だけの被害者を既定にしない。周期の規則性は攻撃時機の難しさにも関わるため、実装都合だけで置かない。周期的な実験自体を非現実的と断定する意味ではない。
- 宣言された待機区間へ物理作用を適用する方向を採用する（D54）。ホスト計算待ちを物理待機にせず、計算量に注意する。モデル・係数・時刻接続の実装完了ではない。

## 2. 対応する境界と現状の差

### 受付・前処理・QPU実行

IBMの`RUNNING`には古典前処理が含まれ、QPU待ちや校正待ちも起こる。古典処理が並列でもQPU上では一度に一つのjobを処理し、状態表示の実経過とQPU使用時間は異なる。[IBM Monitor a job](https://quantum.cloud.ibm.com/docs/en/guides/monitor-job)

現基盤はreceiptとjob受理を分け、compileをcontrol lock外で行う点では対応する。一方、`SharedQpuRuntime._run_next_exclusively()`は割当後に`mark_running()`して直接executorへ進むため、状態名はIBMの段階を忠実に再現していない。host単調時刻・内部連番は物理開始時刻ではなく、公開観測へ転用しない。`device_time_us`は宣言durationの累積だけで、QPU complexの使用時間やジョブ間隔の計測値ではない。

ローカルHTTPの所要時間を、そのままクラウド通信や模擬前処理時間として採用しない。利用者の実際の準備／送信、宣言した通信／サービス遅延、内部compile／数値計算の遅れを区別する。各区間の数値・分布を上記資料から推測して埋めない。

### 順序・専有・共有

IBM batchは独立jobの古典前処理を並列化し、量子実行を続けるが、投入順を保証せず専有でもない。activeなIBM sessionは他利用者や校正のjobを実行させず、最初のjobは通常queueに入る。[IBM Execution modes](https://quantum.cloud.ibm.com/docs/en/guides/execution-modes)

AWS Braketは受領順のqueueを説明する一方、Hybrid Jobsのtaskには通常taskより高い優先度を与える。取得可能なqueue depthや自分のqueue positionから、被害者のID・回路・正確な物理時刻が分かるとはしない。[Amazon Braket task scheduling](https://docs.aws.amazon.com/braket/latest/developerguide/braket-task-when.html)

現FIFO・単一executorは限定profileとして扱えるが、実サービス全体の規則ではない。各社の能力を都合よく合成して実サービスに帰属させない。専有中の被害者に無関係tenantのjobを割り込ませる設定はIBM session準拠と表示できない。現`AttackSession`は仮想環境の所有・資格情報境界であり、IBM sessionという予約機能ではない。live世界は成立前に`experiment.live_time_policy`を凍結する。`host_clock`ではhost単調経過をworld倍率へ写像し、`LiveWorldDriver`だけがbackground pumpを行う。`explicit_wait`ではユーザー思考時間・host処理落ちを時計へ混ぜず、`wait <duration>`／`wait request <request_id>`／`wait any`でのみ進める。両modeで未宣言の計算遅延を物理失敗や被害者停止へ変換しない。

### 被害者進行

実行例には、初めから入力が揃う独立job群と、QPU結果→古典最適化→次の入力という結果依存の反復がある。後者では通常queueへ戻す場合と専有sessionの場合で進行が異なる。[IBM Choose execution mode](https://quantum.cloud.ibm.com/docs/en/guides/choose-execution-mode)

被害者設定はbooleanだけではなく、いつ・何に・どの共通操作を行うかを指定できるworkloadとする。D57後続で単発／事前群／自己結果依存の有限反復を内部typed v1 schemaと純粋進行器に実装した。固定周期・jitterを足すだけで現実性を確保したとはしない。次要求の条件と宣言された古典／通信duration、queue・専有条件から投入と実行を分ける。課題presetはサービス・実行・トポロジーとworkloadを束ねる。JSON設定入力と具体的な課題workload選択は未完了。現Reset課題runnerはquery内同期反復であり、自律進行との統合は残る。[実装・検証範囲](progress.md)を参照。

## 3. 待機物理と計算量

適用対象は、前の物理event終了から次の物理event開始までの**宣言された**区間。queue待ち全体が残留状態の存続区間とは限らず、途中のReset・校正・別job等で区間を分ける。途中の操作を空の待機へ置き換えない。

候補は一定係数の局所T1/Tφチャネルによる時刻差の一括更新。既存`core/cptp.py`の振幅減衰・一般化振幅減衰・位相減衰と同じ規約で、内部`IndependentIdleModel`を実装した。明示した待機区間だけを共同状態上で更新し、runtime接続はない。Qiskit AerにもT1、T2、経過時間、平衡励起率を入力する熱緩和チャネルがあり、T2≤2T1を要求する。[Qiskit Aer thermal relaxation](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.noise.thermal_relaxation_error.html)

候補モデルでは `1/T2 = 1/(2T1) + 1/Tφ` と対応させる。独立局所・一定係数という条件を明示し、平衡励起率を勝手に0としない。共同状態を周辺状態の積へ置き換えず、必要なZZ・駆動・時間変動は対応モデルへ渡すか開始前に非対応とする。モデルや追加条件を課題へ採用した意味ではない。

一定係数の局所チャネルなら、計算回数を実経過秒数や描画frame数に比例させる必要はない。局所n量子ビットの共同密度行列へ各qubitの更新を適用する構成の見積りは時間O(n·4^n)、状態容量O(4^n)。トポロジー全体の密度行列を作らず、局所領域の上限で管理する。計算量の見積りであり、実測latency／RSS保証ではない。

Reset残留ではqueueの隣接と、秘密への依存が残る物理間隔を得たことは別。候補T1モデルで人口差は`exp(-Δt/T1)`で減衰し、長い待機を宣言すれば漏洩も弱くなり得る。攻撃を成立させるために操作秒数をµsへ圧縮したり残留を凍結したりしない。事前送信済みprobeの機会も権限・割当・実行間隔が成立する場合だけ採用できる。この推論は現在のIBMでの攻撃成立／不成立の実証ではない。

## 4. 次の確認と検証計画

D57で有限の単発・事前job群・結果依存workloadを初回の表現範囲として採用し、D60でlive時間進行を明示policyへ更新した。正式schema・具体的な操作列と数値根拠は依然未完了。共通時間契約・受入条件は[五暫定契約](common-foundation-v1-contract-proposal.md)を参照。

先に課題非依存の時間契約として、原点・進行・受付／event確定・公開の意味と、service／workloadが設定できる範囲を定義する。その上で各profileの専有／順次共有、要求投入条件、物理開始規則、結果公開条件を一組として確認する。最初の課題の選択を共通時間契約の前提にしない。公開資料は通信latencyやReset終了→次tenant測定の間隔を保証しないため、数値には追加根拠または明示した模擬仮定が必要。時計倍率と各区間の所要時間は別の選択であり、1倍でも実サービスlatencyが自動的に再現されるわけではない。

更新器の検証計画は、0／長短待機、平衡への収束、T1/Tφ無効条件、共同状態のtrace・正値性・spectator相関、一括／分割更新の一致（一定係数モデル内）、host遅延注入による状態・countsの不変性、宣言待機だけを変えた対照での物理変化、局所幅／event数に対する時間・最大RSS計測。経過時間を延ばしただけで更新回数が増えないことも確認する。

内部更新器の数値・境界試験と1/4/8量子ビットのkernel時間／process peak RSS計測は実施した。PSDは検証済み入力という呼出し契約で、固有値検査をkernel内で繰り返さない。公開境界・状態commit・runtimeへの時間接続、host遅延とcountsを含む統合検証は未実行。最新の試験と性能値は[進捗](progress.md)を参照。正本は[時間境界 §4.3](runtime-boundaries-and-resolutions.md#43-時間接続の前提検査と判断待ち2026-10-04)、未決定は[Q02/Q04/Q07/Q09](open-questions.md)へ集約する。
