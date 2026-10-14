# 共有QPUサービス実行契約の提案

確認日: 2026-09-23。レビュー判断反映: 2026-09-25。実装追跡: 2026-09-26。ジョブの語義確認: 2026-09-30。認可境界合意: 2026-10-02。状態所有契約とジョブ契約に残る未決定事項を、既存量子クラウドの公開仕様を根拠に暫定具体化する。第3.0節のジョブの語義、一部の製品判断と第6節の設定境界、および認可境界は合意済みで、in-memory状態正本と単一QPU/FIFOの内部縦切りまで実装した。C01/C02全体は未決定事項が残るため暫定契約へ昇格していない。

## 1. 参照した公開仕様

- [IBM Quantum: Fair-share scheduler](https://quantum.cloud.ibm.com/docs/en/guides/fair-share-scheduler): インスタンス間は割当量と使用量による動的なfair-share、同一インスタンス内はFIFO。
- [IBM Quantum: Monitor or cancel a job](https://quantum.cloud.ibm.com/docs/en/guides/monitor-job): `Pending`、`In progress`、`Completed`、`Failed`、`Canceled`と、queued/running jobへの取消要求。
- [IBM Quantum Compute REST API: Jobs](https://quantum.cloud.ibm.com/docs/en/api/qiskit-runtime-rest/tags/jobs): 取消と削除を別操作にし、取消不能状態には409を返す。
- [IBM Quantum: Qubit initialization](https://quantum.cloud.ibm.com/docs/en/guides/repetition-rate-execution): 通常は回路開始時に暗黙Resetを挿入し、`rep_delay`が初期化誤差と実行時間へ影響する。特定条件では前のshotの終状態から次を開始できるが、PUB内回路の実行順や単一batch実行は保証されない。
- [Amazon Braket: When will my quantum task run?](https://docs.aws.amazon.com/braket/latest/developerguide/braket-task-when.html): on-demand taskは原則受信順で処理し、queue depth/positionを公開する。
- [Amazon Braket: Terms and concepts](https://docs.aws.amazon.com/braket/latest/developerguide/braket-terms.html): standalone taskとHybrid Jobs由来のpriority queueを区別する。
- [Amazon Braket: Working with reservations](https://docs.aws.amazon.com/braket/latest/developerguide/braket-reservations.html): 予約時間中は対象QPUへの排他的アクセスを提供する。
- [Amazon Braket: CancelQuantumTask](https://docs.aws.amazon.com/braket/latest/APIReference/API_CancelQuantumTask.html): 取消要求に `CANCELLING` / `CANCELLED` を返し、競合時は409を返す。
- [Azure Quantum: Introduction to jobs](https://learn.microsoft.com/en-us/azure/quantum/how-to-work-with-jobs): `waiting`、`executing`、終端状態を区別し、waiting中は全providerが取消可能、executing中はprovider依存。

ここから言えるのは、非同期job ID、状態照会、取消要求、終端状態、複数のqueue policy、排他的予約は現実的な抽象化だということまでである。クラウド利用者がjob間の内部量子状態、物理量子ビットの再割当順、実行中取消後の残留状態を取得できるとは限らない。

## 2. 状態所有契約の提案

### 2.1 唯一の正本

`QpuStateStore`を、対象QPU世界の動的物理状態を保持する唯一の正本とする。既存シミュレーターの公開レスポンスから状態を再構成しない。

物理シミュレーターは次の状態遷移関数として扱う。

```text
Transition(current_state, physical_event, device_config)
  -> next_state, internal_observations, diagnostics
```

API結果、攻撃者向け観測、判定器用真値は、この内部出力から各サービス境界で別々に生成する。

### 2.2 内部v1で保存する状態

- 閉じた局所実行領域の密度行列。2/3準位を区別し、リーク状態を落とさない。
- 局所状態が対応する物理量子ビットIDとトポロジー版。
- シナリオ／仮想装置時刻。
- 単調増加するstate revision。
- 最後に確定した物理event IDとjob ID。
- 実行中windowと割当の参照ID。割当の正本自体はscheduler側に置く。

T1/Tφ、ZZ係数、駆動伝達係数、Reset target、Readout混同行列は、当面はdevice configurationとして版管理し、動的状態へ複製しない。ドリフトや測定履歴依存を導入するときだけ、対応するslow stateを追加する。

### 2.3 確定規則

- 検証失敗・受付拒否・queue中取消: QPU状態を変更しない。
- 物理実行開始前の内部実装失敗: 状態と装置時刻を変更しない。
- 正常な物理event: 期待revisionが一致するときだけ次状態を原子的に確定する。
- 模擬した装置故障・実行中取消: 最後に完了した物理event境界までを確定し、巻き戻さない。
- 後処理・結果保存・通信の失敗: 物理状態は既に確定済みとし、結果取得を再試行可能にする。
- シミュレーター自身の例外や数値異常: 物理故障として偽装せず、未確定transactionを破棄して `INTERNAL_ERROR` とする。

最初のevent境界は「一つの回路実行」「Reset」「待機」の単位とする。積分step単位の外部取消は提供しない。

### 2.4 永続性

2026-10-04のD57による保証範囲更新: 以下は後続のserver再起動復元案として保全する。初回v1の必須保証はページ再読み込みと同一process内の整合復旧までで、process再起動／異常終了からの再開はまだ保証しない。[採用した保証範囲と受入](common-foundation-v1-contract-proposal.md)を優先する。

QPU状態はapplication/server再起動後も復元する。永続snapshotには少なくともstate schema version、QPU ID、state revision、装置時刻、物理量子ビット対応、局所準位数、量子状態、device configuration version、最後に確定したevent IDを含める。

保存媒体は実装設計で選べるが、次を契約上の不変条件とする。

- snapshotとevent履歴のどちらが正本かを曖昧にしない。初期版は最新snapshotを正本、event履歴を監査・再現用とする。
- snapshot保存が完了する前にjobを `SUCCEEDED` としない。
- 破損・schema非対応snapshotを黙って初期状態へ置換しない。復元不能として停止する。
- 課題Resetとserver再起動を同じ意味にしない。server再起動は物理状態を初期化しない。

## 3. ジョブ契約の提案

### 3.0 ジョブの語義（合意済み、具体的なschemaは未決定）

本製品でいう `ServiceJob` は、QPUサービスが受理し、ID・順番・状態・結果を管理する**量子実行の単位**である。攻撃者と被害者は同じ主体非依存のジョブ種別を、各自に許された権限で利用する。ジョブは「QPUへの指示すべて」でも「一つのゲート」「一つのshot」「必ず一つの回路」でもない。

| 単位 | 意味 | 例 |
| --- | --- | --- |
| 利用者の操作要求 | Application APIへ届く要求全般 | 装置照会、ジョブ投稿、結果取得、取消要求、課題提出 |
| `ServiceJob` | 受理後にサービスが追跡する量子実行単位 | 対象装置・program・shotsを指定した回路実行、サービスが許す複数回路の実行 |
| 物理event | ジョブ実行中などにQPU状態・装置時刻へ作用する内部単位 | 初期化、駆動、測定、service-owned Reset |
| workflow／batch／session | 複数ジョブの構成・実行枠・スケジューリング上のまとまり | 反復する古典・量子計算、独立ジョブの一括投入、予約利用 |

最初に公開するジョブ種別は量子program実行を中心とする。programに許される回路・測定・待機・明示Reset・パルス等の範囲、複数回路の受付、shotの初期化規則はservice profileで定める。一般ジョブ定義に特定課題や特定サービスの命令を埋め込まない。workflow／batch／sessionを後で提供する場合も、その内側のQPU実行ジョブとの関係を明示する。

装置照会、ジョブ状態・結果の取得、取消要求、手元での統計計算、課題への提出はジョブではない。取消は既存ジョブを対象とする操作であり、物理eventは通常の利用者へ直接公開しない。現在の `JobCommand` と役割別operationは内部縦切りの型であり、この公開定義を確定するschemaではない。

内部実装追跡（2026-10-02）: 単一QPUのReset runtimeでは`JobCommand.target`にQPU IDと物理量子ビットIDを持たせ、scenarioの`resource_binding`と受付前・実行直前に照合する。targetは送信先の整合性であって認可の証明ではない。tenantの役割認可と公開APIのtarget粒度は別途決める。

実サービスでも呼称と粒度は異なる。Braketの *quantum task* は回路・測定・shotsを含む装置向け要求で、*hybrid job* は複数taskを含み得る。[Braketの用語](https://docs.aws.amazon.com/braket/latest/developerguide/braket-terms.html)。IBM Quantumは個別jobのほかbatch・sessionを実行方式として区別する。[IBMの実行方式](https://quantum.cloud.ibm.com/docs/en/guides/execution-modes)。Azure Quantumはprovider targetへprogramを送る単位をjobと呼ぶ。[Azureのジョブ投稿](https://learn.microsoft.com/en-us/azure/quantum/how-to-submit-jobs)。これらの共通部分を抽象化するのであり、名称や入力形式が全サービスで同一だとは仮定しない。

### 3.1 受付境界

Schedulerを直接HTTP endpointにしない。

```text
Client
  -> Service API（認証、認可、形式、quota、冪等性）
  -> immutable JobCommand
  -> Scheduler（queue policy、割当、順序、取消）
  -> QPU execution window
```

認証はHTTP Authorization header、再送識別はIdempotency-Key相当、回路・shot・対象deviceはbodyのdomain schemaへ置く。利用者入力の任意priority headerは初期版では認めない。

### 3.2 外部操作

```text
POST /jobs                 -> 202 Accepted + job_id
GET  /jobs/{job_id}        -> current state and permitted metadata
POST /jobs/{job_id}/cancel -> cancel request result
GET  /jobs/{job_id}/result -> terminal result or not-ready response
DELETE /jobs/{job_id}      -> terminal job artifact deletion only
```

一つのHTTP要求には一つの最終応答を返す。jobの進行は同じ応答を増やすのではなく、照会または将来のevent streamで伝える。応答到達は保証できないため、同じ冪等性キーの再送でjobを重複生成しない。

### 3.3 状態機械

```text
SUBMITTED -> VALIDATING -> QUEUED -> ALLOCATED -> RUNNING
                                             -> CANCEL_REQUESTED

terminal: SUCCEEDED / FAILED / CANCELLED / REJECTED / INTERNAL_ERROR
```

`DELETE`と`CANCEL`を分離する。取消は実行停止要求、削除は終端後のjob data管理である。

### 3.4 Queue policy

queue policyをサービスプロファイルとして明示する。

| profile | 規則 | 用途 |
| --- | --- | --- |
| `on_demand_fifo_v1` | QPUごとの受信順。同時刻は単調な受付sequenceで決定 | 最初のReset課題、Braket型の説明 |
| `fair_share_v1` | tenant allocation/usageでtenantを選び、tenant内FIFO | IBM型の自由実験。重みとwindowは別設定 |
| `exclusive_reservation_v1` | 予約主体のjobだけを対象時間内に実行 | session/reservation型の対照条件 |
| `co_scheduled_partition_v1` | 明示された複数jobを一つの物理windowへ束ねる | 将来の同時駆動クロストーク課題だけ |

初期実装は `on_demand_fifo_v1` のみに限定する。architectureはpolicy IDを保持し、FIFOを全サービス共通の事実とは主張しない。

### 3.5 排他と同時実行

QPUごとに単一writerを置き、state revisionを一つの実行主体だけが更新する。Mutex、monitor、楽観lock等は内部実装選択であり、外部契約にはしない。

単一writerは、論理jobの同時実行を禁止しない。`co_scheduled_partition_v1`ではSchedulerが複数jobを一つのexecution windowへ束ね、単一writerが合成Hamiltonianとして一度に状態発展させる。通常profileでqueueが近いだけのjobを、黙って同時実行しない。

### 3.6 取消

- `SUBMITTED` / `VALIDATING` / `QUEUED`: 取消成功。物理状態への影響なし。
- `ALLOCATED`: 実行開始前なら取消成功。割当を解放する。
- `RUNNING`: `CANCEL_REQUESTED`へ遷移し、次の物理event境界で停止を試みる。
- 停止境界より先に完了した場合: `SUCCEEDED`。取消要求成功をjob取消成功と同一視しない。
- 終端状態または取消不能区間: 409相当。
- 実行中に停止した場合: 完了済みeventの物理状態は残し、未実行eventだけを破棄する。

実行中取消の可否はQPU service側の `cancellation_policy` で切り替える。初期候補は `queued_only` と `event_boundary_best_effort` とし、課題ごとに新しい取消意味論を増やさない。攻撃者が取消を要求できるかはattacker側の権限設定で別に制御し、実効能力は両設定が許可した場合だけ有効とする。

### 3.7 失敗分類

| failure stage | job終端 | QPU状態 |
| --- | --- | --- |
| 認証・schema・quota | `REJECTED` | 変更なし |
| queue・割当 | `FAILED` | 変更なし |
| simulator内部例外・非物理値 | `INTERNAL_ERROR` | transaction破棄 |
| シナリオで模擬した装置故障 | `FAILED` | 完了eventまで保持 |
| 後処理・結果保存 | `FAILED`または再試行中 | 物理状態は保持 |
| clientへの通信失敗 | job状態を変えない | 状態・結果を保持し再取得可能 |

### 3.8 principal・認可・job所有（認可境界は暫定契約）

操作catalog、ActorPolicy・ScenarioPolicyとの能力合成、tenant-scoped job所有、被害者runner境界の規則は[共通操作・観測・校正契約 §2](common-command-observation-contract.md#2-能力の解決規則)へ固定する。この節ではC02実装の受入条件を定める。

- 外部requestのprincipal・tenant・roleは認証contextから導出し、requestが自己申告するtenant ID／roleを権限判定へ使わない。
- `program.submit`、`job.status.read`、`job.result.read`、`job.cancel`を共通operationとして扱う。service capability、actor grant、scenario allowlistの積集合を通し、job単位操作にはさらに所有権・結果可視性を適用する。
- jobはtenant scopeの資源とし、状態照会・結果取得・取消は同じtenantに属するprincipalのうち、該当operationのgrantを持つものに許可する。作成者専有は既定にせず、`created_by`は監査用に残す。service profileがcreator限定を要求する場合は追加条件として表す。
- 初期`queued_only`取消はtenant scopeとcancel grantを満たすprincipalによるqueued jobだけに許可する。取消拒否時はscheduler・QPU状態・予算を変更しない。
- 被害者workloadは攻撃者と同じcatalog/APIを使い、scenario runnerが内部principalとして起動する。runner専用の起動権限は外部Application APIから到達不能とする。
- 状態・結果返却はactor-scoped projectionを経由し、内部状態、他tenant metadata、hidden判定情報を含めない。
- service capability、ActorPolicy grant、ScenarioPolicy allowlistの欠落・不整合はfail closedとする。認可拒否を物理jobの受付後に遅延させない。

この契約で確定するのは認可の意味と受入条件である。認証provider、credential形式、HTTP status/schema、具体的なprincipal roleの種類・割当管理は未決定。現行内部runtimeがtenant ID文字列で行う照合は、この外部認証契約を満たしたものとは数えない。

このtenant-scope共有は、Azure Quantumがworkspaceへの複数ユーザー／groupのアクセスをroleで付与し、jobのsubmit/view権限を分ける構造や、Amazon Braketがjob/taskごとにread・cancel等のIAM actionとresourceを定義する構造に沿う抽象化である。ただしprovider間で同じ共有範囲・既定roleがあるとは扱わない（[Azure workspace RBAC](https://learn.microsoft.com/en-us/azure/quantum/manage-workspace-access)、[Azure workspace共有](https://learn.microsoft.com/en-us/azure/quantum/how-to-share-access-quantum-workspace)、[Amazon Braket service authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_braket.html)）。

受入時は少なくとも、無権限submit、他tenantのstatus/result/cancel、同一tenant内でgrantのないprincipalによる操作、存在job IDの差替え、内部runner経路への外部到達、challenge設定改変、拒否操作による状態・budget変化を個別に検査する。成功系ではgrantを持つtenant memberのsubmit/status/resultとqueued cancel、Scenario Orchestratorからの被害者job投入を確認する。

内部実装追跡（2026-10-02）: `JobApplicationService`を追加し、4操作のgrant・tenant scope判定と結果の許可項目への投影をin-processで実装した。Reset orchestratorの被害者job投入・攻撃者job投入と結果取得もこのサービスへ接続した。複数principalの同一tenant共有、grant別拒否、他tenantと存在しないjobの同一扱い、拒否時の不変性を焦点testで確認した。外部認証context、HTTP endpoint、principal割当の管理UI、一般化したprogram入力schemaは未実装。

## 4. 最初の不完全Reset課題へ採用する暫定値

- `on_demand_fifo_v1`、単一QPU、利用者指定priorityなし。
- 初期版はこの範囲だけを実装するが、queue policy、cancellation policy、観測能力を版付き識別子で保持し、後続方式を条件分岐の乱造なしに追加できる境界を設ける。
- 被害者jobと攻撃者jobを別の`ServiceJob`として記録し、その間のservice-owned Resetは内部の物理eventとして記録する。Reset eventは利用者へ直接公開するjobではない。
- 同一物理量子ビットへの再割当はScenario Orchestratorが成立条件として設定する。
- queue位置や被害者identityは攻撃者へ自動公開しない。公開情報は観測契約で別途決める。
- running取消はevent境界でbest effort、巻戻しなし。
- QPU状態のserver再起動復元は後続保証案。初回必須保証はD57のページ再読み込み／同一process内の整合復旧とする。
- QPU内部状態は非公開。攻撃者は自分のjob結果だけから推定する。
- 完全Resetを対照profileとして必須にする。

この構成は、現在の一般的なクラウドサービスが他tenantの残留状態へのアクセスを保証すると主張しない。IBMの初期化設定が示すようにReset条件と待機時間が状態準備誤差へ影響し得ることを物理的な根拠とし、同一物理量子ビットへの連続割当と観測可能性は課題側の明示仮定とする。

## 5. まだ採用判断が必要な点

- 一つの回路を複数eventへ分割する条件。shot batchを取消境界にするか。
- state snapshotの永続媒体、保存期間、暗号化、復元失敗時の管理操作。
- fair-shareの重み・使用量window。queue位置の公開可否は設定化するが、公開粒度は未決定。
- `co_scheduled_partition_v1`を現実のどのサービス・装置条件へ対応させるか。
- 模擬装置故障・実行中取消・失敗注入は最初の論文再現課題から除外し、C02一般契約の検証profileで扱う。

## 6. 設定の境界と課題モード（構造合意済み、field未決定）

利用者が考える三分類をdomain設定の基本とする。

- `AttackerConfig`: 攻撃者のrole、認証済み能力、操作API、観測能力、利用予算、使用可能tool。
- `VictimConfig`: 被害者workload、秘密入力の生成・符号化、実行周期、再送・失敗時動作、要求するservice plan。
- `QpuServiceConfig`: device/topology、queue policy、割当、Reset、cancellation policy、公開可能なqueue情報、物理model parameter。

従来の五つの意味区分は、攻撃者→`AttackerConfig`、被害者→`VictimConfig`、装置・サービス→`QpuServiceConfig`を主体別設定として対応させる。装置とサービスの意味上の責務は区別したまま、同じQPU service設定境界の内部へ置く。2026-09-26の[仮定取扱契約](assumption-handling-contract.md)により、通信・配置等の主体間関係と実験条件は三設定へ押し込まず別責務へ分ける方向へ補足した。

ただし、設定の所属と編集権限は分離する。`AttackerConfig`は「攻撃者についての設定」であって、攻撃者本人が自由編集できる設定という意味ではない。実効的な操作・観測能力は、QPU serviceが提供する能力とattacker roleへ付与された権限の共通部分とする。

主体別三設定と関係・実験設定を包む実行方針として、別に `ScenarioPolicy` を置く。これは利用主体設定ではなく、課題管理用の封筒である。

```text
ScenarioPolicy
  - mode: free | graded
  - locked_fields
  - hidden_fields
  - editable_before_start
  - restart_required_fields
  - submission_eligibility
  - grader/private truth references
```

`graded`では三設定をpresetから読み込み、課題開始後は利用者による変更を禁止する。攻撃者は設定編集ではなく、固定された権限内でjob送信・取消・観測等を行う。秘密値、判定器用真値、非公開QPU状態は表示しない。

`free`では環境作成者が三設定を編集できるが、物理状態・queue・時刻へ影響する変更は実行中に即時適用せず、session再開始を要求する。graded presetを編集したsessionは、その元課題への提出資格を失う。

queue情報の可視性はQPU service側の公開能力とAttackerConfigの観測権限の両方で決め、ScenarioPolicyがgraded時の固定・秘匿を強制する。
