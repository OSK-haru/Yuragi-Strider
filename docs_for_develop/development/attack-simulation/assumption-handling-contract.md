# 仮定・設定・根拠の取扱契約

最終更新: 2026-10-03。

状態: **分類・管理方式は方針合意、内部schemaと変更許可境界は一部実装**。仮定の分類、自由modeでの制約付き切替、compile済み実行計画を使う管理方式は合意済み。正式な永続化schema、migration、profile registryは未実装である。

## 1. 目的

攻撃シミュレーションで追加する仮定を、暗黙の分岐や大量の独立booleanへしない。
一つの実行結果について、次を後から説明・再現できる状態にする。

- どの主体または主体間関係へ置いた条件か。
- 実サービスの事実、論文上の仮定、simulator上の選択、利用者overrideのどれか。
- 誰が変更でき、誰に見え、どの処理へ影響したか。
- 無効化した場合に経路が消えるのか、別の挙動へ変わるのか、構成自体を拒否するのか。

カスタマイズ性は、全条件を無制約に編集可能にすることではない。成立可能な組合せ、根拠、差分、再現性を保ったまま比較できることを指す。

## 2. 値と仮定記録を分離する

実行に使う値は通常の設定へ一度だけ保持する。仮定記録は同じ値を複製せず、設定pathと根拠を参照する。

```text
設定値
  実行時に使う唯一の値

AssumptionRecord
  対象path / 分類 / 根拠 / 適用範囲 / 説明

ResolvedScenarioManifest
  profileとoverrideを解決し、実際に使った値と記録を凍結した実行証跡
```

設定値と仮定記録へ同じ値を二重保存し、片方だけ更新される構造を禁止する。

## 3. 設定の分類

### 3.1 主体が所有する設定

- `AttackerConfig`: 役割、事前知識、許可操作、観測能力、取得済みaccess。
- `VictimConfig`: workload、秘密、反復、サービスへの要求、結果利用。
- `QpuServiceConfig`: 受付、認証、queue、割当、Reset、公開情報、装置profile参照。
- `DeviceProfile`: `execution_regime`、topology、物理parameter、制御・Readout構造、model能力。初期版では`QpuServiceConfig`から版付き参照を持つ。NISQは全体固定値ではなく、現行profileの値`nisq_physical`とする。

### 3.2 主体間の関係設定

`RelationConfig`は、どちらか一方へ押し込むと意味が壊れる条件を保持する。

- 接続・通信経路と到達endpoint。
- 認証主体とcredentialの由来。
- attacker／victim／service間の情報可視性。
- 同一QPU・同一qubit・隣接qubit等の配置関係。
- 時間重複、順次再利用、順序保証。
- 仮想計測器、部品供給等の追加経路。

例えば「攻撃者が同じ物理qubitへ割り当てられる」は攻撃者能力だけでも装置特性だけでもなく、攻撃者、被害者、service allocationの関係である。

### 3.3 実験・演習設定

`ExperimentConfig`は模擬世界の主体ではなく、実験構成が所有する条件を保持する。

- 開始時に達成済みとする条件。
- training dataを得るphaseとlabelの由来。
- 対照条件、反復、観測窓。
- free実験の比較計画。

`ScenarioPolicy`は値そのものではなく、modeごとの編集、秘匿、提出資格、評価器を統制する。
hidden case、正解、合格閾値等のgrader情報は`EvaluationPolicy`へ置き、模擬世界内のservice設定へ混ぜない。

## 4. 値の表現方法

独立booleanの列挙を既定にしない。性質に応じて次を使う。

- 排他的な方式: enumまたは判別可能union。
- 複数能力: capability集合。
- 量: 単位付きparameter、範囲または分布。
- 主体間関係: node IDを参照するrelation。
- 未確認条件: `unknown`。`false`や既定値へ黙って変換しない。
- 派生値: 入力から計算し、利用者の直接編集対象にしない。

配置方式の例:

```text
placement_policy =
  guaranteed_same_physical_qubit
  scheduler_dependent
  isolated_physical_resource
```

`scheduler_dependent`だけが配置確率、対象pool、観測可能なqueue情報等の追加fieldを持つ。
相互排他的な状態を複数booleanで表さない。

## 5. 根拠と表示

各`AssumptionRecord`は最低限、次を持つ。

- `assumption_id`: scenario内で安定したID。
- `target_paths`: 影響する設定path。
- `basis`: `service_documented`、`measured`、`paper_assumption`、`simulator_choice`、`user_override`のいずれか。
- `evidence_ref`: 文書、論文、測定記録、親profileへの参照。根拠がない場合はその旨を明示する。
- `applicability`: service、version、装置、model、期間等の適用範囲。
- `rationale`: なぜ必要か、無効時に何が変わるか。

