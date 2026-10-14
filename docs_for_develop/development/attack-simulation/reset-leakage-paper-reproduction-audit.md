# 不完全Reset論文再現契約の欠陥監査

監査日: 2026-09-26。

対象: [`AS-RESET-CCS22-SAME-QUBIT-v0`](reset-leakage-paper-reproduction-contract.md)、既存の攻撃用物理プリミティブ、サービス設計文書、現行API・シミュレーター。

結論: **内部の挙動再現縦切りと一部受入監査は実行可能になったが、採用profile、公開service、永続化、未信頼推定器実行、UIを含む論文再現完了には達していない。以下の欠陥IDは履歴として維持し、実装追跡で残存範囲を示す。**

## 1. 重大欠陥

### F-C1: 現行Resetチャネルは論文のReset機構と一致しない

現行`ImperfectResetSpec`は、成功時に固定状態へ置換し、失敗時に入力密度行列を保持する混合チャネルである。
Miらの対象は測定と条件付きXからなるResetであり、測定が先にコヒーレンスを破壊し、測定誤分類が条件付きXへ誤った入力を与える。

影響:

- 現行チャネルは失敗branchでコヒーレンスやエンタングルメントを保持し得る。
- `Q(theta)`の曲率、非対称誤差、複数Reset後の反転傾向を一般には表せない。
- 二値両端が合っても一般状態の再現性を主張できない。

必要な処置:

- 初期版には共通`ResetModel`境界を追加し、その一実装としてシミュレーター由来の有効観測modelから攻撃者countsを生成する。
- 後続で測定instrument、assignment error、条件付きXを分解した機構modelを同じ境界へ追加する。
- 既存チャネルを使う場合は二値端点同値testと`approximation`表示を必須にする。

実装追跡（2026-09-26）: 共通境界・registry・既存CPTP adapter・`behavioral_approximation`表示と内部QPU runtime接続は実装済み。機構modelと公開service接続は未実装。

### F-C2: job間状態の正本と所有権移転は内部縦切りまで

revision付きin-memory `QpuStateStore`、tenant owner、単一qubit mapping、FIFO scheduler、service Reset eventを実装し、被害者jobの終状態を別tenantの攻撃者jobへ引き継ぐ内部因果は構成できる。
一方、永続snapshot・再起動復元、認証API、結果認可、非同期workerは存在しない。

必要な処置:

- C01状態所有契約とC02 job契約の最小実装を先行する。
- revision付きcommit、tenant所有、physical-qubit mapping、event順序を監査可能にする。

実装追跡（2026-09-26）: 上記のin-memory最小実装と焦点testは完了。欠陥は永続性とservice境界に縮小したが、解消済みとはしない。

### F-C3: 論文と製品でshotの意味を取り違える危険がある

論文の8192 shotsは、被害者準備から攻撃者測定までの回路全体を反復する。
一つの被害者残留状態を攻撃者が8192回連続測定するものではない。

必要な処置:

- orchestratorがsampleごとに`victim -> reset -> attacker`を再実行する。
- 集約samplingを使う場合は、全系列の条件付き分布と同値であることをtestする。

実装追跡（2026-09-26）: orchestratorはshotごとに`victim -> reset -> attacker`を反復する。現行の逐次経路については解消し、将来の集約sampling導入時に同値testを再要求する。

### F-C4: シミュレーター由来の数値profileと実行条件がない

現行コードの`success_probability`と`target_populations`は、課題用の実行条件・model版・seed policyと結び付いた登録profileではない。
論文値への完全一致は目標にしないため、全`a,b,c`値やraw datasetの欠如はblockerではない。一方、条件を固定せずに得たシミュレーター値も検証根拠にはならない。

必要な処置:

- model版、物理・サービス設定、Reset回数、shots、seed policyを固定してシミュレーターからprofileを算出する。
- profileを`simulator_derived`と表示し、論文実験値の再現とは呼ばない。
- 論文とは因果経路、攻撃仮定、評価方法、定性的傾向を比較する。

実装追跡（2026-09-28）: profile・校正runner・JSON CLIを実装し、Reset課題専用v0 parameterと決定論的受入閾値を採用した。有限sampleの統計許容差と実行上限は未決定。

## 2. 重要欠陥

### F-H1: 公開回路schemaにReset operationがない

現行APIは`MEASURE`とclassical condition付き`X`を持つが、tenant境界でserviceが挿入するReset eventは公開schemaにない。
攻撃者がResetを回路へ書く設計にすると、service境界と論文の所有権移転を崩す。

処置: Resetは利用者gateではなくservice eventとして実装し、管理用schemaだけに置く。

実装追跡（2026-09-26）: 内部runtimeでは所有者変更時にservice Resetを自動挿入する。公開API schema自体は未実装。

### F-H2: 論文の強い攻撃者仮定と攻撃側画面だけの体験が未接続

論文は被害者実行の制御、反復、予測可能な同居を攻撃者へ与える。
製品は被害者・QPU設定の直接操作を課題中の攻撃者へ許さない。

