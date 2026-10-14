# 不完全Reset論文再現シナリオ契約

契約ID: `AS-RESET-CCS22-SAME-QUBIT-v0`

状態: **暫定契約・一部実装**。設計対象とReset課題専用v0数値は固定済みだが、公開境界を含む受入検証は未完了。

最終更新: 2026-10-02。

実装追跡（2026-10-02）: `relation.resource_binding`へ両者のQPU ID・物理量子ビットIDを明示し、service inventory・配置方針・初期`QpuStateStore`のIDと開始前に照合する。隔離対照は同一QPUの異なる量子ビットIDへfresh zeroの局所状態枠を割り当て直すeventを記録する。複数QPUや二量子ビット同時状態は扱わない。

実装追跡（2026-09-28）: 内部orchestratorは、一queryの各shotについて被害者準備、service Reset、攻撃者測定を実jobとして反復し、sessionの観測予算を実行前に予約する。hidden graderは明示policyから0/1両方のcaseを生成し、評価用seedを通常queryから分離して、aggregate scoreと合否だけを返す。model mode排他、perfect-reset／isolated-resource／観測無効の対照、定数推定器検査を含む内部受入reportまで実装した。Reset課題専用v0ではReset成功確率0.90、成功時励起人口0.02、Readout誤分類0.02/0.04、標準32 shots/case、hidden 20件中16件正解、候補signal下限0.45 bit・優位性0.30、対照上限1e-9を採用した。未信頼推定器sandbox、公開API・疑似terminalを含む第10節の受入完了ではない。

実装追跡（2026-09-29）: 公開training結果のlabel・shots・countsだけから頻度閾値推定器を構築し、hidden graderへ渡す内部経路を実装した。trainingはlabelごと10 query、合計20 query・640 shotsを採用した。課題modeはbounded preflight済みseed bundle、自由・研究用途は無選別seedとし、candidate・perfect Reset・isolated resource・定数推定器の条件を内部testで確認した。[利用者学習校正](reset-leakage-visible-training-calibration-2026-09-29.md)を参照。

## 1. 目的と根拠

