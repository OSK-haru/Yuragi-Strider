# live受付・照会位置・実所有境界の引継ぎ

2026-10-05。前段は[有限sourceの接続](closed-workload-world-handoff.md)。実装証拠は[進捗](../progress.md)、採用済み時間・所有条件は[共通v1契約](../common-foundation-v1-contract-proposal.md)を正本とする。

## 接続できた部品と未接続境界

`core/attack_simulation/live_ingress.py`は、同じhost単調時刻sampleでreceiptとworld targetを記録し、共通bookへarrival予約を登録する内部owner。`LiveIngressSettings`はclock domain、host domain、通信duration、basis、pending／history上限を必須とする。durationは時計倍率を再乗算せず一度だけ加算する。実サービス既定値を採用した意味ではない。後続の凍結manifest parserは下記を参照。

採時・全sourceの予定公開・grant・退役・cutoffを同じ短いgateで同期する。compile／数値計算／service callbackはgate外。hostの次sampleと同時刻まで確定すると、まだ到着していない同着要求が過去へ挿入され得るため、grant／cutoffはsampleより厳密に過去の時間だけを許す。epsilonや架空のQPU時間を足さない。未解決arrival・自己結果barrierを越えず、host例外・応答喪失・配送取消を模擬DENIEDへ変換しない。退役は共通サービスの明示決定とexact grantを要求し、closeでも証跡をevictしない。

clock／bookのraw参照を別ownerが同期せず更新する構成は契約外。組立担当が全sourceを同じgateへ集約し、一つのlive namespaceを独占させる必要がある。既存`ClosedWorkloadWorld`へ接続しておらず、live cutoffをphysical sealへ無条件に渡すこともできない。HTTPの旧receipt ledgerとこのledgerを併存させてlive接続済みと扱わない。

`DeclaredQpuExecutor.observation_at`とserviceの内部`_get_status_at`／`_get_result_at`は、trusted world位置で状態・結果を投影する。照会処理が遅れて後にFINISHしたとしても、受付位置ではQUEUED／RUNNINGだったという事実を保つ。STARTは当該jobのactive／finished証拠、FINISHは当該jobの確定証拠を要する。共通frontierだけでは別jobのSTARTを証明しない。未追付きは基盤側の未準備として返し、取消／失敗の作用時刻がないtraceを捏造しない。参加者が任意の過去時刻を指定するAPIではなく、live HTTP照会への接続も未完了。

`WebOwnerContext`はtrusted resolverが保持するin-process実所有identity。仮想Bearer・環境ID・新しく作った同型objectでは別所有環境に到達できない。registryのowner binding、HTTP全操作のscope伝搬、受付／破棄開始の競合fenceを実装した。scope同期はcontrolから分離し、control待ちをhost受付時刻へ入れない。trusted declared runnerは別途exact world ownerを証明する。所有許可だけではactor grantを増やさない。

`create_attack_router(..., resolve_owner=...)`はtrusted実所有resolverを受け取るopt-in候補。resolverなしではowned sessionを拒否する。D61の`BrowserEnvironmentManager`がHttpOnly-cookie由来のWebSocket／HTTP owner resolverと環境create/delete入口を実装したが、旧`AttackSessionRegistry`側へは接続していない。テストresolverは実cookie認証ではない。router本mount、実製品factory組立、checkpoint復旧、UI接続は未完了。実所有者の分離を、模擬攻撃者と被害者の物理隔離に読み替えない。

## 監査で修正した境界

- body受信中にunowned環境へownerを付けると、先行HTTP要求がscope再検査なしで受理される問題を再現。全HTTP操作でowner contextを共通receipt／requestへ渡し、bindingと受付をscope gateで原子化した。
- 旧registry aliasの破棄認可とcloseの間に新ownerが付く問題を再現。session内で認可とclose fence開始を原子化し、drainはgate外へ分離した。
- 重なるwindowで先行jobのFINISHを後続jobのSTARTと誤認する投影を修正し、当該job証拠を要求した。
- 厳格なsession型検査で未組立mockを登録していたownership fixtureを修正。scope引数追加に合わせreceipt fixtureも更新した。control待ちを採時へ混入させる途中修正は既存試験で拒否され、専用scope同期へ修正した。

live担当は新規core／test二ファイル、親はsession／HTTP／投影と統合、監査担当は読取り専用。最終件数・検証外範囲は進捗正本へ集約する。

## 採用した接続条件（D59）

1. **実HTTP受付と模擬通信の起点。** 「認証済み完全envelopeのserver受領」を投入起点とし、その先の区間だけprofileで宣言する。実ブラウザ→serverの遅延をクラウド通信の測定値として採用せず、同じ区間の宣言delayを二重に加えない。ゼロdurationも明示した抽象化条件としてのみ扱う。
2. **取消の作用・公開と依存差替え。** queued-onlyを維持し、要求の宣言到達・作用位置・制御応答releaseを共通service timingへ持たせる。作用がSTARTより先なら未実行windowと予定success releaseを取消応答へ置換し、関連する全結果依存barrierも差し替える。STARTが先なら取消拒否。取消後も元のsuccess予定まで待つ方式は採らず、完了済み物理eventは巻き戻さない。failure返信にも同じ一般境界が必要で、Reset専用規則にしない。