処置: `ScenarioPolicy`が同居と反復可能なtraining endpointを保証し、利用者は攻撃側画面からその公開能力だけを使う。UIに「現実に自動取得した能力ではなく、課題前提として与えた」と表示する。

実装追跡（2026-09-26）: training／unknown observationのaccess設定と実行前拒否は内部実装済み。公開service APIとUI表示は未実装。

### F-H3: 学習phaseが正当な公開能力か未定義

ラベル付き学習runを無条件に与えると、被害者制御能力を教材上で増幅しすぎる。逆に与えないと論文のchannel fittingを体験できない。

処置: 学習用reference workloadをserviceが明示的に提供するprofileと、攻撃者が既知入力jobで自ら校正するprofileを分ける。採用方法はservice能力・ActorPolicy・ScenarioPolicyで選び、labelの取得経路を表示する。

実装追跡（2026-09-28）: 現行planはlabel付きtrainingを有効にする場合、`ExperimentConfig.training.label_source`を必須化する。共通操作・観測契約では、利用者校正を返却dataと正当に知るlabelだけに限定した。具体的なtraining profileの採用とUI表示は未完了。

### F-H4: grader閾値は論文由来ではない

旧候補のhidden 32件中24件は採用しなかった。採用したReset課題専用v0は、32 shots/case、0/1各10件のhidden 20件中16件正解である。signal強度とshotsを変更するpresetでは再校正が必要となる。

処置: simulator-derived profile校正時に正規推定器、定数予測、perfect-reset対照を実行し、評価件数と閾値を所有者承認後に版管理する。

実装追跡（2026-09-28）: candidate signal、perfect reset、isolated resource、観測無効、定数推定器、hidden shot整合を判定する内部受入reportとCLIを実装した。候補の相互情報量0.45 bit/case以上・優位性0.30以上、負の対照は両指標1e-9以下を採用し、自動testへ接続済み。

### F-H5: `Q(theta)`を物理状態と観測誤差へ二重計上する危険がある

論文の`Q(theta)`は観測された1-output probabilityを特徴付ける有効量である。これをReset後状態と解釈し、さらに既存Readout errorを適用すると誤差を重ねる可能性がある。

処置: `effective_observation`と`mechanistic_state_transition`を排他的なmodel modeとしてschemaで区別する。

実装追跡（2026-09-26）: `state_transition_with_readout`と`effective_observation_only`を型で分離し、現行stateful runtimeとRuntimePlanは前者以外を開始前に拒否する。後者を実行する専用runtimeは未実装。

## 3. 表示・妥当性上の注意

### F-M1: 「現在のIBMサービス再現」と表示できない

論文は2022年当時のIBM Jakarta、Lagos、Perthと当時のQiskit／backend条件を扱う。
この契約は歴史的実験条件の論文追跡であり、現在のIBM公開サービス仕様を示すものではない。

### F-M2: 論文全体の再現ではない

初期版は同一量子ビットの順次利用、二値状態、被害者測定あり、Reset 1回だけを扱う。
隣接qubitクロストーク、一般Bloch状態、複数Reset、Secure Resetは対象外である。

### F-M3: Magnard論文とMi論文の役割を混同できない

Magnardらはall-microwave resetの物理方式、MiらはReset越し漏洩の脅威モデルと実験を支える。
一方の論文だけで物理機構と攻撃成立性の両方を裏付けたと表示してはならない。

## 4. 契約内部の矛盾確認

| 観点 | 判定 | 理由 |
| --- | --- | --- |
| 攻撃者画面だけで操作 | 整合 | 同居・被害者反復はScenarioPolicy、操作は攻撃者APIに限定 |
| FIFOと同一量子ビット再利用 | 整合 | 被害者job完了後にReset、次に攻撃者jobを割り当てる |
| graded設定固定 | 整合 | 主体別設定、配置relation、実験条件、秘密・seedをScenarioPolicyが固定。現行codeは主体別三設定まで実装 |
| 状態持越しとReset | 整合 | Resetは状態を完全破棄せず、revision付き遷移として扱う |
| 既存物理model固定 | 条件付き整合 | 基準版は変更せず内部拡張を使うが、現行Reset primitiveだけでは論文適合不可 |
| 再現性と未知評価 | 整合 | seedは保存するが攻撃者へ非公開。再実行は管理側だけが行う |
| freeとgradedの共通基盤 | 整合 | 同じ実行基盤を使い、policyと提出資格だけを変える |

## 5. 実装開始判定

結論は **条件付きGO** とする。

最初に実装してよい範囲:

1. scenario schema、主体別三設定、RelationConfig、ExperimentConfig、ScenarioPolicy。
2. 単一QPU/FIFOのjob state machine。
3. revision付き`QpuStateStore`。
4. sample orchestration。
5. 共通`ResetModel`境界、有効観測model、四つの対照条件。
6. training、hidden evaluation、graderの縦切り。

次を満たすまで論文再現完了とはしない。

- F-C1〜F-C4の受入testが通る。
- simulator-derived値と論文報告値の表示が分離される。
- 正規推定器、定数予測、対照条件を使ったgrader監査が通る。
- UIから内部真値・被害者結果へ到達できないことを権限testで確認する。