最初の縦切り課題は、Mi、Deng、Szeferによる
[*Securing Reset Operations in NISQ Quantum Computers*](https://doi.org/10.1145/3548606.3559380)
（CCS 2022）のうち、**同じ物理量子ビットを被害者から攻撃者へ順次再割当する際のReset越し情報漏洩**を再現対象とする。

このscenarioの`execution_regime`は`nisq_physical`である。これは本契約の適用条件であって、攻撃シミュレーター全体の不変条件ではない。`fault_tolerant_logical`では、syndrome、decoder、QEC cycleを通した論理的影響まで別途モデル化しない限り、本契約を適用しない。

Magnardらの
[*Fast and Unconditional All-Microwave Reset of a Superconducting Qubit*](https://doi.org/10.1103/PhysRevLett.121.060502)
は能動Resetの物理的背景として参照するが、攻撃者・共有サービス・情報漏洩の脅威モデルの根拠にはしない。

この契約の目的は、論文の全装置を忠実に複製することではない。論文が示した因果関係を、根拠と近似範囲を追跡できる一つの実行可能な体験パイプラインとして再現することである。

## 2. 適合性の呼称

実装と表示では次の呼称を区別しなければならない。

- `paper_traceable`: 論文の状態、順序、権限、観測、評価指標との対応が記録されている。
- `behavioral_reproduction`: 論文と同じ状態順序・権限・因果経路の下で、シミュレーターが算出した条件付き出力分布により漏洩挙動を再現する。論文の数値への一致は要求しない。
- `mechanistic_reproduction`: 測定、誤分類、条件付きX、緩和、Readoutを別過程として再現する。
- `hardware_replication`: 同一装置・校正値・実行環境で実験値を再取得する。

初期版が名乗れるのは、受入試験通過後の `paper_traceable` と `behavioral_reproduction` までである。
実機データと機構分解がない状態で `mechanistic_reproduction` または `hardware_replication` と表示してはならない。

## 3. 固定する範囲

### 3.1 含むもの

- 単一の仮想超電導QPU。
- `nisq_physical` execution regime。FTQC profileは本契約の対象外。
- 単一の物理量子ビットを被害者、Reset、攻撃者の順に利用する時間共有。
- 被害者状態に依存するReset後の攻撃者観測分布。
- 攻撃者による学習用実験、反復測定、推定器作成、提出。
- 被害者出力への直接アクセスを禁止した論理分離。
- 論文に対応する学習フェーズと未知ケース評価フェーズ。

### 3.2 初期版から除くもの

- 隣接量子ビットへのReset／Readoutクロストーク。
- 同時マルチテナント実行。
- 高エネルギー状態 `|2>`・`|3>` を使う攻撃。
- Secure Reset防御の実装と比較。
- 模擬装置故障、実行中取消、失敗注入。
- IBM Jakarta、Lagos、Perthの実機値の再取得。
- 論文の全72状態・最大32回Resetを必須とする製品課題。

除外項目は不存在と主張せず、後続profileとして追加する。

## 4. 保護資産と違反条件

保護資産は、被害者が計算終了時に持つ非公開の出力状態、初期版では非公開の出力bit `v ∈ {0, 1}` である。課題背景でこれを秘密として扱う場合も、論文が直接扱うのは被害者の計算出力であることを表示する。

サービスが意図する保証は次の二点である。

1. 攻撃者は被害者の回路、測定結果、非公開出力bitを直接読めない。
2. Resetと所有権移転後の攻撃者観測は、被害者の非公開出力に実用上依存しない。

課題が示す違反は、1を維持したまま2が破れ、攻撃者が自分の測定結果だけから秘密を統計的に推定できることである。

## 5. 設定契約

### 5.1 `VictimConfig`

初期graded profileでは次を固定する。

- `logical_qubits = 1`
- `secret_domain = [0, 1]`
- `prepare = |v>`
- `final_measurement = true`
- 同じ評価batch内では、各試行の秘密をScenarioPolicyが非公開で選ぶ。
- 被害者の回路、秘密、測定結果は攻撃者APIへ返さない。

論文追跡用の拡張profileでは、Bloch球角度 `theta`、`phi` と被害者測定の有無を設定できる。

### 5.2 `AttackerConfig`

初期graded profileで攻撃者に許す能力は次に限定する。

- 自分へ割り当てられた量子ビットへ測定jobを送る。
- 自分のjob ID、状態、測定counts、1-output frequencyを読む。
- 同じ公開device profile上で、ラベル付き学習実験を反復する。
- ローカルで推定器を作り、推定プログラムまたは予測結果を提出する。
- `AttackerConfig.reset_leakage_access.labeled_training_enabled = true`
- `AttackerConfig.reset_leakage_access.victim_observation_enabled = true`

禁止する能力は次のとおり。

- 被害者job、回路、秘密、測定countsへの直接アクセス。
- `QpuStateStore`、Reset成功／失敗branch、真の密度行列の参照。
- 物理量子ビット割当、Reset回数、秘密値の変更。
- 管理API、grader用真値、乱数seedの参照。

論文の「被害者実行を制御でき、狙った被害者と同居できる」という強い仮定は、攻撃者へ管理画面を与える形では実装しない。`ScenarioPolicy`が実験条件として被害者実行と再配置を保証し、課題説明にその仮定を明示する。
自由modeでは二つのaccess fieldを編集でき、無効にした過程の実行要求はjob作成・予算消費前に拒否する。graded presetでは`attacker.reset_leakage_access`を固定する。今後追加する仮定も、所有する設定区分、根拠、有効／無効時の挙動を定義し、暗黙の常時有効処理にしない。

### 5.3 `QpuServiceConfig`

- `qpu_count = 1`
- `queue_policy = on_demand_fifo_v1`
- 被害者と攻撃者は別tenantとして扱う。
- 同じ物理量子ビットを `victim -> reset -> attacker` の順に割り当てる。
- Resetは所有権移転前にserviceが実行する。利用者がReset結果を直接受け取らない。
- Reset回数は初期graded profileでは1回に固定する。
- 状態の正本は`QpuStateStore`に置き、各確定event後にrevisionを進める。
- victim、reset、attackerの各eventは一つの量子ビットtimeline上で全順序を持つ。
- host処理時間を装置時間へ加算しない。

### 5.4 `ScenarioPolicy`

- `mode = graded`
- 主体別三設定、配置等のrelation、実験条件、物理profile、secret生成、評価seed、提出条件を固定する。
- 公開説明には論文、採用した強い仮定、近似、対象外範囲を表示する。
- 学習phaseでは既知ラベルを使えるが、評価phaseの秘密・seed・内部状態は秘匿する。
- graded presetを編集したsessionは正式提出資格を失う。
- free modeでは同じ基盤を使えるが、論文準拠表示は設定差分に応じて失効する。

現行実装ではmode非依存の主体別三設定を`ScenarioTemplate`へ保持する。後続schemaでは配置等を`RelationConfig`、training等を`ExperimentConfig`へ移し、同じscenario定義へ自由または課題policyを適用する。課題専用の物理model・scheduler・状態経路を作ってはならない。自由modeの編集は不変templateを直接変更せず、編集用copyから新しいtemplate版を作る。

## 6. 一試行とshotの意味

一つの観測sampleは、次の全系列を一度実行した結果でなければならない。

```text
victim preparation
  -> victim final measurement（profileで有効な場合）
  -> service reset
  -> ownership transfer
  -> attacker measurement
  -> attacker-visible result
```

`shots = N` は、この全系列を同じ条件でN回反復する意味とする。
一度だけ被害者を実行した後、同じ残留状態を攻撃者がN回測定する意味にしてはならない。最初の攻撃者測定が状態を変えるためである。

各sampleの開始時にはscenarioが指定する初期化済み状態から被害者系列を開始する。sample間のホスト実行順や処理落ちは装置状態へ混入させない。

## 7. Resetモデル契約

### 7.1 論文追跡profile

論文の有効誤差チャネルは、被害者角度`theta`に対するReset後の1-output probabilityとして次を持つ。

```text
Q(theta) = a * [b * sin^2(theta / 2) + (b - 1) * theta / pi] + c
```

この式は論文との追跡性、傾向比較、評価方法の参照に使う。初期製品profileの`a`、`b`、`c`を論文グラフへcurve fittingして生成してはならない。

論文追跡検証で`Q`を使う場合は**攻撃者が観測する有効分布**として扱う。Reset内部状態とReadout誤差へ根拠なく分解せず、counts生成後に同じReadout誤差を再適用してはならない。

### 7.2 既存CPTPチャネルの扱い

既存の

```text
E(rho) = (1 - s) rho + s tau Tr(rho)
```

は一般的な不完全Resetの現象論モデルであり、論文の測定＋条件付きXや`Q(theta)`と同一ではない。

初期二値profileでは、宣言した実行条件からシミュレーターが算出する`P(attacker=1 | v)`、攻撃者観測分布、対照条件を版管理する。既存CPTPチャネルを使う場合は、これらを独立試験で満たしたときだけ近似backendとして利用でき、`behavioral approximation`と表示する。

一般の`theta`、コヒーレンス、複数Reset、反転したsigmoidを扱う論文追跡profileでは、このチャネルだけを使って適合とみなしてはならない。

### 7.3 共通モデル境界と拡張

挙動再現を課題専用の例外として実装してはならない。すべてのReset backendは共通の`ResetModel`境界へ登録し、少なくとも`model_id`、`model_version`、`fidelity_level`、入力状態、Reset条件、状態遷移または有効観測分布、provenanceを扱う。

orchestrator、scheduler、`QpuStateStore`、graderは特定の論文名やmodel IDで処理を分岐せず、`QpuServiceConfig`が選んだmodelの能力宣言を通して利用する。初期の有効観測modelと、後続の測定instrument・誤分類・条件付きX・緩和を分解する機構modelは、同じ境界で交換または比較できなければならない。

数値profileには、model版、物理parameter由来の`model_configuration_id`、`model_mode`、victim-dependent／fixed-zero入力区分、物理・サービス設定、Reset回数、被害者測定の有無、shots、seed policy、算出日時を保存し、`source = simulator_derived`と記録する。manifestとruntimeはconfiguration IDを照合する。`state_transition_with_readout`と`effective_observation_only`は排他的に扱い、現行stateful runtimeは前者だけを受理する。論文値との完全一致は受入条件にせず、実行条件、因果経路、対照条件、再現性を受入条件にする。

## 8. 学習・評価・提出

### 8.1 論文追跡用検証profile

- `theta ∈ {0, pi/8, ..., pi}`
- `phi ∈ {0, pi/4, ..., 7pi/4}`
- 合計72状態。
- 被害者測定あり／なしを分ける。
- 学習setと評価setを別seedで生成する。
- 既定shot数は各構成8192。
- Reset回数をprofile fieldとして記録する。1〜複数回を比較し、最大32回は拡張検証とする。
- SNR、二値分類accuracy、角度予測lossを算出する。

8192回の密度行列発展を逐次実行する必要はない。同一条件の理論確率を一度計算し、seed付きbinomial samplingでcountsを生成してよい。ただし分布同値試験を必要とする。

### 8.2 最初の課題profile

- 被害者状態は`|0>`または`|1>`。
- 被害者測定あり。
- Resetは1回。
- 攻撃者はラベル付き学習runから二つの条件付き分布を推定する。
- 提出物は、攻撃者観測countsを入力として被害者の非公開出力bitを返す推定器とする。
- 評価case一件では、graderが非公開出力bit `v`を一つ選び、`victim -> reset -> attacker`の全系列を指定shot数だけ反復して一つのcountsを作り、提出推定器は一つのbitを返す。
- v0のhidden評価は0/1各10件の20件とし、16件以上の正解を合格とする。
- `evaluation_case_count`はgrader内部の統計精度を決める値であり、攻撃者へ許可する観測回数ではない。
- 攻撃者の`shots_per_query`、`training_query_limit`、`victim_observation_limit`、再試行回数、結果粒度を`ScenarioPolicy`の観測予算として別に制限する。
- hidden評価では提出推定器をserver側で実行し、利用者へ返すのはaggregate scoreと合否だけとする。個別caseのcounts、正解bit、seedを返してはならない。
- 一episodeの保護資産は初期版では一bitに限定する。標準観測は32 shots/caseとし、候補の決定論的相互情報量は0.45 bit/case以上、攻撃者優位性は0.30以上を受入下限とする。training query数等はScenarioPolicyのpresetごとに制限する。
- 直接の秘密参照、seed参照、固定順序への依存を検知した場合は失格とする。

件数と合格閾値は論文値ではなく、校正後に所有者が承認した製品上の判定policyである。`source = product_policy`、`scope = reset_leakage_challenge_only`として版管理し、shotsやsignal条件を変える場合は再校正する。

## 9. 対照条件

最低限、次の四条件を同じseed系列で比較する。

1. `insecure_reset`: 課題対象。被害者依存が残る。
2. `perfect_reset`: 攻撃者観測が被害者秘密から独立する。
3. `no_shared_physical_qubit`: 別物理量子ビットを割り当て、同一量子ビット残留を遮断する。
4. `direct_leak_guard`: 被害者結果APIを禁止した状態でも攻撃が成立することを確認する。

`perfect_reset`または`no_shared_physical_qubit`でも同程度に成功する推定器は、Reset越し漏洩を示したとは判定しない。

内部v0では`no_shared_physical_qubit`を`isolated_physical_resource`として実装し、前tenant状態をfresh zero resourceへ置換する割当eventの後、同じReset・測定経路を通す。これは比較用の閉じた対照であり、特定クラウド事業者の割当実装を再現したという主張ではない。
内部v0の割当記録では被害者を同一QPUの量子ビット0、攻撃者を量子ビット1とし、各shotで局所状態枠を切り替える。別QPUを指定したReset経路は対応外として開始前に拒否する。隔離対照の「漏洩しない」はこの近似の下での条件付き出力分布の比較であり、別QPUに一般化した結果ではない。

## 10. 受入条件

実装は次をすべて満たすまで、この契約を`検証済み`へ上げない。

- 一つのpresetから、被害者実行、Reset、再割当、攻撃者観測、提出、判定まで完走する。
- 攻撃者APIから被害者の秘密・結果・内部状態へ到達できない。
- ScenarioPolicyの観測予算を超えるquery、shots、再試行を拒否し、hidden評価の個別結果を公開しない。
- 同じseed、設定版、model版でcountsと判定を再現できる。
- `insecure_reset`では登録したシミュレーター由来の`P(attacker=1 | v)`を統計許容差内で再現する。
- `perfect_reset`と`no_shared_physical_qubit`では秘密と攻撃者観測の条件付き差が許容閾値以下になる。
- 承認済みのhidden評価件数と閾値に対し、正規の推定器は合格し、定数予測は合格できないprofileを用意する。
- 定数予測、seed推測、内部真値参照では合格できない。
- 状態revision、job所有者、物理量子ビット割当、Reset event、公開結果を監査記録へ残す。
- 論文事実、製品上の選択、仮定、近似、未検証値をUIと実行記録で区別する。

内部受入reportは上記のうち候補signal、perfect-reset／isolated-resourceの独立性、観測無効、定数推定器、hidden shot整合を採用閾値で機械判定する。利用者dataだけによる推定器学習、training予算、instance preflightまで内部実装済みである。共通Application API上のpreset完走、公開API非漏洩、永続監査は共通基盤側で未完了なため、契約全体はまだ`検証済み`ではない。

## 11. 追跡表

| 契約要素 | 根拠 | 扱い |
| --- | --- | --- |
| 同一量子ビットを被害者後に利用 | Mi et al. §4–6 | 初期scopeへ採用 |
| Reset前後の情報漏洩 | Mi et al. §5.3、§6.2.1 | 課題の因果 |
| 測定＋条件付きX | Mi et al. §5.1 | 機構再現は未実装 |
| 攻撃者の反復測定・予測可能な同居 | Mi et al. §4 | ScenarioPolicyが保証 |
| 被害者出力への直接アクセス禁止 | Mi et al. §4 | API不変条件 |
| 72状態・8192 shots | Mi et al. §6.2.1 | 論文追跡profile |
| 学習／評価分離 | Mi et al. §6.3 | 採用 |
| 二値分類accuracy | Mi et al. §6.4.2 | 初回graderへ採用 |
| hidden評価件数・合格閾値 | 製品判断 | Reset課題専用v0として20件中16件を承認済み |
| 成功確率混合CPTP | 既存内部モデル | 条件付き近似のみ |
| Magnardのall-microwave reset | 物理背景 | 攻撃権限の根拠にはしない |

## 12. 変更規則

- 論文根拠と異なる権限、状態順序、観測を採用する場合はscenario IDのminor以上を上げる。
- 保護資産、攻撃方向、Reset機構、grader意味論を変える場合はmajorを上げる。
- 数値profile、seed policy、shot上限だけの変更はprofile versionを上げる。
- 契約変更時は、[決定事項](decisions.md)、[進捗台帳](contract-and-domain-progress.md)、受入testの対応を同時に更新する。