二点はユーザー承認済み。実Web認証の具体化は既存ブラウザ所有方針の実装作業だが、実所有識別子の扱いに入る前に必要な秘密情報処理等の承認境界を確認する。上記の内部identity／test resolverで公開安全性を代替しない。

## 後続実装: 凍結設定と時刻付き取消

[live_service_config.py](../../../../core/attack_simulation/live_service_config.py)は、成立前に解決したmanifestの`qpu_service.live_ingress`（`receipt_origin=authenticated_complete_envelope`、`outbound_delay_us`、`basis`）と`control_timing`（`processing_delay_us`、`publication_delay_us`、`basis`、`cancellation_policy=queued_only`）を厳密に検査するopt-in parser。clock domainは共通時計から取得し、host容量は組立側の入力とする。durationは宣言world µsで、倍率を再乗算しない。欠落・未知field・不正値・正durationの座標消失を拒否し、旧閉鎖profileへ必須条件を増やさない。

[attack_controls.py](../../../../api/attack_controls.py)の`DeclaredControlService`は、開始前の共通session／runner／executorへ一度だけ結合する内部adapter。成立後に設定を差し替えず、仮想資格情報からactorを導出し、共通grant・tenant認可を通す。trusted driverのarrivalから作用／応答位置を計算する。参加者指定のbackdateや新しいCLI構文を増やさない。

executorとingress bookは同じ短いtransaction gateを使う。queued取消、未実行windowの削除、release置換、全依存のexact/CAS差替え、私的traceを一括公開する。途中例外は未公開の最新suffixだけ補償し、公開済み取消・START・物理状態は巻き戻さない。exact再送は再認可したうえで同じtraceを返す。取消前はQUEUED、作用後はCANCELLED、結果取得はrelease以降と投影する。releaseだけの登録による迂回を拒否する。

取消自身のarrivalもcutoff・seal・先行grant／退役より過去へ挿入できない。依存がないjobでもこの検査を行う。後続の下界が既存cutoff・grant・退役・sealを越える差替えも拒否する。control有効世界にsubmit-onlyのsuccess下界強化／closed sealを適用する入口をfenceした。閉鎖world constructorの拒否だけでは十分でなかったため、手動入口にも検査を追加した。

この段階では登録済みjob windowに対するtrusted取消接続までだった。後続の単一live driverは次節を参照。private traceが返ることを公開返信済みとは扱わず、releaseまで返信を待つ。bookのcutoffをphysical sealへ無条件に転用せず、既存raw HTTP fenceを維持する。

## 後続実装: 共通live実行器

[attack_live_world.py](../../../../api/attack_live_world.py)の`LiveWorkloadWorld`は凍結manifestのrunner／executor／control／live ingressを開始前に結合する単一owner。内部の`start()`、有限`pump(max_transitions=...)`、`submit`／`cancel`／`status`／`result`／`reply`を提供する。参加者の時刻指定・任意callback・OS実行は受け付けない。操作は既存catalog・Program・actor認証／tenant scopeを通り、自由／課題で同じ構造を使う。まだHTTP routeへmountしていない。

実所有scopeと仮想資格情報を検査してから、worker待ち・compileより前に同sampleのreceipt／arrivalを記録する。live入力には独自のexact ledgerを使い、sessionの旧receiptを作り直さない。sessionの`prepare_live_program`／`admit_live_program`／`submit_live_program`は共通compiler・冪等性・host上限・close/drainへ接続するtrusted入口。有限workloadの旧session callbackは維持するが、そのhost receiptをworld arrivalの正本にしない。外側のdelivery中断で要求を勝手に取消・破棄しない。

arrivalの退役後もcontrol作用イベントを保持し、その位置を越して物理確定しない。取消には開始前に固定したlive namespaceのexact grant／退役証拠を渡す。既に順序確定したarrivalについてだけ過去挿入検査の誤拒否を避け、作用が確定物理より後・STARTより前という条件と全後続依存のcutoff／seal検査は維持する。queued-onlyの拒否は専用の型付き例外とし、一般のhost例外を模擬拒否へ読み替えない。

queued jobの結果依存下界は、成功release、既知の未適用取消release、同じgateで採ったhorizon以降にしか来ない将来取消の最早releaseの最小値で証明する。取消可能なqueued jobへsuccess-only下界を使わない。全pending arrival・結果依存・control作用と厳密な過去horizonを検査してから、物理START／FINISH用のsealを出す。capture・全sourceのbook更新・sealは同じ短いgateを使い、compile／数値計算は外へ出す。

全queued windowを共通FIFO timingから計画し、先行取消があれば未開始の後続windowとreleaseだけ再計画する。arrival・START済みjob・確定物理は変更できない。detached idle計算中にwindowが変わった場合も古いSTARTの公開を拒否する。job window・arrivalは既存job上限へ拘束される。返信済み操作のarrival／receipt履歴は回収し、最新`max_history`件だけ完全一致のreply cacheで再送へ応える。未返信操作は保持し、cache外receiptは再実行しない。有限上限と無限retryなしを維持する。