一つのscenario全体へ「現実的」とだけ表示してはならない。条件ごとの根拠を表示し、override後は元profileとの差分を示す。

## 6. 可視性と変更権限

値の可視性は次を区別する。

- `public`: scenario説明として全利用者に公開。
- `attacker_visible`: 攻撃者が模擬世界内の正規経路から取得可能。
- `internal`: service、victim、物理engineだけが利用。
- `grader_secret`: 評価器だけが利用。

環境作成画面で値を見られることと、課題中の攻撃者がその値を観測できることを同一視しない。

変更規則:

- 不変条件はmodeにかかわらず変更不可。
- free modeは`free_override`対象だけを開始前に変更できる。
- challenge modeはpresetが対象pathを固定・秘匿する。
- challengeを編集する場合はfree scenarioとしてcloneし、元課題への提出資格を失う。
- active sessionの状態へ設定変更を即時混入させず、新しいmanifestとsessionを作る。

内部v0では`ScenarioTemplate.free_override_fields`を変更許可pathの唯一の宣言元とし、未宣言のpathは自由modeでも変更不可とする。親pathの宣言は子pathにも適用する。`ScenarioEnvironment`だけでなく`ScenarioCompiler`もこの宣言を検査し、compiler直呼びでの迂回を防ぐ。templateは値を一度だけ保持し、`ScenarioPreset`は同じtemplateへ課題policyを重ねる。成立時のmanifest凍結は既存の境界を使い、設定管理用の別の値コピーを正本にしない。現時点で変更許可pathのUI編集、永続化、版付きprofile registryは未実装である。

## 7. Profileとoverride

利用者へ全fieldを最初から並べない。版付きprofileを基準にし、必要な差分だけoverrideする。

```text
base profiles
  attacker / victim / service / device / relations / experiment
        ↓
free-mode overrides
        ↓
ScenarioPolicyによるlock・hide
        ↓
整合性検査と派生値計算
        ↓
ResolvedScenarioManifestを凍結
        ↓
session開始
```

実行記録にはprofile ID・version、override差分、解決済みmanifest ID、乱数seed、model versionを残す。

## 8. compileと実行時管理

編集・監査用の情報を、job・shotのhot pathへそのまま渡さない。

```text
AuthoringScenario
  profile / override / 根拠 / UI metadata
          ↓ session開始時に一度だけcompile
ResolvedScenarioManifest
  解決済み設定 / 差分 / version / seed / 根拠
          ↓ 実行に必要な値だけ抽出・前計算
RuntimePlan + RuntimeState
  enum / 数値 / model参照 / capability集合 / index / counter
```

管理規則:

- profile解決、override適用、schema検査、関係制約検査、派生値計算はsession開始前に行う。
- `ResolvedScenarioManifest`はcontent hashで識別し、同一内容をsession間で共有・cacheできる形にする。
- runtimeは文字列path、根拠文書、UI metadataをjob・shotごとに探索しない。
- capability検査、予算更新、model参照、relation lookupは前計算した集合・index・直接参照で行う。
- eventは設定全体を複製せず`manifest_id`を参照する。
- active session中のdriftやservice変更は、上位設定の再compileではなく、許可された時刻付きeventと小さなmutable stateで表す。
- free modeの設定変更は新しいmanifestとsessionを作り、既存状態へ遡及適用しない。

設定管理負荷と、選択した物理model自体の負荷を分ける。qubit数、局所次元、shots、relationから切り出す局所領域、event数、推定memoryをcompile時に見積もり、対応上限を超える構成は開始前に警告または拒否する。具体的な上限は構造確定後のbenchmarkで決める。

現行のin-memory実装では、未信頼設定によるホスト負荷を抑える暫定的な入力・保存上限を別途設ける。設定深さ32、要素16,384、manifest直列化1 MiB、根拠・override各256件、store既定256件／直列化サイズ合計64 MiBを超える入力を拒否する。これらは課題の観測予算でも物理計算領域の正式な上限でもなく、実メモリ使用量を含む実測後に調整する運用上の保護値である。

## 9. 整合性検査

field単体の型検査だけでなく、能力の同時成立を検査する。最低限、次を拒否する。

- serviceが公開しないendpointへattacker accessだけを設定する。
- label生成元がないのにlabeled trainingを有効にする。
- allocationが対応しないのに同一物理qubit配置を保証する。
- pulse endpoint、権限、対応物理modelのいずれかを欠いたpulse操作。
- victim識別経路がないのに特定victimの時刻を既知とする。
- grader secretをattacker-visible経路へ接続する。
- modelが対応しない局所次元、topology、同時実行を黙って近似する。

