# 共通コマンドの実装境界

更新: 2026-10-07。状態: D60 explicit_wait用WebSocket、D61 owner resolver、D64 catalog/resolverと製品factoryを接続し、desktop／development限定でmount済み。新画面は既存clientからこの経路を使用する。公開profileは無効、Reset漏洩のpreset／grader接続は未完了。

## 製品画面の接続境界

`frontend/src/pages/AttackLivePage.tsx`はcatalogのfield descriptorだけから設定formを作る。D68によりD62単一qubit／D66 snapshotの両形式、`required`を扱う。選択方式の変更値・必須値だけをD64 bodyへ入れ、初期値を重複overrideとして送らない。初期値そのものはserver templateからmanifestへ凍結される。

攻撃者資格情報は既存clientのclosure内のみ。画面stateは公開handle・request ID・world time・job status／shots／countsの投影だけを保持する。待機は時間・自分の要求・最も早いreplyの三経路で、送信や照会では時刻を進めない。破棄が確認されるまではhandleを保持し、自動再送／再接続しない。再読み込み後は所有環境一覧から利用者が明示的に復帰を選び、資格情報を再取得して接続する。

共通sampling compilerは明示した古典測定先を要求する。新画面の任意選択で未指定測定先だけを同番号bitへ補い、エディター由来の`duration_us`はサービス時間へ委ねるため送信しない。その他の命令・角度・既存測定先を変換せず、非対応回路は宣言denialへ返す。上限・quota時の再作成・catalog GETのブラウザ互換検査は[補助規則の提案](decisions.md#新画面の補助規則提案実装候補2026-10-07)を参照。

## 利用手順と実サービスの根拠

IBMの[Sampler quickstart](https://quantum.cloud.ibm.com/docs/en/guides/get-started-with-sampler)では、backend選択、対応命令への回路変換、送信、job ID取得、結果取得という手順を取る。[RuntimeJobV2](https://quantum.cloud.ibm.com/docs/en/api/qiskit-ibm-runtime/runtime-job-v2)は状態照会・結果取得・取消を提供する。Amazon Braketも[task IDによる状態照会・結果取得・取消](https://docs.aws.amazon.com/braket/latest/developerguide/braket-monitor-tasks-sdk.html)を提供する。

本実装はこの共通構造を参照し、特定事業者のSDKや状態名をそのまま模倣しない。端末は利用者側のclientで、SDK・将来の画面も同じApplication APIを呼ぶ。

```text
端末 / 将来のUI
  → CredentialedJobConnection（仮想資格情報からprincipalを解決）
  → JobApplicationService（権限・tenant・対応deviceの検査）
  → circuit compiler（命令・入力・処理量の検査）
  → FIFO scheduler → SharedQpuRuntime → QpuStateStore
  → 利用者向けstatus / countsのみを返却
```

## コマンド

```text
help
device list
device show <device_id>
program submit <file> --device <id> --shots <n> [--request-id <id>] [--initialization prepare|inherit]
job status <job_id>
job result <job_id>
job cancel <job_id>
```

`device.info.read`は公開された実行能力の照会権限である。最大qubit数・対応gate・shots上限・取消方針などを返し、状態行列・内部Reset成否・他tenantのjob・物理割当を返さない。既存のservice／actor／scenario capabilityの積集合に従う。

オプションの順序、複数空白、引用したファイル名を許す。未知オプション、重複オプション、欠けた値は拒否する。service profileによってコマンドの文法は変わらない。

ファイル名はactorごとの`TerminalWorkspace`内だけで解決する。Circuit StudioのJSONをUI／editor側が`put_circuit()`で登録する。1ファイル64 KiB・16ファイルまで。ホストのファイルシステムへ名前を解決しない。共有ファイルや任意コード実行の権限は将来のSandbox設計で扱う。

## jobのライフサイクル

送信成功時はQUEUEDのjob IDを返す。状態照会や結果照会は実行を進めず、実行管理が独立に`run_next()`を呼ぶ。未完了結果は`JobResultNotReadyError`になる。最初の取消方針は`queued_only`で、取消済みjobはQPUに触れず、結果はCANCELLEDと空の結果を返す。終了済み・実行中jobの取消はこのprofileでは拒否する。

`--request-id`を同じprincipalが同じ実行要求に再使用すると、同じjob IDを返す。違う実行内容に再使用すると拒否する。省略時はclientが新しいIDを作る。乱数seedはserviceが私有し、再送ごとに変化させない。環境終了による仮想資格情報の失効後は、既存jobの照会も拒否する。

公開job IDは`job-`にUUID4を付けた不透明な識別子。受付連番はscheduler内部に残し、公開IDから他tenantの受付件数を推定させない。IDの乱数は物理sampling seedとは別であり、FIFO順序・同じ要求の再送照合は維持する。

## 2026-10-04 統合境界の修正

成立済みmanifestとserviceのID、解決された認可・compiler、executorのruntime identityに加え、`qpu_service.runtime_execution`の全挙動値をsession生成時に照合する。これにはstate-transition model／回数／測定設定、各物理duration、tenant配置、queue・取消方針、領域資源上限を含む。共通設定は`measure_input_state_before_transition`として表現し、Reset物理modelへ渡す際にmodel固有の入力名へ写像する。profile schemaは`shared_qpu_runtime_v2`。reset課題では同ブロックをchallenge presetで固定し、free modeでは環境成立前に選んだ値を凍結する。Reset orchestratorも同profileが欠けたmanifestを実行拒否する。同じservice／runtime／仮想資格情報authorityを別sessionへ再利用しない。authorityの発行先も登録principalへ制限する。将来の別runtime向けに共通`RuntimePlan`全体を定義済みという意味ではない。

参加者操作のcontrol lockと数値計算のexecutor gateを分離し、FIFOの記録・状態遷移を短いlockで同期した。遅延を注入した計算中にもstatus・後続受付・queued取消を許し、単一の物理executorを守る。close開始で新規操作を止め、現在の計算を受付lockなしで待ち、完了shotを巻き戻さない。[ホスト配置の原則](runtime-boundaries-and-resolutions.md)を、自由／課題共通の境界へ反映する。

UIは新規操作と未確定操作の手動再送を区別し、現在の1要求だけをsession内で保持する。任意の共通端末入力を`CommandIntent(line, requestId)`として渡し、候補HTTPのterminalにも`Idempotency-Key`を接続した。共通parserが`明示--request-id → transport key → 新規UUID`を解決するので、UIで文法を再実装しない。新しい実行は新ID、同じ要求の再送は同じID。内容変更は共通serviceの照合で拒否する。自動再試行やHTTP本接続は追加していない。

未完了: 管理者bootstrap、UI client接続、総資源管理／復旧。receipt順の受理とcompile分離は下記の後続統合を参照。lock分離だけで全ホストタイミング情報を除去したとは主張しない。検証は[進捗](progress.md)を参照。

## 内部受付記録（2026-10-04）

`RequestReceiptLedger`は受領した要求のhost monotonic ns、内部連番、認証済みprincipal、operationを記録する。HTTP候補は認証・上限付きbody/schema・header検査の後、threadpool待ちと回路JSON変換／compileより前に記録する。端末とstructured要求は同じsession記録器を使い、信頼された直接呼出しも`request()`内で記録する。押下・送信時刻、仮想世界への到着時刻、無認証・不正envelopeの全アクセスログではない。

受領はジョブ受理と異なる。失敗・dispatch前の棄却・要求処理完了を区別し、job viewを返した処理には対応job IDだけを関連付ける。`completed`は要求処理の完了であり、ジョブや物理実行の成功ではない。同じ冪等性キーの再送は別のreceiptだが同じjobへ照合される。本文・資格情報・例外内容は保持せず、受付連番・host時刻を参加者応答へ公開しない。

`SessionHostLimits`は`ReceiptLimits(max_pending, max_history)`も必須とする。pendingと完了履歴は別々に上限を持ち、historyは完了記録の有限ringで、全追試に必要な永続traceではない。製品既定値は選んでいない。workerはreceiptを一度だけclaimし、HTTP側の待機取消だけで走行中workerの処理を棄却した扱いにはしない。未dispatchの予約は解放し、session closeは未処理receiptを棄却する。closeは復旧再開ではない（D51）。

liveの完了操作はreply返却時にreceiptとarrivalの履歴を解放し、最新`max_history`件の厳密な同一receipt再送応答だけを有限cacheに保持する。submit／cancelも対象だが、未返信操作の証跡は解放しない。cacheから外れたreceiptは再実行せず、期限切れとして返す。よってpollingや完了済み操作だけで履歴を詰まらせない一方、応答未取得の要求が積み上がればhost受付上限に達する。cacheは再起動後の保存保証ではない。pump reportのtarget／latest-confirmed距離はidle時間も含み得るので、過負荷判定値として扱わない。

初回の記録器だけでは到着順を保存するだけだった。後続の受理接続は次節を参照。異なるprocessや再起動をまたいだhost時刻の比較を保証しない。

## 受付順の受理・compile分離（2026-10-04後続統合）

`AttackSession.request_async()`をHTTP／内部client共通の窓口とし、送信・workspace更新をreceipt順の有限dispatch gateへ通す。実行枠はworker投入前に非同期で待つ。先行要求がまだworker待ちでも、後続の同期待ちでworker poolを埋めない。端末は共通parserで送信／照会／取消を区別し、送信だけを同じgateへ接続する。device・job照会とqueued取消はcompileの終了を待たず、世界を進めない。gateの待機容量は既存のpending receipt上限に含め、追加の無制限queueを作らない。

receipt予約とdispatch記録の短いingress同期はcontrol同期からも分離し、control待ちをhost受付時刻へ混入させない。生存／認可／受理側からingressへ入る順だけを許し、ingress内ではservice／QPU状態やcontrol lockを待たない。workspaceの保持量更新も独自の短い同期で保護する。上限いっぱいの場合はhost容量異常として返し、模擬サービスの拒否や攻撃失敗へ読み替えない。

現adapterでは準備・受理を受付順に直列化し、並列compileによる後続準備の投機は行わない。受領後のhost処理待ちであり、模擬サービスのqueue待ちやQPU時間に加算しない。compiler処理量の上限は従来どおり。照会も含む全host latency、world-clock対応、被害者の自律進行を保証する仕組みではない。

serviceの内部`_prepare_program()`は認可・compiler・server割当を使って不変commandを作り、job／状態を更新しない。callbackの回路変換とcompileはcontrol lock外で実行する。`_admit_program()`直前にsessionの生存・資格情報・容量、serviceの権限・manifest・割当を再検査する。信頼済み内部の二段階境界であり、参加者へcompiled operation・seed・receiptを指定するAPIは増やさない。1要求の送信は1 jobまで。同期`request()`は互換経路で、有限worker poolへ到着順と無関係に大量投入する代わりに`request_async()`を使う。未知の内部callbackは保守的に順序付けし、照会は信頼された呼出元がoperation IDを明示する。

jobは最初の有効受付の`ingress_receipt`を公開commandと別の内部metadataとして保持する。履歴ringのevictionで有効jobの受付時刻・順序は消えない。同一キーの再送は別receiptで元のjobへ照合し、元のmetadata／seedを更新しない。異内容は拒否し、失敗・未dispatchの棄却は後続枠を解放する。応答待ち取消は走行中workerを取り消さない。closeは資格情報失効・待機要求の棄却後、走行中の準備／物理実行をcontrol lockなしでdrainし、受理を再開せず、確定shotを巻き戻さない。

内部clientは攻撃者／被害者とも同じasync窓口を利用できるが、既存Reset orchestrator／予算の移行はまだ行っていない。直接service/runtimeを使う旧内部経路にreceipt順を保証したとは扱わない。world-clock mapping、commit horizon、公開結果の利用可能時刻、復旧は未接続。ホスト時刻・順序は依然として非公開で、QPU時刻ではない。

## 宣言workloadの共通受付接続（2026-10-04）

`api/attack_workloads.py`は凍結済みmanifestの`attacker.workload`／`victim.workload`と`experiment.world_clock`から組み立てる内部owner。`workload_config.py`で版付き有限JSONを検査し、両主体に同じ共通Program／認証／grant／冪等性／host上限を適用する。role・device・layout・service能力の事前検査は操作権限の付与ではなく、明示権限ゼロも開始可能で実操作時に拒否する。

開始前に全予定を予約し、宣言到達順に共通sessionへ一件ずつadmitする。応答喪失は同一jobへの明示再送で整合し、自己結果を共通`job.result.read`で取得してから、trusted ownerの宣言公開時刻を用いて進行する。ホスト認証切れ／容量超過／compile例外では予約を保持し、架空の攻撃失敗を生成しない。設定凍結は維持し、破棄でもResetや設定解凍を追加しない。

専用ownerをclaimしたsessionは未対応のraw HTTP要求／raw実行を拒否する。通常sessionのHTTP候補の文法・動作は変えない。これは一時的な接続境界であり、HTTPと宣言到達の混在を実装済みと主張しない。host receiptは依然host metadataで、runnerのarrival／同着順序／状態snapshotは参加者へ公開しない。実QPU進行・結果公開時刻生成・物理stateとfrontierの原子的確定・復旧は未接続。[引継ぎ](parallel/declared-workload-handoff.md)を参照。

## live接続前の投影・実所有境界（2026-10-05）

共通serviceにtrusted world位置での状態／結果投影を追加した。処理中にtargetが進んでも受付時点より未来の結果へ置換せず、当該jobのSTART／FINISH・release証拠を要求する。参加者入力へtimestampを追加したものではなく、live HTTPへの適用は未完了。

session／registryへ実サイトownerのopaque contextを追加し、HTTP候補はtrusted resolverを任意指定できる。owned sessionには実ownerと仮想Bearerの双方が必要で、actorの共通grant・tenant scopeを維持する。body受信中のowner binding、旧aliasの破棄、receipt captureの競合を短いscope gateで順序付け、control待ちをhost採時へ混入させない。実cookie認証・管理者bootstrap・本mountではない。

新規`LiveIngress`のledger／book同期も内部単独部品の段階。現在のsession receipt処理を黙って置換せず、`ClosedWorkloadWorld`へraw HTTPを混ぜない。[引継ぎ・採用条件](parallel/live-ingress-boundaries-handoff.md)を参照。D60で共通terminal catalogへwait操作を加えたが、これはQPU service grantとは別のclient制御操作で、HTTP pollingへ独自機能を積む意味ではない。

D59採用後の`DeclaredControlService`は、成立前の凍結通信／control timingと、共通actor認証・grant・tenant scopeを時刻付きqueued取消へ接続する内部adapter。取消の作用・結果release・全結果依存を一括更新し、exact再送と未公開suffixの補償を行う。後続`LiveWorkloadWorld`でlive receipt・有限source・control作用・FIFO再計画・物理確定・reply releaseを時刻順に統合した。status／resultはarrival時点を投影し、返信待ちを分ける。cancel ackに別途status-read grantを要求せず、私的時刻やtraceはreplyへ含めない。共通Programのlive入力は旧session ledgerを使わず共通compiler／admissionへ接続する。

## live時間policyと共通wait操作（D60、2026-10-06）

`experiment.live_time_policy`は成立前に必須指定する。製品は`host_clock`、倍率1で`LiveWorldDriver`がbackground pumpし、`pause_while_catching_up`がonなら数値計算を行うpump中だけclock sourceを止める。`explicit_wait`は内部試験経路に残し、`PausedWorldController`が攻撃側の明示waitだけで時計を進める。製品catalogからは選べない。実ユーザーの思考時間・通信待ちはworld clockに反映し、host側の数値計算遅延だけは選択的に除外できる。

terminal grammarは`wait <duration>`（例`wait 250us`, `wait 1.5ms`, `wait 2s`）、`wait request <request_id>`、`wait any`。request IDは接続clientが扱うopaque handleであり、host時刻・receipt sequenceではない。wait request／anyは同じ認証主体の要求だけをcontroller境界で扱い、`max_wait_us`／`max_steps_per_wait`を適用する。操作は共通terminal catalogに登録するがservice operation grantを増やさない。parser／catalog/dispatchは実装済み。WebSocketはwait後に解放済みreplyをpushするopt-in候補として実装した。HTTP候補には機能を追加せず、置換／削除時期はWebSocket mount・UI接続・運用検証後に判断する。

`attack_live_driver.py`は製品`host_clock`（単調host時計、倍率1）専用の内部background pump。成立前設定`experiment.live_time_policy.pause_while_catching_up`がonなら、有限pumpの数値計算中だけhost-clock sourceを止める。`explicit_wait`は内部試験経路に残すが製品catalogには出さない。`attack_live_http.py`はrequest_id＋pollingの暫定ASGI候補で、機能追加の基盤にしない。WebSocketはhost-clock製品とexplicit_wait試験の両方を扱う。[D72](decisions.md#d72-製品のhost-clockと計算追付き時の時計停止決定済み2026-10-08)を参照。

## WebSocket clock protocol（製品host_clock／試験explicit_wait）

接続先は`/api/attack/live/{world_id}/socket`、subprotocolは`quanyu.attack.v1`。router factoryには必須のtrusted `resolve_owner(WebSocket) -> WebOwnerContext`とexact Origin allowlistを渡す。Origin欠落・複数・不一致、またはsubprotocol不一致はaccept前に`4403 ORIGIN_OR_PROTOCOL_REJECTED`で拒否する。Origin検査は認証の代替ではない。ブラウザは任意Authorization headerを付けられないため、accept直後の最初のtext frameを`{"type":"authenticate","credential":"<simulator-issued virtual credential>"}`とする。資格情報をURL・subprotocol・error/logに含めず、公開環境ではWSSを必須とする。

接続後の`ready`は攻撃者自身の時計として`world_time_us`に加え、`clock_mode`・`pause_while_catching_up`を返す。製品の`clock_mode`は`host_clock`（1x）で、時刻更新は`clock` frame、replyはdeclared release後のserver pushで通知する。製品UIに再生／wait操作はなく、host_clockのwait operationは`OPERATION_NOT_SUPPORTED`。内部`explicit_wait`試験routeでは従来のwait operationを使える。command envelopeは`{"type":"command","command_id":"<bounded opaque idempotency key>","operation":"...","payload":{...}}`。共通service operationは`program.submit`、`job.cancel`、`job.status.read`、`job.result.read`。program bodyの形・sizeだけを受領時に検査し、circuitは`LiveProgramEnvelope`として記録して宣言到着時にparseする。`command_id`はprincipal単位で冪等性照合され、内容差し替えは`IDEMPOTENCY_CONFLICT`。`accepted`が返すrequest_idはopaqueで、内部receiptとは別。

エラーframeは`{"type":"error","code":"...","command_id":"..."}`。固定コードは`INVALID_MESSAGE`、`INVALID_REQUEST`、`OPERATION_NOT_SUPPORTED`、`IDEMPOTENCY_CONFLICT`、`REQUEST_NOT_FOUND`、`HOST_CAPACITY_REACHED`、`WORLD_UNAVAILABLE`、`WAIT_LIMIT_REACHED`、`UNAUTHENTICATED`、`CONNECTION_LIMIT`、`INVALID_JSON`、`MESSAGE_TOO_LARGE`。所有者違い・資格情報不正・不存在worldは、最初の認証frame後に同一`4401 AUTHENTICATION_FAILED` close。別principalのrequest_idと未知idは同じ`REQUEST_NOT_FOUND`。service側の認可拒否はrequest受領後のreply内`error_code`として返り、事前に内容検証結果を漏らさない。

解放済み本人replyは`reply` frame（`request_id`, public `value`, `error_code`）で通知する。explicit_wait試験では`wait.completed`（`outcome`と`world_time_us`）の後にreplyを返し、`wait.request`で要求IDのreplyを再取得できる。`request.pending.list`は本人の未配信要求だけを読み取り、`pending.list`に`requests: [{request_id,kind,job_id,issued_at_world_us}]`を返す。これは宣言service到着時刻でありhost timestamp／QPU内部時刻ではない。一覧は時刻を進めず、他principalの要求を含まない。replyを実際に送信できるまで配信済み扱いにせず、未配信要求は黙ってevictせず保持上限で新規受付を拒否する。内部receipt、host時刻、tenant、QPU trace／順序はwireに出さない。text messageはUTF-8 64 KiBまで、worldごとの同時接続は既定4。background pumpとserver pushのpoll cadenceは実行基盤の制御であり、world/QPU timeへ加算しない。数値計算はworker threadへ移し、ASGI event loopを塞がない。desktop Uvicornでは`websockets` protocolと同じ`ws_max_size`を設定する。

## D61 実所有者cookie・環境lifecycle候補（未mount）

`api/attack_web_ownership.py`は、既存の環境factoryと`PausedWorldWebSocketRegistry`を受け取るopt-in adapterであり、`api/main.py`にはmountしない。supervisorのtrusted factoryが`WebOwnerContext`を受け取り、そのcontextをparticipant操作前の共通sessionへbindしてcontrollerを返す。create endpointは設定JSONや仮想資格情報を受け取らず、環境IDは128-bit乱数、cookie bearerは256-bit乱数とする。server recordに残すのはbearerのSHA-256 digestで、raw cookieは`Set-Cookie`応答後に保持しない。

cookie名は`quanyu_attack_<startup nonce>_<environment id>`で環境ごとに別名。`HttpOnly; SameSite=Strict; Path=/api/attack`を必須とし、許可OriginがHTTPSの時だけ`Secure`。仮設定はidle 1,800秒・absolute 86,400秒で、`ATTACK_SIMULATOR_IDLE_TIMEOUT_SECONDS`／`ATTACK_SIMULATOR_ABSOLUTE_TIMEOUT_SECONDS`から変更できる。idle期限はserver recordで判定し、HTTP owner照会およびWebSocket commandごとに活動時刻を更新する。絶対期限はcookie `Max-Age`とserver record双方に適用する。失効後のcookieは環境不存在と同じ応答にする。

通常profileのブラウザ上限は1、server全体上限は暫定8。local developmentとdesktopは明示的にブラウザ上限4を使う。上限応答はbrowser 429 `ENVIRONMENT_LIMIT`、server 503 `ENVIRONMENT_CAPACITY`。devは`ATTACK_SIMULATOR_PROFILE=development`で選び、generic/public profileは`ATTACK_SIMULATOR_ALLOWED_ORIGINS`と`ATTACK_SIMULATOR_ALLOWED_HOSTS`を明示する。既存の`ALLOWED_ORIGINS`の有無からpublic/localを推定しない。desktopは起動ごとにcookie namespaceを再生成し、loopback Hostだけを許す。

WebSocket resolverはOriginとHostの両方を明示allowlist照合する。HTTP resolverもOrigin/Hostを照合し、GET/HEAD/OPTIONS以外では`X-Attack-Simulator-Request: 1`を要求する。環境作成・破棄routerも同じ規則を強制する。未知・不正ID、誤cookie、期限切れ、破棄済みはHTTP 404 `ENVIRONMENT_NOT_FOUND`（既存owned adapterから呼ぶ場合は同じ認証拒否境界）に揃える。cookieを全消去すれば匿名browser ownerは新規になるため、per-browser上限は保持中cookieに基づく運用上限であり、global上限が最終資源境界となる。process-local registryのため、複数worker共有・再起動復元・公開proxyは対象外。

## D63 owner lifecycleとproduct factoryの接続候補（未mount）

`BrowserEnvironmentManager`のtrusted factory callbackは、成立前に設定側が選んだ`ScenarioEnvironment`を`ProductEnvironmentFactory.assemble(environment, owner)`へ渡し、`ProductAttackEnvironment`を返す。factoryはmanifestを成立・検証してから組み立てる。作成POSTはmanifestや資格情報を受け取らず、未検証のbrowser JSONから実行条件を作らない。自由／課題の選択、ユーザー設定の検証、preset registryからの選択は、呼出し側の設定resolver／UIが用意されるまで別の未完了境界である。

成功したPOSTは`201`の`Cache-Control: no-store`本文に`environment_id`と`attacker_credential`を返す。credentialをWebSocketの最初の`authenticate` frameへ渡す必要があるため本文で返し、Cookie／URL／subprotocolには入れない。owner CookieはHttpOnlyのまま、攻撃者virtual credentialとは分離する。被害者credentialはfactory内部に留まり、作成応答・Cookie・WebSocket frameへ投影しない。ブラウザ側は攻撃者credentialをURLや永続的なWeb storageへ保存しない。

同一process内の再読み込み復帰: `POST /api/attack/environments/owned`はcookieで証明できた環境の`environment_id`・`mode`・表示用`created_at`だけを返す。`POST /api/attack/environments/{id}/attacker-credential`は環境固有cookieの所有者照合後、既存の攻撃者credentialを`Cache-Control: no-store`で再交付する。両方ともOrigin・Host・`X-Attack-Simulator-Request: 1`を必須とし、他人／期限切れ／破棄済み／未知IDは同じ404、一覧は空となる。旧credential・接続は存続し、同じworldの既存上限を共有する。破棄・idle／absolute期限切れでは旧新すべてのWSを閉じる。manifest・権限・被害者credentialは投影しない。復帰後は同じWSで`request.pending.list`を要求し、画面の未完了一覧を戻す。復帰と一覧取得だけではD60世界時刻は進まない。[D70承認済み契約](decisions.md#d70-同一process内の再読み込み復帰承認済み2026-10-07)を参照。

既定のbrowser上限1に達した作成は`429 ENVIRONMENT_LIMIT`で拒否し、既存環境を置換しない。server上限は`503 ENVIRONMENT_CAPACITY`。factoryまたはregistry登録が失敗した場合は作成途中のcontroller／authorityを閉じ、owner record・registry entry・予約枠を残さない。成功後のidle／absolute期限はhost lifecycle timerで監視し、期限到達時はrecordを失効させてWebSocketを閉じ、controller/worldを閉鎖してregistry枠を返す。host timerは仮想時計・QPU時間を進めない。明示破棄も同じ資源閉鎖経路を使う。

ASGI routerは引き続きopt-inで、`api/main.py`にはmountしていない。process-localの所有者record／registryなので、複数worker、再起動をまたぐ環境復元、公開proxyは含まない。Reset課題のpreset・grader接続と画面も未完了。[D63](decisions.md#d63-owner-lifecycleから凍結manifestの製品assemblyへ接続する2026-10-07)

## D64 設定catalog／resolver（router未mount）

`api/attack_settings_resolver.py`はブラウザJSONをmanifestとして扱わず、server登録済みの自由テンプレートまたはchallenge presetと照合して`ScenarioEnvironment`を作る。`BrowserEnvironmentManager`には`resolver.bind_product_factory(factory)`を`request_environment_factory`として渡し、owner quota予約後にresolver→製品factoryの順で呼ぶ。入力拒否は汎用`422 INVALID_SETTINGS`、過大bodyは`413 SETTINGS_TOO_LARGE`。内部compile／assembly失敗は入力エラーへ偽装せずowner managerの`ENVIRONMENT_UNAVAILABLE`にする。catalog取得とenvironment作成routerは別々のopt-in候補であり、`api/main.py`にはmountしない。

GET `/api/attack/settings/catalog`の形:

```json
{
  "schema_version": "attack-settings-catalog-v1",
  "modes": [{"id":"free","label":"Free mode"},{"id":"challenge","label":"Challenge mode"}],
  "free_mode": {
    "idle_noise_modes": [{"id":"calibration_values","available":true,"fields":[{"path":"...","type":"number","minimum":0,"maximum":10,"default":2.2,"unit":"us","basis":"...","description":"..."}]}],
    "standard_initial_values": {"source":"...","values":{"t1_us":{"value":300.16,"basis":"..."}}}
  },
  "challenges": [{"id":"reset-leakage","title":"Reset leakage","description":"...","available":false,"fields":[]}]
}
```

descriptorはserverが明示登録した編集項目だけを返す。challengeの`hidden_fields`は値もpathも返さない。unavailableな課題枠は説明と`available:false`だけで、作成には使えない。自由モード作成bodyは`{"mode":"free","idle_noise_mode":"calibration_values|physical_parameters","settings":{"canonical.path":value}}`、課題は`{"mode":"challenge","preset_id":"...","settings":{"canonical.path":value}}`。challenge側の編集はpresetの`editable_fields`かつ可視fieldとしてサーバー登録されたものだけ。モードで選ばなかったnoise入力、未知path、余分なroot keyは拒否する。authority・credential・manifest・basis/evidence・challenge policy pathは、server設定のfield allowlistへ誤登録されても拒否する。これらの値をrequestから受け取らず、型はboolとnumberを区別し、finite numeric valueとfield boundsを確認する。

transport上限はJSON 64 KiB、設定field 128個、string 256 UTF-8 bytes。数値rangeはdescriptorごとに宣言し、全体共通の物理上限を推測で設けない。D62標準値は設定初期値のcatalog表示であり、未登録templateへruntime既定値として流し込まない。製品用free templateや実Reset preset／graderはこの区切りで作成しておらず、未登録設定でworldは成立しない。このwire shapeと上限は[D64](decisions.md#d64-ブラウザ設定resolverの入力境界2026-10-07ユーザー承認済み)で承認済み。

## D62 共通製品assembly factory（in-process候補）

この節のqubit-150既定値と当時の一qubit／synthetic product構成は履歴記録で、製品profileとしてはD65承認およびD66で置換済み。現行実装は本書末尾の「D66 Kingston snapshot製品profile」を参照。

`api/attack_product_factory.py`の`ProductEnvironmentFactory`は、成立済み`ScenarioEnvironment`とtrusted `WebOwnerContext`から、共通`AttackSession`、virtual credential authority、QPU state/runtime、被害者runner、executor、`LiveWorkloadWorld`、host-clockまたはexplicit-wait test controllerを組み立てる。製品host-clockではbounded `LiveWorldDriver`も所有し、環境closeで停止してからworldを閉じる。経路は`ScenarioPreset.materialize(mode)`→成立前`ScenarioEnvironment.set_assumption()`→`assemble()`。free編集はtemplateの許可path、challenge編集はpresetの`editable_fields` allowlist内かつ`locked_fields`外だけに限定し、編集可否もmanifest v2/hashへ凍結する。隠し値は`hidden_fields`とlocked設定で保持する。

製品assemblyは凍結manifestの値だけを読む。必須設定はactorのprincipal／tenant／grant、scenarioのallowed operations、QPUの`runtime_execution`・`reset_model`・`idle_dynamics`・`topology`・`retained_region`・完全な`initial_state`・`circuit_execution` gate durations、宣言service／ingress／control timing、`experiment.world_clock.rate=1`と`live_time_policy=host_clock`／`pause_while_catching_up`。実行係数・service time・初期状態・gate durationは対応するassumption basis recordを要する。manifestにない値、非対応schema/model、値とmodel configuration IDの不一致は組立て前に拒否する。ホストのjob／event／receipt／topology metadata上限は`ProductAssemblyLimits`で別注入し、manifestから読まない。数値実行に必要な所有者を公開する前に全体を構築し、失敗時は生成済みworld／runner／session／authorityを閉じる。これはQPU状態をResetする処理ではない。

待機ノイズ入力は`idle_dynamics.input_mode`で排他選択し、`calibration_values`ならT1/T2／熱平衡占有率の`qubits`のみ、`physical_parameters`なら`physical_inputs`のみをmanifestへ置く。混在は拒否する。較正方式ではT2から`1/Tφ = 1/T2 − 1/(2T1)`でTφを導き、較正由来値に温度・flux noiseを重ねない。標準qubit 150のT1/T2とreadout/gate/reset durationは`validation_results/phase3b_runtime_calibration.json`を直接読む試験で一致を検査する。他の保持qubitに較正値を複製せず、個別入力を求める。

物理方式は明示した`environment_model=generic_superconducting_open_system_v1`、`device_quality`、`temperature_mk`、`flux_noise_phi0`、`qubit_frequency_ghz`、共有T1/Tφ最大値、および物理qubit ID別の`per_qubit` T1/Tφ最大値overrideから、本体と同じ`PhysicalEnvironmentInputs`／`compute_environment_rates_per_qubit()`で率を導出する。待機modelへは`T1=1/(γ↓+γ↑)`、`Tφ=1/γφ`、`p_eq=γ↑/(γ↓+γ↑)`で変換する。クラス内の既存`GenericTransmonProfile`はこの環境modelの固定係数で、temperature等の入力ではない。D62どおり磁束noiseのtransmon現象論は全qubitへ無条件に普遍化できないため、その適用限界を表示する。

`standard_calibration_initial_values()`は設定画面が表示するD62標準候補と値ごとのbasisを返す。出所はIBM Kingston物理qubit 150、2026-08-03の1 qubit値であり、現在値・装置全体の代表値ではない。Reset成功確率は平均readout assignment errorから導くphenomenological値、熱励起0は変更可能な仮定として表示する。このcatalogは較正方式の初期フォーム値で、runtimeへ自動投入しない。選択した方式・値・basisをtemplate／assumptionsへ反映してcompileする。現製品profileの単一qubit時間／誤差はD69の`single_qubit_native_gates`と`single_qubit_gate_decompositions`を使い、CZは結合別較正値を使う。旧`gate_durations_us`は非product profileとCZ schema fallbackの経路に残す。

factoryが適用する待機物理modelは明示的な`independent_constant_rate_v1`だけ。較正方式はT1/T2／平衡占有率、物理方式は物理入力の率導出からT1/Tφ／平衡占有率を得る。どちらも保持qubitごとに確定し、領域外qubitへ値を複製しない。これは独立な定数率の緩和・位相緩和以外（ZZ、クロストーク、ドリフト等）を再現せず、待機modelとして十分かは未決定。未知／非対応modelは推測で補わない。初期topology、保持領域、密度行列、Victim workload、課題判定は各manifestで明示し、最初のReset漏洩課題を自動生成しない。

このfactoryは未mountの内部組立部品であり、D61 `BrowserEnvironmentManager`の環境数予約／registry、create応答での攻撃者virtual credential引渡し、mode/config UI、HTTP/WS接続にはまだ接続していない。D61 quotaとowner managerを通さない直接呼出しは、製品のlifecycle経路として使ってはならない。Reset課題のtraining／victim query予算・grader・seed preflightも共通Program環境へ未移行。

## 最初の回路実行能力

2026-10-05の後続`ClosedWorkloadWorld`は、凍結有限workloadの時計・受付・START／FINISH・共通結果公開・自己結果反復を接続した。service timingは成立前manifestへ処理／公開／拒否durationとbasisを明示する。実provider既定値ではない。manual runner進行・未登録sourceはfenceし、live HTTP／取消controlを未接続のまま混ぜない。通常のHTTP候補の文法・操作catalogは変更していない。[共通接続の引継ぎ](parallel/closed-workload-world-handoff.md)を参照。次の段落は前段physical adapter単独の保証範囲である。

後続の有限宣言列adapterでは、共通sessionのtrusted `run_declared_next`からdetached物理計算と原子的確定を行える。物理success後も宣言releaseへtargetが達するまで`get_result`は未完了を返し、workloadは登録releaseを用いて進む。`run_next`で新境界を迂回できず、session破棄もexecutorをfenceする。通常の旧runtimeの動作は保全する。自律clock同期・live seal・公開duration生成まで実装済みとしない。[物理確定の引継ぎ](parallel/declared-physical-commit-handoff.md)を参照。

`qpu_service.execution_regime=nisq_physical`と`qpu_service.circuit_execution`の`compiler_id=local_sampling_v1`を明示して有効にする。`LocalCircuitCompiler.from_manifest()`で対応gate・`max_program_qubits`・shots上限・命令実行回数上限を解決してから、QPU状態を作る。FTQCはこのcompilerでは拒否するが、製品全体をNISQへ固定しない。compilerを設定していない既存serviceは回路送信を受付前に拒否する。最初の1量子ビット専用compilerは共通IRとともに置換し、未永続化の旧`single_qubit_sampling_v1` profileは黙って読み替えず拒否する。

回路入力は最大8 qubit・32 classical bit、最後の測定blockを受理する。実装可能なgateはI/H/X/Y/Z/S/T/RX/RY/RZ/CNOT/CZ/CP/CCX/SWAP/QFT/ORACLE/MEASUREのsubset。条件付きgate、途中測定、重複する測定／古典出力先は拒否する。countsは古典レジスタ幅のbit列で、`c0`を左端と明示する（事業者SDKのbit順へ暗黙に合わせない）。未書込み古典bitは0。未観測のbit列は省略できるため、32古典bitの全組合せを列挙しない。1 bitの場合だけ既存互換の0／1 keyを残す。

配置は`PhysicalQubitLayout`で論理index順の物理ID列を持ち、認証後のserverで割り当てる。最初のFIFO adapterは事前設定したprincipal別の配置、または保持領域の先頭から回路幅分を使用する。この配置方針は事業者の割当保証ではない。物理IDは連続でなくてもよく、入力bodyからは受け取らない。Resetの旧bindingは明示的な1 qubitプリセットとして保持する。

`QpuStateStore`は領域全体の共同密度行列を正本とし、局所演算・測定後の条件付き状態をそこへcommitする。操作しない周辺qubitも保持し、独立な一ビット状態へ分解しない。gate行列はoperand部分だけを保持し、tensor軸へ適用する。Resetも既存のCPTP局所Kraus channelを同じ共同状態へ適用する。並列columnのgate時間は和ではなく最大値、同じcolumnの終端測定時間は一回分として計上する。

既定の`initialization=prepare`では各shotで、editorの宣言初期状態を「登録済みResetモデルを1回適用し、必要なX/Hを実行」として準備する。理想状態行列への直接置換はしない。`inherit`ではshot初期化と準備gateを省き、前の確定状態を使う。利用にはprofileの`circuit_execution.supported_initializations`（省略時prepareのみ）と、service／actor／scenario／principalの`program.state.inherit`許可が必要で、準備・受理・runtime境界で再検査する。両主体に同じ規則を適用し、tenant交代時のservice Resetは除去しない。gateは既存coreの理想unitaryを使い、測定の真の結果で状態を射影し、登録モデルのreadout誤差を古典結果に適用する。継承時もreadoutを無雑音化せず、Resetなしのreadout問合せに未対応のmodelは拒否する。gate中の雑音・pulse制御の実装とは扱わない。

`initialization`はeditor JSONではなく共通要求envelopeのfieldで、HTTP候補のstructured送信とterminal送信で共有する。省略／明示prepareは同じ冪等性要求になり、同じrequest IDでprepareとinheritを切り替えると競合になる。HTTP本mount・UI選択欄・既存Reset課題移行は未完了。

全体metadataは`QpuTopology`、局所共同状態との対応は`RetainedRegionBinding`で保持する。manifestにtopologyを宣言したsessionは同じmetadataのbindingを必須とし、領域外へのstate relabelを拒否する。ゲート接続・ZZ候補・制御線／伝達・Readout共有は別の関係であり、保存だけでroutingや物理作用が有効になる意味ではない。[今回の接続・残件](parallel/common-foundation-v1-handoff.md)を参照。

1 jobにつき最大4096 shots、初期化・準備gateを含む命令実行は最大16384回。この範囲内でprofileがさらに制限する。shot内部の計算は局所作業状態で行い、完了shotごとに状態と装置時間をcommitする。履歴は1 shotにつき1 eventであり、途中の命令列のevent記録ではない。処理エラー時は完了shotの物理影響を残し、未commitの作業状態を棄却する。

これは主体非依存の`CircuitExecutionOperation`で実行する。既存Reset課題の役割別operationへの黙った変換は行わない。既存の課題orchestratorはこのcompilerを有効にしていない。課題への接続では、観測予算・reference workload・被害者反復を共通program経路へ移す作業が残る。

共通clientの成立にReset課題のtraining／victim query予算を必須にしない。`ScenarioPolicy.observation_budget=None`の構成もmanifestへ解決できるが、service／compiler／hostの資源上限は引き続き適用される。Reset用query経路は明示予算なしでは開始できない。予算の種類と強制箇所を混同しない。[共通契約と課題設定の依存方向](architecture.md#共通契約と課題設定の依存方向2026-10-04)を参照。

回路幅とは別に、現dense adapterは`max_local_qubits`（暫定既定8、実装対応最大10）と`max_execution_work`を検査する。最大10の密度行列はcomplex128のraw行列だけで16 MiBであり、copyやPython tupleのoverheadは別に発生する。これは共通schemaや物理近似の正式な採用上限ではなく、現adapterのホスト保護上限である。8入力＋1 spectatorを同じ9 qubit領域で実行するtestを持ち、回路幅と物理領域幅を同一視しない。

小さい回路でも保持領域が大きければ費用が増える。重み付きmatrix-workのproxyと、compile時のunitary要素数上限を受付前に適用する。これらは壁時計時間や最大RSSの保証ではなく、1要求の暫定処理量上限であり、session全体の予算や履歴の総メモリ上限ではない。job／event履歴の保持上限・永続化・実HTTP sessionの終了／破棄は後続の共通runtime契約で扱う。

今回の一般化は固定した一つの局所領域をFIFOで排他実行する段階。所有tenantのmetadataも領域単位であり、tenant変更時には新jobの配置へservice Resetを適用する。動的な領域拡大・多領域合成、qubitごとの所有権、トポロジーの結線に基づくルーティング、同時job間ZZ／駆動干渉・相関Readoutは未接続であり、これらまで実現したとは扱わない。

隔離対照のfresh-zero割当adapterは現時点では1 qubit領域だけに対応する。`isolated_physical_resource`と複数qubit領域の組合せはruntime生成時に拒否し、tenant交代まで不正構成を持ち越さない。共通回路IRや共有領域の複数qubit対応を1 qubitへ戻したものではない。状態snapshot・transitionへ渡された可変行列は不変コピーへ変換し、参照元の変更でrevisionを迂回させない。

## 今後の順序

環境のmanifest確定は`ScenarioEnvironment`で同期し、同時の設定編集と複数`establish()`による異なるmanifestの公開を防ぐ。成功後の再呼出しは同じmanifestを返し、検査失敗時は未成立のまま再編集できる。authoring session参照はread-onlyで、差替えは別環境を作る。これは成立前設定の凍結を守る処理であり、world時計の起動／破棄後の復旧を追加したものではない。

時間接続は[採用方式・残る判断と内部検査器](runtime-boundaries-and-resolutions.md#43-時間接続の前提検査と判断待ち2026-10-04)を参照。world目標と確定位置の分離／追付きはD53で採用した。明示時刻列の検査まで実装し、live mapping・自律被害者・原子的なhorizon接続と遅延注入の受入検証は未完了。

後続の`DeclaredWorldFrontier`は、明示target・到着網羅済みwatermark・未解決ticketを内部で同期して時刻metadataを管理する。sessionへhost receiptをworld位置として接続したものではなく、`run_next()`の物理commitと公開応答への接続は残る。正確な範囲と検証証拠は[進捗](progress.md)へ記録する。

並列検証の修正では、service／runtimeのsession所有権を短いlockで確保し、authorityもbinding・発行・照合・closeを同期した。sessionは局所bookkeepingを先に作り、service確保後にauthorityをbindし、失敗時はその未公開生成だけのservice／runtime claimを解除する。成立済みsessionのcloseでは解除せず、旧世界の資源を新世界へ再利用しない。読み取り専用の結果投影等はcontrol lock外で行い、返却前に生存・認証・receipt・service同一性を再検査する。queued取消はcloseとの順序を同じcontrol lockで確定する。

ユーザー判断: まず共通コマンドを実装し、Sandbox設計が完了した後で任意コード実行を追加する。

次は共通のsession管理・HTTP・画面へ接続する。任意コードのファイルアクセス・通信先・CPU／memory・停止／破棄はSandbox契約で決める。今回の端末clientを任意コードの隔離機構とは扱わない。

検証: `tests.test_common_command_pipeline`で両actorの同一操作、認証、device照会、送信・冪等性、未完了結果、実行・測定後の状態、取消、権限拒否、未対応回路の無副作用拒否、workspace境界を確認する。

2026-10-03の焦点検証は下記55件が38.257秒で成功。異常Reset状態を測定で隠さないこと、途中失敗で完了shotだけが残ること、Readout誤分類と真のpost-stateの分離、FTQC拒否も含む。全test suite／実機／HTTP／UIの検証とは扱わない。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_common_command_pipeline tests.test_attack_terminal tests.test_attack_program_request tests.test_job_application_service tests.test_shared_qpu_runtime tests.test_reset_leakage_scenario tests.test_reset_leakage_preflight
```

## 複数量子ビットへの一般化（2026-10-03）

所有者指示により、共通基盤の1 qubit制限を廃止した。`CircuitInstruction`は小さいoperand行列・論理target・古典出力先、`CircuitExecutionOperation`は論理／古典幅、`JobCommand.layout`はserver-owned物理ID列を保持する。1 qubitにとどめるのは既存Reset課題の設定であり、共通IRの定義ではない。

Resetは同じCPTP channelの部分trace縮約式で評価し、複素・相関状態について既存Kraus primitiveと照合した。gateは既存coreの拡大行列と照合した。正定値検査は削除せず、commit時の重複検査を除去し、厳密に対角と確認できる状態だけ対角要素を固有値として検査する。終端の互いに異なる測定targetは可換なため、一つの共同分布から標本化して状態を射影する。この一括化は測定間の物理発展を持たない現adapterに限り、将来の雑音／干渉adapterへ無条件に移さない。独立な周辺確率を掛け合わせる近似ではない。

`tests.test_local_program_runtime`でBell共同観測、任意の非連続配置と古典配線、spectator coherence、部分測定による共有相関の更新、Reset失敗時のjob／tenant間状態継承、8 qubit実行、8入力＋9物理qubit領域、異常な対角／非対角状態の拒否、受付前の計算量・配置・命令拒否を確認する。

32 shots・完全Reset・独立Readout誤差0・8 qubit領域で、環境組立→共通端末送信→FIFO実行を一回測定した参考値は、同時X gateで1379.159 ms、H＋CNOT列のGHZで1729.343 ms。両者とも成功し、GHZのcountsは00000000／11111111だけだった。これは実機測定・最大RSS・一般的なlatency保証ではない。先行試走には大きな所要時間変動もあったため、平均的性能や最悪時間が確定したとは扱わない。

最終焦点検証: 115件成功、49.790秒（2026-10-03）。既存Resetのtraining／hidden／負の対照／preflight、仮想資格情報と結果認可も含めた回帰検証であり、全suiteやUIの検証ではない。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_local_program_runtime tests.test_common_command_pipeline tests.test_attack_terminal tests.test_attack_program_request tests.test_job_application_service tests.test_shared_qpu_runtime tests.test_reset_calibration tests.test_reset_leakage_scenario tests.test_reset_leakage_preflight tests.test_reset_leakage_grader tests.test_reset_leakage_analysis tests.test_reset_leakage_acceptance tests.test_experimental_isolation_physics tests.test_scenario_service_conditions tests.test_scenario_credential_lifecycle
```

## Desktop／development接続（2026-10-07）

`api.main.configure_attack_simulator()`は`development`／`desktop`だけで設定catalog・環境lifecycle・WebSocketのrouterを一括mountする。generic/public profileではmountしない。developmentは`ATTACK_SIMULATOR_PROFILE=development`を明示し、desktop launcherは実行ごとの`WebOwnershipSettings.desktop()`を渡す。設定構築に失敗した場合は警告を記録し、攻撃simulatorのみ無効にする。Viteの`/api` proxyはHTTPとWebSocketを中継する。desktopではSPA fallbackより前にAPI routerを追加する。

`api/attack_device_snapshot.py`は`SOURCE.md`記録のSHA-256と照合してからIBM Kingston 2026-04-15 snapshotを読む。製品topologyは156 physical qubits・352 directed coupling entries（176 unique undirected edges）で、dense retained regionは最大6 qubits。D66記録の閾値に従って配置不可qubitもcatalogとmanifestに理由付きで載せ、欠落したT1/T2等は値を補わない。CZ per-edge誤差／duration、qubit別T1/T2、readout assignment error、measurement/reset durationを元値のまま保存する。Reset成功確率はqubitごとに`1-(P(1|0)+P(0|1))/2`で導く近似で、直接測定値ではない。qubit 150のD62値は他qubitへ展開しない。

calibration modeは保持領域のqubitごとのsnapshot T1/T2を待機modelへ入力し、physical-parameter modeは既存`GenericTransmonProfile` rate modelだけから待機率を導く。後者もdevice topology／CZ／readout/reset service metadataを使う範囲はD67で承認済み。CZのsnapshot `gate_error`は平均ゲート不忠実度`r`として、二量子ビット部分系（`d=4`）の完全混合置換チャネル確率`p=4r/3`へ変換する。Choi状態のentanglement fidelityから得る平均ゲート不忠実度は`r=p(d-1)/d`となる。この承認済み写像はシミュレーターで採用するチャネルであり、snapshotのみで実機チャネルを一意に同定した意味ではない。複数qubit同時measurement/resetの共通所要時間は対象値の最大値とする（D67承認）。物理入力modeではqubit frequencyをユーザーが明示送信しない限り作成を拒否する。Reset leakage presetはunavailableのまま。

`frontend/src/clients/attackLiveClient.ts`は`attackLive.ts`の型を変更せず実装する。attacker credentialはclient内のMapに限り、create結果・URL・storage・consoleへ出さない。HTTP create／deleteはD61 CSRF headerを付け、429は`browser_limit`へ変換する。WebSocketは手動接続のみで、自動reconnectしない。UIの動的device/topology discoveryは型に含まれないため、現行単一profileのdevice IDは画面側で固定値として扱う必要がある。複数service profileを選ぶ段階ではcatalog/handleへread-only capability descriptorを加える型提案が必要になるが、今回は型を変えない。