status／resultはarrival位置の共通投影を保持し、`control_timing`の処理・公開durationから計画したreleaseまでreplyを返さない。取消ackはcancel操作の出力であり、別途status-read grantを要求しない。replyは実owner・元principal・現資格情報を再検査し、detached値／error codeだけ返す。receipt・host ns・world順序・private traceを参加者出力に含めない。係数・basisはprofileの明示値で、実providerの測定値ではない。

破棄はsession／executorをfenceしてdrainし、確定影響・pending証拠を残す。runner snapshotはphysical gate外で取得し、gate内でcloseを再検査する。これによりrunner closeの後処理と逆向きのlock循環を避ける。有限sourceが終了してもlive入力を受け付け続け、reportの`finished`は現時点でのsettled状態であって環境破棄を意味しない。

凍結サービス上限やProgramの不一致による`ProgramRequestError`、操作grant拒否は宣言denialとして退役・返信する。一般compile例外、容量不足、資格情報喪失、closeはexact未解決証拠を残し、明示retry／復旧へ渡す。拒否記録もsession lifecycleの共通commitに含め、破棄fence後に退役しない。STARTと取消作用の同着、既に取消済みjobへの新しい取消要求はqueued-onlyの正常拒否であり、全sourceの進行停止にしない。

残るのはWebSocket候補の本mount、実ブラウザ所有session発行・CSRF／期限管理・成立／破棄入口、UI、同一process checkpoint復旧、Resetの共通Program／予算移行、模擬物理故障profile・実行中取消・他scheduler方式。explicit_wait用WebSocket ASGI候補と`quanyu.attack.v1` protocolは実装・直接ASGI検証済みで、host_clockは拒否する。websockets 17.2のdesktop package収集も隔離build archiveで確認した。HTTP候補はrequest_id＋pollingまでで、機能を積まない。WebSocketの運用mount・実owner／Origin設定とUI接続を検証するまでは残す案で、削除は別途確認する。現在は単一retained領域・FIFO／queued-only・有限submit workload対応で、全サービスへの対応ではない。HTTPの旧ledgerとlive ledgerを二重に使って接続済みと主張せず、raw HTTP fenceは維持する。未合意の物理係数・grantを補わない。件数・監査・未検証範囲は[進捗](../progress.md)を参照。

## D61 cookie owner/lifecycle候補（2026-10-06）

`api/attack_web_ownership.py`に、random environment IDとenvironment別cookie名、256-bit cookie bearerのSHA-256 digest保管、HttpOnly／SameSite=Strict／`/api/attack` path／HTTPS時Secure、idle 30分・absolute 24時間を実装。generic既定browser上限1、desktop/devは4、global上限は仮に8。上限超過はbrowser 429、global 503。Origin・Host allowlistは明示設定、unsafe HTTPはCSRF header必須。期限切れ・破棄済み・未知／不正IDは同じ404、WSのowner失敗は既存generic authentication closeへ畳む。これらのrouterも未mount。desktopは起動nonce付きcookie namespaceを`app.state`へ設定し、devは`ATTACK_SIMULATOR_PROFILE=development`で明示選択する。`ALLOWED_ORIGINS`の有無からprofileを推論しない。期限値・全体上限・cookie属性と匿名ownerの制約は[共通コマンドclient](../common-command-client.md#d61-実所有者cookie環境lifecycle候補未mount)を参照。

## D60の時間進行・wait操作の接続（2026-10-06）

`LiveWorkloadWorld`は成立manifestの`experiment.live_time_policy`を必須読込し、`explicit_wait`に手動advance可能なhost clockを要求する。`LiveWorldDriver`は`host_clock`以外を拒否する。`PausedWorldController`はworldの公開query/control methodsを通し、最早pending event（horizon内）とreply releaseを内部APIへ委譲する。controllerから`LiveWorkloadWorld`のprivate属性参照はなくした。

共通terminal catalogは`wait <duration>`（単位`us`／`ms`／`s`）、`wait request <request_id>`、`wait any`を認識する。request待ちの認可対象は発行者本人の保持request。waitはservice QPU grantではなくclient制御操作で、適用上限は凍結policyの`max_wait_us`／`max_steps_per_wait`。parser／catalog／client capability dispatch・controller動作・WebSocket protocol経由のwait後reply通知をASGIでテスト済み。

`attack_live_driver.py`は`host_clock`用内部background pump。`attack_live_http.py`はrequest_id＋pollingの暫定ASGI adapter候補で、通常applicationへmountしていない。`attack_live_websocket.py`はexplicit_wait専用opt-in候補で、通常applicationへmountしていない。HTTP上へ機能は積まず、WebSocketのmount／UI／実運用が検証できた後にHTTP候補削除を提案する。WebSocket依存はユーザー承認済みで固定済み、desktopのprotocol収集を確認した。deploymentは未実施。
