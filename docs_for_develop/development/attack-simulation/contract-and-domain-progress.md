# 契約・検討領域の進捗台帳

最終更新: 2026-10-16（下の補正と明記した行のみ。他の行は2026-10-05時点の記載で、全項目の再検証はしていない）。最新の実装・検証証跡は[進捗](progress.md)を参照。以下の既存証跡は各記載時点の確認であり、全項目の最新検証ではない。

D56による所有境界: 共通時間契約（C03）は最初の課題選択に従属しない。service／物理profileと被害者workloadが共通契約を使い、課題presetが固定・秘匿・評価を束ねる。予算を使わない構成と明示権限ゼロも保持する。D57/D58に沿い内部clock／有限workloadのtyped／JSON入力、認証済み宣言到達runner、Program準備／継承と認可、全体topology metadata／局所state bindingを追加した。閉鎖有限sourceでは物理時刻生成・原子的確定・自己結果進行も接続した。live HTTPとの時計対応、Resetの共通Program移行は未完了。[詳細と実装境界](architecture.md#共通契約と課題設定の依存方向2026-10-04)を参照。

攻撃シミュレーションの8契約と、それだけでは覆えない9検討領域を同じ場所で追跡する。詳細設計をこの表へ詰め込まず、決定文書・実装・テストへの索引として使う。

## 状態の定義

直近の五判断はD57で[共通基盤v1の暫定契約](common-foundation-v1-contract-proposal.md)へ昇格。D58で全体トポロジー・回路幅・局所物理領域の分離と複数qubitを共通境界で扱うことを確認した。採否待ちは解消したが、C01〜C08全体の実装・受入が完了した意味ではなく、下表は残件を含む。

| 状態 | 意味 |
| --- | --- |
| `未着手` | 問いだけが存在し、方向も決めていない |
| `方向合意` | 目的・原則は合意したが、検証可能な契約ではない |
| `提案中` | 定義案があり、採否・未決定値が残る |
| `暫定契約` | 初期版へ実装可能な規則を選択済み |
| `一部実装` | 契約の一部にコードとテストがある |
| `検証済み` | 対象範囲の受入条件と検証証跡が揃った |

状態は最も進んだ部分ではなく、その項目全体で保証できる段階を記載する。

## 8契約

| ID | 契約 | 状態 | 現在固定していること | 次に決めること・証跡 |
| --- | --- | --- | --- | --- |
| C01 | 状態所有 | `一部実装` | revision付きin-memory `QpuStateStore`を正本とし、状態・owner・device time・履歴を保持。有限宣言列adapterでdetached候補検証とstate／job／frontierの共有gate確定、未公開更新のbounded補償を追加。D57の初回復旧保証は同一process内まで | lifecycle全体のcheckpoint整合・保存期間・復旧受入は未完了。未公開transaction補償を一般復旧・永続化と同一視しない。永続snapshot・migration・server再起動復元は後続。[v1契約](common-foundation-v1-contract-proposal.md) |
| C02 | ジョブ | `一部実装` | 共通認証／tenant・Program・冪等性・host上限と単一QPU/FIFOを実装。有限sourceに加え単一live driverへ受付・queued取消・FIFO再計画・物理START／FINISH・結果進行を接続。全依存一括差替え・exact再送／未公開suffix補償、入力／START同着／重複取消の型付き拒否を検査 | 正式HTTP契約・本mount・管理者bootstrap、Reset予算統合、実行中取消profile、模擬故障・後処理失敗、総資源／性能／復旧受入。[共通client](common-command-client.md)と[進捗](progress.md)、[認可契約 §3.8](service-runtime-contract-proposal-2026-09-23.md#38-principal認可job所有認可境界は暫定契約) |
| C03 | 時間 | `一部実装` | D51/D53/D57/D59の三時間・目標／確定分離。単一live driverで同sample受付・全source gate・厳密な過去cutoffとcontrol作用保護・物理seal／確定・arrival投影／reply releaseを統合。queued下界には既知／将来取消を含め、host遅延不変性を検査 | HTTP async ingress／delivery・実運用loop、模擬故障trace、根拠表示／係数採用、性能／checkpoint復旧受入。[live接続範囲・残件](parallel/live-ingress-boundaries-handoff.md) |
| C04 | Reset | `一部実装` | CPTP・registry・校正・交代Reset・課題v0に加え、共通Programのprepare／inherit、複数qubit／spectator、service／actor／scenario／principal認可、Resetなしのreadout adapterを実装 | 旧課題移行と予算／物理列照合・再校正／対照受入、機構model。共通継承の追加を旧課題移行完了としない。[共通client](common-command-client.md)と[論文契約](reset-leakage-paper-reproduction-contract.md) |
| C05 | 権限・観測 | `一部実装` | 三policy積集合・principal grant／tenant scope・共通結果投影、仮想資格情報からのprincipal導出を実装。実owner context＋仮想Bearerを二段検査し、binding／受付／破棄競合をfence。live driverへarrival時点の状態／結果投影・release待ち・元principalへのdetached返信を接続 | 実ブラウザ所有認証／CSRF・管理者bootstrap、正式API／本mount、principal割当管理、公開粒度のprofile化。test resolverを公開認証の証明にしない。[共通操作契約](common-command-observation-contract.md) |
| C06 | 課題 | `一部実装` | 同一`ScenarioTemplate`へ自由／課題policyを適用する基盤、field固定・秘匿、観測予算、evaluator指定を実装。製品の課題はpreset（`api/attack_challenge.py`）と評価器が担い、評価器は個別caseを隠して集計結果だけ返す（2026-10-16補正: 旧orchestratorは削除済み）。v0の観測量・評価件数・合格数を承認済み | instance秘密・seedの永続化、未信頼コードsandbox、解説後の扱い、API受入test |
| C07 | 再現 | `方向合意` | seed・設定版・実行履歴を保存対象とし、復旧は新規開始と区別（D51）。D57でページ再読み込み／同一process内復旧を初回必須、server再起動復元を後続とした | 壁時計入力・checkpoint整合、旧executor切替・同一所有session再接続、保存期間、replay範囲／誤差、障害注入。再起動保証は別の後続受入 |
| C08 | 局所計算 | `提案中` | 大規模topologyを保持し、閉じた局所領域だけを物理計算。event時点の有効関係と持越し相関で領域を選び、必要相関を保てない／資源超過の構成はpreflight拒否する原則に合意。全体案は[共通契約案](local-physics-approximation-contract-proposal.md) | 必要相関の操作的定義、状態表現、対象observable、許容誤差と性能基準、拒否条件の検証、benchmark |

## 9検討領域

| ID | 領域 | 状態 | 現在固定していること | 次に決めること・証跡 |
| --- | --- | --- | --- | --- |
| A01 | 因果・保護資産 | `暫定契約` | 最初の課題は非公開出力bit→被害者状態→service Reset→同一qubit上の攻撃者counts→推定。perfect reset等の負の対照を必須化。Reset課題専用v0 profileと決定論的受入testを採用済み | 有限sampleの統計許容差、公開経路の権限test、UI上の仮定表示 |
| A02 | 脅威モデル | `一部実装` | 製品全体は宣言された攻撃者→被害者の共有資源・経路を成立条件とする（D48）。古典的準備を開始条件として与えられる。追加仮定は所有設定へ明示し、自由modeで切替、課題modeで固定する。Reset課題の学習・被害者観測accessを実装 | 標的service/version、攻撃者role、能力取得経路、同一QPU配置等の残る仮定の設定化と根拠。追加経路の採用範囲は未決定 |
| A03 | 被害者 | `一部実装` | D57の有限typed／JSON workload・認証済み共通runner。閉鎖sourceでは共通Program・時計／物理START／FINISH／release・自己counts分岐／反復を接続し、host遅延不変性を検査 | live HTTPと宣言sourceの統合、control返信の依存差替え、具体的な課題操作列／秘密入力。閉鎖sourceの成功をlive被害者の自律進行完成と扱わない |
| A04 | 統計・判定 | `一部実装` | 一評価caseの意味を固定。観測予算、二値相互情報量、Bayes最適精度、hidden集計判定、負の対照を実装。Reset課題専用v0のshots・training 20 query・件数・閾値を承認し、公開training countsだけからの推定器学習を実装（2026-10-16補正: bounded seed preflightと受入監査は旧経路ごと削除）。自由／研究用途は無選別seed | 共通提出・flag発行、有限sample指標を導入する場合の統計許容差、private report永続化 |
| A05 | parameter根拠 | `一部実装` | device実測・文献・仮定・教材用強調を区別する方針。Reset課題専用v0を`product_policy`、`reset_leakage_challenge_only`として固定し、物理configuration IDと校正記録へ結合 | 共通provenance schema、UI表示、他課題・実機profileとの混同防止test |
| A06 | service境界 | `一部実装` | 主体別設定、Relation、Experiment、Policyをmanifestへ解決し、内部FIFO、QPU状態、観測集約、access検査まで接続（2026-10-16補正: 旧Reset漏洩RuntimePlanは削除し、製品は`SharedQpuRuntime`と課題presetを通る）。共有QPU型の`attack_path`宣言とservice・runtimeの対応可否、`resource_binding`と実行状態のID照合を追加。課題だけがfield固定・秘匿・提出指定を持つ | Simulated QPU Service API、Application API、instance境界、残るfield所有、error mapping、永続化、内部真値の非漏洩API試験。複数QPU／サービス層経路を導入する際のdevice識別・経路authority・検査境界は未決定 |
| A07 | scenario・版管理 | `一部実装` | 主体・関係・実験／評価を含む`ScenarioTemplate`、仮定根拠、free override、content-addressed manifest、in-memory storeを実装（2026-10-16補正: 旧Reset漏洩RuntimePlanは削除）。`execution_regime`をprofile値とし、NISQ対応範囲は`circuit_program.py`が製品経路で検査 | 永続schema、profile registry、FTQC runtime、互換性、migration、準拠表示失効、compile cache・保存期間 |
| A08 | 品質・性能 | `一部実装` | 物理v0の全backend 813件と、2026-09-26のReset・policy・compiler・runtime・orchestrator・grader・acceptance焦点64件が成功。hot pathはauthoring treeを保持せず、現行小規模profileのcompile最良値は約45.5 us/回。直前の全599件走査は依存未導入31 error・Rust未配置2 failure | 大規模workload benchmark、runtime overhead、計算量見積り、timeout、checkpoint、数値許容差、障害注入 |
| A09 | 体験 | `一部実装` | 攻撃側画面だけで操作し、一本道tutorialと自由な調査を両立する。Reset課題の案内つきtutorialと攻撃IDE（キーボード操作・中立な案内）を実装済み（2026-10-16補正） | 操作列、hint、可視化、提出feedback、最初の課題のuser test |

## 共通化の到達度（2026-10-16の見立て）

[仮定・過程変化・不変の基盤](assumption-process-model.md)（D74）の三層で見た到達度。上の表の各行は個別の契約・領域の状態で、これは層ごとの総括。

- **進んでいる（管）**: 共通受付・認可・tenant scope・冪等性・FIFO・host_clock時計・確定イベント列・manifest凍結・自由／課題の同一`ScenarioTemplate`・製品への接続（共通コマンド経路、WebSocket、ブラウザ内Python、環境の作成と破棄）。`SharedQpuRuntime`は課題を知らない。
- **Reset1本にしか通っていない（流れる中身）**: 物理（Resetのみサービスへ接続。残留ZZ・駆動クロストーク・相関Readoutは内部部品で未接続）、被害者workload（有限・決定的な宣言列に限る）、提出環境（課題2つ分の評価器のみで、共通の提出・flag発行はない）。
- **未着手**: 同時実行（FIFOのみ）、局所計算C08（提案段階）、永続化・復旧・管理者bootstrap、そして「課題を足しても基盤を触らない」ことの実証（課題はReset系の2つだけ）。

次の最初の実証は、クロストークとGroverの課題で行う。基盤の拡張は同時共有の一般機能に限る（D74）。

## 横断順序

2026-10-04のD54後続で内部局所T1/Tφ更新器・数値試験・kernel計測を追加し、その後閉鎖有限sourceへ待機物理と原子的commitを接続した。現在C03は`一部実装`で、live全体・C08/A08まで受入済みとはしない。[進捗](progress.md)と[サービス対応](service-time-and-workload-comparison.md)を参照。

2026-10-04のD52で、C05/A06の環境管理者をメタ層、参加者資格情報の発行をサービス層へ置き、両modeの管理者設定を開始前のみ変更可能とした。[資格情報境界](credential-delivery-boundary.md#環境管理者とサービス発行の境界2026-10-04合意)を参照。既存の設定凍結・内部authorityだけで管理者認証や発行APIまで実装済みとは扱わない。

1. C01/C02の状態所有・失敗・永続化の残件をレビューする（C02/C05の認可意味論は2026-10-02に暫定契約化済み）。
2. [不完全Reset論文再現契約](reset-leakage-paper-reproduction-contract.md)を使い、A01/A02/A03のschemaと受入testを実装する。
3. C03/C04/C05とA04/A05/A06を同じ因果経路上で確定する。
4. C06/C07/C08とA07/A08/A09を加え、最小縦切りの受入条件を作る。
5. 実装後、項目ごとにcode path、test、確認日を追記して`検証済み`判定を行う。

この順序は、後続項目を考えなくてよいという意味ではない。前段の決定が後段を実装不能にしないか、各更新時に全17項目を横断確認する。
