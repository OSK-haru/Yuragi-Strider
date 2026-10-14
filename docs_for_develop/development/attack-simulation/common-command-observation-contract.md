# 共通操作・観測・校正契約

最終同期: 2026-10-03（service条件の成立前固定と共通CLI構文を反映）。

本書は、攻撃者・被害者の操作、疑似ターミナル、QPU service、取得できる統計量、Resetの責務について、現在の合意を一つの境界へ固定する。
状態は**設計合意、認証付きin-process共通コマンドと限定回路実行は部分実装、HTTP API・画面は未実装**である。実装済み構文と対応profileは[共通コマンドclient](common-command-client.md)を参照。本書だけで全操作の正式構文やHTTP schemaを確定しない。

## 1. 不変条件

1. 攻撃者専用の物理操作コマンドは作らない。攻撃者と被害者は、選択されたservice profileが提供する同じ主体非依存の操作catalogを使う。
2. 実際に使える範囲は主体とscenarioで制限する。全サービス、全主体へ万能な操作集合を与える意味ではない。
3. ターミナルはSDK／CLI／Application APIのclientであり、QPU制御機器やservice内部shellへの直接接続ではない。
4. UIとターミナルは同じApplication APIを使う。UI専用の抜け道、攻撃者専用の内部APIを作らない。
5. tenant交代時のservice ResetはQPU serviceが挿入する内部eventであり、通常利用者の回路命令ではない。
6. 利用者へ返す観測と、simulator内部真値・grader秘密を分離する。統計量には取得元を持たせる。
7. 課題modeで利用者が操作するのは攻撃側画面だけだが、背後の被害者も同じ共通操作契約を通る。違うのは操作主体、権限、可視性、scenarioによる自動実行である。

## 2. 能力の解決規則

主体が実行・観測できる能力は、次の積集合として解決する。

```text
EffectiveCapabilities(actor, session)
  = ServiceProfile.capabilities
  ∩ ActorPolicy(actor).grants
  ∩ ScenarioPolicy(session).allowed_and_visible
```

- `ServiceProfile`: 対象serviceが技術的・契約的に提供する操作、結果、公開校正情報。
- `ActorPolicy`: その主体の認証済みrole、所有resource、許可操作、観測範囲。ここでは論理的な認可結果を指し、新しいdataclass名を確定するものではない。現行案では`AttackerConfig`／`VictimConfig`等から解決する。
- `ScenarioPolicy`: 課題／自由modeの固定、秘匿、予算、提出資格。

同一service profile内では、攻撃者・被害者に別々のコマンド体系を作らない。主体差は`ActorPolicy`で表す。
service間の差は`ServiceProfile`で表すため、あるserviceのパルス能力を別serviceの正規利用者へ移植したことにはしない。
自由modeの設定変更もこの積集合を越えられず、追加仮定を有効化した場合はresolved manifestへ根拠とともに残す。

### service条件の設定時点とCLI境界

service条件は`QpuServiceConfig`／選択した`ServiceProfile`の設定として、仮想環境の成立前に確定する。自由modeでは成立前に変更可能、課題modeではpresetが固定する。成立後は権限・公開範囲・実行方式などを変更できず、変更したい場合は環境を作り直す。対象には提供操作、実行方式と排他条件、受理するprogram形式、認証・取消方針、queueの公開範囲、結果形式と観測粒度を含める。actor別のgrantや秘密は対応する別設定に置き、serviceの公開能力と混同しない。

疑似ターミナルの文法はservice profileに依存させない。同じ入力を共通構文からoperationへ解釈し、その後にservice capability、actor grant、scenario policyを検査する。構文上正しい操作でも選択serviceが提供しなければ、job作成・物理event・予算消費の前に拒否する。service固有の入力形式や結果形式はadapter境界で扱い、変換できないものを黙って受理しない。共通構文自体は今後の操作追加に合わせて版を進められるが、serviceを切り替えるたびに別の文法へ変えない。

