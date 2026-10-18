# 決定事項と提案の区別

最終同期: 2026-10-05。実装状況は [progress.md](progress.md) を参照。

## 合意済み方針

| ID | 方針 | 設計への反映 |
| --- | --- | --- |
| D01 | 攻撃者環境を独立させ、高機能にする | CTF回答者の環境に相当するターミナル・ツール・コード実行を持つ。回路編集と送信ボタンだけで済ませない |
| D02 | 攻撃者環境と標的サービスの抽象化範囲を取り決める | 高機能化の必要性は合意。OS、通信、サービス内部の再現範囲は未決定 |
| D03 | ローカル解析による正解の直接取得への耐性は割り切る | 教育用途の対象外とする。ただし通常APIのアクセス権と、実行コードのホスト保護は別途必要 |
| D04 | 時間・計算量・利用予算の設計を明確にする | 上限値や仮想時計の仕様は未決定 |
| D05 | 一直線で終えられるチュートリアルを用意する | 最初の成功まで案内する。自由な自作コード・調査は維持する |
| D06 | CTFのflag提出に対応する達成判定を持たせる | 正解提出、ジョブ提出、スクリプト評価、flag発行方式は未決定 |
| D07 | 共通ワークスペース上へ問題ごとの提出機能を設定する | 同じ実行基盤上で`free`と`graded`のセッション方針を分ける。別製品・別物理実装にはしない |
| D08 | 共有方式の切り替えを被害者側の設定から扱えるようにする | 画面の入口はまとめる。被害者側は希望する利用条件を指定し、実際の共有方式・割当・保証はQPU service側が所有する |
| D09 | 物理モデルだけを先に固定する | 基準版は作成済み。新モードのUI・サービス・攻撃者権限まで固定したわけではない |
| D10 | 課題内で操作できるのは攻撃側画面のみ | 攻撃行為はすべて攻撃側環境から行う。被害者・実機を直接操作する画面を攻略手段として与えない |
| D11 | 課題より一段上にシミュレーター設定を置く | 実機側・被害者側を設定できる。D07の自由度はこの階層にもあり、課題中の攻撃者権限と混ぜない |
| D12 | CTF型プリセットに攻撃者・被害者・実機側の設定を割り当てる | プリセットを読み込んで共通基盤上に課題を構成する。提出・判定の具体方式は引き続き未決定 |
| D13 | 量子コンピューターを通したセキュリティインシデントの体験を提供する | 物理現象の操作だけで終えず、情報取得・結果への干渉などの被害と結び付ける。量子暗号は今回の対象外 |
| D14 | 古典的手法の有無にかかわらず、量子コンピューター特有の攻撃・懸念を見せる | 古典的ハッキングの実行を必須にしない。何を開始条件として与えるかは未決定。現実に古典的準備が不要と主張するものではない |
| D15 | 主要な攻撃目的は、結果への干渉・改ざん、処理内容の推定、埋め込まれた秘密の推定 | ユーザーが明示した三目的。D31の初期課題は、被害者の非公開出力bitを推定する情報漏洩として具体化し、他二目的は後続課題とする |
| D16 | 手動設定またはプリセット適用後、攻撃側画面で調査・操作し、CTF型では課題別に判定する | ユーザー提示の共通体験の骨格。キュー・被害者の観測可否や結果の漏洩経路は未確定 |
| D17 | 攻撃者・被害者の環境を分離し、共有QPU型シナリオでは同じ仮想QPU世界へ接続する | 当該シナリオでは共通スケジューラ、仮想時計、物理状態へ両者の要求を接続し、独立した物理計算の事後合成にはしない。製品全体への適用はD48で更新 |
| D18 | 同一実機への参加を要する課題では、その成立までを開始条件として与えられる | 省略した古典的準備、権限、配置条件を課題に明記する。同一実機を要しない課題へこの条件を強制しない |
| D19 | 大規模QPUは全体トポロジーを保持し、物理計算は閉じた局所領域に限定する | 被害者・攻撃者・必要な干渉要素だけを計算し、領域外の影響を根拠なく追加しない |
| D20 | サービスコンフィグで操作・観測能力を制御する | 各能力に提供経路、権限、時間・配置条件、根拠を持たせ、矛盾した組合せを拒否する |
| D21 | 利用者側経過時間、シナリオ／仮想装置時間、ホスト処理時間を分離する | 要求到着はシナリオへ影響し得るが、内部計算時間を仮想QPUの実行時間や攻撃成否へ混ぜない |
| D22 | 攻撃ごとに現実の前提に対応した経路を持つ | ジョブ、パルス、計測器、部品供給等を一つの攻撃APIへ無理に統合しない |
| D23 | 観測能力に情報範囲と取得場所を定義する | 公開API、自分の測定、仮想計測器、公開出力、判定器専用証跡を区別する |
| D24 | 攻撃用物理機構は既存物理モデルを合成拡張し、当面Gate-aware/Pulseの公開選択肢にしない | 基準版V1を変更せず、内部版 `shared_qpu_isolation_physics_v0` としてReset、ZZ、駆動クロストーク、相関Readoutを実装する。サービス接続と公開は別決定とする |
| D25 | C01/C02初期版は単一QPU・FIFO・不完全Reset課題へ限定する | 初期実装を小さくする一方、queue/cancellation/observation policyを版付き境界にして将来方式を追加可能にする。拡張機能を先行実装する意味ではない |
| D26 | QPU状態をapplication/server再起動後も保持する | 永続snapshotから復元し、server再起動を物理Resetとして扱わない。媒体・保存期間・復元失敗時の管理操作は未決定 |
| D27 | 実行中取消の可否を設定で切り替える | QPU serviceのcancellation policyとattacker権限の両方が許した場合だけ取消要求を可能にする。完了済み物理eventは巻き戻さない |
| D28 | queue情報の可視性を攻撃シミュレーター設定で切り替える | 攻撃者権限の仮定として扱い、QPU serviceの公開能力、attacker観測権限、graded時のScenarioPolicyをすべて満たす範囲だけ見せる |
| D29 | 実行中取消・模擬故障までに完了した物理的影響を残す | 未実行eventだけを破棄する。内部実装例外は模擬故障と区別し、transactionを確定しない |
| D30 | 主体が所有する設定を`AttackerConfig`・`VictimConfig`・`QpuServiceConfig`へ分ける | 五区分との対応は、攻撃者→Attacker、被害者→Victim、装置・サービス→QPU Serviceを基本とする。D37により、主体間関係と実験条件を一主体へ押し込まず、別の設定責務として追加する |
| D31 | 最初の論文再現はMiらCCS 2022のReset越し情報漏洩を対象にする | 初期縦切りは同一物理量子ビットの被害者→Reset→攻撃者という順次再利用。MagnardらはResetの物理背景、Miらは脅威モデルと攻撃シナリオの根拠として分離する |
| D32 | 初期版は挙動再現とするが、共通のResetモデル境界を使う | 論文課題専用の例外分岐へ埋め込まない。同じregistry・実行契約の下で、有効観測modelを後続の機構modelへ交換・併用できるようにする |
| D33 | 数値profileは宣言した実行条件の下でシミュレーターが算出する | 論文値への完全一致やcurve fittingを目標にしない。論文は状態順序、攻撃仮定、評価方法、期待される傾向の根拠として使う |
| D34 | 攻撃シミュレーション設計文書をGit管理対象にする | `docs_for_develop/development/attack-simulation/`だけをgitignoreから除外し、その他の開発用文書は従来どおり除外する |
| D35 | graderの評価件数と攻撃者の観測予算を分離する | 統計的に安定した判定のためhidden評価件数は増やせるが、攻撃者が閲覧できるshots、再試行、training query、結果粒度はScenarioPolicyで独立に制限する。hidden評価の個別counts・正解は返さない |
| D36 | 追加する攻撃仮定・省略過程は自由モードの設定で有効／無効を切り替えられるようにする | 仮定を実装へ暗黙に埋め込まず、Attacker・Victim・QPU Serviceの所有設定へ置く。課題モードではpresetが固定し、無効化した設定を処理側で黙って有効扱いしない。未対応の組合せは拒否する |
| D37 | 仮定を主体・主体間関係・実験／評価へ分け、profileと制約付きoverrideで構成する | 通信・配置・時間関係を一主体のboolean群へ押し込まない。実行値と根拠記録を分離し、resolved manifestへ差分・版・seedを凍結する。正式field名と永続化schemaは提案中 |
| D38 | 編集用scenarioをsession開始時にcompileし、runtimeでは小さなRuntimePlanとRuntimeStateだけを使う | 根拠・UI metadata・文字列pathをjob／shotごとに走査しない。manifestをcontent hashで共有し、eventはmanifest IDを参照する。動的変化は時刻付きevent、物理計算量はcompile時見積りとする |
| D39 | NISQを攻撃シミュレーター全体の不変条件にしない | 実行方式はdevice／service profileの`execution_regime`で宣言する。現行Reset課題は`nisq_physical`だけに対応し、FTQCは論理量子ビット、QEC cycle、syndrome、decoderを持つ別profile／runtime拡張として扱う。対応外の組合せは黙ってNISQ近似せず開始前に拒否する |
| D40 | 攻撃者・被害者は同じ主体非依存の操作catalogを使う | 同一service profile内で役割専用の公開物理コマンドを作らない。実効能力はservice能力、ActorPolicy、ScenarioPolicyの積集合で決める。service間の能力差は維持する |
| D41 | 疑似ターミナルとUIを同じApplication APIのclientにする | ターミナルはQPU内部shellではない。UI専用・攻撃者専用の抜け道を作らず、認証・認可・結果投影を共通化する |
| D42 | 利用者が行う校正と内部受入校正を分離する | 利用者の統計は返却data、公開校正値、正当に知るlabelだけから算出する。simulator内部真値・hidden正解・grader証跡は通常主体へ返さず、統計量に来歴を付ける |
| D43 | Reset課題v0のtraining予算をlabelごと10 queryとする | 統計上の必要量8 query/labelに余剰2を加え、合計20 query・640 shotsを許可する。Reset課題だけのproduct policyであり、他課題の既定値にしない |
| D44 | 課題modeと自由・研究用途でhidden seedの選定方針を分ける | 課題modeはcanonical estimatorが合格し、定数推定器と負の対照が不合格になるseed bundleだけをpreflight後に発行する。自由mode・研究的評価はseedを選別せず、無作為または利用者指定で実行する。課題modeの結果を無作為instance上の攻撃成功率として扱わない |
| D45 | 共通QPU基盤では百量子ビット級のトポロジーを保持し、一回路の編集上限を8量子ビットとする | 8は物理計算領域の上限ではない。局所状態と明示的な疎結合による近似を主経路とし、相関を捨てられない条件は検証済みの結合計算か開始前拒否で扱う。数値上限と誤差・性能基準は比較計算とベンチマークで決める。[実行境界 §2](runtime-boundaries-and-resolutions.md#2-閉じた局所実行領域)を参照 |
| D46 | Rust化はPyO3変換コストを含めて採否を決める | PyO3の変換コストは概ねO(n)と見積もり、定数倍を含むend-to-end総費用より、対象ボトルネックの解消効果が十分大きい場合にRustを採用する。処理単位は可能な範囲でjob／実行windowへまとめる。Rust化する機能・時期は実測後に決め、二重の状態authorityを設けない |
| D47 | Python/Rust境界はバッチ・Python型検査・ゼロコピーを基本とする | Rust呼出しはjob／実行window等のバッチ単位にまとめる。公開入力・schema・domain型の検査はPython側で行い、Rustへは検査済みの型付きデータを渡す。データ配列はFFIで共有bufferを使い、不要な複製を避ける。buffer形式・連続性・dtype・所有期間等、ゼロコピー成立の条件とRust側で必要な安全検査は契約化する |
| D48 | 製品全体の成立条件は「攻撃者から被害者へ届く、設定で宣言された共有資源・経路がある」 | 同じQPUの利用は一つの経路であり、全課題の不変条件ではない。経路の共有対象、作用機構、権限、時間・配置条件、観測先をシナリオで宣言し、未対応・不成立の組合せは成立した攻撃として扱わない。最初のReset課題は同一QPU・同一物理量子ビットの順次再利用を維持する。複数QPUやサービス層経路の実装を承認したものではない |
| D49 | job認可を操作権限・tenant scope・結果可視性に分ける | 共通catalogの`program.submit`、`job.status.read`、`job.result.read`、`job.cancel`を使う。service capability ∩ ActorPolicy grant ∩ ScenarioPolicy allowlistに加え、個別job操作は認証context由来tenant scopeと結果projectionを検査する。同じtenant内の複数principalによる共有は許可し得るが、該当operationのgrantを必要とし、作成者だけに自動限定しない。service固有のcreator限定はprofileで追加できる。被害者runnerは同じcatalog/APIの内部principalで動き、外部から呼べる特権経路を持たない。外部応答から認可されないjobの存在を漏らさない。初期取消はgrantを持つtenant memberによるqueued jobのみ。認証方式・HTTP schemaは未決定。受入条件は[サービス実行契約 §3.8](service-runtime-contract-proposal-2026-09-23.md#38-principal認可job所有認可境界は暫定契約) |
| D50 | service条件は環境成立前の設定集に含め、成立後に固定する。疑似ターミナルの構文はservice条件で切り替えない | `QpuServiceConfig`／選択したservice profileに、提供操作、実行方式、入力形式、認証・取消・queue公開範囲、結果形式などの条件を宣言する。自由modeの変更は成立前のみ、課題modeはpresetで固定する。共通CLI構文で解釈した後、選択serviceの能力・主体権限・scenario方針で許可／拒否し、必要な入力変換と結果投影をadapterで行う。未対応の形式や組合せは明示的に拒否し、暗黙の互換化をしない。構文は後続機能追加で共通に拡張し得るが、serviceごとには分岐させない |
| D51 | ホスト処理能力超過時のフォールバックは、要求の競合・重複実行を防いだ再開とする | 復旧は環境破棄・新規開始と区別し、確定済み物理影響・受付順序・冪等性・予算の整合を守る。復旧で設定を編集可能に戻さない。通常の世界停止や攻撃失敗への読み替えは採用しない。閾値・checkpoint・時刻対応・復旧手順の具体化と検証は残る。[実行境界 §4.2](runtime-boundaries-and-resolutions.md#42-フォールバックとしての再開2026-10-04合意) |
| D52 | 環境管理者はメタ層に置き、参加者の仮想資格情報は下位のQPUサービスが発行する | 管理者設定は自由／課題の両modeで開始前だけ変更可能。課題presetの固定・秘匿を緩めない。サービス側発行をメタ管理者の認証・権限へ流用しない。具体的な管理者認証とbootstrap/APIは未実装・未確定。[資格情報境界](credential-delivery-boundary.md#環境管理者とサービス発行の境界2026-10-04合意) |
| D53 | 実経過に追従するworld目標と計算済みの確定位置を分け、先行未解決要求を越さず追い付く | ホスト計算待ちは状態確定・結果表示の遅れとして扱い、架空の被害者停止・攻撃失敗・機会消失・物理duration追加・Resetへ変換しない。予定生成と受付を確定待ちだけで停止しない。性能差による架空現象を防ぐことを受入条件とし、同じ宣言時刻の入力列への遅延注入で検証する。方式の採用であり、live接続・性能保証は未達。原点・対応倍率・通信境界・公開時刻・資源上限・被害者物理条件は別途具体化する。[時間境界 §4.3](runtime-boundaries-and-resolutions.md#43-時間接続の前提検査と判断待ち2026-10-04) |
| D54 | 宣言された待機区間の物理作用を、計算量に配慮して適用する | ホストの数値計算待ちを物理時間へ加算しない。固定係数等の適用条件を満たす場合は時刻差の一括更新を検討し、途中の物理eventを省略しない。モデル、係数、時刻接続、性能上限は未確定でruntime接続は未実装。[比較・適用候補・検証計画](service-time-and-workload-comparison.md) |
| D55 | 共通の一般サービスモデルから、範囲を定めた現実サービスへの特殊化可能性を設計上示す | FIFOを一般モデルの普遍規則にせず、方針・制約・主体別観測へ対応を持たせる。個別サービスを先行実装せず、比較範囲・対応写像・profile構成・確認義務・不明／非対応を示す。設定保存・表現可能性・実行対応を区別し、全サービスの完全再現とは主張しない。[定義と対応案](service-specialization-contract.md)。具体schemaと形式的確認は未完了 |
| D56 | 共通契約を個別課題へ従属させない | 時間・状態所有・認可・操作・ホスト保護を共通契約とし、service／物理profile・操作列としての被害者workloadをその上に載せ、課題presetは構成と固定・秘匿・判定を束ねる。最初の課題選択を共通時間契約の前提にしない。paired training／victim query予算は必要な課題だけが要求し、欠落を無制限へ読み替えない。[所有境界](architecture.md#共通契約と課題設定の依存方向2026-10-04)。当時未決定だった初期倍率・workload範囲・初期化方針はD57で具体化。正式schemaと実装は未完了 |
| D57 | 共通基盤v1の五契約を採用する | 初期時計1倍と基盤異常時の判定保留、有限の単発／事前job群／結果依存workload、サービス条件・権限で制限する共通状態継承、server正本と登録不要のブラウザ所有session、ページ再読み込み／同一process内の整合復旧を採用。Webは実際の公開基盤への配備を指し、模擬Webではない。server再起動復元は後続、配備先・deploy・詳細schema・性能値・物理係数は未確定。[契約と受入](common-foundation-v1-contract-proposal.md) |
| D58 | 初回契約を1量子ビットや簡素なトポロジーへ局所化しない | D45の百量子ビット級トポロジー／一回路編集上限8を維持し、全体metadata・論理回路幅・局所物理領域幅を分離する。複数qubit・物理ID集合・異種影響関係を共通境界で扱い、Reset専用の1qubit adapter制限を全製品へ強制しない。全体密度行列や未実装機構の対応を約束した意味ではない。[設計条件](common-foundation-v1-contract-proposal.md#局所化を避ける設計条件d58) |

D48の「届く」は作用と情報の向きを一つに固定しない。Reset漏洩では攻撃者の操作が共有資源へ届き、被害者由来の情報が攻撃者の測定へ戻る。対照条件として経路を遮断した実行は、成立した攻撃例には数えない。

## D59: 通信起点と取消時刻の追加合意（2026-10-05）

認証済み完全envelopeのserver受領を模擬サービスへの投入起点とし、その先だけ宣言delayを加える。実ブラウザ通信を模擬クラウドlatencyへ二重計上しない。取消作用が宣言STARTより先なら未実行window・予定success releaseを取消応答releaseへ置換し、関連する全結果依存も差し替える。STARTが先ならqueued-only条件で拒否し、確定済み物理・受付順序を巻き戻さない。

duration・basisは成立前に明示し、課題専用の時間規則やprovider既定値を補わない。二条件はユーザー承認済みであり、live全体の実装完了・数値係数の採用を意味しない。[採用条件・実装境界](parallel/live-ingress-boundaries-handoff.md#採用した接続条件d59)を参照。

## D60: 攻撃者操作で世界時刻を進める（2026-10-06）

ユーザー（ブラウザの前の現実の人間）、攻撃者（ユーザーが操作する仮想環境内の主体）、被害者（宣言workloadで動く仮想環境内の主体）を区別する。live入力の世界時刻を現実のhost時刻へ結び付けず、攻撃者が何も待っていない間は世界時刻を止める。ユーザーが考えている現実の時間に、被害者は進まない。

攻撃者の操作は、停止中の世界時刻に宣言delayを加えた位置へ置く。世界はその操作のreply releaseまで、または明示的な待機コマンドの目標まで進み、再び止まる。被害者・サービス・物理はこの進行区間だけ宣言どおりに動く。同じ操作列から同じ世界が得られ、host計算速度・parse時間は物理と被害者進行に入らない。

これはlive入力についてD57の初期時計1倍とD59の「server受領」起点を置き換える。D59の取消規則、宣言済みworkloadの扱い、D53の目標／確定位置の分離は維持する。ユーザーの反応速度は攻撃条件に入らず、必要なら攻撃者の思考時間を宣言値として扱う。

攻撃者の操作はreplyを待たない（キュー待ちjobの取消に必要なため）。送信・取消・照会は世界時刻を進めず、進めるのは明示的な待機だけとする。端末文法は `wait <duration>`（例: `wait 250us`, `wait 1.5ms`, `wait 2s`）、`wait request <request_id>`、`wait any`。要求待ちは同じ認証主体が発行した保持中のrequestだけを対象にし、`wait any`はその主体の最初のreplyで止まる。duration／reply待ちは`max_wait_us`、進行は`max_steps_per_wait`で制限し、上限到達は`LIMITED`として返す。待機はevent順に進み、目標位置で止まる。reply待機の停止位置はhost ns分解能だけreleaseを越え得る。

進行方式は成立前に`experiment.live_time_policy`へ凍結する。`{"mode": "explicit_wait", "max_wait_us", "max_steps_per_wait"}`または`{"mode": "host_clock"}`で、既定値はなく、未指定なら成立を拒否する。`explicit_wait`は手動進行可能な世界時計と`PausedWorldController`を要求し、`host_clock`だけがbackground pumpを使える。host時刻による進行を選んだ場合、待機端末コマンドはそのclientでは利用できず、物理失敗へ読み替えない。実装は[live_time_policy.py](../../../core/attack_simulation/live_time_policy.py)、[attack_paused_world.py](../../../api/attack_paused_world.py)、[attack_live_driver.py](../../../api/attack_live_driver.py)。

ユーザーと仮想環境serverの接続はWebSocketを採用してよい（ユーザー承認済み）。`websockets`の版固定とdesktop版への同梱確認は後続作業。現HTTPのrequest_id＋pollingはWebSocket置換までの暫定候補であり、この上へ機能を積まない。未決定: 攻撃者の思考時間の宣言値と既定の有無、基盤異常時の判定保留との関係。

### D60 実装具体化（2026-10-06）

`explicit_wait`専用のopt-in WebSocket候補を実装する。資格情報は接続後の最初のframeで送り、trusted real-owner resolver・完全一致Origin allowlist・有限message／connection／request上限を要求する。host_clock世界は対象外とし、要求操作は世界時計を進めず、waitだけが進める。wait後はrelease済み本人replyをpushする。ASGI handlerは数値処理をworker threadへ移す。routeは`api/main.py`へmountせず、request_id＋pollingのHTTP候補には機能を追加しない。wire schema・error・size policyの詳細は[共通コマンドclient](common-command-client.md#d60-explicit_wait-websocket-protocol候補未mount)。このprotocol実装は実サービス認証・運用server設定・UI接続の完了を意味しない。

## D61: 実所有者はHttpOnly cookieで識別し、環境数は配備ごとに上限を設ける（2026-10-06）

D57の登録不要のブラウザ所有sessionを、所有token（JavaScriptから読める保存）ではなくHttpOnly cookieで実装する（ユーザー承認済み）。目的は、XSSが起きても識別子そのものを読み取り・持ち出しできなくすること。登録・ログインは導入しない。

- 環境作成時にserverが推測不能な乱数を発行し、server側はhashだけを保持する。cookieは`HttpOnly`、`SameSite=Strict`、適用pathを攻撃シミュレーターのAPIに限定し、httpsのときだけ`Secure`を付ける。期限（無操作・作成からの絶対時間）と明示破棄で無効化する。
- 自動送信されるため、WebSocketの握手ではOriginとHostを、状態を変えるHTTP操作ではそれに加えて独自headerを要求する。仮想資格情報（攻撃者の権限）とは別の層として扱う。
- cookieは環境ごとに別名で発行する。1つのブラウザが同時に持てる環境数の上限を起動設定で決め、既定値は1とする。desktop版とdev serverだけが明示的に4（仮の値）を指定する。公開判定を`ALLOWED_ORIGINS`の有無に頼らない。
- localhostではcookieがportで分離されないため、cookie名に起動ごとの値を含める。DNS rebinding対策としてHostを`127.0.0.1`と`localhost`に限定する。
- 公開版は同一originが前提になるため、Cloudflare側で攻撃シミュレーターのAPIとWebSocketを中継する設定が必要。配備用の別リポジトリの作業であり、審査期間後に行う。

前提として、desktop版の画面とAPIにCSPを入れ、公開版worker（`frontend/src/worker.ts`）と`connect-src`以外を一致させる試験を置いた（`tests/test_security_headers.py`）。未決定: 期限の具体値、server全体の環境数の上限（8qubit環境のメモリ実測後に決める）、上限到達時のUI。

### D61 実装具体化（2026-10-06）

`api/attack_web_ownership.py`に未mountのHTTP create/deleteとtrusted owner resolverを実装。environment IDは128-bit、owner cookie bearerは256-bitで、serverはSHA-256 digestだけを保持する。cookieはenvironmentごとの名前、`HttpOnly`、`SameSite=Strict`、`Path=/api/attack`、HTTPS Origin時のみ`Secure`。期限は仮設定としてidle 30分・absolute 24時間、設定可能。generic既定はbrowser 1・server全体8、local development／desktopはbrowser 4。browser上限は保持中のenvironment cookieで認識する匿名owner単位であり、全cookieを消去した際のidentity resetはloginなし設計の性質。server全体上限で孤立環境もboundedにする。Host/Origin allowlistは専用設定で明示し、`ALLOWED_ORIGINS`から配備profileを推定しない。状態変更HTTPには`X-Attack-Simulator-Request: 1`を要求する。routeは未mount、factoryはtrusted supervisorから注入し、公開proxy・multiworker・再起動永続化は含めない。詳細・応答コード・残る接続範囲は[共通コマンドclient](common-command-client.md#d61-実所有者cookie環境lifecycle候補未mount)。

## D62: 標準ノイズ値を持たせ、最初にモードを選ばせる（2026-10-06）

ユーザーは環境を作る前に、まず自由モードか課題モードかを選ぶ。その後に設定へ進む。

物理的な相互作用（待機中のノイズなど）は、標準値を持たせた上で、ユーザーがパラメタを変えられるようにする（ユーザー承認済み）。これは「係数を推測で補わない」原則の例外ではなく、標準値を明示的な設定として扱う方針である。

- 標準値は、成立前の設定画面に初期値として表示する。成立時にmanifestへ値と根拠（basis）を凍結し、runtimeの暗黙の既定値にはしない。
- 自由モードではユーザーが変更できる。課題モードでは課題presetが値を固定・秘匿でき（D56）、変更可否はpresetが決める。
- 根拠は値ごとに表示する。実機の現在値の再現を主張しない。

標準値は、公開されている実機較正値から根拠付きで選ぶ（ユーザー承認済み）。出所は一つに揃え、[phase3b_runtime_calibration.json](../../../validation_results/phase3b_runtime_calibration.json)（ibm_kingston、physical qubit 150、較正時刻 2026-08-03T08:28:25+09:00、job `d9nlh5ssfqic73ar6f30`）とする。ある時点の1量子ビットの値であり、現在の実機や装置全体の代表値ではないことを表示する。

- 待機中の緩和と位相緩和（`IdleQubitSpec`）: T1 = 300.16 µs、T2 = 315.27 µs。モデルは純粋な位相緩和時間を受け取るため、Tφ = 1/(1/T2 − 1/(2T1)) ≈ 663.96 µs を設定値とし、元のT2も根拠として表示する。
- 読み出し誤差: P(1|0) = 0.006592、P(0|1) = 0.007568（`prob_meas1_prep0`、`prob_meas0_prep1`）。
- 操作の所要時間: 測定 2.180 µs、Reset 2.212 µs、1量子ビットゲート 0.032 µs。
- 熱平衡の励起状態占有率: 0。較正データに対応する値がないため、変更可能な仮定として表示する。
- Resetの成功確率: 較正データは所要時間だけで誤差を持たない。実機のResetは測定と条件付きXなので、読み出しの割り当て誤差から導く近似（失敗確率 ≈ 0.00708、`readout_error`）を「導出値」と明示して使う。直接測った値ではない。

以前の案にあった T1 = 303 µs / T2 = 340 µs は、別の時点のIBM比較の入力値であり、読み出し値と出所が揃わないため採用しない。

### D62追記: 待機ノイズの二つの入力方式（2026-10-06、ユーザー承認済み）

待機中のノイズは、次の二つの方式から一つを選ぶ。二つは排他で、選ばなかった方式の値は使わない。画面にもそう表示する。

- **実機較正値の方式（初期値）:** T1、T2、熱平衡の励起状態占有率を直接指定する。標準値は上記の較正値。温度・磁束ノイズの項目は表示しない。実測のT1/T2には測定時点の温度や磁束ノイズの影響がすでに含まれるため、その上に物理パラメタを重ねない（二重計上の防止）。
- **物理パラメタの方式:** 本体シミュレーターの環境設定（[physical_environment.py](../../../core/physical_environment.py)の`PhysicalEnvironmentInputs`: 温度、qubit周波数、磁束ノイズ、デバイス品質、qubitごとの上書き）を使い、同じ導出（Bose-Einsteinの熱的占有数から上向き・下向きの緩和率、位相緩和率）で崩壊率を求める。待機モデルへは、T1 = 1/(γ↓+γ↑)、Tφ = 1/γφ、熱平衡占有率 = γ↑/(γ↓+γ↑) として渡す。新しい物理モデルは作らず、本体と同じ層を使い回す。磁束ノイズは周波数可変のtransmonを想定した現象論であり、固定周波数の実機qubitにそのまま当てはまるとは限らないことを表示する。

温度・磁場・周波数は、成立前に凍結する仮想世界の前提条件であり、攻撃者の操作能力ではない。成立後に攻撃者が変更する手段は設けない。読み出し誤差とResetの値は、どちらの方式でも較正値の項目として別に扱う。

## D63: owner lifecycleから凍結manifestの製品assemblyへ接続する（2026-10-07）

環境作成では、owner managerが上限枠を予約した後に、信頼済みの設定側が選択・準備した`ScenarioEnvironment`を製品factoryへ渡す。factoryは成立済みmanifestから`ProductAttackEnvironment`を組み立て、managerはそのcontrollerをWebSocket registryへ登録してから作成成功を返す。manifestや仮想資格情報をブラウザ入力から直接構築・信用しない。自由／課題の選択、設定編集、preset解決を担うUI／設定resolverは別の未接続境界として残す。

作成成功のHTTP 201 JSONに、作成したブラウザへ攻撃者の仮想資格情報だけを返す。WebSocket認証で必要なためであり、owner cookie・URL・subprotocolへ混ぜない。被害者資格情報は応答・cookie・WebSocket frameに含めない。browser上限1の既定で既存環境がある場合は429で新規作成を拒否し、既存環境を置換しない（2026-10-07ユーザー承認済み。UIは「今の環境を破棄して作り直すか」を確かめ、承認されたら破棄→作成の順に送る）。factory失敗・registry登録失敗では部分環境を閉じ、予約枠とowner登録を残さない。

idle／absolute期限はhost上のlifecycle timerで環境を失効・WebSocket close・controller closeし、manager／registryの枠を返す。これはhost timerによる資源cleanupであり、D60の仮想時計やQPU物理時間を進めない。managerとregistryはprocess-localであり、複数worker間の共有・再起動復元は引き続き対象外。[接続契約](common-command-client.md#d63-owner-lifecycleとproduct-factoryの接続候補未mount)

## D64: ブラウザ設定resolverの入力境界（2026-10-07、ユーザー承認済み）

`api/attack_settings_resolver.py`のresolverを、D63のowner managerが呼ぶ前処理として置く。resolverが受け取るのはmode／preset ID／許可された設定差分だけで、manifest、権限、credential、basis、物理係数の由来は受け取らない。自由テンプレートとchallenge presetはサーバー側の明示登録値に限り、未登録なら作成不能とする。Reset leakageはpreset IDの非公開設定枠だけをcatalogへ出し、実preset・graderが登録されるまでunavailableとする。

wire形は`GET /api/attack/settings/catalog`と、既存`POST /api/attack/environments`のJSON bodyとする。catalogは`schema_version`、`modes`、`free_mode.idle_noise_modes[]`（field descriptorとdefault）、`free_mode.standard_initial_values`（D62値・basis）、`challenges[]`（公開説明、available、編集可能field descriptor）を返す。hidden fieldとその値はdescriptor/defaultを含めて省く。作成bodyはfreeが`{mode:"free", idle_noise_mode, settings:{canonical.path:value}}`、challengeが`{mode:"challenge", preset_id, settings:{canonical.path:value}}`。inactive noise modeや追加root keyは拒否する。

JSON body上限は64 KiB、field数上限は128、string値上限は256 UTF-8 bytesとし、型・有限数値・server descriptorの範囲／choiceを厳密検証する。公開エラーは理由を細分化せず`INVALID_SETTINGS`、過大bodyは`SETTINGS_TOO_LARGE`とする。authority／credential／manifest provenance／challenge policyのpathは、誤ってfield登録されても拒否する。catalog routeもenvironment create routeもopt-inのままにし、`api/main.py`にはmountしない。

実Reset課題preset／grader、製品用自由テンプレート／service profileは別途サーバー登録が必要。これらが未登録なら、catalogでは利用不可と示し、作成を成立させない。

## 2026-10-04の時間接続に関する追加合意

先行のD54時点では実時間1倍・固定周期の被害者を採用しなかった。その後のD57で、共通時計の初期値1倍と有限workloadの三構成を採用した。固定周期だけを既定とする案は引き続き採用しない。1倍は実サービスのlatency再現を保証せず、通信・アクセス・物理計算の宣言条件と数値根拠は[比較と未検証範囲](service-time-and-workload-comparison.md)に従って具体化する。D53の目標／確定位置の分離は維持する。

## 直近の認識と文書化する設計方向

ユーザーは、異なる量子サービスで設定・到達可能な権限・仮定の成立範囲が大きく違うと指摘した。
今回の更新では、この差を明示して扱う方向を全体へ反映する。
「装置・サービス・被害者・攻撃者・課題」という五区分は意味上の責務として維持し、D30の実装向け設定構造へ対応させる。
具体的なfield、画面数、プロファイル形式、API schemaまで合意済みとはしない。

2026-09-17のユーザー説明で、D07/D08の画面階層をD10〜D12として具体化した。
「自由ワークスペース」は課題中に全設定を編集できるという意味ではない。
量子暗号を対象外とすることから、その他のセキュリティ題材の採否まで自動的に決めない。

## 具体化案（未確定）

- 共通フローを先に仮置きし、性質の異なる課題例で検証してから、状態・時間・権限と最小実装を具体化する。
- サービス別の公開機能・実行方式と、攻撃者の操作・観測能力を表現する。全サービス共通の万能な権限は与えない。
- ジョブ送信を許可された操作要求の一種とする。パルス、仮想計測、指定部品の編集などの採否は課題別に決める。
- プリセットに実サービス準拠・研究上の構成・自由実験の来歴と、条件ごとの根拠を記録する。別製品モードにはしない。
- アカウント取得等の古典的準備を開始条件として与え、量子固有の調査・操作に集中させる。与える権限・情報と、利用者が調べる事項を課題ごとに区別する。
- 被害者を業務分野ごとに作り分けず、単発・固定反復・適応的実行などの振る舞いで整理する。
- 8つの論理責務候補を使う。ただし部品数・境界・権限は未決定。[レイヤー別整理](layered-discussion.md)を参照。
- 攻撃者環境にPython、エディタ、ターミナル、ファイル、SDK/CLI、集計・作図を用意する。
  実行方式、言語、ライブラリ、コマンドは未確定であり、新規依存を承認したものではない。
- 標的サービスと攻撃者環境はAPIを境界にする。既存シミュレーションAPIをそのまま公開すると
  内部状態を見せすぎるため、テナント向け結果への変換が必要。
- `VictimConfig` は希望する利用条件・サービスプロファイル参照を保持し、`QpuServiceConfig` が実際の共有方式・割当・Reset方針を保持する。
  被害者向け画面に設定入口をまとめても、サービス内部の事実を被害者が所有・自由変更する意味にはしない。
- 仮想時刻とPCの実計算時間、演習予算と実行上限を分ける。
- 問題の前提を変更する場合は、環境成立前に編集するか、成立済み環境を棄却して新規開始する。元の課題の正式クリアには数えない。
  実行記録と設定バージョンを紐付ける。復旧再開で前提を変更しない（D51/D52）。
- 提出時に推定値を照合する、または対照実験との影響差を評価し、達成証明としてflagを発行する。
  静的flagの直接復元を採る課題も候補であり、方式は一律に確定しない。
- 正規クラウドテナントは一つの候補。計測経路や被害者の利用部品へアクセスする立場も文献上の候補として整理したが、採用範囲は未決定。

## 更新済み・採用しない説明

| 以前の説明 | 現在の扱い |
| --- | --- |
| シナリオ演習と自由実験を別製品・別実行基盤として作る | D07に更新。同じ基盤上で`free`と`graded`のセッション方針を分ける |
| 攻撃者端末は回路エディタと送信ボタンで十分 | D01に更新。コード実行・調査・分析ができる独立した環境を求める |
| 攻撃者がReset効率や漏れ係数を直接操作する | 通常テナントの能力とは分離。環境作成者側の設定であり、例外は脅威モデルで明示する |
| クラウドを共有していれば隣接量子ビットを同時利用する | 時間共有と物理的同時共有を区別。課題ごとに成立条件を置く |
| ジョブがrunningなら被害者との同時駆動が分かる | 古典処理の並列化と物理実行を区別。状態表示だけから断定しない |
| Reset後に励起が残れば秘密情報が漏れる | 被害者の秘密によって攻撃者の観測分布が変わる必要がある |
| 攻撃者に被害者時刻・短いReset待機を推測根拠として表示する | 公開情報である根拠がなければ表示しない。全知の演習管理画面と分ける |
| 情報窃取と改ざんを同じ成功条件で扱う | 秘密の推定と被害者結果への干渉を分ける。誤り増加と任意値への改ざんも区別する |
| 最初の一課題を決めなければ共通フローを検討できない | 共通フローの仮案を先に置き、課題例で妥当性を検証する進め方へ更新 |
| 攻撃者は必ず自分のジョブ送信だけで攻撃する | ジョブは操作要求の一種。追加アクセスを伴う経路は課題条件として別に扱う |
| 被害者が結果を受け取り、その後に攻撃者へ漏れるのが共通経路 | 自分の測定・許可された計測・公開出力等を分ける。被害の発生と攻撃者の確認も別 |
| 論文で現実的と記された能力は現在の全サービスで利用可能 | 対象サービス・時点・能力の入手経路と、全条件の同時成立を確認する |

## 引き継ぐ設計上の注意

情報取得は「被害者の秘密 → 物理・制御・観測経路への依存 → 攻撃者に許された観測 → 推定」で説明する。
干渉は「攻撃者の操作 → 物理的影響 → 被害者の結果」で説明する。
被害者APIの結果を直接改変する機構とは区別する。
不完全Resetでは残留を読むなら被害者→攻撃者、状態を汚染するなら攻撃者→被害者の順になる。
任意の被害者PC内ファイルを、量子状態との関係なしに読み取れる設定にはしない。

## D65 desktop／development向け最初の製品profile — 承認済み（D66で一部置換）

ユーザーはD65を、D66が置き換えた部分を除き、desktop／development限定の暫定値として承認した。D65はサービス一般や公開環境の仕様ではない。

- D66により、合成10×10 topology、単一qubit calibrationを別qubitへ流用する方式、固定CNOT/CZ 0.2 µsは置換された。現行値はD66およびhash検証済みKingston snapshotを参照する。
- 残る暫定条件: dense状態の保持領域上限6 qubit、初期状態|0…0>、1 µs victim operation、0 µsの追加service／ingress／control delay、development／desktop限定mount、Reset leakage challengeはunavailable。
- 上記の残存値は最初の体験用profileに限定し、サービス一般の契約へ昇格させない。profileや適用範囲を広げる判断は別途レビューする。

## D66: 標準値をibm_kingston全体の較正スナップショットへ置き換える（2026-10-07）

ユーザー決定: qubit 150の1個の値を全qubitへ均一に当てる近似は、現実性を大きく損なうため採らない。追加の費用をかけずに、qubitごとに異なる実在の値を使う。

- 出所は、open sourceの`qiskit-ibm-runtime` 0.50.0に同梱されたibm_kingstonの較正スナップショット（2026-04-15、156 qubit、物理結合176本＝coupling mapの有向352件）。Apache-2.0で、ファイルを改変せずライセンスと出所を添えて[data/device_snapshots/ibm_kingston_2026-04-15/](../../../data/device_snapshots/ibm_kingston_2026-04-15/SOURCE.md)に置いた。Qiskitは依存に加えない。
- これにより、D62の「qubit 150・2026-08-03」の値は、全qubitの標準値としては使わない。D62の値は、同じ装置の別時点の1 qubitの実測として、時間による揺れを示す参考に残す。
- D65の仮の値（合成10×10格子、CNOT 0.2 µsなど）は、このスナップショットの結合map・CZの誤差と所要時間で置き換える方向とする。

追加の決定（2026-10-07、ユーザー承認済み）:
- **壊れかけのqubit:** 読み出し誤差が10%超、またはCZ誤差0.5以上の結合に属するqubitは、配置の対象から外す。目録には「使えないqubit」として表示し、存在は利用者に見せる（実機のサービスと同様）。判定の閾値は上の二つで、スナップショットとともに記録する。
- **qubit 146（T1/T2なし）:** 上の条件で配置対象外になるため、仮の値を作らない。目録には「使えないqubit（較正値なし）」と表示する。
- **周波数:** スナップショットにないが、較正値の方式はT1/T2を直接使うため不要。物理パラメタの方式では、本体のシミュレーターと同様にユーザーが周波数を指定する。乱数で割り当てない（実機の範囲と合わず、根拠のない値を実在値と並べることになるため）。
- **仮想QPUの形:** D65の合成10×10格子をやめ、スナップショットの156 qubit・物理結合176本（有向352件）の形に合わせる。CZの誤差と所要時間もスナップショットの値を使う。保持領域の上限（D65の6 qubit）は、メモリの制約として別に維持する。
詳しい操作候補、能力の根拠、論文の適用限界は [サービス差とアクセス条件](service-and-access-model.md) を参照。

## D67 snapshot CZ errorのチャネル写像とqubit別サービス時間の集約（修正承認済み、2026-10-07）

D66のsnapshotに含まれるCZ `gate_error`は平均ゲート不忠実度であり、共通runtimeはチャネル確率を要求する。ユーザーは次の修正を承認した。

- CZの平均不忠実度を`r = gate_error`とし、二量子ビット部分系（`d=4`）への完全混合置換チャネル確率を`p = 4r/3`とする。チャネルは`E(ρ)=(1-p)ρ+p(I₄/4 ⊗ Tr_CZ(ρ))`（周辺系との相関は保持）で、平均ゲート不忠実度`r=p(d-1)/d`となる。これは承認されたシミュレーター写像であり、実機の誤差指標だけから実際のノイズチャネルを一意に決める主張ではない。
- 同時register-level reset／measurement durationは、対象qubit群の個別durationの最大値とする。個別の元データはmanifestへ残す。この集約規則は承認済みだが、実サービスの同時実行時間を保証するものではない。
- D66の「physical_parameters方式はsnapshotを使わない」は待機T1/T2等のnoise入力を指し、デバイス定義のtopology・CZ・readout/reset service metadataは較正値方式と共有する。この適用範囲は承認済み。

## D68 UI catalog型の追加互換拡張（承認済み、2026-10-07）

ユーザーは、既存形式を保持したまま`StandardInitialValues`へD66 snapshotの日付・qubit別較正値・配置可否／理由を追加し、`SettingFieldDescriptor`へserverの`required`を追加する案を承認した。D62の単一qubit形式もunionの一方として保持する。環境handle・client・connectionの公開interfaceと資格情報の境界は変更しない。

## D69 単一qubitゲートのsnapshot分解時間と誤差（承認済み、2026-10-07）

ユーザーは、単一qubitゲート一律0.032 µs・ゲート誤差なしという製品profileの扱いを、qubitごとのsnapshot値を使う次の方式へ置き換えることを承認した。D65の初期値、D66/D67のtopology・CZ・測定／Reset規則はこの変更範囲以外で維持する。

- Z・S・T・RZは`rz`のみ、Xは`x`一回、Hは`rz・sx・rz`、Yは`x・rz`、RX・RY（任意角）は`rz・sx・rz・sx・rz`。角度が0でもこの宣言分解を使い、黙って最適化しない。
- 所要時間は対象physical qubitのnative `gate_length`の和とする。現snapshotでは`rz`が0、`sx`・`x`が全qubitで32 nsであり、Z/S/T/RZは0、X/H/Yは0.032 µs、RX/RYは0.064 µs。CZは結合ごとのsnapshot値を継続する。
- `sx`・`x`の平均不忠実度`r=gate_error`から、一qubit（`d=2`）完全混合置換チャネル確率`p=2r`を得て、分解に含まれる回数分を適用する。`rz`には誤差を入れない。実機のチャネルを誤差指標だけで一意に同定する主張ではない。
- 実装は保持領域のnative元値を`circuit_execution.single_qubit_native_gates`、分解を`single_qubit_gate_decompositions`として根拠とともにmanifestへ凍結する。較正方式と物理パラメタ方式の両方で使い、準備X/Hも同経路を通す。欠落値・表現不能な確率は暗黙補完・clampせず拒否する。
- 既存runtimeの局所置換チャネルは理想一qubit unitaryと可換なので、内部に別の物理発展を挿入しない現モデルでは`p_eff=1-∏(1-p_i)`で厳密に合成してよい。sx二回は`1-(1-2r)^2`であり、`2p`にはしない。これは状態計算を増やさない実装具体化で、内部パルスの物理再現を追加したものではない。

## 新画面の補助規則（提案・実装候補、2026-10-07）

以下はD60〜D68の実装を支えるUI候補であり、新たな承認済み製品契約には昇格していない。

- Circuit Studioの局所計算用`duration_us`は送信前に除き、ゲート時間は凍結済みサービス値を使う旨を画面に表示する。測定先が未指定の場合だけ、利用者の明示選択で`qN → cN`を送信回路へ補える。既存の保存先・エディター状態は変更しない。samplingには保存先が必要という共通compiler契約は緩めない。
- UIは返信100件・未完了要求128件・ホスト待ち30秒で有限に保つ。timeoutは通信を切断するだけで、仮想時計やジョブを失敗へ進めず、自動再送／再接続もしない。
- `browser_limit`時は確認後に破棄→作成。画面が既存handleを持たない場合は、別の環境画面で公開environment IDを確認して指定する。HttpOnly cookieや資格情報から環境を勝手に探索しない。reload後の所有環境一覧・資格情報の再発行は後続検討。
- 公開catalogの読み取り専用GETだけは、Originがないブラウザ同一origin要求を`Sec-Fetch-Site: same-origin`＋許可Hostで検査する。Refererがあれば許可Originも照合するが、既存`no-referrer`方針では省略を許す。Originがある場合は従来の厳格照合。状態変更HTTPとWebSocketのOrigin／Host／独自header検査は変更しない。

## D70 同一process内の再読み込み復帰（承認済み、2026-10-07）

D57の再読み込み復帰要件をD61/D63の所有境界内で具体化した三点をユーザーが承認した。旧第三項の「一覧を取得できない」は当時の実装制限であり、本決定の後続指示により未完了要求一覧を追加する。

- 所有環境一覧は`POST /api/attack/environments/owned`で照会する。通常のGETで省略されるOriginを推測しないため、読み取り専用でもPOSTとし、D61のOrigin・Host・独自headerを要求する。cookieで証明できた同一ブラウザcontextの環境ID・mode・表示用host作成日時のみ返す。manifest・権限・資格情報・QPU内部時刻は出さず、`Cache-Control: no-store`とする。
- `POST /api/attack/environments/{id}/attacker-credential`も同じ検査と環境固有cookie照合を要求し、同じ所有者に既存の攻撃者仮想資格情報を再交付する。旧資格情報と旧タブの接続は破棄・ローテーションせず併用する。各WSは所有者cookieと仮想資格情報を照合し、単一worldの既存の直列化・同時接続上限を共有する。再交付だけでQPU／世界時刻を進めず、余計な失効による他の正常タブの中断を避ける。**環境の破棄・idle／absolute期限切れでは旧新すべての接続を閉じ、資格情報を無効にする。**期限切れ・破棄済み・所有者違い・未知IDは同じ404とする。
- 復帰後のWSは本人の未完了要求の公開一覧（request ID・種類・job ID・発行時世界時刻）を取得できる。仮想時刻を進めず、receipt・host時刻・他人の要求を出さない。旧reply履歴の再生、process再起動・複数workerをまたぐ復元は含まない。取得失敗時だけ不明と画面に表示し、`wait any`の経路を維持する。

## D71: 利用者交代時のReset前測定と配置方針（条件付き承認済み、2026-10-08）

製品profileの`runtime_execution.measure_input_state_before_transition=true`は、利用者交代時に対象qubitを測定してから条件付きXでResetするモデル選択として承認する。一般化された現行manifest名を維持し、Reset漏洩課題固有の`victim_measurement_before_reset`へ戻さない。

`placement_policy`は自由モードの公開設定目録に表示し、利用者が成立前に選択できるようにする。共用（`guaranteed_same_physical_qubit`）と利用者別領域（`tenant_dedicated_region`）の意味、および現実のクラウドにおける配置保証ではないことを画面と公式docsで示す。課題モードではpresetが固定できる。製品では両領域を各6 qubitのまま保持し、利用していない側の状態も失わない（Huewの追加承認）。両領域は同じQPUトポロジーとFIFOに属し、別領域を選んでも同時実行や未宣言のクロストークを自動導入しない。従来の1 qubit隔離対照`isolated_physical_resource`とは別の識別子を使う。選択した値と根拠はmanifestへ凍結し、成立後の変更は許さない。

## D72 製品のhost clockと計算追付き時の時計停止（決定済み、2026-10-08）

製品経路の時間方針を、ユーザー決定により次のように変更する。

- 製品環境は単調な現実時計に追従する`host_clock`、倍率1.0とする。倍率は製品設定に公開しない。`explicit_wait`は内部試験・互換検証経路に残してよいが、製品設定目録／presetの利用選択肢にはしない。
- `experiment.live_time_policy.pause_while_catching_up`を成立前に選べるboolean設定として凍結する。初期値はoff。onの場合は数値計算workerが有限pumpを実行している間だけ時計sourceを止め、catch-upが終われば再開する。利用者の思考・操作待ち、通信待ち、画面描画では止めない。停止は実行基盤の数値計算時間を仮想世界の時間へ重ねて数えないための補正で、QPUの物理時間やサービス遅延を変更しない。
- WebSocketの製品接続は`clock_mode=host_clock`とpause設定をreadyで示す。世界時刻更新は`clock`通知、解放済みreplyはserver pushで通知する。host_clockのwait操作は拒否し、製品画面に再生／待機ボタンを設けない。explicit_waitの内部試験routeでは従来のwaitを維持する。

### 製品service-time見直し（提案・未承認）

QPU内部の物理時間と、サービス／通信／実行基盤の時間は別領域として維持する。ゲート・測定・Reset・idle channelが使うQPU時刻はsnapshotまたはmanifestに明示された物理所要時間を使う。API往復やブラウザ／server実行基盤の実時間はhost_clockへ現実に反映され、同じ時間を固定の仮想通信遅延として二重計上しない。

公開されているIBM Quantum資料では、queue情報はジョブがqueueへ入った後に取得され、positionや推定開始／終了時刻を含み得るが、未知／未適用なら`None`となり得る。`quantum_seconds`はjobの推定system execution timeで、一般的なHTTP通信・compile・結果公開時間の固定値ではない。[RuntimeJob API](https://quantum.cloud.ibm.com/docs/api/qiskit-ibm-runtime/0.28/runtime-job) また、Sessionでは後続jobもqueue待ちになり得て、active/inactive等の状態がある。[Session jobsガイド](https://quantum.cloud.ibm.com/docs/en/guides/run-jobs-session) このため単一の公開値から、全サービスに通用するqueue／communication／publication遅延を導くことはできない。

候補として、queue時間は固定delayではなく、宣言されたservice scheduling／他jobとの占有と待ち行列から算出する。HTTP通信・compile・結果公開、拒否応答、被害者の操作時間は、対象とする公開serviceの明示資料または実測記録が得られるまで数値を採用せず、「仮の値」として明記する。現profileのzero service delays、victim operation 1 µs、ingress/control delayはSIMULATOR_CHOICEの仮置きとして保持し、根拠調査を理由に変更しない。数値の採用・変更はユーザー承認後とする。

## D73 設定画面のQPUグラフ・領域選択・qubit別noise調整（決定済み、2026-10-09）

設定画面に、hash検証済みIBM Kingston snapshotの156 qubit・176物理結合を使ったグラフを表示する。各qubitに番号を付け、配置対象外は赤と×、配置可能は緑、選択領域は黄と太い枠で示す。表示座標は決定的な一度限りの配置で固定データとして保持し、「表示用の配置であり、実機の物理的な位置ではない」とデータと画面に明記する。新規依存は追加しない。

自由モードでは緑のqubit追加時に、利用者が既存の黄色qubitを選んで置換する。置換後もCZ結合で連結し、上限6 qubitを守る。赤qubitは理由を示して拒否する。利用者別領域では被害者／攻撃者の領域を切り替えて表示し、領域は非重複とする。課題modeはpresetが領域を固定でき、固定時は操作不能とする。画面検査は利便性のためだけに用い、server resolverも連結性・上限・配置可否・非重複・preset固定を再検査する。成立時の選択領域はmanifestへ凍結し、成立後は変更しない。

較正値方式は黄色qubitのT1・T2・熱平衡励起状態占有率をqubit単位で表示・編集できる。値のbasisと、snapshot時点の値であって現在の実機代表値ではない旨を近くに表示する。緑／赤qubitは表示のみ、計算対象にしない。物理パラメタ方式はqubit別設定を持たず、グラフを領域選択のみに使う。ノイズの別名・範囲・物理仮定は既存のD62/D66に従う。

## D74 仮定・過程変化・不変の基盤を分ける（合意、2026-10-16）

仮定は環境設定時に固定し、攻撃者の権限、サービスが提供する機能、アーキテクチャの詳細、被害者の条件を定める。過程変化は仮定が定めたサービス環境の上での自由度の変化であり、攻撃者へ権限が突然与えられることではない。仮定に依存せず、シミュレーター内のサービス、API、機能の境界は原則変化させない。課題モードは自由モード上で再現できるプリセットを持ち、必要に応じて提出環境を提供する。仮定の根拠や難易度の説明は公式ドキュメントへ記載し、操作画面には出さない。[整理](assumption-process-model.md)。同日、状態三領域、提出環境の分離、難易度軸の固定、同時共有スケジューラ方針、Groverを4量子ビットとする案、基準比較による妨害判定も承認された。ゆらぎ基準は実装中の実験で定め、反復数とoracleは回路長・領域での成立を実測してから決める。

D74追記（2026-10-16）: 旧Reset課題の単独経路を削除し、設定項目の根拠は画面に表示せず公式ドキュメントにだけ記載する（D73の較正値根拠の近接表示を置き換える）。

## D75 shot間の待機をサービスの模倣として入れる（合意、2026-10-16）

クラウドQPUが回路の間に入れる待機（IBMの`rep_delay`）を、サービスの模倣の一種として入れる。`runtime_execution.repetition_delay_us`は明示の宣言項目で、初期値は0（待機なし、従来の挙動）。公開資料に採用できる値が無いため、数値は置かない。連続するshotの間にだけ働き、待機中は待機モデルで状態が緩和する。自由モードの設定項目に加え、課題モードはpresetで固定できる。根拠は公式ドキュメントに記載し、画面には出さない。交互観測とは併用しない。[比較](service-time-and-workload-comparison.md#5-時間窓設計の追加確認2026-10-16)。

## D76 同時共有の漏れは最初から双方向を対象にし、拡張性を優先する（合意、2026-10-17）

同時共有の窓の効果は、片方向に限らず最初から双方向を扱う。領域をまたぐ影響は、活動記録・関係・時刻付き効果の三部品に分け、基盤は部品の形だけを知り、個別の事象（クロストーク）は関係の種類として登録する。窓の効果は活動記録だけから計算し、サービスが測定結果に依存する条件分岐を持たない間は、重なり群を結果の依存なしに計算できる。[設計案](concurrent-sharing-window-design.md)の他の項目は未承認。

## D77 遅延命令の追加、残留ZZの段階的な扱い、結果の遅れの許容（合意、2026-10-17）

1. 遅延命令: 実サービス（Qiskitの`delay`、OpenQASM 3の`delay`）に対応物があることを確認し、回路エディタ形式の`I`＋`params.duration_us`としてサービスの命令に追加した。指定した量子ビットだけが待機モデルで緩和する。[設計案 §8](concurrent-sharing-window-design.md)。
2. 残留ZZ: 扱う。計算量が爆発するなら近似的な手法を使い、それでも不可なら扱わない。厳密計算は12 qubitの稠密行列となり上限を超えるため、段階（厳密の評価→近似→扱わない）で進める。
3. 同時共有の計算を受付の封印まで遅らせ、結果が遅れることは、現実のサービスにも同様のqueue・公開の遅れがあるため許容する。

## D78 入口状態の近似再利用を入れる（合意、2026-10-18）

同じジョブのshotで、入口状態の差（トレース距離）が許容値以内なら、先のshotの測定前までの計算を再利用する。過程はCPTP写像でトレース距離を増やさないので、ジョブ全体の測定結果の分布の誤差は、shot数×許容値以内に保証される。許容値は実行設定の`entry_reuse_tolerance`で、manifestへ凍結する。0は厳密（同じ状態のときだけ再利用）、製品の初期値は1e-5で、自由モードで変更できる。実サービスの性質ではなく、シミュレーションの精度の設定であり、根拠は公式ドキュメントに記載する。同じ状態への厳密な再利用は、不完全Resetが記憶を残すため効かなかった。[設計案 §9](concurrent-sharing-window-design.md)。