未対応の組合せは、近い別設定へ自動変換せず、構成時に理由付きで拒否する。

## 10. Reset漏洩scenarioへの適用

現時点の対応は次のとおり。

- Attacker: `labeled_training_enabled`、`victim_observation_enabled`は実装済み。
- Victim: binary secret、状態準備、最終測定は暫定契約済み。
- Service: 単一QPU、FIFO、所有者変更Resetは内部実装済み。
- Relation: `placement.policy`を`RelationConfig`へ移し、現行planは`guaranteed_same_physical_qubit`と負の対照`isolated_physical_resource`を受理する。`scheduler_dependent`は未実装。
- Experiment: training／unknown observation／hidden evaluationの分離は内部実装済み。
- Evaluation: case構成と閾値を明示する内部graderを実装し、Reset課題専用v0の値は採用済み。未信頼code sandboxと、利用者dataだけを読む共通分析環境は未実装。

`ScenarioCompiler`は主体別設定、Relation、Experiment、Policy、AssumptionRecord、free-mode overrideを解決し、content-addressed manifestを生成する。Reset漏洩orchestratorはmanifestから抽出した`ResetLeakageRuntimePlan`だけを保持し、各jobと物理eventへ`manifest_id`を付与する。
`isolated_physical_resource`はfresh zero resourceへの割当eventを記録して被害者依存を遮断する負の対照として実装済み。`scheduler_dependent`は配置決定と観測可能性を伴う後続機能とする。

## 11. 受入条件

仮定schemaは次を満たすまで確定扱いにしない。

- 同じbase profileとoverrideから同じresolved manifestを生成できる。
- free modeの変更が新しいmanifestを作り、active sessionを遡及変更しない。
- challenge modeの固定・秘匿fieldを通常操作から変更・参照できない。
- 無効化した能力がjob、event、予算消費より前に拒否される。
- relationを欠く攻撃は物理効果だけを発火させない。
- 矛盾した能力の組合せを理由付きで拒否する。
- 実サービス準拠profileを変更した場合、準拠表示と提出資格を更新する。
- 実行記録から設定、根拠、model、seed、結果を追跡できる。
- job・shotごとの処理がauthoring設定treeや根拠metadataを再走査しない。
- 同じmanifestのcompile結果を安全に再利用でき、session固有のmutable stateを共有しない。
- 設定compile時間、runtime設定overhead、memory、選択物理modelの計算時間を分けて計測できる。

## 12. 現時点で未決定

- 永続化用の正式JSON schemaとmigration規則。
- profile registryの配置、配布、署名、互換性。
- 現行内部v0で`RelationConfig`と`ExperimentConfig`は`ScenarioTemplate`へ直接保持する。版付きprofile参照へ分離する正式schema。
- `unknown`を含む設定の実行可否とUI表示。
- 実サービス準拠表示が失効するoverrideの自動判定規則。
- expert向けoverrideをUI、設定file、APIのどこまで提供するか。
- 現行はReset漏洩固有RuntimePlanを実装。RuntimePlanのscenario共通部分と攻撃機構別部分の正式境界。
- compile cacheの保存範囲、破棄規則、性能上限値。

## 13. 提出可能な課題プリセットの開始前検査

`ScenarioPreset`は課題ポリシーと、その設定根拠を示す`AssumptionRecord`を保持する。課題modeへのmaterializeで根拠を`ScenarioSession`へ渡し、`ScenarioCompiler`は環境成立前にresolved manifestを検査する。

提出可能なchallengeでは、次を開始前条件とする。

- `attacker.operation_grants`、`victim.operation_grants`、`qpu_service.supported_operations`が存在し、権限フィールドは固定されている。
- これら三つの権限関連fieldそれぞれに、対象pathを明記した根拠記録がある。
- actorのgrantはservice capabilityの範囲内で、`ScenarioPolicy.allowed_operations`もservice capabilityの範囲内にある。
- すべてのlock／hidden pathと根拠pathがresolved manifest上に存在する。

これは提出課題に対する一般的な設定・権限の整合性検査である。攻撃機構、物理条件、資源割当などの課題固有制約は`ScenarioConstraint`または専用RuntimePlanで引き続き検証し、Reset漏洩専用条件を共通compilerへ持ち込まない。検査はcompile時のみで、job／shot経路では再走査しない。

`ScenarioEnvironment.establish()`はcompile成功時だけmanifestを確定し、失敗時は未成立のままにする。Reset v0の生成経路では、この成立検査と専用RuntimePlanの検証が通ってからQPU状態と実行器を作る。