現行コードは`qpu_service_config`の成立時凍結、`supported_operations`による認可、service非依存の`help`／`device`／`program submit`／`job`構文を持つ。明示profileの最大8量子ビット局所sampling compiler、server-owned配置、bit列counts投影を接続したが、全service adapterや上記条件の完全なschema／事前検査は未実装であり、実service互換を主張しない。1量子ビットはReset課題の設定であり、共通IRの制約にしない。

### 操作の認可とjob所有

各利用者操作は共通catalog上のoperation IDで表す。初期job APIの操作IDは次のとおりとする。

| operation ID | 対象 | 権限判定 |
| --- | --- | --- |
| `device.info.read` | 実行先一覧・公開能力の照会 | service能力・actor grant・scenario許可の積集合。内部状態や配置は返さない |
| `program.submit` | 量子programのjob作成 | capabilityの積集合に加えてservice quota・入力検証を適用 |
| `job.status.read` | job状態・許可metadataの照会 | principalのtenant scopeと、そのroleへのread grant |
| `job.result.read` | 終端jobの結果取得 | tenant scope、result-read grant、scenarioの可視範囲 |
| `job.cancel` | job取消要求 | tenant scope、cancel grant、service cancellation policy |

operation IDは主体別に複製しない。攻撃者・被害者の違いはActorPolicyのgrant、認証context上のprincipal、返却projectionで表す。認可は、(1) service profileがoperationを提供する、(2) ActorPolicyがprincipalのscenario actor roleへgrantする、(3) ScenarioPolicyがsessionで許可する、(4) jobがprincipalのtenant scopeに属し、操作固有のresource条件を満たす、のすべてを要求する。job IDを知っていることは権限の証明にならない。

tenantは個人アカウントではなく、job等の資源を束ねるworkspace／project境界とする。同一tenant内で複数principalがjobを共有できるかはservice profileと各principalのrole/grantで決まり、作成者だけに自動限定しない。`created_by`は監査・追跡用に保持し、認可の唯一の根拠にはしない。実サービスごとにjob単位のresource policy等が異なるため、同じtenantなら常に全jobが見えるとも仮定しない。

外部APIのprincipalとtenantは認証contextから導出する。request bodyやURLのtenant ID・role指定を認証情報として受け入れない。課題内の被害者workloadは同じoperation catalogとjob経路を使うが、Scenario Orchestratorが確立した内部principalとして動かす。内部起動権限は利用者APIへ公開せず、`victim.prepare`等の役割名付きoperationにも変換しない。

初期profileの取消は`queued_only`とし、tenant scopeとcancel grantのあるprincipalだけを対象にする。作成者限定にするprofileでは追加条件としてcreator一致を要求できる。実行開始後・終端後の取消は失敗し、物理状態を変更しない。存在しないjobとprincipalに許可されないjobは外部応答で区別せず、jobの存在を漏らさない。job一覧・状態・結果はtenant scopeとoperation grantの両方を適用し、共有queue情報は明示的に公開されたmetadataだけに限る。grader用真値・内部event・他tenant情報は結果projectionへ含めない。

上記5操作は内部`JobApplicationService`で認可する。同サービスのprincipal IDは信頼済み呼出元から受け取る前提であり、外部requestの認証を行うものではない。端末では`CredentialedJobConnection`が仮想資格情報を検証して主体を解決する。HTTP schema、共有tenant内のprincipal割当管理、実Web sessionの認証境界は別途設計する。

## 3. 共通操作境界

ここでいうjobの語義と、利用者の操作要求・物理event・workflowとの区別は[共有QPUサービス実行契約の第3.0節](service-runtime-contract-proposal-2026-09-23.md#30-ジョブの語義合意済み具体的なschemaは未決定)を正本とする。

想定する操作分類は次のとおり。名称は概念名でありCLI構文ではない。

| 分類 | 共通操作の例 | 境界 |
| --- | --- | --- |
| 探索 | device／service一覧、公開能力、公開校正情報の照会 | profileと権限が公開する情報だけ |
| job | program／circuit送信、状態照会、結果取得、queued取消 | 認証主体が所有または閲覧許可されたjobだけ |
| quantum program | 状態準備、gate、待機、測定等をjob bodyへ記述 | serviceの命令集合、qubit割当、実行方式に従う |
| local analysis | counts集計、頻度、信頼区間、閾値学習、相互情報量等 | clientへ返されたdataと、主体が正当に知るlabelだけを入力にする |
| reset request | serviceが明示的に提供する場合だけ要求可能 | tenant交代時の自動Resetとは別。profileとActorPolicyの両方が必要 |

`prepare_victim`や`attacker_measure`のような役割名付き公開コマンドは作らない。
たとえば被害者の準備と攻撃者の測定は、どちらも主体非依存のprogram送信であり、scenario orchestratorが異なる主体・順序・秘密を割り当てる。

```text
Terminal / UI / Victim runner
            ↓
      Application API
            ↓
 Auth + ActorPolicy + ScenarioPolicy
            ↓
 Job service / Scheduler / QPU runtime
            ↓
  actor-scoped result projection

Local analyze ── clientへ返された観測だけを読む
Internal grader ─ simulator内部証跡を読めるが利用者APIへ返さない
```

## 4. 観測と統計量の来歴

| 来歴 | 例 | 通常主体への扱い |
| --- | --- | --- |
| `measured_by_actor` | 自分のjobのcounts、per-shot memory、status、許可されたmetadata | service profileと権限が許す粒度で返す |
| `service_published` | T1、T2、readout error、device status等 | 対象serviceが公開する項目・時点だけ返す |
| `actor_derived` | 頻度、信頼区間、推定閾値、相互情報量、分類精度 | 上二つと、主体が知るlabelからclient側で算出する |
| `simulator_internal` | 真の状態、Reset乱数、被害者秘密、hidden正解、全event履歴 | grader・監査専用。通常主体へ返さない |

実サービスが一般に返すのはjob結果や公開校正情報であり、相互情報量を攻撃者へ自動的に返すとは仮定しない。
相互情報量には秘密側のlabelと観測の対応が必要なため、既知入力の校正job、明示されたreference workload、または内部graderのhidden labelのいずれを使ったかを区別する。

`analyze`は攻撃者専用機能ではなく共通のlocal toolである。内部確率や非公開labelを暗黙に読める実装は認めない。

## 5. 二種類の校正

### 製品・開発用の受入校正

物理model、負の対照、採用profile、grader閾値を検証する内部処理である。
simulator内部真値や固定seedを使用できるが、その出力を課題中の攻撃者の能力とは数えない。

### 模擬世界内で利用者が行う実験校正

共通job、結果取得、local analysisを使って行う。利用者が知れるのは、自分が指定した既知入力、serviceが明示したreference workloadのlabel、公開校正情報、返却された測定結果だけである。
利用可能な校正方法と回数は`ServiceProfile`、`ActorPolicy`、`ScenarioPolicy`に従う。

この分離により、課題成立性を内部で検証するための情報量と、攻撃者が実際に盗める情報量を混同しない。

## 6. Reset漏洩課題への写像

1. scenarioが被害者runnerへ主体非依存のprogram送信を行わせる。
2. schedulerが被害者jobを同一QPUで実行する。
3. tenant所有権の移行時にQPU serviceがReset eventを挿入する。
4. 攻撃者は同じjob契約から測定programを送信し、自分のcountsだけを得る。
5. 攻撃者は許可されたtraining結果からlocalに推定器を作る。
6. hidden評価では同じ公開境界を通す一方、正解bitと個別caseはgrader内部へ閉じる。

課題画面が攻撃者だけを表示しても、被害者を専用物理APIで動かす理由にはならない。
被害者の自動実行、同一qubit配置、反復可能性はscenarioの前提であり、攻撃者が被害者設定画面を直接操作できることを意味しない。

Reset課題専用v0の採用値は、成功確率`0.90`、標準`32 shots/case`、trainingはlabelごと10・合計20 query、hidden `20`件中`16`件、候補の相互情報量`0.45 bit/case`以上・優位性`0.30`以上、負の対照は両指標`1e-9`以下である。これらは`reset_leakage_challenge_only`の`product_policy`であり、共通serviceや他課題の既定値にはしない。

## 7. 現行実装との対応

| 項目 | 判定 | 現状 |
| --- | --- | --- |
| 共通job envelope | 部分整合 | `JobCommand`、`tenant_id`、FIFO、結果がある |
| 操作認可の積集合 | 部分実装 | `program.submit`について`JobAuthorizationPolicy`がservice能力・actor grants・`ScenarioPolicy.allowed_operations`を照合。challengeでは権限fieldの固定も要求する |
| job scope・read/result/cancel認可 | 部分実装 | 内部`JobApplicationService`と仮想資格情報接続がprincipal grant、tenant scope、状態照会、結果投影、queued取消を実行。HTTP認証contextは未接続 |
| service-owned Reset | 整合 | 所有者変更時の内部eventとして実装済み |
| policy・予算・秘匿 | 部分整合 | `ScenarioPolicy`、観測予算、hidden graderがある |
| 主体非依存operation | 部分実装 | 限定回路compilerが`CircuitExecutionOperation`へ変換し、両主体が同じ送信要求を使う。Reset既存内部operationの置換は未完了 |
| 共通Application API | 部分実装 | in-process jobサービスにdevice照会・回路送信・job操作を接続。Reset既存runnerもjobサービスを共有するが、HTTP／UI境界は未実装 |
| 疑似ターミナル | 部分実装 | actor別のbounded回路namespaceから`device`／`program submit`／`job status/result/cancel`を実行。host shell・任意コード・HTTP／画面は未実装 |
| 共通local analysis | 未実装 | 校正runnerとgraderはあるが、利用者dataだけを読む一般toolではない |
| 統計量provenance | 未実装 | 文書上の契約のみ |

役割別operationはReset縦切りを成立させる内部足場であり、公開APIとして固定しない。現runtimeは、通常の`submit`経由で被害者準備operationを拒否し、内部専用経路に限って投入する。端末の中立回路要求は仮想資格情報からprincipalとtenantを導出し、利用者bodyから役割・配置・seedを受け取らない。内部専用経路を利用者へ公開してはならない。
Resetのheadless受入は、公開training由来の推定器、hidden判定、負の対照、bounded instance preflightまで完了した。次は役割別内部operationを共通program requestへ置換する際に、観測予算と被害者のshotごとの再実行を保持し、認証済み外部Application APIへ接続する。新しいsampling経路は各shotを初期化するため、既存漏洩課題の測定経路と無条件に置換しない。

2026-10-04のD56で、共通policy／manifestのpaired query予算を任意にした。Reset query経路では明示予算を必須とし、一般Program／session経路では不要なtraining・被害者観測を作らない。service／compiler／hostの上限は別に維持する。共通時間・認可・操作の規則は課題選択に依存させず、[所有境界](architecture.md#共通契約と課題設定の依存方向2026-10-04)に沿って課題設定を適用する。

## 8. 実装順と受入条件

1. **完了:** Reset headless pipelineの受入を閉じる。
2. **部分実装:** in-process job操作の認可・結果投影、主体非依存の限定回路要求を接続。Reset runner移行、外部認証、HTTP APIを実装する。
3. **部分実装:** 同じApplication API上の疑似ターミナルに共通コマンドを接続。被害者runnerのin-process接続は実装済み。Web workspaceへの接続が残る。任意コード実行はSandbox設計後に追加する（所有者指示）。
4. 返却dataだけを読むlocal analysisと、統計量provenanceを実装する。
5. 同じAPIを使うUIを実装する。

共通操作境界は、少なくとも次を満たすまで完成扱いにしない。

- 同じprogram requestを、異なる主体へ異なる権限で許可／拒否できる。
- 禁止操作はjob作成・物理event・予算消費より前に拒否される。
- 主体Aが主体Bのjob、result、秘密、内部真値を識別子差替えで取得できない。
- UIとターミナルの同一操作が同じApplication APIとpolicy判定を通る。
- Reset課題を役割名付き公開operationなしで再実行できる。
- `actor_derived`統計を、返却dataと許可labelだけから再計算できる。
- 内部graderの値を通常clientへ投影しないことを権限testで確認できる。

## 9. 外部serviceとの対応根拠

実在serviceでも利用者はSDK、REST API、CLI、console等からjobを送信し、statusと結果を取得する。QPU内部shellを操作するわけではない。
IBM QuantumはREST job API、Sampler結果、backendのT1／T2／readout error等を文書化している。
Amazon BraketとAzure QuantumもSDK／CLI経由のtask／job送信と結果取得を提供するが、結果形式・provider能力は同一ではない。

したがって本製品の疑似ターミナルは、利用者PC上のshellから認証済みSDK／CLI／REST clientを操作する層として実装する。
QPU、制御装置、scheduler hostへのshell loginを模擬しない。通常tenantが見られない他者job、tenant ID、物理割当、内部queue、Reset成否をterminal固有機能として追加してはならない。
必要な追加情報はServiceProfile・ActorPolicy・ScenarioPolicyに取得経路と根拠を持たせる。

課題固有の`attack`コマンドも標準機能にはしない。利用者は共通のdevice照会、program／circuit送信、job状態・結果取得、local analysisを組み合わせて攻撃を構成する。
Reset課題のラベル付きtrainingは一般事業者の標準CLI機能とは主張せず、`controlled_reference`という課題前提を持つreference workloadとして公開する。

- [IBM Quantum REST API](https://quantum.cloud.ibm.com/docs/en/api/qiskit-runtime-rest)
- [IBM Samplerの入出力](https://quantum.cloud.ibm.com/docs/en/guides/sampler-input-output)
- [IBM QPU情報と校正値](https://quantum.cloud.ibm.com/docs/en/guides/qpu-information)
- [Amazon Braketの利用方法](https://docs.aws.amazon.com/braket/latest/developerguide/braket-using.html)
- [Amazon Braket CLI](https://docs.aws.amazon.com/cli/latest/reference/braket/)
- [Azure Quantum CLIでのjob送信](https://learn.microsoft.com/en-us/azure/quantum/how-to-submit-jobs-azure-cli)
- [Azure Quantumのjob送信方式](https://learn.microsoft.com/en-us/azure/quantum/how-to-submit-jobs?pivots=ide-jupyter)

## 10. 矛盾監査

| 論点 | 判定 | 解消規則 |
| --- | --- | --- |
| 共通操作とservice差 | 整合 | catalogの形式は共通、能力集合はServiceProfileごとに異なる |
| 攻撃者・被害者の同じ最大範囲と権限差 | 整合 | 同じservice catalogに対しActorPolicyでsubsetを与える |
| 攻撃側画面だけと被害者の操作 | 整合 | 被害者は同じAPIを使うbackground actorで、利用者には画面を公開しない |
| 自動ResetとReset command | 整合 | 前者はservice event、後者はprofileが提供するときだけの利用者request |
| 利用者校正と内部grader | 整合 | 取得元と可視性を分離し、内部真値を利用者能力へ数えない |
| 現行の役割別operation | **内部限定の未完了事項** | 共通capability認可は接続済み。公開前に主体非依存operationとApplication APIへrefactorする |
| UI先行と共通API | 条件付き整合 | wireframeは先行可、動作UIは共通APIのclientとして実装する |
| 疑似terminalと実service | 整合 | 利用者側CLI／SDK clientとして実装し、QPU内部shellや他tenant観測を捏造しない |

したがって、現在の思想は一貫しているが、コードはまだ完全には従っていない。最大の残件は、Reset専用の役割別Python operationを共通Application APIへ移すことである。
