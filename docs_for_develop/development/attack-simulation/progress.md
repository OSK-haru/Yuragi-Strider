# 現在の進捗と実装根拠

設計の最終同期: 2026-10-08。実装の確認日: 2026-10-09。

## 設計整合の点検と修正（2026-10-20）

対象は、現行設計の共通状態・時刻・主体境界と、それに接続する共通Program受付・観測予算・提出評価。
根拠は[共通操作契約](common-command-observation-contract.md)の§2・§4、
[実行境界](runtime-boundaries-and-resolutions.md)の§3・§4と、
[仮定・過程変化・基盤](assumption-process-model.md)の§3・§4。
新しい製品要件や物理係数は決めていない。

根本で維持すべき注意点:

- 物理状態・サービス状態・観測状態の所有者と確定境界をそろえる。物理計算だけ正しくても、
  受付・予算・再送の意味が違えば同じ実験にはならない。
- ホストの計算時間を装置時間や攻撃失敗へ混入させない。公開時刻と確定位置の分離が必要であり、
  永続化・再起動復元・複数processでの復旧は既知の未完了事項のまま。
- 校正由来の値、サンプル値、近似モデル、抽象コマンドの条件を混同しない。
  内部モデルの数値検証を、実機妥当性や実サービス上の攻撃成立の証明には広げない。

再現して修正した矛盾（確信度: 高）:

1. `job_service.py`の観測予算と被害者列の再送識別が`tenant + key`で、
   schedulerの`tenant + principal + key`と不一致だった。同じtenantの別principalが同じkeyを使うと、
   別jobなのに観測予算が計上されず、被害者列の位置も重複した。
   予算計上をschedulerの初回受理に結び付け、位置の識別もschedulerと同じscopeへそろえた。
2. 通常Programで使用済みのkeyを交互観測で再使用すると、schedulerが衝突を拒否する前に
   観測予算を消費した。予算検査は、認可・入力・receipt・再送衝突の検査後、queue変更直前に実施する。
   受理・予算・列位置はschedulerの同じ短いlockで確定する。
   被害者列の回路は成立準備時に重複を除いてcompileし、受理lock内にはcompileを持ち込まない。
3. `HiddenCaseEvaluator.submit()`は内部計算の失敗でも提出回数を消費し、
   内部`ValueError`はHTTPで利用者の入力不正と扱われた。
   採点成功後だけ回数を確定し、内部障害は秘密を含まないHTTP 503
   `EVALUATION_UNAVAILABLE`として分離した。成功済みのhidden観測は再提出でも再利用する。

検証: 修正前に4本の回帰テストの失敗を確認。修正後は以下の232件がすべて成功した。
新規6件は別principalの同一key、拒否時の無消費、受理時compileの禁止、内部採点障害とHTTP再提出を検証する。

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_interleaved_service tests.test_reset_challenge tests.test_message_challenge tests.test_shared_qpu_runtime tests.test_job_application_service
# 54件 PASS
.\.venv\Scripts\python.exe -B -m unittest tests.test_declared_transaction_stores tests.test_attack_sessions tests.test_live_program_admission tests.test_common_command_pipeline tests.test_attack_product_runtime tests.test_attack_web_ownership tests.test_interleaved_product tests.test_interleaved_execution tests.test_victim_activity tests.test_attack_public_profile
# 109件 PASS
.\.venv\Scripts\python.exe -B scripts/run_tests.py fast
# 69件 PASS
```

`git diff --check`も成功。外部文献との再照合、全物理validation、ブラウザでの動作確認は今回の検証範囲に含めない。
既存の未コミット変更`docs/validation/performance_notes.md`は変更していない。

## 進行中: メッセージ課題（2026-10-14 着手）

利用者の決定: 旧1 bit 課題（id `reset-leakage`）は題名を "Presumption of reset bits" に変えて残す（変更済み）。新課題のメッセージは固定の `WhatIcannotcreateIdonotunderstand`（33文字、ASCII 8 bit・上位ビットから、264 bit）。被害者の観測上限はビット数×5（1320回）、採点は完全一致、提出は3回まで。待ち行列8本の上限も承認済み。

- **済**: 被害者がプログラムの列を順に実行する仕組み（`InterleaveVictimSpec.sequence`）。受理された k 回目の実行は `sequence[k mod len]` を使う。k は run 鍵ごとに初回の受理で固定するので、再送は同じ内容になる。拒否された送信は k も予算も消費しない。
- **済**: 非公開設定 `victim.interleaved_run.secret_message` の読み取り（印字可能 ASCII 1〜64文字）と、課題定義 `reset_leakage_message_challenge`（題名 "Reset leakage"、観測上限 1320、説明文、自由モードで再現できないものに「被害者のメッセージ」）。採点器がまだ無いので、製品カタログには未登録。
- **済**: 採点器 `message-exact-v1`（完全一致、`{attempt, attempts_remaining, passed}` だけを返す。形式違いの提出は回数を減らさない）。工場は `evaluator_id` で採点器を選ぶ。製品カタログの先頭に登録。提出経路はターミナル `answer --message <文字列>`、SDK `session.answer(message=...)`、ブラウザの疑似シェル、画面の通知。フロントは正解数などの集計を任意にした（全部そろうか全部無いかを検査）。
- **実測（2026-10-14）**: 課題の manifest から採点器と同じ service stack を組み、学習用ラベルの観測（32 rounds）を各1000回＋学習10+10回×5組で測った。32 rounds 中の1の平均回数は bit 0 で 0.188、bit 1 で 3.345。学習した閾値（実質「1が2回以上で1」）の 1 bit 誤り率は 7.75%（引継ぎ時の見積り「数%」より大きい）。264 bit の完全一致率は、1回ずつの観測で約0%、5回の多数決で約34%、5回分の1の回数を合算して閾値を引く方法で約89%（二項分布で、合算160 rounds 中の1が6回以上で1、1 bit 誤り約0.05%。保留した200ケースでの誤りは0件）。所要時間は製品で1ジョブ約0.45秒なので、観測1320回で約10分。
- **体験時間の短縮（同日、利用者の決定）**: 課題のプリセット（両課題）は、保持する qubit を被害者と共有する1 qubit だけに絞った（`challenge_region`、共有 qubit は従来の6 qubit 領域の先頭と同じ）。自由モードは制限しない。1ジョブの計算は0.31秒→0.044秒。返事を送る周期（`REPLY_POLL_INTERVAL_S`、ホスト側だけで世界時刻には入らない）を0.1秒→0.02秒にした。製品の課題環境で直列に流すと1ジョブ0.31秒→0.21秒（送信の返事0.14秒、結果0.07秒）、観測1320回で約4.6分。8本並べても速くならない（SDK の `run()` が送信の返事を待つため）。
  - 1 qubit にした後の再測定: 32 rounds 中の1の平均は bit 0 で 0.181、bit 1 で 3.259（6 qubit のときと有意差なし）。学習した閾値での 1 bit 誤りは約8.6%、完全一致率は1回ずつ約0%、多数決約23%、合算（160 rounds 中6回以上で1）約87%（二項分布、保留200ケースで誤り0件）。旧課題の合格率テストも通過。
  - 密度行列の作り直しのコストそのものは、Rust 化のときに扱う（[未決事項](open-questions.md#共有qpuの計算速度後回し)）。
- **画面での通し確認（同日）**: 開発サーバー版をヘッドレス Chrome で操作して、以下を確かめた。
  - カタログの先頭に "Reset leakage"、旧課題は "Presumption of reset bits" と表示される。説明文（k mod 264、完全一致）は出て、メッセージ本体は画面に出ない。
  - ターミナルの `answer --message` は「メッセージは一致しません → 不合格。残り 2 回。」だけを出す。
  - ブラウザの疑似シェルで、学習10+10回の後に被害者の観測1320回を合算して解き、`WhatIcannotcreateIdonotunderstand` を復元して合格した（7.7分、1観測約0.32秒。手元の Python からの0.21秒より遅い）。
  - 確認中に見つけた不具合: 返事の周期を0.02秒にしたとき、同じ処理が送る時計の更新も毎秒50回になり、画面が再描画に追われて固まった（自由モードでも発生）。時計の更新だけ0.1秒間隔に戻した（`CLOCK_FRAME_INTERVAL_S`）。
  - 未解決の観察: 修正後の1回目の通し実行で、約600観測後に画面が応答しなくなった。DOM 数と JS ヒープを見ながら流した2回目では再現せず、最後まで応答した（応答時間は最大約0.15秒）。原因は特定していない。
  - 回路エディターの量子ビット数の範囲を環境に合わせた（同日）。攻撃者のデバイス情報（`list_devices` の `max_qubits`）を WebSocket の `ready` に `max_program_qubits` として載せ、エディターの上限と表示に使う。課題では「1（この環境の上限）」、自由モードでは保持領域に応じて「1〜6」など。範囲を超える回路は上限まで縮める（元に戻す操作で戻せる）。以前は攻撃用エディターでも定数の「2〜8」を表示していた。

## 最新の実装区切り: 公開の再開と手元Python接続の削除（2026-10-14）

- **公開**: 事前審査の結果を受け、公開リポジトリ OSK-haru/Yuragi-Strider を再開。作業ツリーの `origin` を公開側へ切り替えた。公開デモの配信 Worker に COOP/COEP と Python 実行用パスの CSP を足し（`run_worker_first`、`binding: "ASSETS"`）、公開 URL で `crossOriginIsolated`、CDN からの numpy 読み込み、ジョブ2本の実行を確かめた。`binding` の宣言漏れで全ページが一時 error 1101 になった（同日復旧）。1回目の公開確認では、目録と所有環境の取得が 429 になった（このアプリは GET で 429 を返さない。Render か Cloudflare 側の制限とみられる。原因未確認）。
- **手元のPythonからの接続を削除**（利用者の決定、画面のスクリプト実行と重複するため）: スクリプト用トークン、Bearer 認証、Origin の無い WebSocket、`script_access`、`attack_client/websocket.py`、「Pythonから接続」の画面を取り除いた。詳細は[スクリプトアクセス](script-access.md)。`attack_client` は画面のスクリプト実行でだけ開ける。
- **SDK の修正**: `job.result()` が結果公開前の `RESULT_NOT_READY` を例外にしていた。待つ合図として扱うよう直した（以前は、短いジョブでは照会時に結果が出ていたので表に出なかった）。
- 手元 Python からの通しテスト（`tests/test_attack_script_client.py`）は、ブリッジを模した SDK の単体テストと、トークン発行の経路が無いことの確認に置き換えた。
- **SDK の送信を並列に**（2026-10-15）: `session.run()` は送信の返事を待たずにジョブを返し、ジョブIDは初めて必要になったときに待つ。ブラウザで、交互観測200本を直列で流すと1本0.071秒、8本ずつ並べると0.043秒（開発サーバー＋確認用 API、ヘッドレス Chrome）。拒否された送信は、そのジョブを最初に使ったときに `AttackError` になる。検証: SDK の単体テスト5件、validate:attack-script 19件、型検査・lint・build、攻撃系 pytest 396件。`validate:attack-script` の同期チャネルの待機中断の検査（`slices >= 1`）は、負荷が高いと1回落ちた。原因はテスト側で、固定の待ち時間（120ms）の書き込みが、Worker スレッドが待ち始める前に済むことがあった（待ち始めに Worker を300ms遅らせると、旧版は必ず落ちる）。Worker が最初の待ちに入る直前の合図の後でだけ書き込むようにして直した（同じ遅延でも通る）。実装（`syncChannel.ts`）は変えていない。
- 公開デモで1回だけ出た 429 は、翌日に目録へ12回続けて要求しても再現しなかった。
- **画面が固まった件の再調査**: メインスレッドの最大停止時間、longtask、WebSocket 受信数、ヒープ、DOM 数を15秒ごとに記録しながら、1320観測を2回流した（単純な観測ループと、メッセージを解いて提出する通し）。どちらも固まらなかった。メインスレッドの最大停止は約70ミリ秒で、ヒープと DOM 数は増え続けなかった。通しは1.7分（1観測約0.07秒）で合格し、以前の7.7分より大幅に速い。速くなった理由も、以前固まった理由も特定できていない。再現しないため、修正はしていない。

## 実装区切り: ジョブ履歴の保持方式（2026-10-14）

環境1つで受け付けるジョブが生涯8本までだった制限を外した。履歴の持ち方を変えることは利用者が承認済み。

- **保持**: 完了し、結果が公開されたジョブの記録は、新しい順に128本だけ残す（`retained_jobs`）。古いものはスケジューラ・実行器・live world の各層からそろえて捨てる。直前に物理実行したジョブ、まだ届いていない要求や workload の結果待ちが名指ししているジョブは捨てない。
- **鍵**: 再送を見分ける冪等性の鍵は環境の寿命の間残す（`SessionHostLimits.max_jobs` = 16384 本。1時間で約1万本の速度に対する上限）。捨てたジョブの再送は二重実行せず、観測予算も二重に引かない。
- **照会**: 捨てたジョブの状態・結果の照会と再送は `JOB_EXPIRED` を返す。他の tenant のジョブは従来どおり `JOB_NOT_FOUND` と区別しない。
- **上限の返事**: 待ち行列のジョブは8本まで（`max_queued_jobs`）。上限を超えた送信と鍵の上限は、どちらも `HOST_CAPACITY_REACHED` の返事で返す。以前は返事が無く、クライアントが30秒でタイムアウトしていた。
- 実行器の記録上限（`max_runtime_jobs`）は 128 + 待ち行列8 + 実行中1 + 未処理要求16 を収める160。

**検証**: 攻撃系の pytest 437件、`tests/test_declared_transaction_stores.py` 31件、`scripts/run_tests.py fast` 69件、フロントの型検査・lint・validate:attack-live-page／attack-live／attack-script が通過。確認用 API（ポート8003）で、1環境に交互観測ジョブ300本を送信→結果待ちで直列に流し、1本あたり0.46秒で劣化なし、最初のジョブの照会が `JOB_EXPIRED` になることを確かめた。

QPU のイベント記録（1ジョブあたり shot 数程度のメタデータ）も、捨てたジョブの分を一緒に消す。最新の確定バッチは巻き戻し用に残す。

**判断待ち**: 待ち行列8本の上限は、結果を待たずに大量送信するスクリプトを `HOST_CAPACITY_REACHED` で止める。送信→結果待ちを直列で回す使い方（上の300本）では当たらない。

## 実装区切り: 自由モードの「被害者の動き」（2026-10-15）

利用者の依頼で、自由モード限定で被害者の動きが分かるようにした。形は、利用者が選んだ「実際の履歴（サーバーのイベント）と被害者のプログラムの説明」で、有効化は「抽象コマンドとして分け、自由モードの設定でオン（既定オフ）」。

- **サービス**: 新しい操作 `job.victim_activity.read`（抽象コマンド）。ジョブに結び付いた記録済みのイベントを、shot ごとの手順に直して返す（`JobApplicationService.get_victim_activity`、live では `_get_victim_activity_at` で結果と同じ「公開されてから」の時刻制約）。手順は「被害者の実行 → サービスの Reset（被害者の後）→ 攻撃者の測定」と、次の shot の前の「サービスの Reset（攻撃者の後）」、ジョブが始まるまでの待機。各手順に世界時刻（µs）を付ける。先頭16 shot だけを返し、全体の shot 数も返す。**返さないもの**: 密度行列などの状態、被害者の測定値、テナントの識別子。ジョブは自分のものだけ読める。
- **設定**: `qpu_service.abstract_commands.victim_activity`（自由モードの設定項目、既定オフ）。オンにしたときだけ、`supported_operations` にこの操作が加わる。交互観測とは独立（片方だけでもオンにできる）。仮定の記録（`assumptions`）にも出所と制限を残した。
- **課題モードでは絶対に有効にならない**: 課題プリセットは、サポートする操作・許可する操作・攻撃者と被害者の権限の3か所すべてを、この操作を含まない `CHALLENGE_OPERATIONS` にし、設定値を明示的にオフにして固定する。テストで、2つの課題のどちらも3か所に無いこと・設定が編集できないこと・実際の課題環境で要求すると `OPERATION_DENIED` になることを確かめた。
- **経路**: ライブ世界、時計の2種類のコントローラー、WebSocket（`victim_activity` の種別）。接続直後の `ready` に `victim_activity: bool`（この主体が使えるか）を足し、画面は設定を覚えておかなくても、再読み込み後も判別できる。
- **画面**: 使える環境だけ、下の面に「被害者の動き」タブが出る。左に4段階の説明と「このジョブの動きを読む」、右に被害者・サービス・攻撃者の3つの帯の図（幅は所要時間に比例、最小幅と最大幅あり）と、文章（正確な時刻）。ターミナルには `activity [ジョブID]` を足し、返事は JSON ではなく文章で出す。クライアントは、返事を厳密に検査して通った形だけをページの状態に入れる（知らない項目は落ちる）。
- **SDK**: Python の `Job.victim_activity()`（結果の公開まで待つ）。疑似シェルの許可する操作にも加えた。
- **検証**: pytest 攻撃系470件・fast、フロントの型検査・lint・build と、validate の5種（attack-live-page 42件、attack-live 10件、attack-script 20件、tutorial 8件ほか）が通過。確認用サーバー＋ヘッドレス Chrome で、(1) 既定では「使えない」と出る、(2) 2つの設定をオンにして交互観測を送ると図と文章が出る、(3) 通常のジョブには「被害者の実行が挟まっていません」と出る、(4) 課題モードでは、タブが出ず、`activity` も使えない、を確かめた。
- **注意（既存の挙動）**: ジョブの最初の shot の前には、環境ができてからジョブが始まるまでの「待機」（実時間で約1秒）が入る。図では幅に上限を付けた。
- **未対応**: 被害者の測定結果の記録（利用者が選ばなかった案）。公開デモ・狭い画面での見え方。

## 実装区切り: Reset課題のチュートリアル（2026-10-15）

既存のナビペット方式（`context/TutorialContext*`、`components/TutorialOverlay.tsx`）に、3本目のコース `reset-challenge`「Resetの漏れを突く」（約8分、30場面、8章）を足した。利用者が、形（画面内のガイド）と題材（1 bit課題 "Presumption of reset bits" を課題モードで）を選んだ。台本は `utils/tutorialResetChallenge.ts`。

- **流れ**: Resetの漏れとは → 課題モードで課題を選び環境を作成 → 測定だけの回路を置く → 練習相手 `ref:0`/`ref:1` を観測して `result` を読む → しきい値を自分で決める → 被害者を1回だけ観測 → `answer --threshold … --direction ge` で判定ルールを提出 → メッセージ課題 "Reset leakage" へ。手を動かす場面は、達成条件を満たすと自動で次へ進む（満たせないときは「とばす」）。
- **画面側**: 目印（`attack-mode-cards`、`attack-challenge-<id>`、`attack-create-button`、`attack-circuit-editor`、`attack-terminal`、既存の `gate-MEASURE`）、9個の達成条件の報告（環境は課題プリセット `reset-leakage` で作ったものだけを数える）、ビートが指す画面（`attackView`）への切替、コース中はターミナルの面を閉じていても出す、攻撃画面の「Reset課題のチュートリアル」ボタン、ホームの3本目の入口。
- **台本の決まり**: 解答そのものになる数値は書かない（しきい値は「自分の値に置き換える」）。隠しケースの数・合格ライン・提出回数・練習の上限・メッセージ本文は書かない。メッセージ課題の解き方は「どう使うかが勝負」までで止める。
- **検証**: `validate:tutorial`（新設、8件）が、ビートid・章の連続・待ちの条件と案内文・強調する目印が画面のソースに実在すること・台本が案内するコマンドが本物のターミナルの構文解析を通ること（`submit --interleave ref:0/ref:1/other`、`answer`、`result` の3種類の参照を含む）・隠れた設定値とメッセージを書いていないこと・しきい値の例を置き換える前提であることを確かめる。台本を1つずつ壊して、対応する検査が落ちることを確認した。ヘッドレス Chrome で、攻撃画面の入口から最後の「おわり」まで30場面を実際に操作して通し、待ちの場面はすべて条件の達成で自動で進み、修了が保存された。型検査・lint・build・attack-live-page / attack-live / attack-proxy / attack-script も通過。
- **未確認**: 公開デモ・狭い画面・スマートフォンでの見え方。ペットの吹き出しが右下のターミナルの一部に重なる（強調した場所は隠さない）。ページを再読み込みして再開した環境では、課題プリセットを判別できないため「環境ができた」場面が自動で進まない（「とばす」で進める）。コースの文言の読みやすさは、実際の利用者に読んでもらって確かめていない。

## 実装区切り: 攻撃IDEのUI/UX改善（2026-10-15）

実際に操作して（ヘッドレス Chrome、1400px と 390px）、`ui-design.md` の基準（WCAG 2.2 AA、赤は警告だけ）とニールセンの10則に照らして洗い出した11項目のうち、「結果の見せ方」（結果が生のJSONで、完了も自動では来ない）だけを利用者の判断で現状のままにし、残りを直した。

- **選択中のゲートの文字が消える**: 攻撃画面の `button[aria-pressed=true]` の薄い地が、パレットの「選択中は塗りつぶす」より強く、文字色だけ選択中（ほぼ白）になっていた。ホバーも同じ不具合を起こした。詳しさを揃え、ホバーは選択中を塗り替えない。選択中の文字と地は両テーマで 6.05〜8.46:1。
- **ゲート色の文字のコントラスト**: 系統色は暗いテーマ用で、ライトテーマ用の値が無かった。文字用の `--gate-*-ink-rgb` を足し（暗いテーマは従来と同じ、ライトテーマは濃い値）、塗り・枠・グラフの色は変えていない。基準未満の要素は41 → 0。
- **赤の使い方**: 開いた直後の自動検査は赤い「ERROR:」ではなく中立の「送信前の確認」にした（`submit` を打った時の本物のエラーは赤のまま）。通知は中立の線を既定にし、失敗・拒否だけ赤い線と `role=alert`（`noticeWarning`）。1箇所の案内文の赤い線も中立にした。
- **情報の整理**: 環境設定の分類は、0でない件数だけを出す。エラーが無い間の下の面（以前は画面の約25%）は出さない（エラーが出たら自動で出る）。
- **課題カード**: 1枚ずつ囲み、題名を見出しに、説明を文ごとの行に、「この課題を選ぶ」「自由モードで再現」を同じ枠の中に置いた。モードのカードは、選んだ時に枠を太くして「✓ 選択中」と書く（色だけで伝えない）。
- **次にすること**: 作成ボタンのすぐ横に、状態に合わせた案内（モードを選ぶ→課題を選ぶ→作成）を出し、`aria-describedby` で結んだ。無効な「攻撃者ワークスペース」には理由を付けた。
- **狭い画面**: 積む順を「編集欄 → ターミナル → ナビゲーター」にした。幅の広い回路・出力はその欄の中で横にスクロールし、ページの横はみ出し（583px → 390px）は無くなった。ターミナル入力欄まで 2,167px → 1,550px。
- **キーボード**: ゲートのパレットは toolbar の約束どおり、グループごとに Tab の止まりを1つにして矢印キーで移動する（共通の `useRovingToolbars`。通常モードの回路画面も同じパレットを使う）。「ターミナルの入力欄へ移動」のリンクを最初の Tab 付近に置いた。「回路エディター／スクリプト」は `tablist`・`tab`・`tabpanel` と矢印キーに対応した。Tab を押していってターミナルに着くまで、45回超 → 31回（ジャンプリンクなら3回）。
- **大きさ**: 閉じる × を 24×24px、表示の切替ボタンとターミナル入力欄を高さ24px以上にし、量子ビット数の範囲表示（「1〜6」）の折り返しを止めた。

**未対応・未確認**: 分割線（幅5px）は、キーボードでも動かせるが、24px には満たない。狭い画面でターミナルは約1,550px 下で、積む順を変えただけである。Firefox・Safari・実機の読み上げソフトでは確認していない。コントラストは、縞の背景を単色に近似した計算である。

## 実装区切り: 画面のスクリプト実行（2026-10-14）

設計は[スクリプトアクセス](script-access.md#画面のスクリプト実行疑似シェル)。ブラウザ内で利用者のコードを実行すること、依存 `pyodide` の追加を、同日に利用者が承認した。

- **疑似シェル**: 編集欄の「スクリプト」タブでファイルを書き、ターミナルの `python <ファイル> [引数...]` で実行する。標準出力・標準エラー、`input()`、Ctrl+C（KeyboardInterrupt、3秒で Worker ごと停止）、終了コード、利用者のファイルだけのトレースバック。
- **同期SDK**: 手元のPythonと同じ `attack_client` を Worker の Pyodide に組み込む。`attack_client` は通信を差し替え式にした（`websocket.py` と `browser.py`）。`AttackSession()` を引数なしで呼べるのはブラウザの実行環境だけ。待機は SharedArrayBuffer と `Atomics.wait` による同期チャネル。
- **reply の配送の修正**: 同じ所有者の2本の接続（画面とスクリプト、または画面と手元のPython）で、reply が先に取りに来た接続へ渡っていた。reply は要求を出した接続へ返し、その接続が閉じた後は所有者のどの接続でも受け取れるようにした。
- **配信**: 開発サーバーとデスクトップ版に COOP/COEP。Pyodide 本体は `/assets/python-runtime/` から配り、そこだけ CSP に `'wasm-unsafe-eval' 'unsafe-eval'` と公式CDNを足す。画面の CSP は変えない。
- **凍結アーティファクト**: `api/main.py` が対象ソースのため `physical_model_baseline_v1.json` を再生成した（`frozen_contract` は不変、変わったのは日時・hash・コミット）。
- **検証**: Python は攻撃系・freeze系・security header を含む433件と `run_tests.py fast` 69件が成功（新規: 2本の接続の reply 配送1件、CSP と COOP/COEP 2件）。フロントは型・eslint・build と、既存の検証に加えて `validate:attack-script` 18 checks（実 worker thread での同期チャネル、サービス、実行窓口、ファイル置き場）が成功。ヘッドレス Chrome で、開発サーバーとデスクトップ版のビルドの両方について、cross-origin isolation、SDK によるジョブ投入と結果収集、抽象コマンドの拒否、引数、`input()`、Ctrl+C（実行中・入力待ち）、トレースバック、存在しないファイルを確認した。課題モードでは、スクリプトから交互観測と `answer` を通し、20件中19件正解で合格、スクリプトの reply が画面のターミナルに出ないことを確認した。

**未実施**: numpy 等の追加パッケージの実読み込み（公式CDNへの接続を伴うため未確認）。Firefox・Safari での確認。公開デモでは使えない（[未決事項](open-questions.md#画面のスクリプト実行の残件)）。

## 実装区切り: 手元のPythonからの攻撃と抽象コマンドの分離（2026-10-13）

設計は[手元のPythonから攻撃する](script-access.md)。ローカルの開発・デスクトップ用で、`public` には入れない。

- **スクリプトアクセス**: 画面の「Pythonから接続」でトークンを発行し、手元のPythonが`Authorization: Bearer`で所有者として既存のWebSocket・提出HTTPを使う。トークンはhashだけ保持し、再発行で前のものは無効。`script_access`は`local_development`と`desktop`だけが有効で、`generic`／`public`は無効。
- **Pythonクライアント** `attack_client/`: `Circuit`ビルダー、`AttackSession.run()`／`Job.result()`／`answer()`。複数ジョブの並列投入と結果の後収集、自作の回路・学習が自由に書ける。サンプルは`examples/reset_leakage_attack.py`（収集→学習→観測→推定→提出）。
- **ブラウザから学習・自動化を撤去**: `train`／`guess`／`attack`と解析ビューは作らない（サービスの窓口にサービスの操作だけを残す）。
- **抽象コマンド**: 交互観測（`--interleave`）を`qpu_service.abstract_commands.interleaved_observation`で分け、自由モードの既定はオフ。オフのときはサービスの対応操作から外れ、`OPERATION_DENIED`。課題プリセットはReset課題で明示的にオンにする。ターミナルの引数名も`other`／`ref:0`／`ref:1`に変えた。
- **課題の自由モード再現**: 目録の`free_equivalent`が課題の公開パラメーターを自由モードの設定値で返し、課題カードの「この課題を自由モードで再現」で読み込む。隠れた値と採点は含まれない（`not_reproduced`で明記）。
- **検証**: `test_attack_script_client`7件（実サーバーを起動し、トークン・誤トークン・再発行・提出・サンプル攻撃の完走・抽象コマンドのオン/オフ）、攻撃系・freeze系を含む426件が成功。フロントは型・eslint・検証テストが成功。

**未実施**: 画面での「Pythonから接続」と「自由モードで再現」の目視確認、読み込んだ設定で環境を作った後の挙動の確認。

## 最新の実装区切り: 画面確認とReset課題の修正（2026-10-13）

ローカルのブラウザで、交互観測から`answer`提出までを通して確認した。確認用の専用サーバーはポート8002／5190（開発用の許可設定が8001／5173固定のため、起動スクリプトで許可ポートを渡した）。

- **qubit上限の撤廃**: 課題の領域が1 qubitだったため、通常の2 qubit回路が「上限1」で拒否された。課題も自由モードと同じ6 qubitの保持領域にし、被害者は先頭qubitだけを共有する。
- **同一領域判定の緩和**: 交互観測は「被害者のqubitが攻撃者のレイアウトに含まれる」ことを条件にした（以前はレイアウトの完全一致で、被害者1 qubit・攻撃者2 qubit以上の回路が全て拒否された。自由モードも同様）。受付時（`job_service`）と実行時（`shared_qpu_runtime`）の2か所。
- **ターミナルの用語**: 攻撃者視点で「被害者」を名乗らないよう、引数を`other`／`ref:0`／`ref:1`に変え、表示と拒否メッセージを「他の利用者」「参照ラベル」にした。設定画面の領域名など設定項目の名前はそのまま。
- **課題カード**: 「測定前にゲートで状態を変えると、Resetの残りは観測できません。」を追記。ゲート入りの回路では1の割合が約5割に固定され、情報が見えない。
- **実画面の結果**: 測定のみの回路で、ref:0は0/32、ref:1は5/32（Reset成功0.90の予想0.1前後と整合）。しきい値0.05の提出は15/20で不合格（32 shotでは1回の1が0.031で0.05に届かないため）。提出回数の減少、使い切り後の拒否、同一規則で同一結果になることを確認した。

**承認**: 提出回数の上限3と被害者の実観測上限1を、2026-10-13に利用者が承認した。凍結アーティファクト（`cptp_model_freeze.json`、`physical_model_baseline_v1.json`）は、9/10以降のPulse Lab整理でソースのhashが変わったため同日に再生成した（凍結した物理契約`frozen_contract`は不変、変わったのは日時・hash・コミットのみ）。

**残件**: 公開デプロイ（保留）。

## 最新の実装区切り: 公開デモ向けの `public` プロファイル（2026-10-13）

公開デモ（Cloudflare Worker + Render）へ載せるための、**既定では無効**な経路を用意した。環境変数 `ATTACK_SIMULATOR_PROFILE=public` を明示しない限り、これまでどおりマウントされない。手順は[公開手順](../../deployment/web-demo.md)の6節。

- **`public` プロファイル**: HTTPSのoriginのみ、サーバー全体の環境数4（上限8）、ブラウザごと1、無操作15分・最大1時間。ゆるい設定（HTTP origin、`WebOwnershipSettings.public()` を通らない設定）は起動時に拒否し、攻撃シミュレーターだけを無効にする。ワーカーが2以上（`WEB_CONCURRENCY`／`UVICORN_WORKERS`）なら同様に無効にする（状態とcookieが1プロセスのメモリ内にあるため）。
- **全体の流入制御**: 環境作成は毎分6回（429 `CREATE_RATE_LIMIT`）、隠しケースの採点は同時1件（503 `EVALUATION_BUSY`）。Worker経由ではクライアントIPが信頼できないため、個別IPではなくサーバー全体で制限する。
- **同一オリジン化**: cookieが `SameSite=Strict` のため、別オリジン構成（workers.dev と onrender.com）では届かない。`frontend/src/worker.ts` が `/api/attack/*`（HTTPとWebSocket）だけをAPIへ中継する。`Origin`・cookie・CSRFヘッダはそのまま転送し、`X-Forwarded-*` は捨てる。所有者検査は `Origin` と `Host` の両方のホスト名が許可リストにあることを要求するため、`ATTACK_SIMULATOR_ALLOWED_HOSTS` にはAPIとフロントの両方を入れる。
- **Docker**: `data/device_snapshots` を取り込むようにした（スナップショットが無いとプロファイルを構築できない）。api・core・dataだけの最小構成で公開プロファイルがマウントされることを確認した。
- **性能**: 提出時の隠し20ケースの計算は5.2秒から0.41秒になった。Reset用CPTPチャネルが呼び出しごとに作り直され検査されていたため、同一の引数ではチャネルを1度だけ作るようにした（結果は変わらない。Reset関連の59件・隔離物理の22件が成功）。

**検証**: `test_attack_public_profile` 8件、`validate:attack-proxy` 7件が成功。**未実施（利用者の操作が必要）**: Renderの環境変数設定、Workerの再デプロイ、実環境での確認チェックリスト（公開手順6節）。

## 最新の実装区切り: 交互観測とReset課題の製品化（2026-10-13）

**交互観測（共通操作）**: 攻撃者のProgramに公開名 `interleave`（`victim` / `training:0` / `training:1`）を付けると、攻撃者の各shotの前に「被害者の宣言プログラム1回 → 引継ぎ時のサービスReset → 攻撃者1 shot」が入る。従来の1ジョブ複数shotでは持越しが先頭1 shotにしか効かず、Reset漏洩が観測できなかったための共通操作で、Reset専用の経路ではない。被害者プログラムと秘密bitはサーバーがmanifestから持ち、攻撃者は名前しか指定できない。結果は攻撃者側のcountsだけで、被害者の結果は返さない。

- 権限は `program.state.inherit`・`program.interleave.victim`・`program.interleave.training` に分け、サービス能力・シナリオ許可・主体別grantの三層で判定する。
- 同一領域の共有配置でだけ許可する。専用領域・分離資源は公開理由つきで拒否する。
- 所要時間とイベント数は `interleaved_execution.py` の1関数で計画し、時刻窓・実行器の終了検査・ランタイムが同じ値を使う。
- 観測予算は要求単位で課金し、同じ要求の再送では二重課金しない。検証に失敗した要求は予算を消費しない。
- 1回の物理イベント数が環境の上限を超える要求は、受付時に公開理由つきで拒否する（実行器が止まらないため）。

**自由モードでの再現**: 被害者の秘密bit（`victim.interleaved_run.secret_bit`）、攻撃者の1回あたり繰り返し上限（`attacker.interleaved_access.max_rounds_per_query`、上限256）、qubitごとのReset成功確率を編集できる。Reset成功確率の編集に合わせ、`reset_model_configuration_id` はサーバーが再計算する（ブラウザからは受け付けない）。各仮定には根拠区分つきのAssumptionRecordを付けた。自由モードの観測予算は host 保護のための固定値（各1000回）で、物理仮定ではない。

**Reset課題（`reset-leakage`）**: 製品デバイスの保持領域（6 qubit、先頭qubitを被害者と共有）、Reset成功確率0.90・1回あたり32 shots・学習20回（承認済みv0）をロックして提供する。被害者の秘密bitは環境ごとに乱数で決まり非公開。判定規則（`answer --threshold X --direction ge|le`）をHTTP `POST /api/attack/environments/{id}/submission` で提出すると、同じmanifestから作った別環境で隠し20ケースを実行し、正解数・合格ライン・合否の集計だけを返す。提出回数の上限3と被害者の実観測1回（`victim_observation_limit=1`）は承認済み。

**検証**: 新規テスト（`test_interleaved_execution` 8件・`test_interleaved_service` 6件・`test_interleaved_product` 6件・`test_reset_challenge` 8件）が成功。物理: Reset成功0.90での漏洩は約0.10（失敗率と一致）、完全Resetでは被害者bitへの依存なし、既知ラベル訓練と実被害者は一致。課題: 学習した規則は隠しケースで13/20以上、定数推定器は10/20で不合格、完全Resetの対照は15/20以下。全バックエンド1608件のうち失敗3件は今回の変更と無関係（下記）。frontendは `validate:attack-live`（7）・`validate:attack-live-page`・`tsc -b` が成功。

**既知の未解決（今回の変更とは無関係）**: `test_cptp_model_freeze` と `test_physical_model_baseline` は凍結アーティファクトのhash不一致、`test_attack_single_qubit_calibration` の1件は host_clock 化後も `controller.wait` を呼ぶ古いテスト、frontend lint は `AppNavigation.tsx:73` の `react-hooks/set-state-in-effect`。

**未実施・残件**: 公開デプロイ向けの運用（公開proxy・複数worker・再起動復元・レート制限）、課題の解説・チュートリアル。課題modeの物理は製品profile（読み出し誤差はスナップショット値、target populationは(1,0)）で、v0校正時の旧モデル（誤差0.02/0.04、target 0.98/0.02）とは細部が異なる。同じ合格率を保つかは上記テストで確認したが、受入監査CLIは未更新。最初の提出は隠し20ケースの実行のため数秒かかる。

## 最新の実装区切り: 環境設定を分類タブへ整理（2026-10-09）

攻撃シミュレーターの設定画面を、モード／仮想QPU／待機ノイズ／サービスと時間／確認して作成の横並びカテゴリに分け、選択中の分類だけを表示する。カテゴリには入力エラー数と初期値からの変更数を表示し、作成時のローカル入力エラーは最初の該当カテゴリへ移動する。確認画面に変更項目と作成操作をまとめ、各設定のbasisはキーボード操作可能な「根拠」detailsへ移した。仮想QPU分類はsnapshot情報・配置方針・結合図と領域選択、待機ノイズ分類は排他的な方式と物理入力またはqubit別較正値編集、サービス分類は残る時間・実行条件を扱う。Claude作業の`api/attack_product_profiles.py`日本語ラベル・basis差分もレビューし、意味を保った表記変更と確認できたため同じ区切りに含める。

検証: `npm.cmd run validate:attack-live-page` **31 checks成功**。`npm.cmd run lint`・`npm.cmd run build`成功（既存の500 kB超chunk警告）。`git diff --check`成功。画面構成・分類集計・自動エラー分類・根拠detailsのvalidationを追加。desktopブラウザ操作はこの区切りでは行っていない。

残件: serverが返す非項目別の作成失敗は、分類への自動移動に必要な公開理由がないため、既存のnotice表示を維持する。desktop実画面での視覚確認は未実施。

## 最新の実装区切り: 製品profileの複数量子ビット回路送信修正（2026-10-09）

拒否箇所はUIの事前検査ではなくserver compilerだった。UI側の検査は測定の有無・測定後ゲート・古典bit割当だけを確認し、製品の対応ゲート一覧は検査していない。`LocalCircuitCompiler`で直接再現し、製品profileの`supported_gates`にCNOTがなく、`gate or classical condition is not supported by this service`で拒否されることを確認した。製品profileにはCNOTを追加し、snapshot上のネイティブ校正へ接続するため、1 control/1 targetのCNOTをH(target)–CZ(control,target)–H(target)へ展開する。control=0はX(control)を前後に加える。各プリミティブのqubit別H/X校正・unordered物理CZ edge校正を使い、並列段の所要時間は段内最大値を計上する。CNOTの集約durationはmanifest schema用fallbackとして根拠を記録し、実行には使わない。

あわせて、既定領域のBFS列`(2,3,4,16,5,23)`が論理qubit順としては隣接CZを保証しないことを確認した。製品profileとcatalogの既定・代替領域は物理ID昇順へ正規化し、既定6-qubit領域の論理隣接ペアがsnapshot結合に対応するようにした。選択領域はresolverの既存検査で昇順に正規化される。CZの向きはcompilerで物理IDの小さい順へcanonicalizeされ、測定の論理qubit/classical bit対応は従来どおり保持される。

検証: 2-qubit・6-qubitでH、複数の向きのCNOT/CZ、各qubitから対応する古典bitへの測定を行い、既定領域と別の選択領域の両方で送信・結果取得まで行うASGI統合試験を追加。割当物理ID、CNOT展開後の命令数、校正値からの所要時間、測定先もassertする。新規統合試験は5回連続成功。正本focus 60 modules **698件成功（16.297秒）**。`scripts/run_tests.py fast` **69件成功（2.572秒）**。frontend `npm.cmd run lint`・`npm.cmd run build`成功（既存の729 kB chunk警告）。`git diff --check`成功。実ブラウザ操作は今回行っていない。

残件: compilerは選択されたconnected region内のすべての非隣接CZをrouteする機能までは持たない。今回の検証回路は既定・代替領域双方でsnapshot結合が確認できる論理ペアを使う。任意の論理ペアに対するroutingは今回の修正範囲外。

## D73 グラフトポロジー・領域選択・qubit別ノイズ設定（2026-10-09）

ユーザー承認済みのD73を決定事項として`decisions.md`へ記録。IBM Kingston snapshotの156 qubit・176 CZ edgeを表示する決定的な表示座標を`frontend/src/data/ibmKingstonGraphLayout.ts`へ固定し、実機上の物理位置ではない旨をデータ・画面の双方に表示する。使用可／不可／選択中は色だけに依存せず、使用不可の×印と選択領域の強調枠を併用。画面上の領域入替は最大6 qubit・CZ連結性・使用可能性を検査し、配置方針が利用者別領域ならvictim／attackerを区別し重なりを許さない。Challenge presetの公開領域は固定表示し、非公開presetは領域情報を露出しない。較正値方式では選択qubitだけにT1/T2/平衡励起占有率の編集項目を生成し、非選択qubitは表示のみ。physical_parameters方式ではqubit別項目を生成しない。free create要求の領域とnoise overrideはresolverが再検査し、manifestへ凍結する。

検証: snapshot／factory／ownership／runtime／settings resolverを加えたfocus 60 modules **697件成功（16.784秒）**。`scripts/run_tests.py fast` **69件成功（1.431秒）**。resolver＋factory 24件を5回連続成功。frontend `npm run validate:attack-live` 6 checks成功、page validation 28 checksを5回連続成功、`npm run lint`／`npm run build`成功。buildの既存728 kB chunk警告は残る。`git diff --check`成功。Desktop sourceをport 18877で起動し、先行するport 8766の既存processは停止せず残したまま、Chromeから156/176 graph表示、qubit 6との領域入替、T1編集、free環境成立、回路（H→M・古典bit自動補完）送信まで確認。送信応答はjob queued。最新buildの再読込後、qubit 146のクリックで使用不可理由が表示されることも確認。環境は検査後に破棄した。公開deploy・実機QPU試験はしていない。

残件: Desktopブラウザでは回路の受付（queued）まで確認し、job完了replyまでは待っていない。別領域配置とpreset固定は自動試験の範囲で確認し、手動ブラウザ操作では未確認。既存desktop process（port 8766）はこの作業で変更していない。

## D72 製品host_clockと数値計算追付き設定（2026-10-08）

製品profile/factoryを`host_clock`・倍率1へ統一し、`explicit_wait`は試験・互換経路に限定。自由モードの成立前設定`pause_while_catching_up`（既定off）を追加し、onでは数値workerのcatch-up pump中だけ単調時計sourceを停止する。製品WebSocketのreadyに時計mode/pause設定を載せ、時計更新と解放済みreplyをserver push。host-clockのwaitは拒否し、画面に再生／待機操作を表示しない。受付時世界時刻は受付結果に結び付いた値を返す。

製品service-time見直しは、公開IBM資料を調べたうえで**未承認提案**としてD72に記録。QPUのゲート・測定・Reset・idleの物理時間と、service/queue/communication/compile/publication・被害者操作時間を区別した。公開資料のqueue情報・`quantum_seconds`から一般的な固定遅延は導けないため、既存profile値は変更せず、根拠未確定の値を暫定値として明記。数値の変更・採用はユーザー承認待ち。

検証: progress正本のfocus 55 moduleにsnapshot・product factory／ownership・settings resolver／runtimeの5 moduleを追加した60 module・694件成功（14.757秒）。`scripts/run_tests.py fast` 69件成功（2.619秒）。時計source／driver／WebSocket／paused controller／product runtimeの50件を5回連続成功。frontend live client 6 checks・page 24 checksを各5回成功、`npm run lint`・`npm run build`成功。buildには既存の714 kB chunk警告。製品UIの模擬検証は成功したが、desktop実ブラウザ操作は未実施。未決定はservice-time候補の採否・値、catch-up pauseを既定off以外にする必要性、公開資料で埋まらない遅延の根拠。公開IBM資料: [RuntimeJob API](https://quantum.cloud.ibm.com/docs/api/qiskit-ibm-runtime/0.28/runtime-job)、[Session jobs guide](https://quantum.cloud.ibm.com/docs/en/guides/run-jobs-session)。

## D71配置選択と二領域状態保持（2026-10-08）

自由モードの設定目録に配置方針を追加し、同一物理領域を共有する方式と、利用者ごとに別の物理領域を割り当てる方式を選べる。後者は同じ156-qubitスナップショット内の互いに素な6-qubit領域を使う。使用していない領域も状態を保持し、再割当て時にはその間の宣言済みidleノイズだけを遅延適用する。両領域は同じQPU状態storeとFIFOに属し、選択による同時実行や未宣言のクロストークは増やさない。課題preset側の固定も維持する。画面・公式制限ページに選択肢の意味と、クラウドの配置保証を表さない旨を表示。

検証: 配置切替・状態保持・idle進行・原子的rollback・snapshot配置可能性・課題preset固定・自由/課題の作成からreplyまでを含む焦点回帰**63 module・711件成功**。`scripts/run_tests.py fast` **69件成功**。frontend validation **21 checks成功**、`npm.cmd run lint`／`npm.cmd run build`成功。docs `npm.cmd run build`成功。既存のfrontend chunk警告とDocusaurus update-check権限警告は残る。

残件: ブラウザでの手動操作はこの区切りでは実施していない。Reset漏洩課題の採点器/preset接続、プロセス再起動後の状態復旧、領域間の物理クロストークは未実装であり、今回の配置選択から推定しない。

## 監査4点の再検証と対処（2026-10-08）

1. **待機中の破棄・期限:** `prepare_next`を止める試験で旧`close`が待機lockの後ろに回ることを再現。controllerの閉鎖要求を先にfenceし、registryはcontroller/worldの閉鎖と全接続の終了までentryを保持する。owner recordもclosing中は認証を拒否する一方、閉鎖完了までbrowser／server双方の上限枠に残す。中断待機は`WORLD_UNAVAILABLE`であり、`REACHED`や物理失敗にしない。確定済みのQPUイベントprefixが保たれること、明示破棄・期限中の枠保持を試験した。
2. **製品profileの暗黙値:** `measure_input_state_before_transition`と`placement_policy`がruntime既定値から入ること、basis欠落をコードで確認。両値を明示し、測定後の条件付きXによるReset機構／同じ物理領域への配置というシナリオ選択としてそれぞれ`SIMULATOR_CHOICE`根拠を凍結。前者の一般化名は既存manifestにあり、Reset課題経路は変更しない。意味の製品契約化はdecisions.mdに**未承認の提案**として記録し、公式制限ページにも実サービスの保証ではない旨を追記。
3. **スレッド枯渇の推論:** 同じ環境の2接続で待機を強制し、別接続の操作が待機中に`to_thread`へ投入されないことを試験。既存の`entry.lock`は非同期で取得され、環境あたりworkerを待たせる操作は1つ。指摘された同一環境4接続による占有は再現せず、実装変更なし。8環境すべての重い数値計算による一般的な性能限界は別途計測が必要。
4. **受付時刻の割込み:** `now`と受付の間に別WSから`wait`を投入する試験で、先に受付側が持つ`entry.lock`のため割り込めず、発行時刻は0 µsのまま一致した。指摘された別接続経路は再現せず、実装変更なし。controllerをWebSocket以外から同時に直接操作する別経路はこの試験の対象外。

検証: 新規競合・閉鎖6試験を15回連続成功。焦点回帰は正本55 module＋D69の7 moduleで**62 module・701件成功**（最初の1回は既存の期限試験が「WS close frame受信＝全cleanup完了」と仮定して失敗し、閉鎖完了を待つ形へ修正後に再実行）。`scripts/run_tests.py fast` **69件成功**。frontend関連validation 3 module成功（client 6 checks、page 20 checksを含む）、`npm.cmd run lint`／`npm.cmd run build`成功。公式docs `npm.cmd run build`成功。既存のfrontend大きいchunk警告とdocs更新確認用configの権限警告は残る。実ブラウザ操作・公開deploy・実機検証はこの区切りに含まない。

残件: 上記2の意味に関する未承認提案。数値計算を実行中のthreadは強制終了せず、安全な計算境界で閉鎖を完了するため、その間は枠を返さない。大量環境・8qubit条件での性能測定は別作業。

---

## 最新の実装区切り: D70承認・再読み込み後の未完了要求一覧（2026-10-07）

D57の同一process復帰三点をD70として承認済みに更新。既存credentialの旧新接続併用は破棄・idle／absolute期限切れ時の**全接続閉鎖**を条件とし、ASGIで双方の失効を検査した。WebSocketへ`request.pending.list`→`pending.list`を追加し、本人の未配信request ID・種類・job ID（未判明のsubmitはnull）・発行時世界時刻だけを返す。内部receipt・host時刻・tenant・他principalの要求は出さず、一覧取得で世界時刻は進めない。未配信要求を保持上限で黙ってevictせず、容量拒否する。replyの配信済み印はWS送信成功後に付け、送信失敗時は次の接続から一覧と`wait.request`で回復可能にした。clientは資格情報をclosure内に保ち、画面の復帰操作後に一覧を取得して表示する。取得失敗時のみ不明表示に戻す。

検証: WS／製品所有者統合19件を5回連続成功。復帰前の要求を新WSで取得・待機し、他principalの空一覧、送信失敗後の再取得、未配信容量上限、旧新接続の破棄・idle／absolute失効を含む。正本55 module＋D69の7 moduleの焦点回帰**62 module・695件成功（97.298秒）**。`scripts/run_tests.py fast` **69件成功（1.590秒）**。frontend `validate:attack-live` 6 checks／`validate:attack-live-page` 20 checksを各5回、`npm.cmd run lint`／`npm.cmd run build`成功。buildの既存500 kB超警告は残る。今区切りはブラウザ実操作を再実施していない。

残件: process再起動・複数worker間の復旧と、配信済みreply履歴の再生は対象外。Reset課題preset／grader接続も別作業。

---

## 最新の実装区切り: 同一process内のページ再読み込み復帰（2026-10-07）

D57/D61/D63/D64の所有境界に、所有環境一覧と攻撃者資格情報の再交付を追加。両入口はOrigin・Host・D61独自headerを要求するPOST、`Cache-Control: no-store`。一覧は環境固有cookieで証明できたID・mode・表示用host作成日時のみで、manifest／権限／資格情報を含まない。再交付は環境固有cookieを再検査し、旧資格情報を同一所有者に返す。旧タブは維持し、同一worldの接続上限を共有する。未知・他人・期限切れ・破棄済みは同じ404。owner cookieは引き続きhashのみ保持し、仮想の攻撃者資格情報だけをprocess内の環境recordに保持する。具体的な再交付／併用規則はdecisions.mdに**提案**として記録し、承認済み契約とは扱わない。

画面は起動時に所有環境一覧を取得し、「続きから」を利用者が選ぶと新しいclientのclosureだけに資格情報を受け取ってWebSocketへ再接続する。localStorage／sessionStorage／URL／画面state／ログには置かない。復帰だけでは世界時刻を進めない。旧未完了要求一覧の取得APIはないため、画面に「復元できない／表示件数は現画面分」と明示し、`wait any`は維持する。process再起動・複数worker間・旧reply履歴の復元は対象外。

検証: 新規ASGI統合2件を含む所有者関連18件を5回連続成功（追加の旧タブ併用assert後も単独6件成功）。正本55-module listとD69の7 moduleを合わせた**62 modules・692件成功（14.432秒）**。`scripts/run_tests.py fast` **69件成功（1.383秒）**。frontend `validate:attack-live` 6 checks、`validate:attack-live-page` 20 checksを各5回連続成功、`npm.cmd run lint`／`npm.cmd run build`成功（既存のbundle >500 kB警告あり）。Chromeでsource desktop launcher（port 8891）から自由環境を作成し、再読み込み後の「続きから」→接続、0.000 µs維持、H＋測定の32-shot回路送信、明示待機、結果照会のreply（`0:19, 1:13`）まで確認。これは実機・公開deploy・別process復旧の証拠ではない。

残件: 旧未完了要求ID／reply履歴の再取得、process再起動／複数worker間の復元、Reset課題preset／grader接続。再交付時の旧資格情報併用は提案のまま。

---

## 最新の実装区切り: D69単一qubitのsnapshotゲート時間・誤差（2026-10-07）

ユーザー承認済み分解をD69へ記録。snapshot loaderはSHA照合済み原本から全qubitの`rz/sx/x`の`gate_length`と`gate_error`を読み、製品profileは保持領域の元値と分解を根拠付きでmanifestへ凍結する。共通compilerはphysical IDごとに時間を合計し、各sx/xの`p=2r`を`p_eff=1-∏(1-p_i)`で厳密合成して既存runtimeの局所完全混合置換へ渡す。rzは誤差なし。Z/S/T/RZは0、X/H/Yは32 ns、RX/RYは64 nsで、角度0でも宣言分解どおり。準備X/Hも同経路、並列columnの時間は最大値。CZの結合別時間／誤差、原本JSON、UIの型・client、認可・待機方式は変更なし。旧一律gate duration経路は既存の非product profileに保全した。

`tests.test_attack_single_qubit_calibration`新規7件は、全156-qubitのnative元値／単位、両待機方式と保持qubitごとの全9ゲート、分解の凍結、異なるqubit時間を注入した配置／並列時間／準備、Choiによる`d=2`平均不忠実度、相関状態と角度0を含むsx二回のチャネル等価性、rz誤差を追加しないこと、欠落／非有限／表現不能値の拒否、製品factory→controller→runtimeでのチャネル適用を検査。初回は試験入力の初期状態`"1"`が既存schemaで拒否されたため整数`1`へ修正し、最終suiteは**5回連続成功（各7件、0.797–0.849秒）**。基本QPU設定のnode数は較正方式15,583／物理方式15,567で、既存上限を引き上げず両製品経路が成立することを確認した。

検証: 正本55-module listへ下記7 moduleを追加した**62 modules・690件成功（14.082秒）**。`python -B scripts/run_tests.py fast` **69件成功（1.531秒）**。`formalweb/website`の`npm.cmd run build`成功（更新通知無効）、生成HTMLの分解・64 ns・数式エラーなしを検査。`git diff --check`成功。前提・限界ページの一律0.032 µs説明と共通client文書を現実装へ更新した。再現は既存の55-module `$focus`配列に次を足す。

```powershell
$focus += @('tests.test_attack_device_snapshot','tests.test_attack_product_runtime','tests.test_attack_product_factory','tests.test_attack_product_ownership','tests.test_attack_settings_resolver','tests.test_physical_environment','tests.test_attack_single_qubit_calibration')
.\.venv\Scripts\python.exe -B -m unittest $focus
```

残件: 採用した置換チャネルは平均不忠実度を合わせた近似であり、内部pulse・gate中の連続雑音を再現したものではない。runtimeの密度行列更新数は増やしていないが、今回の区切りは性能保証ではない。Reset課題接続、manifest metadata圧縮、reload後の環境発見は継続。新たな判断待ちはなく、実機比較・ブラウザ操作・公開deployは行っていない。

---

## 最新の実装区切り: 公式docsの前提・限界ページと画面リンク（2026-10-07）

`formalweb/website/docs/limitations/attack-simulator.md`を追加し、D60〜D68、既存の進捗証拠、snapshotの`SOURCE.md`を正本として製品profile／owner管理／factoryの実装値と照合した。explicit_waitとhost実時間、156-qubit topologyと最大6-qubit保持領域、歴史的較正値、待機ノイズ二方式、Reset・CZ・所要時間の近似、環境数／期限、未完の課題・公開運用を区別して説明した。既存roadmapの「設計未着手」を現在の共通基盤実装／Reset課題未接続へ修正。sidebarsに登録し、新画面の仮リンクを公式docsの固定URLへ置き換えた。runtime・型・資格情報境界・決定事項は変更していない。

検証: snapshot loaderを実行しSHA照合成功、156 qubits／176 physical edges／352 directed entries、保持領域`(2,3,4,16,5,23)`、qubit 146の除外理由・記録済み閾値を確認。`formalweb/website`の`npm.cmd run build`成功（Docusaurusのリンク検査を含む）。初回の数式内日本語に対するKaTeX警告は記号へ直し、更新通知を無効化した再buildで警告なし。生成HTMLの本文・数式エラーなし・sidebarsリンクを検査。frontend `npm.cmd run validate:attack-live-page` **19 checks成功**（実React出力の公式リンク検査を追加）、`npm.cmd run lint`／`npm.cmd run build`成功。frontend buildの既存500 kB bundle警告は残る。`git diff --check`成功。Pythonの挙動変更はなく、焦点回帰／fast／実ブラウザの物理操作は本区切りでは再実行していない。

残件: deployは行っておらず、画面からの新URLが公開サイトで利用可能になるのは別途docsを公開した後。Reset課題、manifest metadataの圧縮／資源契約、reload後の所有環境発見／資格情報再受渡しは継続。新規設計判断・未承認提案の昇格なし。

---

## 最新の実装区切り: 製品catalogからの新画面とdesktop実ブラウザ接続（2026-10-07）

`AttackLivePage.tsx`／CSSを新設し、既存`attackLiveClient`へ接続した。Claudeの未追跡`attackLiveSettings.ts`を確認・修正して採用し、公開stateだけを持つ`attackLiveWorkspace.ts`とvalidation試験を追加。モード選択、catalog生成form、方式排他／必須周波数、basis／歴史的snapshot注記、使用不可qubit、送信前検査、quota確認→破棄→作成、三方式wait、pending／reply／counts、手動再接続、破棄を実装。実ブラウザ確認後にAppの通常`/attack-simulator`入口を差し替えた。旧`AttackSimulatorPage.tsx`と既存clientは変更していない。型はユーザー承認済みD68の追加互換拡張だけ。

境界修正: same-origin catalog GETは通常Originを送らず、アプリのno-referrer方針はRefererも消す。この読み取り専用入口だけFetch Metadata／許可Hostと任意Refererで検査し、Origin付き要求、状態変更HTTP／WSは従来の厳格検査を維持。設定初期値は重複overrideとして送らずserver templateから凍結。sampling用の測定先補完は利用者が明示選択し、ローカル編集用durationは宣言service時間へ委ねる。補助規則はdecisions.mdに未承認の提案として記録した。

検証: 正本55-module listにsnapshot／product runtime・factory・ownership／resolver／physical environmentを足した**61 modules・683件成功（21.642秒）**。`python -B scripts/run_tests.py fast` **69件成功（1.273秒）**。resolver suite **11件を5回連続成功**。frontend新規validation **18 checksを5回連続成功**、既存client **5 checks**、旧workspace **28 checks**成功。`npm.cmd run lint`／`npm.cmd run build`成功（既存の500 kB bundle警告あり）。途中のlintで非hook関数名`useServiceGateDurations`がhookと判定される失敗があり、`applyServiceGateDurations`へ変更して再検査した。

実ブラウザ: `desktop_app.py --no-browser --port 8890`のsource launcherをChromeから操作。ログはworkspace外へ書かないconsole設定で起動し、packaged exe再buildはしていない。較正値方式／物理方式（周波数5 GHzを利用者入力）の双方で作成→接続→既存エディターのH＋最終測定→32 shots送信→待機→結果照会→reply／実counts→破棄を確認。送信後0 µsを維持、宣言250 µs waitだけで進行。較正方式countsは`10:18,00:14`、物理方式は`10:11,00:21`で各32。課題の準備中表示・選択禁止、範囲外入力の阻止、qubit 146の除外理由もブラウザで確認。countsは実runtime出力で、比較採点／実機一致の検証ではない。quota置換／通信失敗／timeout／課題modeは差替え通信試験で検査したもので、実Reset課題を攻略した証拠ではない。

差替え後の通常`/attack-simulator`でも作成→接続→測定のみの2-qubit回路→wait→結果照会→reply（32 shots、`00:32`）→破棄を再確認した。検証用desktop serverは最後に停止した。

残件: Reset leakage preset／grader／被害者の共通Program接続、公式説明リンク本文（現在`/help#attack-settings`の仮リンク）、reload後の所有環境発見／資格情報再受渡し。snapshot manifestはnode上限に近く、多数の値を同時変更すると根拠metadata追加で成立が拒否され得る（全初期値をoverrideにした再現で確認）。UIの初期値重複は解消したが、許可された全項目の同時変更を支えるmanifestの圧縮／資源契約は別途必要。上限を勝手に引き上げていない。新画面補助規則は提案であり、公開mount／deployも行っていない。

---

## 最新の実装区切り: D67修正承認・CZ channel校正とSOURCE改行耐性（2026-10-07）

`decisions.md`のD67を修正承認済みに更新した。CZのsnapshot `gate_error`を平均不忠実度`r`として扱い、二量子ビット（`d=4`）完全混合置換チャネルの確率を`p=4r/3`へ変換する。Choi状態による解析的検査で平均ゲート不忠実度`1-Favg=r`を確認する試験を追加した。物理パラメタ方式でもtopology/CZ/readout/reset metadataを共有すること、同時measurement/reset durationに最大値を使うことも文書へ反映した。`SOURCE.md`はSHA検証前にCRLF/CRをLFへ正規化してhash行を読む。原本JSONは変更していない。

factoryはCZ channel確率が`[0,1]`外となる場合を拒否する。以前から最大値集約は実装済みで、本区切りでは承認に合わせて設計記録を確定した。snapshotの生値・トポロジー、製品設定範囲はD66どおり維持する。

検証: snapshot suite 5件を**5回連続成功**（各0.343–0.409秒）。正本focusへsnapshot、product runtime/factory/ownership/resolver、physical environmentを加えた**61 modules・682件成功（14.420秒）**。`scripts/run_tests.py fast` **69件成功（1.531秒）**。`git diff --check`成功。焦点にはCRLF `SOURCE.md`読み込み、原本hash不一致拒否、qubit別snapshot、CZ校正値のcompiled probability接続、usable region除外を含む。

残件: 採用した置換チャネルは明示的なシミュレーター近似で、デバイスの実noise channelを同定したものではない。desktop実ブラウザ試験、Reset漏洩preset/graderは本区切りの対象外。frontendの未追跡`frontend/src/utils/attackLiveSettings.ts`は既存ユーザー作業として保持し、今回の変更・commit対象から除外した。`ruff`は環境に未導入のため実行していない。

---

## 最新の実装区切り: D64 browser settings catalog／resolver（2026-10-07）

`api/attack_settings_resolver.py`に、server登録済み自由profile／challenge presetだけを公開するcatalogと、未信頼JSONを検査して`ScenarioEnvironment`へ解決するresolverを追加。factoryとD63 owner managerをrequest-bound callbackで接続し、mode/preset・field allowlist・型／range・選択noise mode排他・重複JSON key・NaN・body sizeを検査する。入力エラーは汎用422、64 KiB超過は413。hidden field/valueはcatalogから省く。catalog GETとcreate POSTは別々のopt-in routerで、`api/main.py`は変更なし。

ASGI直接の新規10件は、catalog秘匿／D62 basis、free overrideのmanifest反映、challenge editable allowlistと固定値拒否、authority／provenance path封鎖、unknown／range／type／inactive mode／duplicate／size拒否、両modeでcatalog→create→WS authenticate→submit→wait→reply→deleteを検査。Reset leakageはunavailable preset枠のみで、製品用自由profile／実challenge preset・grader／UIは未登録・未接続。wire schemaと上限はユーザー承認済みD64。新規suiteは**5回連続成功（各10件）**。正本focusにproduct factory、関連physics 3件、product ownership、resolverを加えた**61 module・689件成功（12.634秒）**。`scripts/run_tests.py fast` **69件成功（1.420秒）**。

未決: 実用service profileと自由mode template、Reset challenge policy/grader/observation budgetの登録、HTTP/WS routerのmount、UI接続。server registryが空ならfree設定はunavailableとして返り、設定を推測して環境を成立させない。

---

## 前区切り: D61 owner lifecycleとD62 product factoryの接続（2026-10-07）

`BrowserEnvironmentManager`がtrusted factoryから`ProductAttackEnvironment`を受け取り、そのcontrollerをregistryへ登録する。成功した作成POSTは`environment_id`と攻撃者virtual credentialだけを返し、被害者credentialはresponse／cookie／WSへ出さない。既定browser上限1では429で拒否し既存環境を置換しない。作成中のfactory／registry失敗では部分controllerを閉じ、owner record・予約枠を残さない。既存のcontroller-only callbackはowner境界試験用に維持し、product credentialは返さない。

idle／absolute deadlineごとにhost timerを設け、期限到達でowner recordを失効し、registry経由でWSを閉じてworld/controllerを閉鎖する。HTTP／WS照会時の失効も同じ資源cleanupを行う。host lifecycle timerはD60のvirtual clock／QPU timeを進めない。作成側の設定resolverは信頼済み呼出し側から準備済み`ScenarioEnvironment`を選んでcallbackへ束縛する。browser入力からmanifestを受け取らず、自由／課題選択UIとpreset registryは未接続。

ASGI直接の新規統合4件は、free／challenge双方のcreate→WebSocket auth→submit→explicit wait→reply→delete、attacker-only credential投影、browser/global quota、途中失敗後の再試行、idle／absolute期限時のWS closeと不存在同等応答を検査。新規suiteは**5回連続成功**。焦点回帰はD62の59-module構成に`tests.test_attack_product_ownership`を追加した60 modules **679件成功（20.116秒）**。`scripts/run_tests.py fast` **69件成功（1.391秒）**。追加suite実行後`compileall`成功、`git diff --check`成功（CRLF変換の警告のみ）。

残件: routerはopt-inで`api/main.py`へ未mount。自由／課題の選択・設定編集・preset解決を行うUI／resolver、Reset課題preset・grader接続は未完。credentialは成功POSTの応答時だけブラウザへ渡し、reload後の再取得はない。browser上限は有効environment cookieから識別するD61匿名ownerの範囲で、全cookie消去後のidentity reset、process-local registryのmultiworker共有／再起動復元、公開proxy運用は未対応。単体上限到達は既存環境を保持して429拒否とした。[D63接続契約](decisions.md#d63-owner-lifecycleから凍結manifestの製品assemblyへ接続する2026-10-07)と[共通client境界](common-command-client.md#d63-owner-lifecycleとproduct-factoryの接続候補未mount)を参照。

## 最新の実装区切り: D62待機ノイズ二方式のfactory対応（2026-10-06）

`api/attack_product_factory.py`の待機noise manifestを排他的な`calibration_values`／`physical_parameters`へ拡張。較正方式は保持qubitごとにT1・T2・平衡占有率を要求し、Tφだけを式から導出する。物理方式は明示した環境model、device quality、temperature、flux noise、qubit frequency、共有T1/Tφ最大値、物理ID別のT1/Tφ最大値overrideを読み、本体の`PhysicalEnvironmentInputs`と`compute_environment_rates_per_qubit()`を利用して`IdleQubitSpec`へ変換する。方式外のfield混在、欠落、範囲外、非対応modelは拒否する。自由templateでの物理parameter変更と、challenge presetで方式・値を固定する経路を試験した。

較正catalogがD62 source JSONのT1/T2/readout assignment/readout duration/reset duration/1Q gate durationと一致することを試験で照合。温度上昇で平衡占有率が上がり、flux noise上昇でTφが短くなること、rate outputが本体simulation結果と一致することも確認した。

検証: D61 canonical 55-module focusへ`tests.test_attack_product_factory`および関連physics 3 moduleを加えた59 module **673件成功（9.656秒）**。`scripts/run_tests.py fast` **69件成功（1.356秒）**。factory suite 8件を5回連続成功、factory/testの`compileall`・`git diff --check`成功。最初のD62 calibration-only 56-module結果は直前区切りの履歴として残す。

残る点: 物理方式は現行`GenericTransmonProfile`の既存係数と環境率モデルへ従属し、flux-noise現象論が特定transmon想定である限界は変わらない。温度・周波数・flux noiseのqubit別overrideは既存rate layerにないため実装していない（既存のqubit別T1/Tφ最大値overrideのみ対応）。UIでの方式選択／説明は未接続。Reset課題presetへの適用、物理profile係数自体をユーザー編集可能にするか、対象範囲の物理妥当性評価は未決。詳細は[共通コマンドclient](common-command-client.md#d62-共通製品assembly-factory-in-process候補)と[D62追記](decisions.md#d62-標準ノイズ値を持たせ最初にモードを選ばせる2026-10-06)。

## 最新の実装区切り: D62 explicit-wait製品assembly factory（2026-10-06）

`api/attack_product_factory.py`に未mountの`ProductEnvironmentFactory`を追加。準備済み自由／課題`ScenarioEnvironment`を成立させ、manifest v2の明示値だけから共通session、credential authority、QPU state/runtime、victim runner、executor、live world、explicit-wait controllerを組み立てる。host側の資源上限は`ProductAssemblyLimits`で別注入し、manifest既定値で補わない。未対応schema／idle model、物理・サービス設定不足、reset modelとruntimeの構成ID不一致、Reset/readout modelの適用qubit範囲不一致は拒否する。構築途中で失敗した場合は作成済み部品とauthorityを閉じる。

D62 calibration catalogは設定画面向け初期値だけを返し、runtimeは参照しない。値ごとのbasisと出所を付け、IBM Kingston physical qubit 150の2026-08-03較正値であり現在／装置全体の代表値でないと表示する。TφとReset成功率は導出値、熱占有率0は変更可能な仮定として記録する。製品compilerはgate durationをmanifestから受け取り、未指定gate durationを暗黙補完しない。free-modeの変更可能pathとchallenge presetの`editable_fields` allowlist／`locked_fields`を分離し、challenge側の変更可能性もmanifest v2/hashへ含めた。Reset漏洩課題そのものはまだ共通経路に移していない。

検証: D61の55 module focusに`tests.test_attack_product_factory`を追加した56 module **651件成功（9.531秒）**。同focusのうちproduct factory suiteは5件を5回連続成功。`scripts/run_tests.py fast` **69件成功（1.327秒）**。対象7 Python filesの`compileall`と`git diff --check`成功。focus再実行は直前のD61 `$focus`配列へ`tests.test_attack_product_factory`を追加して実行する。

未完了／制約: このfactoryはin-process組立部品で、D61 `BrowserEnvironmentManager`（現状controller-only callback／create response）とのassembly・attacker credential受渡し、mode/config UI、HTTP/WS接続・mountには未接続。したがってbrowser/global quotaのlifecycle統合や実ブラウザ体験の完成を意味しない。最初のReset課題preset／grader／training予算／seed移行も未決。待機物理は明示的`independent_constant_rate_v1`のみで、ZZ・クロストーク・ドリフトを含めず、妥当性の判断は残る。設計詳細は[共通コマンドclient](common-command-client.md#d62-共通製品assembly-factory-in-process候補)と[D62](decisions.md#d62-標準ノイズ値を持たせ最初にモードを選ばせる2026-10-06)。

## 最新の実装区切り: D61 HttpOnly owner cookie・環境lifecycle候補（2026-10-06）

`api/attack_web_ownership.py`に未mountのcreate/delete router、cookie由来HTTP／WebSocket owner resolver、明示的なhost/origin・CSRF検査を実装した。環境IDは128-bit乱数、environmentごとに別名のcookieへ256-bit bearerを発行し、serverはSHA-256 digestだけを保持する。cookieはHttpOnly／SameSite=Strict／`Path=/api/attack`、HTTPS Origin時Secure。idle 30分・absolute 24時間は起動設定で変更可能。generic既定はbrowser 1・server全体8、development／desktopはbrowser 4。上限応答は429 `ENVIRONMENT_LIMIT`／503 `ENVIRONMENT_CAPACITY`。`ALLOWED_ORIGINS`の有無からprofileを推論しない。desktop起動ごとにcookie namespaceを変える。HTTP routerは`api/main.py`へmountしていない。

ASGI直接試験はowner/lifecycle 12件・WebSocket既存境界10件が成功し、実sessionへowner contextをbindしてWebSocket仮想資格情報を認証する統合も含む。期限切れ・不正／未知IDの同じ404、Origin／Host・CSRF拒否、Cookie属性・hash-only、既定／local 4／global上限、破棄後拒否、設定profileと未mountを検査した。owner suiteは10回連続成功。D60に記録したfocus listへowner／security headerの2 moduleを加えたfocus **55 modules・646件成功（104.323秒）**。`scripts/run_tests.py fast` **69件成功（14.737秒）**。追加17件（owner 12＋security header 5）も成功。`compileall`対象5 Python files、`git diff --check`成功。焦点回帰を再実行する場合はD60の`$focus`配列に`tests.test_attack_web_ownership`と`tests.test_security_headers`を加える。

匿名ownerは保持中の有効cookieで識別するため、全cookie消去後は新identityとして再開できる。browser上限はcookie保持を前提とした運用上限で、global上限8が最終process内資源境界。process-local registryのためmultiworker共有・再起動復旧は未対応。実環境factoryの製品組立、HTTP/WS本mount、公開proxy、UI、期限値・global上限の負荷校正は未完了。設計詳細は[D61](decisions.md#d61-実装具体化2026-10-06)と[共通コマンドclient](common-command-client.md#d61-実所有者cookie環境lifecycle候補未mount)。

## 最新の実装区切り: D60 explicit_wait WebSocket候補（2026-10-06）

`api/attack_live_websocket.py`に未mountのopt-in ASGI transport候補を追加。`quanyu.attack.v1`、first-frame仮想資格情報（URL/header/subprotocolには置かない）、必須trusted owner resolver、exact Origin allowlist、text frame 64 KiB、worldごと接続上限、有限request/idempotency cache、明示wait後の本人reply pushを実装した。接続上限は同時認証でも原子的に守り、未知request IDのエラーも有限cacheを超えない。`LiveProgramEnvelope`で受付を先に記録し、宣言到着時に回路をparseする。receipt／host時刻／tenant／内部traceはwireへ出さない。数値処理・controller操作は`asyncio.to_thread`へ移し、host_clock worldはcontroller境界で拒否する。環境破棄時は接続をcloseし、request idの別principal／未知を同じ`REQUEST_NOT_FOUND`で扱う。

Protocol詳細とerror schemaは[共通コマンドclient](common-command-client.md#d60-explicit_wait-websocket-protocol候補未mount)。`api/main.py`にはmountしておらず、公開認証resolver／Origin設定、実ブラウザclient・運用server接続は未完了。HTTP polling候補は機能追加せず残した。explicit_waitのUI移行後に利用が不要と確認できれば削除候補だが、host_clockを含む他modeのtransport代替が決まるまでは保持を提案する（削除は未実施）。

WebSocket ASGI直接試験35件、焦点回帰52 module **629件成功**、`scripts/run_tests.py fast` **69件成功**。新規WebSocket suiteは10回連続成功。Testはsocket／外部test dependencyなし。PyInstaller desktop buildは提出releaseと別の`build/websocket-validation/`へ生成し、archive内の`uvicorn.protocols.websockets.websockets_impl`と`websockets` modulesを確認。`desktop_app.py`は`ws="websockets"`／64 KiB上限を指定するが、攻撃routeは未mount。websockets 17.2は承認済み固定依存。build scriptへrelease保護と隔離出力path指定を追加した。D61区切りではこの52 moduleへowner lifecycle／security header試験を加えて再実行する。

## 前回の実装区切り: D60時計policy・wait操作・live接続境界（2026-10-06）

`LiveWorkloadWorld`は成立manifestの`experiment.live_time_policy`を必須読込し、欠落／不正policyを拒否する。`explicit_wait`では手動advance可能なclock sourceを要求し、`LiveWorldDriver`は`host_clock`だけで構成可能とした。これによりD60の「攻撃者の明示waitのみで進む」契約と、単調host clockへ追従するmodeを実装境界で区別する。host上の数値処理遅延を宣言QPU時間へ加算しない。

`LiveWorkloadWorld`へhorizon内の最早未処理event時刻、receipt replyのrelease状態を含む正式query／clock-control入口を追加し、`PausedWorldController`からworld private stateへの参照を除去した。`wait <duration>`（`us`／`ms`／`s`）、`wait request <request_id>`、`wait any`をterminal parser・terminal操作catalog・client capability dispatchへ追加した。waitはQPU service grantではなくclient操作で、本人の要求だけを待ち、凍結`max_wait_us`／`max_steps_per_wait`を適用する。WebSocket adapter候補は後続区切りで接続した。

`attack_live_driver.py`は`host_clock`用のbackground pump。`attack_live_http.py`はrequest_id＋pollingの暫定ASGI候補で、この上へ機能を積まない。HTTP mount、ブラウザ実操作と運用接続は未完了。

前回の進捗には「48 module」と件数だけがあり、完全なmodule listは記録されていなかった。このため同一の48件構成を特定できず、今回の再現可能なfocusとして関連48 moduleを明示し、新規`test_attack_live_driver`／`test_attack_live_http`／`test_attack_paused_world`を加えた51 moduleを実行した。最初の再実行では新規host-clock testのimport漏れが1件発生し、修正後に全51 module **619件成功（75.695秒）**。既存fast **69件成功（1.297秒）**。live直接回帰78件も成功。文書10件のローカルMarkdown link／heading anchor 165件、`git diff --check`成功。UI変更なし。以下は今回の明示focus再現コマンド。

```powershell
$focus = @('tests.test_attack_http_adapter','tests.test_attack_live_world','tests.test_attack_program_request','tests.test_attack_scenario_policy','tests.test_attack_sessions','tests.test_attack_terminal','tests.test_attack_workloads','tests.test_attack_world','tests.test_declared_controls','tests.test_declared_execution','tests.test_declared_ingress','tests.test_declared_observation','tests.test_declared_service_timing','tests.test_declared_transaction_stores','tests.test_declared_world_clock','tests.test_job_application_service','tests.test_live_control_proof','tests.test_live_ingress','tests.test_live_program_admission','tests.test_live_service_config','tests.test_local_program_runtime','tests.test_program_admission','tests.test_program_initialization','tests.test_qpu_topology','tests.test_reset_leakage_scenario','tests.test_scenario_compiler','tests.test_scenario_credential_lifecycle','tests.test_scenario_environment','tests.test_scenario_service_conditions','tests.test_session_ownership','tests.test_world_event_frontier','tests.test_common_command_pipeline','tests.test_receipt_admission','tests.test_request_receipts','tests.test_event_commit_boundary','tests.test_experimental_isolation_physics','tests.test_host_delay_invariance','tests.test_shared_qpu_runtime','tests.test_victim_workload','tests.test_workload_config','tests.test_reset_calibration','tests.test_reset_leakage_acceptance','tests.test_reset_leakage_analysis','tests.test_reset_leakage_grader','tests.test_reset_leakage_preflight','tests.test_http_owner_boundary','tests.test_web_owner_boundary','tests.test_snapshot_request_policy','tests.test_state_snapshots','tests.test_attack_live_driver','tests.test_attack_live_http','tests.test_attack_paused_world','tests.test_attack_live_websocket','tests.test_attack_web_ownership','tests.test_security_headers')
.\.venv\Scripts\python.exe -B -m unittest $focus
.\.venv\Scripts\python.exe -B scripts/run_tests.py fast
```

D60実装はin-process／ASGI direct WebSocket testとPyInstaller archive確認までで、公開認証・実network・browser実操作、WebSocket route mount／運用server、server復旧、サービス別実機妥当性を検証したものではない。

## 最新の実装区切り: 共通live実行器の時刻順統合

2026-10-05。新規[attack_live_world.py](../../../api/attack_live_world.py)の`LiveWorkloadWorld`で凍結actor workload・共通session／executor・live ingress／controlを組立て、受付・取消作用・照会・FIFO再計画・物理START／FINISH・自己結果進行・返信releaseへ接続した。全sourceのbook／短いgateを共有し、有限`pump`で追い付く。live入力はworker待ち前に記録し、旧session ledgerでreceiptを作り直さない。自由／課題modeに同じProgram・操作catalog・認可・時間規則を使い、Reset専用分岐・成立後設定変更・新しい係数／grantを導入していない。[構造・対応範囲・残件](parallel/live-ingress-boundaries-handoff.md#後続実装-共通live実行器)を参照。

queued結果の下界は成功releaseだけでなく既知の未適用取消と将来live取消の最早releaseを含め、pending control作用を越して物理確定しない。取消には固定live namespaceのexact受付証拠を渡し、START前取消と確定物理保全を両立する。先行取消後の未開始FIFO window／releaseは再計画でき、既存arrival・実行開始済みjobは変えない。状態・結果照会はarrival位置で投影し、返信は宣言release以降に元principal／実ownerへdetached値だけ渡す。cancel ackに別途status-read grantを要求しない。

分担はsessionの共通live Program準備／受理入口と新規9試験、executorのexact受付証拠・queued再計画と新規22試験、親のlive driver・runner統合と新規22試験。書込み範囲を分離し、親が全差分を検査・統合した。読取り専用監査は別担当。

並列検査でrunner closeとmetadata copyのlock循環を再現し、copyをphysical gate外へ移動してgate内close再検査へ修正。3経路の独立再probeを確認した。独立監査の三指摘（START同着／重複取消の進行停止、サービス入力拒否によるgrant残留、拒否公開のclose競合）も修正。型付きqueued-only拒否・Program入力denialとhost例外の保留を分け、拒否記録もlifecycle commitへ含めた。修正後の独立5 probeは全PASSで、今回trusted driver範囲の残存重要指摘なし。公開認証・HTTP／UI完成を保証する監査ではない。

最終焦点回帰48 module **574件成功（46.515秒）**、既存fast **69件成功（1.334秒）**。新規53件を含む。最終焦点135件成功（1.167秒）。途中567／570／571件の成功を最終証拠へ代えない。途中の585件runはsession module11件の重複指定を含み、最終runは48 moduleを重複なしで再実施した。担当70件／115件・親245件等は重複するため加算しない。広い試験の成功後にも監査で三不具合を再現したため、監査と回帰試験を別証拠とする。

新規受入は両modeの複数qubit・有限被害者反復とlive Program、300qubit topology＋3qubit retained領域、同ns receiptと厳密な過去horizon、取消分岐／FIFO再計画／同着／重複拒否、歴史QUEUED／結果未準備投影、cancel-only grant、他tenant／不存在の同じ投影、reply copy保護、host容量・実owner／exact receipt・raw HTTP fence、compile／数値中断と明示retry、数値中の破棄・compile中の受付を含む。同じ宣言入力について10msの数値遅延と1件ずつのpumpを加えても、物理event時刻・counts・状態・被害者進行・確定位置が不変であることを検査した。preset係数や実サービス妥当性の証明ではない。

後続監査の指摘1には、`qpu_service.runtime_execution`へruntimeの物理・時間・配置・queue・資源値を全て直列化し、`AttackSession`生成時とReset orchestrator起動時のruntime／manifest完全一致を必須化した。Reset v0 preflightも同じ明示profileを作る。共通設定名`measure_input_state_before_transition`と`shared_qpu_runtime_v2`へ改め、Reset model側の入力名はadapter内へ閉じた。challenge presetの値は既定値を明文化しただけで、実機サービス妥当性を示さない。指摘2はhost実行時間とtarget／confirmed距離の診断値を追加した段階で、逸脱閾値・判定保留・復旧は未実装。指摘3は返信済み操作を有限exact reply cacheへ移す形で修正し、未返信操作だけ履歴に残す。

有限actor workloadなしのlive worldも組立可能とし、物理sealをactor clientの存在から独立させた。target／latest-confirmed距離はidle時間を含み得るため、過負荷・未処理仕事量とは解釈しない。

2026-10-05のHTTP adapter監査で、envelope回路parseが`program.submit`／`program.state.inherit` grant確認より先に走る順序不備を再現し、共通認可をparse前へ移した。サービス側のcompile／admission認可は維持して再確認する。無許可の不正回路・正規回路とinherit要求でparser未呼出しを検査し、HTTP／driver／live world焦点46件成功。HTTP adapterはASGI内の候補で、公開mount・実owner認証・browser実操作は未検証。

live runner作業直後の記録では回帰未実施・actorなし経路未受入だった。その後の受入試験は下記の履歴を参照。ユーザー報告の574件・fast 69件はこの後続修正の検証結果ではない。1shot／8qubit以上のhost負荷計測・P1の性能逸脱判定閾値／判定保留／復旧は未実装で、pump計測値だけでは性能条件を判定しない。host上限・job履歴・reply replay cacheの関係も資源試験を要する。

最短の再実行:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_attack_live_world tests.test_live_program_admission tests.test_live_control_proof tests.test_attack_workloads tests.test_attack_world tests.test_declared_controls tests.test_declared_execution tests.test_live_service_config
.\.venv\Scripts\python.exe -B scripts/run_tests.py fast
```

前回回帰は前段45 moduleへlive driver 3 moduleを加えた48 module。今回の差分は回帰していない。次はmanifest/runtime binding・actorなしworld・read reply retentionの回帰と、実行資源の計測。HTTPのasync ingress／delivery・worker接続と実所有lifecycle／checkpoint、その後共通UIとResetの共通Program／予算移行も残る。HTTP本mount・実ブラウザ所有session発行／CSRF／期限・成立／破棄公開入口、同一process checkpoint復旧、UI・任意コードsandbox、模擬物理故障profile・実行中取消・他scheduler方式は未完了。現在は単一retained領域のFIFO／queued-only、有限被害者workload有無どちらも内部組立可能で、全サービス・全攻撃経路対応とはしない。既存raw HTTP world fenceを維持する。

UI変更なしのためUI／lint／buildは再実施していない。全suite・実ネットワーク・ブラウザ・provider妥当性・長時間RSS／性能・checkpoint復旧は検証外。追加依存・network・実秘密処理・Git履歴変更・commit／push／deploy・automation再開なし。既存画像・人格・無関係な変更を保全し、実装担当／監査担当を終了した。最終文書検査は9文書のローカルfile参照165件、今回code／test 9ファイルの末尾空白、tracked `git diff --check`が成功（heading anchor・外部URLは対象外）。

## 前の実装区切り: 凍結live設定と共通時刻付き取消

2026-10-05。通信起点と取消作用／応答・依存差替えの二条件はユーザー承認により[D59](decisions.md#d59-通信起点と取消時刻の追加合意2026-10-05)へ昇格。`live_service_config.py`で明示duration・basis・queued-only条件を凍結manifestから検査し、`api/attack_controls.py`で共通資格情報・grant・tenant認可へ接続した。自由／課題modeに同じ境界を使い、Reset専用規則・参加者指定時刻・provider既定値・成立後設定変更を追加していない。[構造・schema・未接続](parallel/live-ingress-boundaries-handoff.md#後続実装-凍結設定と時刻付き取消)を参照。

取消、未実行windowの削除、予定success release置換、関連する全結果依存のexact/CAS差替え、私的traceを共有gateで一括公開する。未公開suffixだけをbounded journalから補償し、公開済み取消・確定済み物理を巻き戻さない。履歴QUEUED／CANCELLEDと結果releaseを分け、exact再送は再認可後に同じtraceを返す。control履歴は既存job上限、依存はbook上限に拘束され、準備tokenはweak retention。取消処理に数値計算・host採時・networkを追加しない。

独立監査でclosed-only手動sealがcontrol世界でも通る点、後続依存がない取消自身をcutoff以前へ挿入できる点を再現し修正した。closed-only seal／success下界強化をfenceし、取消arrivalもcutoff・physical seal・grant／退役の過去へ挿入できない検査を追加。releaseだけの登録による迂回と設定object差替えも拒否する。原子的更新の試験は複数依存の途中書込み失敗・再試行、stale／copied token、取消／破棄競合、declared START先行、遅いhost処理での時刻・状態不変性を含む。

最終焦点回帰45 module **521件成功（77.759秒）**、既存fast **69件成功（9.960秒）**。新規parser14件・controls20件を含む。修正前518件成功は最終証拠に代えない。途中の93件はfixtureの実event順序とSTART同着順序の期待で2 failure、115件は破棄後service参照／例外型変更で各1 errorが発生し、fixtureと従来の基盤例外契約を修正した。最終焦点115件成功（1.325秒）。担当116件・親121件等は重複するため加算しない。

読取り専用の独立再監査は関連100件と追加probeで今回整合境界のblockingなし。初回80件成功でも二欠陥が残っており、試験成功と監査結果を区別した。監査件数は親521件へ加算しない。

最短の再実行:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_declared_controls tests.test_live_service_config tests.test_declared_ingress tests.test_attack_workloads tests.test_attack_world tests.test_declared_execution tests.test_declared_observation
.\.venv\Scripts\python.exe -B scripts/run_tests.py fast
```

全焦点commandは前段43 moduleへ`tests.test_live_service_config tests.test_declared_controls`を追加した45 module。未完了は全sourceの単一live owner、取消予約・照会・公開・物理eventの全体順序、失敗応答trace、live HTTP／安全seal・実運用loop、実所有lifecycle／復旧、共通UI、Resetの共通Program／予算移行。現在の取消adapterは登録済みjob windowを要し、private trace返却は公開返信済みを意味しない。既存raw HTTP world fenceを維持する。二条件の再承認は不要で、次は単一live driverの接続・遅延不変性検査。

UI変更なしのためUI／lint／buildは再実施していない。全suite・実ネットワーク・ブラウザ・provider妥当性・長時間RSS／性能・checkpoint復旧は検証外。追加依存・network・実秘密処理・Git履歴変更・commit／push／deploy・automation再開なし。既存の画像・人格・無関係な変更は保全。文書最終検査は更新9文書のローカルfile参照160件、code／test 11ファイルの末尾空白、tracked `git diff --check`成功（heading anchor・外部URLは対象外）。実装担当・独立監査担当を終了。

## 前の実装区切り: live受付・照会位置・実所有の前提部品

2026-10-05。`LiveIngress`へ同host sampleのreceipt／world target・arrival予約、全source共通gate、厳密に過去のgrant／cutoff、exact退役／再送・有限容量・close証跡保全を追加した。executor／serviceにはtrusted位置での状態・結果投影、session／HTTP候補には実owner context＋仮想Bearerの二段検査を追加。新規三境界とも共通基盤で、Reset専用操作・時刻公開・成立後の設定編集は増やしていない。[実装・未接続・判断点](parallel/live-ingress-boundaries-handoff.md)を参照。

独立監査のowner後付けとHTTP受付／旧alias破棄の競合、別jobのFINISHをSTARTと誤認する投影を修正した。ownership fixtureの未組立mock登録も修正。途中修正でcontrol待ちがhost採時へ混入した回帰は既存試験で捕捉し、scope同期を分離した。全HTTP操作へowner contextを伝搬し、破棄は認可＋close開始を原子的にfenceしてdrainを外へ出す。

最終焦点回帰487件成功（54.091秒、新規50件を含む）、既存fast 69件成功（1.349秒）。途中481件はownership fixtureの4 error、486件は受付control待ちとfixture引数不一致による2 failure／5 errorがあり、修正後487件を最終証拠とする。狭い117件と担当70件の成功は重複があるため加算しない。

読取り専用の最終再監査は関連18 moduleの245件（1.510秒）と独立10 probeを確認し、今回範囲の修正必須指摘なし。control保持中の受付／認証、他者receipt保全、正しい実ownerでもraw HTTP world fenceが403・無受付／無物理作用、exact workload ownerだけの別入口を含む。件数は親487件へ加算しない。live physical seal・実認証・復旧の完成を保証する監査ではない。

全焦点commandは前段39 moduleへ`tests.test_live_ingress tests.test_declared_observation tests.test_web_owner_boundary tests.test_http_owner_boundary`を追加した43 module。最短の再実行:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_live_ingress tests.test_declared_observation tests.test_web_owner_boundary tests.test_http_owner_boundary tests.test_receipt_admission tests.test_session_ownership tests.test_attack_sessions tests.test_attack_http_adapter tests.test_attack_workloads tests.test_declared_execution tests.test_attack_world
.\.venv\Scripts\python.exe -B scripts/run_tests.py fast
```

**当時の判断待ち（後続D59で採用済み）:** 実HTTP受付から模擬通信をどの区間として宣言するか、取消作用／返信時刻と予定success／結果依存barrierの差替え。現在の採用条件は引継ぎへ更新済み。live bookを閉鎖worldのphysical sealへ接続せず、raw HTTP混入fenceを維持する。実ブラウザ所有認証の実装も秘密情報処理等の承認境界を越えない。

未完了: live／宣言sourceの単一owner組立・physical seal、取消／failure／照会の時刻順適用、実Web認証／lifecycle／復旧、共通UI、Resetの共通Program／予算移行。単独部品の検証を体験完成や公開安全性の証明にしない。UI変更なしのためUI／lint／buildは再実施していない。全suite・実ネットワーク・ブラウザ・provider妥当性・長時間RSS／性能・checkpoint復旧は検証外。追加依存・network・実秘密処理・Git履歴変更・commit／push／deploy・automation再開なし。

文書最終検査: 更新8文書のローカルfile参照140件、code／testを含む19ファイルの末尾空白、tracked `git diff --check`が成功。heading anchor・外部URLは検査外。実装担当・監査担当を終了した。判断点に従属する接続は停止し、既存の画像・人格・無関係な変更は保全した。

## 前の実装区切り: 有限sourceの共通時計・受付・実行・公開接続

`api/attack_world.py`の`ClosedWorkloadWorld`で、凍結済み両主体workloadと既存session／物理executorを接続した。明示`start()`と有限`pump(max_transitions=...)`から、nominal target同期、共通受付、FIFO window生成、START／FINISH、共通自己結果公開と反復を進める。手動window・seal・release callbackは不要。実運用のtimer／live HTTP／UI接続の完成ではない。[分担・入力・証明範囲](parallel/closed-workload-world-handoff.md)を参照。

serviceの処理／公開／拒否durationとbasisは成立前manifestに必須とし、provider既定値・最初の課題数値を補わない。`declared_service_timing.py`は共通Program・shot・実tenantから物理durationを求める純粋planner。結果公開はQPU占有と分離し、STARTをFINISHより先に確定する。runnerを一度だけexclusive claimし、全有限reservationと自己結果の証明済み下界を越さないsealを生成する。submit-only範囲の証明であり、live取消／失敗返信を未登録のまま混ぜない。

独立監査のadmission応答喪失後の再送停止と、FINISH確定後の継続位置喪失を修正し回帰を追加した。残存grantをqueue解釈より先に再送し、次windowはexecutorの確定位置から生成する。別担当の再点検で10秒後の小数物理durationが座標差の丸めで拒否される問題を再現し、丸め済み終点の照合とコンパイル値による物理dt維持に修正した。満杯履歴をidleと誤認する問題と、registry破棄後のseal metadata更新も修正・検査した。

最終焦点回帰437件成功（48.333秒）。新規timing 19件、world統合17件、book下界1件を含む。途中435件はfixture拡張と既存mockの引数不一致で1件失敗し、既定fixtureに不要kwを渡さない修正を行った。修正前434件／座標修正前436件の成功を最終証拠へ代えない。最終再実施の既存fast 69件も成功（1.656秒）。独立確認件数は重複するため加算しない。

新規受入は両mode／両主体の複数qubit Program、300qubit topology＋3qubit retained領域／spectator、自己結果分岐・有限反復・ゼロdelay同着、START先行・公開待ち、数値遅延不変性、応答喪失、数値中断・明示retry、host容量・破棄と所有fenceを含む。fixture係数・timingは試験用の明示条件で、実サービス妥当性や初回preset採用の証明ではない。

最短の再実行:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_attack_world tests.test_declared_service_timing tests.test_declared_ingress tests.test_declared_execution tests.test_attack_workloads tests.test_attack_sessions tests.test_victim_workload
.\.venv\Scripts\python.exe -B scripts/run_tests.py fast
```

全焦点回帰は従来37 moduleのcommandへ`tests.test_declared_service_timing tests.test_attack_world`を追加した39 module。未完了はlive HTTP／control到着と安全sealへの同期、正式service profile根拠表示・idleモデル選択、実Web所有・成立／破棄と整合復旧、共通UIの実client接続、旧Reset課題の共通Program／予算移行。次は閉鎖sourceだけのownerをlive受付へ接続し、その後既存UIへつなぐ。取消を共通操作catalogから除いた意味ではなく、到着・返信を未対応の時計へ混ぜない能力制限である。

UI変更なしのためUI／lint／buildは再実施していない。全suite・実サービス・実ネットワーク・ブラウザ・長時間RSS／性能・同一processcheckpoint復旧は検証外。追加依存・network有効化・実秘密処理・Git履歴変更・commit／push／deploy・automation再開なし。

独立の狭い再監査は関連52件と、FINISH座標の±1 ULP改変、正durationの完全消失、overflowのSTART前拒否、同一seed／Programの早期・10秒後でのkernel duration／counts／状態一致を確認した。timing担当の再点検も同52件とH／H+CNOT実計算を確認し、今回修正範囲の残存指摘なし。初回監査の103件も含め親437件へ加算しない。担当を終了し、書込み範囲の重複なし。更新7文書のローカルfile参照122件と、source／test 11ファイルの末尾空白検査に成功。heading anchor／外部URL到達は検査外。

## 前の実装区切り: 有限宣言列の物理確定と共通結果公開

`declared_execution.py`に、受理済み共通Programのarrival／START／FINISH／releaseを明示登録する`DeclaredQpuExecutor`を追加した。有限のtrusted seal上で、宣言待機状態・RUNNING・START、次に検証済み物理event batch・success・FINISHを共通state／scheduler／frontierの共有gateで確定する。正本外で数値計算・PSD検査・結果copyを行い、発行済み準備tokenと現在世代を再検査する。[分担と保証範囲](parallel/declared-physical-commit-handoff.md)を参照。

共通認証sessionの`run_declared_next`へ接続し、破棄はexecutorを先にfenceしてdrainする。数値中断は確定済み待機／STARTを保持してRUNNINGのまま明示retryへ渡し、架空の物理失敗へ変換しない。START／FINISHのowner更新途中に例外が起きた場合は、共有gate内で最新の未公開suffixだけをbounded journalから補償する。確定済み状態の巻戻し・世界再初期化・一般的な復旧の完成ではない。

共通`get_result`はsuccessだけでなくdeclared release到達を要求し、workloadの入力公開時刻も登録releaseと照合する。window登録前取消は制御応答releaseだけを登録し、QPU実行を捏造せず結果を返せる。非ゼロ初期device時刻はoffset対応が未実装なので組立前に拒否する。独立監査でこれら二点を再現し、修正・回帰試験を追加した。初期値0は現adapterの対応制限であり、全製品の不変条件ではない。

最終焦点回帰400件成功（49.501秒、今回の新規44件を含む）。既存fast 69件も成功（1.291秒）。修正前397件（54.732秒）を最終証拠へ代えない。新規試験は複数qubit・両主体の継承／service Reset、実session結果公開・時刻改変拒否、host数値遅延不変性、非ゼロ待機channelの解析値、queued取消／開始後取消拒否、破棄・数値中断、foreign／stale token、資源拒否、START／FINISH途中のowner故障補償と明示再試行を含む。

独立監査は関連95件、STARTの6・FINISHの4メモリ内故障注入を確認し、追加二指摘を親が修正した。狭い再監査と対応2試験で両指摘の解消・対象範囲の残存指摘なしを確認。担当store検証51件も成功したが、いずれも親400件と重複するため加算しない。実サービス／live ingress網羅性の監査ではない。

未完了: clock targetの自律同期、workload予約／自己結果依存とlive HTTPからの網羅的seal生成、job window／公開durationの自動生成・profile正式fieldと根拠、ZZ等の待機model選択、整合checkpoint／復旧、実Web所有・lifecycle・UI、旧Reset課題の共通Program／予算移行。有限の明示列を実行できることをlive体験完成としない。実サービス時間・係数・課題presetを推測して採用していない。UI変更なしのためUI／lint／buildは再実施していない。全suite・実サービス・実ネットワーク・ブラウザ・長時間RSS／性能は検証外。

最短の再実行:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_declared_execution tests.test_declared_transaction_stores tests.test_attack_workloads tests.test_attack_sessions tests.test_world_event_frontier
.\.venv\Scripts\python.exe -B scripts/run_tests.py fast
```

全焦点回帰は次節の35 moduleへ`tests.test_declared_execution tests.test_declared_transaction_stores`を追加する。追加依存・外部通信・実秘密処理・Git履歴変更・commit／push／deploy・automation再開なし。

最終文書検査: 更新8文書のローカルfile参照130件、変更code／test 10ファイルを含む末尾空白をPowerShellで検査し成功。tracked `git diff --check`も成功（既存CRLF警告あり）。heading anchor／外部URL到達は検査外。最終docstring同期後の新規2 module直接44件も成功（0.149秒）。実装・監査担当を終了した。次は予約／依存を含む有限sourceからの安全seal生成とclock／job window／公開時刻生成を接続し、その後live HTTPへ広げる。

## 前の実装区切り: 凍結workloadから認証済み共通受付への接続

D57/D58の後続。`workload_config.py`の有限JSON境界、`declared_ingress.py`の予約／結果barrier／再送履歴、`api/attack_workloads.py`の実session adapterを接続した。両主体は同じProgram送信・認証・認可・冪等性・host上限を使い、role／device／layout／service能力を時計開始前に検査する。凍結manifestだけから構成し、権限ゼロでも開始可能、実操作は拒否する。全予定の公開後にownerをclaimし、明示`start()`でのみ時計原点を記録する。[分担・操作・残る境界](parallel/declared-workload-handoff.md)を参照。

宣言arrivalをcallback前に予約し、宣言position／producer／event順でadmitする。退役とWAITING／barrier公開を原子的metadata更新にし、未解決予約の省略や退役済み順序への遡及挿入を拒否する。応答喪失後の再送は同じ実jobへ戻り、容量不足・認証失効・host例外を模擬DENIEDや物理失敗へ変えない。履歴は明示上限まで保持し、満杯時は新callback前に止める。compile待ち中も時計targetを読める。未接続のraw HTTP要求が混ざるのを防ぐため、宣言runnerの専用sessionをownerでfenceした。通常HTTP候補の文法は変えていない。

独立監査で、拒否結果からsession廃棄後も後続が生成される問題と、事前compileがsession lockを保持して廃棄を遅らせる問題を再現した。親が拒否経路のlive認証確認、最後のstate／book更新と廃棄の短い共通lock、lock外事前compileとbinding再検査を追加した。新規3試験は廃棄／認証失効後の拒否結果、成功／拒否結果のpublish直前廃棄、compile保留中のregistry廃棄完了を検査する。

修正後の親の最終焦点回帰356件成功（50.854秒、今回の新規51件を含む）。既存fast 69件も成功（1.314秒）。修正前353件（47.485秒）は最終証拠に代えない。実session試験15件は複数qubit・両主体のprepare／inherit、実queue順、権限ゼロ、取消、応答喪失、host容量、認証切れ、compile中clock進行、破棄、HTTP混入拒否を含む。QPU実行のsmokeはtrusted経路のuntimed実行であり、物理時刻／frontierの原子的確定を検証した意味ではない。HTTPはネットワークなしのASGI直接呼出し。独立監査の関連117件と修正後の3件は重複があるため親356件に加算しない。狭い再監査で二指摘の解消と今回範囲の残存指摘なしを確認した。

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_idle_evolution tests.test_host_delay_invariance tests.test_session_ownership tests.test_world_event_frontier tests.test_event_commit_boundary tests.test_scenario_environment tests.test_receipt_admission tests.test_program_admission tests.test_request_receipts tests.test_local_program_runtime tests.test_common_command_pipeline tests.test_attack_terminal tests.test_attack_program_request tests.test_job_application_service tests.test_shared_qpu_runtime tests.test_reset_calibration tests.test_reset_leakage_scenario tests.test_reset_leakage_preflight tests.test_reset_leakage_grader tests.test_reset_leakage_analysis tests.test_reset_leakage_acceptance tests.test_experimental_isolation_physics tests.test_scenario_service_conditions tests.test_scenario_credential_lifecycle tests.test_attack_sessions tests.test_attack_http_adapter tests.test_attack_scenario_policy tests.test_scenario_compiler tests.test_qpu_topology tests.test_program_initialization tests.test_declared_world_clock tests.test_victim_workload tests.test_declared_ingress tests.test_workload_config tests.test_attack_workloads
.\.venv\Scripts\python.exe -B scripts/run_tests.py fast
```

未完了: jobへの宣言作用記録、網羅的live seal、公開時刻生成・QPU state／frontierの原子的確定、宣言待機物理、自律物理進行、live HTTP対応、実Web所有・成立／破棄・同一process復旧、UI、旧Reset課題の共通Program／予算移行。`complete_result`はtrusted ownerが明示する公開時刻を使うが、現段階ではその物理的な正しさを証明できない。物理係数・service latency・grantを推測して補わない。全suite・実サービス・実ネットワーク・ブラウザ・長時間性能は今回の検証外。UI変更なしのためUI／lint／buildは再実施していない。追加依存・外部ネットワーク・実秘密処理・Git履歴変更・commit／push／deploy・automation再開なし。

文書検査: 更新9文書のローカルfile参照130件と、変更code／test 9ファイルを含む末尾空白をPowerShellで検査し成功。tracked `git diff --check`も成功（既存CRLF警告あり）。heading anchor／外部URL到達は検査外。実装・監査担当は終了し、書込み範囲の重複なし。次は宣言job作用・物理実行／公開／frontierの原子的確定境界へ進む。

## 前の実装区切り: 共通基盤v1の内部部品と準備／継承

D57/D58に沿う初回の並列実装。共通基盤全体・Reset課題・体験UIの完成ではない。[書込み分担・コード境界・次の接続](parallel/common-foundation-v1-handoff.md)を参照。

- 全体metadata／局所state: `qpu_topology.py`の有限・版付きinventory／gate／ZZ候補／control伝達／Readout共有と、`RetainedRegionBinding`を追加。store commitの物理ID付け替えと、session manifestとの不一致を拒否する。300qubit・527edgeのfixture上で非連番IDの複数qubit Programを共通terminalから実行した。全体の密度行列や動的領域選択を追加していない。
- 共通準備／継承: envelopeの`initialization=prepare|inherit`、profileの`supported_initializations`と`program.state.inherit`をcompiler／service／runtime／terminal／HTTP候補へ接続。省略時prepareの互換性、両主体の認可再検査、spectator相関／shot収縮、宣言readout、交代Reset、同一IDのmode競合を検査した。継承時のreadout問合せでResetを呼ばず、未対応modelは拒否する。
- 時間／被害者: `declared_world_clock.py`の明示ready／一度だけの開始・固定倍率・同着入力と、`victim_workload.py`の内部typed v1 schema／純粋進行器を追加。現在の操作は共通Program送信。有限の単発／事前群／自己counts分岐／反復、prepare／inherit、宣言古典／通信duration、未投入予約／producer付き因果barrierを扱う。結果依存の論理投入時刻や再送keyはhost callback順で変わらない。実dispatcherではなくtest doubleと明示world入力での検証である。

独立監査で、疎gate edgeを実行前に照合しない問題と、固定regionに旧isolated再割当を組み合わせられる問題を再現し、親が修正した。topology付きadapterはroutingなしの保守的制限としてunitaryの全operand pairに直接edgeを要求し、受付／実行で再検査する。cliqueがnative gateを保証する意味ではない。固定region＋isolated再割当は組立で拒否し、旧unbound隔離対照は残す。初回監査の111件は親の検証件数に加算しない。

参考計測: 300qubit／527edgeのmetadata再parseは100回×5反復の中央値0.609902 ms、JSON 9106 bytes。8qubit complex128行列の要素は1 MiB。数値kernel・実RSS・HTTP・全体latencyを含まず、製品上限や速度保証ではない。

最終焦点回帰305件成功（11.438秒、新規49件を含む）。修正前の302件（52.343秒）も成功したが、最終証拠は修正後305件とする。HTTPはASGI直接呼出しでネットワークを使わない。既存fast 69件も成功。独立再監査はtopology／継承／HTTP関連133件と両主体×四認可境界の8拒否ケースを確認し、二指摘の解消と対象範囲の残存指摘なし。別の独立時計／workload監査は焦点33件＋メモリ内4検査、独立3列の完了順36通り・再送・宣言時刻を検査し、対象範囲の指摘なし。これらは重複があるため親305件へ加算しない。

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_idle_evolution tests.test_host_delay_invariance tests.test_session_ownership tests.test_world_event_frontier tests.test_event_commit_boundary tests.test_scenario_environment tests.test_receipt_admission tests.test_program_admission tests.test_request_receipts tests.test_local_program_runtime tests.test_common_command_pipeline tests.test_attack_terminal tests.test_attack_program_request tests.test_job_application_service tests.test_shared_qpu_runtime tests.test_reset_calibration tests.test_reset_leakage_scenario tests.test_reset_leakage_preflight tests.test_reset_leakage_grader tests.test_reset_leakage_analysis tests.test_reset_leakage_acceptance tests.test_experimental_isolation_physics tests.test_scenario_service_conditions tests.test_scenario_credential_lifecycle tests.test_attack_sessions tests.test_attack_http_adapter tests.test_attack_scenario_policy tests.test_scenario_compiler tests.test_qpu_topology tests.test_program_initialization tests.test_declared_world_clock tests.test_victim_workload
.\.venv\Scripts\python.exe -B scripts/run_tests.py fast
```

UIの変更なしのためUI／lint／buildは今回再実施していない。全suite、実サービス・実ネットワーク・ブラウザ、live自律進行、公開認証／同一process復旧の受入、長時間性能は検証外。

最終文書検査: 変更11文書のローカルファイル参照133件と、対象code／test 18ファイルを含む末尾空白をPowerShellで検査して成功。tracked `git diff --check`も成功（既存CRLF警告あり）。heading anchor／外部URL到達は検査外。監査担当を終了し、書込みの重複と自動継続の再開は行っていない。

未完了: 成立ownerへの時計起動、認可済み宣言到達adapter、live受付＋workload予約／依存と全体同着順序、網羅的watermark・QPU原子的commit・宣言結果公開／待機、実Web所有者・成立／破棄・整合復旧、UI本接続、旧Reset課題の共通Program／予算移行。物理係数や実サービス時間を推測して接続せず、既存継承grantを実サービスが提供する能力と主張しない。追加依存・外部ネットワーク・実秘密処理・commit／push／deploy・automation再開なし。

## 前の契約区切り: 五契約の採用と局所化防止（D57/D58）

ユーザーがP1〜P5を承認したため、[共通基盤v1契約](common-foundation-v1-contract-proposal.md)を暫定契約へ昇格し、決定・構造・時間境界・未決定・台帳・資格情報・旧永続化提案を同期した。公開WebはCloudflare等の実際の配備基盤であり、模擬Webではないことを確認した。1倍は初期時計値のみで、クラウドlatencyや物理係数の採用ではない。再起動復元は後続へ分け、初回はページ再読み込み／同一process内の整合復旧を必須とする。

D58で全体の百量子ビット級metadata、一回路編集上限8、局所物理領域幅を分離し、共通型・workload・割当・状態・APIを1qubitのReset presetへ固定しない条件と受入例を記録した。点検では既存Program／密度行列／layoutは複数qubitを扱う一方、旧Reset binding／隔離placementは1qubit限定adapterに残ることを確認した。トポロジー全体の動的割当・疎な影響接続は未実装で、簡単なfixtureを完成した全体モデルと扱わない。

今回のコード変更はなし。新しく実行した`.venv\Scripts\python.exe -B -m unittest tests.test_local_program_runtime tests.test_attack_program_request`は19件成功（0.562秒）。8qubit回路、9qubit物理領域内の8qubit回路、非連番ID／古典配線、Bell相関、spectator、部分測定、tenant交代、入力・layout・資源拒否を含む既存焦点試験。百量子ビット級metadata／疎結合接続、live時計、新継承入力、実Web認証・再接続・復旧の受入ではない。UI・lint/build・全suiteは再実施していない。

文書検査: PowerShellで変更13文書の末尾空白、ローカル文書ファイル参照154件、D01〜D58連続性、P1〜P5採用表示の一意性、D58設計条件の存在を確認し成功。tracked diffの`git diff --check`も成功（既存CRLF警告あり）。一般的なheading anchor／外部URL到達は検査外。独立読み取り監査で古い時計条件の未決定表示と、server正本採用後も全責務のブラウザ配置を未決定とする記述を指摘し、決定・構造・時間境界・レイヤー整理・進捗表を修正した。一般境界への1qubit制約の漏れは対象内で指摘なし。親の直接確認と、D56行・構造の配置／時間記述・時間境界の指定4箇所に限定した独立再確認で、両指摘の修正を確認した。全設計の再監査ではない。

次は採用契約に沿って時間coordinator／有限workload、register初期化／継承、実HTTP所有境界を責務別に具体化・接続し、全体トポロジーと局所状態のadapter境界を統合監査する。具体的な物理条件・未対応機構を補って完成扱いにしない。

## 前の設計区切り: 共通基盤v1の五契約案

[実装前契約案](common-foundation-v1-contract-proposal.md)へ、共通時間・被害者workload・Program初期化／サービスReset・公開Web所有／計算境界・保存／復旧の五提案を記録した。合意、推奨、選択の代償、検証可能な受入を分離。1倍でも実サービスlatencyは再現されず、物理列のhost遅延不変性でも人間の結果待ちの不利は消えないことを明記した。旧再起動復元は未採用提案として保全し、初回v1の保証範囲の判断へ集約した。

今回は文書のみ。推奨値の採用、D57追加、schema／runtime／HTTP／UI実装、追加依存・配備・automation再開は行っていない。次は五提案の採否を確認し、採用範囲の契約同期後に責務別実装と独立監査を進める。下記Python256件・UI28件等は前区切りの証拠で、新規試験とは扱わない。

検査: PowerShellで変更5文書の末尾空白、ローカル文書ファイル参照100件、P1〜P5の一意性、Q参照先の存在、D01〜D56の保全、既存永続性節の存在を確認して成功。tracked diffの`git diff --check`も成功（既存CRLF警告あり、未追跡文書は上記の直接検査で補完）。一般的なheading anchor／外部URLの到達検査はしていない。独立読み取り監査はD51〜D56・構造／時間／共通client／旧契約案との整合を点検し、対象範囲の指摘なし。コード・UI変更なしのため実行試験は再実施していない。

## 前の実装区切り: 共通契約の課題従属を除去（D56）

時間の具体化を最初の課題profile選択の後へ置いていた文書を修正し、共通契約→service／物理profile・被害者workload→課題presetの所有境界を[構造正本](architecture.md#共通契約と課題設定の依存方向2026-10-04)へ記録した。被害者はいつ・何に・どの共通操作を行うかを指定する構造とし、課題例は共通契約の反例検査に使う。時計倍率、初期化条件、workload正式schemaを今回決定した意味ではない。Web公開も配備対象として明記し、実利用者間共有と模擬世界内共有を区別した。

実装修正: `ScenarioPolicy.observation_budget`を任意にし、paired training／victim queryを使わない構成のためのダミー予算を不要にした。manifestは`null`を明示し、従来の予算ありReset manifestのhashは維持。Reset RuntimePlan／ledgerは明示予算なしを拒否し、無制限化しない。共通認可／graded compileでactor grantの空集合を許可し、全操作拒否の意味を維持した。欠落・不正値・重複・固定／根拠不足と空のservice catalogは引き続き拒否。UIの公開設定groupへ`relation`／`experiment`を追加し、既存の宣言field検査・課題固定・成立後固定を使う。秘密や新しい仮定fieldを追加していない。

検証: 新規Python 10件を含む直接焦点66件成功（0.056秒）。最終焦点回帰256件成功（51.409秒）；共通terminal／sessionのfixtureからダミー予算を除き、実送信・結果・認可・compiler shots上限・host容量・receipt・取消・失敗・close・遅延不変性・既存Reset対照／graderを再検査した。追加fast 69件成功（1.336秒）。UI 28検査、lint、build成功。buildの約690 kB chunk警告は残る。独立読み取り監査は予算修正を確認し、権限ゼロ／UI群の欠落を追加指摘、親側で修正した。最終再監査は三変更のコードと境界を再確認して今回範囲の残存指摘なし；独立実行はPython66件、予算なしfixture変更後の関連36件、UI28件が成功（重複を含み、親256件の独立再実行ではない）。HTTPはネットワークなしのASGI直接呼出し。変更23ファイルの末尾空白、文書のローカルファイル参照96件、D01〜D56連続性、tracked diffの空白検査も成功（heading anchor／外部URL検査は含まない）。

再実行は前区切り246件と同じ28 module（新規10件は既存moduleへ追加）。UIは`frontend/`で`node src/validation/attackWorkspace.test.mjs`、`npm run lint`、`npm run build`。コード変更範囲はpolicy／manifest／Reset予算境界／認可と公開設定groupに限定した。

未完了: live時計・状態transaction、共通workload実行器、Reset legacy operationの共通Program移行、他種類の予算channel、HTTP本mount・UI接続、管理者bootstrap、Web配備。現FIFO adapterのReset用binding／同期反復は限定内部実装として残し、全製品の必須構造とはしない。未合意の権限・時間・物理条件を補って接続しない。全suite、実ネットワーク、ブラウザ操作、実サービス、長時間性能・復旧は今回の検証外。

## 前の設計同期: 一般モデルの特殊化可能性（D55）

[一般サービスの特殊化条件](service-specialization-contract.md)へ、D07/D13/D20/D37/D40/D48/D50との要件対応、共通方針境界、比較範囲・対応写像・profile構成・確認義務・不明／非対応の区別を記録した。IBM active sessionの専有、batch順序非保証、Braket優先queue／queue観測を一次資料で確認し、仕様片の構成案と反例を示す。設定が保存できることを、振る舞いの表現や実行対応の証明にしない。

今回は文書のみ。特殊化の具体schema、方針意味論の完全定義、形式的証明・checker、予約／batch／優先runtime、個別provider実装は未追加。下記246件／fast 69件は前の実装区切りの証拠。次は限定したprofileの意味論と反例検査を具体化する。全サービスの完全再現を初回の前提にせず、権限・経路・順序・状態寿命・観測・資源上限に具体的な反例がある場合だけ再検討する案を併記した。

文書検査: 更新6文書の末尾空白、ローカルファイル参照95件、D01〜D55の決定ID連続性をPowerShellで検査し成功（heading anchor／外部URLのリンク検査は含まない）。コード未変更のためPython・UI・lint/buildは再実行していない。

## 前の実装区切り: 明示待機区間の内部物理更新器

`core/experimental_isolation_physics/idle_evolution.py`に、独立局所・一定係数・2準位の`IndependentIdleModel`と`IdleQubitSpec`を追加した。T1/Tφ、平衡励起率、局所幅上限は明示入力で、既定の物理係数を課題へ追加しない。共同密度行列のtensor blockを一括更新し、spectator相関を周辺状態の積へ捨てず、host時計・queue・RNG・commitを所有しない。0待機／無効時も入力を変更せず独立arrayを返す。shape・finite・Hermitian・traceを検査し、PSDは呼出し側で検証済みという内部契約として、kernel中の固有値計算を省く。

新規9件と既存物理11件の直接焦点20件成功（0.117秒）。前区切りの237件に新規9件を加えた回帰246件成功（45.290秒）。独立に埋め込んだ既存Krausチャネルとの複素相関状態比較（1〜3 qubit）、一括／分割区間、局所channel順、T1人口差・Tφ coherence、長時間平衡、Bell spectator、微小／極端な比、入力保全、境界拒否、host時計を読まないことを検査した。独立担当の監査ではなく、同じ作業内での異なる計算方法による検証である。

追加回帰: `.venv\Scripts\python.exe -B scripts/run_tests.py fast`の既存69件も成功（1.344秒）。変更10ファイルの末尾空白とローカル文書ファイル参照52件をPowerShellで検査して成功（anchor検査は含まない）。UI変更なしのためUI／lint／buildは再実行していない。全suite・実サービス・ブラウザ・復旧は今回の検証外。

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_idle_evolution tests.test_experimental_isolation_physics
.\.venv\Scripts\python.exe -B scripts/benchmark_idle_evolution.py
```

246件の再実行は、下記の前区切り237件commandへ`tests.test_idle_evolution`を追加する。

参考計測: 各caseを別processで実行し、3回warmup後20回×5反復。宣言dt 0.1 us／1e8 usに対し、1 qubitの中央値0.01338／0.01447 ms、4 qubitは0.07940／0.08207 ms、8 qubitは4.96513／4.02742 ms。8 qubitの入力行列は1 MiB、process peak RSSは40.23／40.42 MiB（入力作成後のprocess peakは38.13／38.08 MiB）。RSSはPython/NumPy・入力・一時arrayを含むprocessの最大working setで、kernel単体の厳密な追加割当量ではない。係数はsyntheticな計測入力。推定時間O(n·4^n)・空間O(4^n)で、dtやframe数を増やして更新回数を増やさない。長時間側の方が速い計測を一般則とはしない。

未完了: scenarioへのモデル選択・係数根拠、live時刻からの待機区間生成、ZZ等を含む対応モデル選択、QPU transaction接続、公開PSD検査、状態保存／serialization／HTTP/UI込みのlatency・RSSとhost遅延注入。既存Resetの挙動・device timeを変更していない。公開Gate-aware/Pulseでは選択できず、内部capabilityも`runtime_connected=False`とする。時計倍率や被害者周期を採用した意味ではない。次はサービスprofileの投入／専有／物理開始／結果公開条件と、待機区間の入力契約を具体化してから統合する。

## 前の設計同期: 実サービス比較と待機物理の採用方向

IBMの受付／古典処理／QPU実行、batchの順序非保証、active sessionの専有、AWSの通常／優先queue、結果依存workloadを公開一次資料で確認し、[対応・不一致・検証計画](service-time-and-workload-comparison.md)へ記録した。実時間1倍・固定周期の被害者は未採用。待機区間の物理作用を計算量に注意して適用する方向だけD54として同期した。現runtimeの累積durationを実経過へ読み替えず、host待ちを追加緩和へ転用しない。

今回は文書変更のみ。実サービスへの送信／時間計測、待機更新器、live時計・被害者進行の実装／検証は行っていない。下記237件は前の実装区切りの証拠として維持する。次はprofileの投入／専有／物理開始／結果公開条件を確認し、待機モデルと時間対応を具体化する。局所チャネルの一括更新は候補であり、T1/Tφや平衡励起率を推測して接続しない。

文書検査: 更新6文書の末尾空白、ローカルファイル参照84件、D01〜D54の決定ID連続性をPowerShellで検査して成功。anchorと外部URLのリンク検査は含まない。コード／UI変更なしのためPython・UI・lint/buildは再実行していない。

## 前の実装区切り: D53内部時刻管理と並列検証→修正

実装: `DeclaredWorldFrontier`でtarget・到着網羅済みwatermark・未解決ticket・confirmedをboundedに同期管理する。pendingが空でもcallerの明示watermarkなしに確定しない。targetは確定待ちと独立に進められ、late／foreign／copied ticket、容量超過、巻戻しを拒否する。上限は明示必須。ホスト時刻の読み出し、物理状態・queue・予算・RNGの所有、live受付への接続は追加していない。

検証→修正: 独立監査で同じruntimeの並行session生成が二重所有・上限迂回を許す競合と、失敗した生成の片側binding残留を再現。service／runtimeのclaimを同期し、session局所初期化→service確保→authority bindingの順へ変更、binding失敗時はその未公開生成だけのclaimを解除した。authorityの発行／照合／bind／closeも同期する。別の遅延検証で結果投影中のcontrol lock保持を確認し、読み取りはlock外で処理、返却前の生存／認証／receipt／service再検査を残した。取消はcloseと同じ短いlockで順序を確定する。

親側の最終焦点回帰237件成功（44.990秒）。直接焦点77件も再実行成功（1.108秒）。新規は時刻管理5件、所有権4件、独立遅延比較3件。2主体のcompile待ち、結果投影待ち中の照会／取消／workspace操作／受理／次の実行、応答待ち取消＋同一要求再送、close中の失効／遅い結果の拒否を検査した。同じmanifest・内部乱数・入力順序でイベント順・counts・最終密度行列・物理durationが基準と一致し、公開viewへprivate ingress情報を追加していない。独立検証側も関連49件成功し、旧投影lock動作の注入が有界TimeoutErrorとなることを確認した。独立再監査は焦点43件成功、共有形態3種の競合36回、代入直前の競合2件、rollback／再利用3条件、投影遅延3条件を確認し、点検範囲の残る具体的欠陥・lock inversionの指摘なし。新時刻部品の初回独立監査は756通りの時刻列と16回の同時confirmも検査した。

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_host_delay_invariance tests.test_session_ownership tests.test_world_event_frontier tests.test_event_commit_boundary tests.test_scenario_environment tests.test_receipt_admission tests.test_program_admission tests.test_request_receipts tests.test_local_program_runtime tests.test_common_command_pipeline tests.test_attack_terminal tests.test_attack_program_request tests.test_job_application_service tests.test_shared_qpu_runtime tests.test_reset_calibration tests.test_reset_leakage_scenario tests.test_reset_leakage_preflight tests.test_reset_leakage_grader tests.test_reset_leakage_analysis tests.test_reset_leakage_acceptance tests.test_experimental_isolation_physics tests.test_scenario_service_conditions tests.test_scenario_credential_lifecycle tests.test_attack_sessions tests.test_attack_http_adapter tests.test_attack_scenario_policy tests.test_scenario_compiler
```

参考計測: 時刻管理のblocked confirmのみ、pending 1／32／256・500回×5反復の中央値1.867／5.731／21.237 us。容量の採用値やAPI／physics／RSS／最大遅延保証ではない。文書・末尾空白の最終検査は下記引継ぎへ記録する。UI未変更のためUI／lint／buildは今回再実行していない。全suite、実ネットワーク、ブラウザ描画遅延、長時間負荷、復旧は未検証。

残件と次の判断: 時刻metadataの内部同期をQPU transactionの原子的確定と混同しない。liveの開始原点／対応倍率・通信境界・結果公開時刻、被害者周期／待機物理条件、許容遅れ／未処理量の具体化、網羅的受付からのwatermark発行とscheduler／物理commit接続が残る。HTTP/UI本接続の管理者bootstrap、Reset共通Program移行の予算／初期化条件も未解消。倍率や待機物理を推測して接続しない。この区切りは手動の並列作業であり、PAUSEDの睡眠中automationは再開していない。[書込分担と引継ぎ](parallel/time-frontier-handoff.md)を参照。

## 前区切り: manifest凍結と純粋な時刻列検査

`ScenarioEnvironment`のmanifest確定と設定編集を同期し、並行／reentrantな成立、成立処理中の編集、authoring session差替えによる不整合を防いだ。検査失敗時は未成立のまま再編集可能。`event_commit_boundary.validate_event_commit()`は明示world時刻列だけを検査し、先行未解決要求を越す確定、巻戻し・重複・late挿入・domain混在を拒否する。時計・queue・物理状態・乱数・予算の変更やlive runtime接続は行っていない。

新規各6件を含む最終焦点回帰225件成功（44.919秒）、直接焦点101件も再実行成功（0.558秒）。独立読み取り専用の時刻境界監査は関連24件と独立3件成功、小さい時刻／順序294組とforeign domain／host receipt混入を確認し、再現可能な欠陥の指摘なし。検査器だけの参考計測はpending 1／32／256、1000回×5反復の中央値0.582／4.963／38.560 us。CPU数値計算・HTTP・RSS・最大latencyは含まず、性能保証や採用上限値ではない。全suite・ブラウザ・実ネットワーク・復旧は未検証。再実行は前区切りの22 moduleへ`tests.test_scenario_environment tests.test_event_commit_boundary`を追加する。今回の直接焦点:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_event_commit_boundary tests.test_scenario_environment tests.test_receipt_admission tests.test_program_admission tests.test_request_receipts tests.test_attack_sessions tests.test_attack_http_adapter tests.test_scenario_service_conditions tests.test_scenario_compiler
```

この区切り後の合意反映: D53として「実経過に追従するworld目標と、計算済みの確定位置を分け、影響する先行要求の解決まで確定だけを保留し追い付く」を採用した。ホスト性能による架空の現象を防ぐことが条件で、遅延注入による受入項目を[時間境界 §4.3](runtime-boundaries-and-resolutions.md#43-時間接続の前提検査と判断待ち2026-10-04)へ記録した。合意反映時は文書同期のみで、上記225件はその前の実装区切りの証拠。live接続や条件達成の検証済みとはしない。残る判断は、開始原点・対応倍率・通信範囲・結果公開時刻・追付き遅れ／未処理量の許容値・被害者周期／待機物理条件。世界停止・host時刻のQPU時刻への転用・追加Resetを既定にしない。

文書同期の検査: PowerShellで更新6文書の末尾空白、ローカルMarkdownファイル参照49件、D01〜D53の決定ID重複を検査し成功。コード変更なしのためPython／UI試験は今回再実行していない。

前の実装区切りで依存順の独立作業を閉じた。時間実接続は上記具体条件、HTTP/UI本接続は管理者認証／bootstrap、Reset共通Program移行は予算／初期化条件を待つため、未合意を補って進めない。睡眠中heartbeat `automation`は判断待ちとして`PAUSED`へ更新済みで、アプリの結果と保存状態で確認した（prompt／schedule／対象threadは維持）。今回の方式採用を自動継続の再開指示へ読み替えず、再開操作は行っていない。次はcoordinatorの網羅的な未解決snapshot＋原子的check/commitを、合意済み方式と残る具体条件を分けて設計する。

## 前区切り: 受付順の受理とcompile分離

receipt到着順を同じsession窓口の有効job受理へ接続し、回路変換／compileをcontrol lock外へ移した。`request_async()`は有限dispatch gateをworker投入前に非同期で待ち、先行要求がworker待ちでも後続の同期待ちでpoolを埋めない。照会・queued取消はcompileを待たない。serviceは副作用のないprepareと、生存・認証・容量・権限・manifest・server割当を再検査するadmitに分離した。最初の受付receiptをjob内部へ保持し、診断historyのeviction／再送で上書きしない。closeは失効・待機起床・準備と物理実行のdrainを行い、設定凍結・確定shotを維持する。

前区切りの最終焦点回帰は213件成功（47.588秒）、直接焦点59件も成功（0.822秒）。新規の二段階受理11件と受付順／競合7件を含む。内部同着時刻42・private entropy・同じ入力の比較で、worker前の遅れを加えてもjob順・counts・最終状態・装置durationが一致した。control lock待ちより前のreceipt予約、1 worker枠で後続要求がworkerを占有しないこと、compile中のstatus／端末照会／queued取消、失敗・未dispatch取消後の進行、破棄中の起床と受理拒否、pending／成功受付上限、再送、foreign receipt／終了後client拒否を検査した。独立読み取り専用監査では対象8ファイルに再現可能な欠陥の指摘なし。ingress同期分離後の最終追加監査も関連91件（1.227秒）とメモリ内競合試験2件成功、受付／破棄の競合と1枠poolでの取消・FIFO維持を確認した。変更13ファイルの末尾空白と更新5文書のローカルファイル参照も検査済み。全suite・実ネットワーク・ブラウザ・長時間負荷・複数process・復旧の証拠ではない。

再実行コマンドは次節の既存20 moduleに`tests.test_receipt_admission tests.test_program_admission`を追加する。最短の直接焦点:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_receipt_admission tests.test_program_admission tests.test_request_receipts tests.test_attack_sessions tests.test_attack_http_adapter
```

未完了: 仮想時計の対応・commit horizon・被害者自律進行・結果のモデル上の公開時刻、管理者bootstrap／HTTP・UI本接続、Resetの共通Program移行／予算、復旧。host receiptはQPU時間でも攻撃者情報でもない。準備は現adapterで受付順に直列化し、並列compileの投機はしない。内部runnerも同じ非同期窓口を利用できるが、旧Reset orchestratorを接続済みとは扱わない。次は時間契約の残る具体化を整理し、決定済みの範囲だけで被害者進行・イベント確定境界を接続する。未合意のmapping／管理者権限／物理初期化条件を補って完了扱いにしない。

## 前区切り: 内部receipt記録

boundedな内部`RequestReceiptLedger`をsession／HTTP候補へ接続した。認証・envelope検査後、threadpool待ちと回路変換／compileより前にhost時刻と到着順を記録し、受領とjob受理を分離する。pending／完了履歴の上限は明示必須で製品既定値を選んでいない。receipt再claim、応答待ち取消と走行中worker、再送と同一job、closeの競合を検査した。全要求処理完了は物理成功を意味せず、内部時刻・連番・資格情報を公開viewへ出さない。新記録器の初期化は共有所有権のclaimより前に行う。

前区切りの焦点回帰は195件成功（44.176秒）、記録器・session・HTTPの直接焦点41件も再実行で成功（0.432秒）。独立した読み取り専用監査では対象6ファイルの重大な再現可能欠陥の指摘なし。全suite・実ネットワーク・ブラウザ・復旧機能の検証ではない。記録器だけをpending上限1／history上限64、reserve→claim→finishの10000回×5反復で参考計測し、反復中央値は2.934 us/要求だった（history 64件維持）。scope外のcompile・同期競合・HTTP・RSS・最大latencyを含まず、製品の性能保証や採用上限値にはしない。前区切りの回帰コマンド:

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_request_receipts tests.test_local_program_runtime tests.test_common_command_pipeline tests.test_attack_terminal tests.test_attack_program_request tests.test_job_application_service tests.test_shared_qpu_runtime tests.test_reset_calibration tests.test_reset_leakage_scenario tests.test_reset_leakage_preflight tests.test_reset_leakage_grader tests.test_reset_leakage_analysis tests.test_reset_leakage_acceptance tests.test_experimental_isolation_physics tests.test_scenario_service_conditions tests.test_scenario_credential_lifecycle tests.test_attack_sessions tests.test_attack_http_adapter tests.test_attack_scenario_policy tests.test_scenario_compiler
```

以下の同日記録はこの区切り以前の確認として区別する。
同日の追加判断D51/D52で、基盤異常時の競合・重複実行を防ぐ復旧再開、メタ層の環境管理者、サービス層の参加者資格情報発行、両modeでの開始前限定の管理者設定を合意した。[復旧契約](runtime-boundaries-and-resolutions.md#42-フォールバックとしての再開2026-10-04合意)と[資格情報境界](credential-delivery-boundary.md#環境管理者とサービス発行の境界2026-10-04合意)へ反映。今回の変更は設計文書の同期であり、checkpoint復旧・管理者認証・発行APIの実装完了ではない。復旧で設定を解凍せず、`close()`を再開として流用しない。確認は`.venv\Scripts\python.exe -B -m unittest tests.test_scenario_credential_lifecycle tests.test_attack_sessions`の既存11件成功（0.023秒）と、更新8文書のローカルファイル参照・決定表連続性の検査。新しい復旧機能を試験した結果ではない。
同日の後続指示で、ホスト配置の都合をサービス／物理条件へ混入させない原則を[時間境界の正本](runtime-boundaries-and-resolutions.md)へ記録した。統合チェックの再現条件に対し、manifest／認可／compiler／executorの一致検査、service・runtime・資格情報authorityの単一session所有、参加者操作と数値計算のlock分離、FIFOの同期、UIの新規／手動再送の識別とterminal transport IDを実装した。数値計算をEvent barrierで停止しても受付・status・queued取消が成立し、遅延なしと同じcounts・装置時間・最終状態になること、closeで完了shotを巻き戻さないことを検証した。焦点回帰はPython 173件成功（43.869秒）、UI 26検査・lint・build成功。buildの約690 kB chunk警告は残る。全suite・ブラウザ・実機・最大RSSの検証ではない。

前区切りからの後続計画にあったreceipt順の受理・compile分離・jobへの受付metadata保持・内部client用async窓口は冒頭の区切りで実装した。world-clock mappingとcommit horizon、被害者runnerの実接続、管理者bootstrap・HTTP/UI本接続は残る。被害者自律進行、同時物理干渉、全ホスト側情報の遮断、latency保証、checkpoint復旧は未実現。実装境界は[共通コマンドclient](common-command-client.md)、分担根拠は[時間分離の独立点検](parallel/host-time-handoff.md)と[UI再送の引継ぎ](parallel/ui-retry-handoff.md)を参照。以下は日付付きの履歴であり最新状態とは区別する。

2026-10-04にサービス／UI部隊の成果物を監査し、利用者の修正指示で応答喪失時の状態不明扱い、攻撃側1〜8 qubit入力と既存Gate-awareの2〜8維持、HTTP回路名の256 UTF-8 bytes共通境界、公開job IDの不透明化、非対応隔離領域の生成時拒否、可変行列の状態所有迂回を修正した。HTTP adapterは未mount、UIも未接続であり、管理者bootstrap・Resetの共通program移行・予算統合・復旧は残る。変更箇所と検証は[監査修正記録](parallel/audit-repairs-2026-10-04.md)を参照。以下の日付付き記述は当時の検証履歴として残す。
同日の後続指示で、共通基盤の1量子ビット制約を撤去した。`LocalCircuitCompiler`／operand付きIR、server-owned `PhysicalQubitLayout`、共同密度行列上の局所演算・Reset・終端共同測定、古典幅に対応するbit列countsへ一般化した。回路入力は最大8、dense物理領域は別の上限（既定8、暫定adapter対応最大10）で管理する。8入力＋9物理qubit、Bell相関、部分測定、spectator保持を直接検証し、既存Reset課題の1 qubit設定と内部経路は維持した。最終焦点検証は115件成功（49.790秒）。詳細・検証証跡は[共通コマンドclient](common-command-client.md)を参照。
2026-10-03に、[共通コマンドclient](common-command-client.md)を実装した。仮想資格情報で認証する同じ利用者接続から`device list/show`、`program submit`、`job status/result/cancel`を扱い、actor別のbounded回路namespace、明示profileの1量子ビットcompiler、主体非依存operation、FIFO受付、shotごとの不完全Reset・準備gate・測定・状態確定、counts投影まで接続した。HTTP／UI・任意コードは未実装であり、任意コードはSandbox設計後に追加する。既存Reset課題の観測予算・被害者反復をまだ移行していないため、その課題では新compilerを有効化しない。異常Reset状態・途中失敗・Readout・FTQC拒否を含む関連55件が38.257秒で成功した。具体的な検証コマンドは共通コマンド文書を参照。
2026-10-03に、提出可能な課題プリセットの共通preflightを追加した。`ScenarioPreset`から`ScenarioSession`・resolved manifestへ仮定根拠を引き継ぎ、compile時に権限fieldの固定／根拠、actor grantとservice capability、提出操作の整合を検査する。Reset固有の物理条件は共通検査へ含めず、従来の専用RuntimePlan／constraint境界に残す。scenario compiler／policy、credential lifecycle、job service、Reset scenario／grader／analysis／preflightの焦点テスト83件が成功。
同日に、Reset v0の生成順序を設定成立・専用RuntimePlan検証→QPU状態・実行器生成に変更した。不正な提出課題プリセットは`ScenarioEnvironment.establish()`で未成立のまま拒否され、検査失敗時にはResetのQPU状態／実行器／orchestratorを生成しない。新しい境界テストを含む`.venv\Scripts\python.exe scripts/run_tests.py full-audit`は963件成功（2026-10-03）。
2026-10-02に`relation.attack_path`の内部v0宣言とservice側の対応機構照合を追加し、ResetのRuntimePlan生成時に入口・共有資源・機構・保護資産・観測先を検査するようにした。続いて`relation.resource_binding`をservice inventory・実行状態のQPU／量子ビットIDと照合し、隔離対照では別の量子ビットIDへfresh zeroの局所状態枠を割り当て直すeventを追加した。service能力・actor grants・`ScenarioPolicy.allowed_operations`の積集合による`program.submit`受付／実行時認可に加え、内部`JobApplicationService`でstatus/result/cancelのprincipal別grant、tenant scope、結果投影を接続した。Reset orchestratorの被害者runnerと攻撃者処理も同じjobサービスを使う。外部認証context、HTTP API、共通program schemaは未実装。複数QPU・サービス層経路、汎用経路registryも未実装。
今回のjob認可変更について、`tests.test_job_application_service`ほかReset runtime・scenario・preflight・grader・analysis・compilerの焦点64件が成功。policy三層のそれぞれによる拒否を追加検査した8件も成功した。これはin-process境界の証拠であり、外部API認証の証拠ではない。
同日に監査第一団・第三団の報告を現状と照合した。第一団の共有QPU前提に関する未決定の記載はD48で解消済み。第三団の指摘を受け、全体索引の9月7日状態を履歴snapshotと明示し、攻撃シミュレーションの内部実装状況へ更新した。次に内部`JobCommand.target`を`resource_binding`へ照合する受付・実行境界を追加した。これは送信先検査であり、tenant認可や公開ジョブschemaではない。
閉じた局所実行領域、時間境界、観測経路の方針に加え、攻撃用物理プリミティブの内部v0を実装・検証した。
2026-09-23に状態所有・job契約の具体案を作り、8契約・9検討領域の[進捗台帳](contract-and-domain-progress.md)を追加した。提案段階であり採用・実装済みではない。
2026-09-25に初期scope、再起動後の状態保持、設定式取消、queue可視性、部分的物理影響の保持を製品判断として合意した。C01/C02全体はschema・完全な状態遷移・受入試験が残るため、まだ`提案中`とする。
同日に、三設定（Attacker/Victim/QPU Service）を`ScenarioPolicy`で統制する構造と、従来の五つの意味区分との対応を合意した。具体的なfield・API schemaは未決定である。
同日に、最初の論文再現をMiらCCS 2022の同一量子ビット・順次再利用によるReset越し漏洩へ固定し、[暫定契約](reset-leakage-paper-reproduction-contract.md)と[欠陥監査](reset-leakage-paper-reproduction-audit.md)を追加した。実装開始判定は条件付きGOである。
2026-09-26に共通`ResetModel`境界、registry、既存CPTP primitiveの挙動近似adapter、simulator-derived profile、観測予算schema、相互情報量・Bayes最適精度の校正runnerを内部実装した。これはprofile生成基盤であり、共有QPUサービスや課題パイプラインの完成ではない。
同日に`ObservationBudget`をReset固有層から共通scenario層へ移し、同一`ScenarioTemplate`を自由・課題modeで共有する`ScenarioPreset`・`ScenarioPolicy`・`ScenarioSession`を実装した。課題modeだけがfield固定・秘匿・提出判定を追加し、自由modeは設定の編集用copyを作る。このpolicy基盤は、後続の共通runtimeへ接続する前提で実装した。
続いて、revision付きin-memory `QpuStateStore`、`on_demand_fifo_v1` scheduler、tenant単位の冪等受付、queued取消、service-owned Reset event、被害者準備・攻撃者一測定sampleを接続した。永続snapshot、認証API、複数QPU、実行中取消は未実装。
さらに、session単位の`ObservationBudgetLedger`とReset漏洩scenario orchestratorを追加した。queryごとに予算を事前確保し、各shotで被害者準備→service Reset→攻撃者一測定を反復する。学習queryだけ既知labelを返し、未知被害者queryのbit・seed・部分sampleは攻撃者結果へ含めない。
同日に、明示的な`HiddenEvaluationPolicy`、二値推定器境界、周波数閾値推定器、集計限定`ResetLeakageHiddenGrader`を追加した。hidden caseは0/1を両方含めて順序を秘匿し、個別counts・正解bit・seedを結果へ含めない。評価用測定seedは通常queryの乱数列から分離した。現在の推定器境界は信頼済みin-process object専用であり、利用者コードを安全に実行するsandboxではない。
続いて、ラベル付き学習と未知被害者観測を`AttackerConfig.reset_leakage_access`の明示fieldへ移し、orchestratorの実行前権限検査へ接続した。自由modeはfieldを切替可能、課題modeはpresetでfield全体を固定する。無効な操作はjob作成・物理event・予算消費より前に拒否する。
仮定fieldの増殖を避けるため、主体別設定、主体間の`RelationConfig`、`ExperimentConfig`、根拠記録、profile＋override、resolved manifestを定義する[取扱契約](assumption-handling-contract.md)を追加した。`ScenarioTemplate`へRelation／Experimentを追加し、正式な永続化schemaとprofile registryは未実装である。
設定管理は`AuthoringScenario -> ResolvedScenarioManifest -> RuntimePlan + RuntimeState`としてcompile時と実行時を分離した。`ScenarioCompiler`はcontent hash、AssumptionRecord、free-mode override、lock path、constraint hookを扱い、Reset漏洩orchestratorは小さな`ResetLeakageRuntimePlan`だけを保持する。各job・物理eventはmanifest IDを参照する。共通plan境界、永続store、compile cache、計算量見積りは未実装。
NISQを製品全体の不変条件にせず、`ExecutionRegime`と`QpuServiceConfig.execution_regime`を追加した。共通compilerは`fault_tolerant_logical` manifestも生成できる一方、現行Reset漏洩RuntimePlanは`nisq_physical`以外を開始前に拒否する。FTQC runtime、QEC cycle、syndrome、decoderは未実装である。
続いて、`ResetModelMode`で状態遷移＋Readoutと有効観測だけのmodelを区別し、現行stateful runtimeへ後者を混入できないようにした。物理parameterから`model_configuration_id`を生成し、manifest、RuntimePlan、runtime、校正profileで照合するため、同じmodel名のparameter差替えを再現上同一とは扱わない。`isolated_physical_resource`は同じFIFO経路上で、前tenant状態をfresh zero resourceへ置換した監査eventを記録してから通常Reset・測定を行う負の対照として実装した。候補、perfect reset、isolated resource、観測無効、定数推定器、hidden shot整合を一括検査する受入reportとCLIも追加した。2026-09-28にReset課題専用v0の閾値とprofile値を採用した。
同日、攻撃者・被害者の主体非依存操作catalog、疑似ターミナルとUIの共通Application API、統計量の来歴、利用者校正と内部受入校正の分離を[共通操作・観測・校正契約](common-command-observation-contract.md)へ固定した。続いて内部Reset runtimeへ操作認可の積集合を接続したが、operation型はまだ役割別であり、公開API・認証境界も未実装。
2026-09-29に、攻撃者へ返されたラベル付きcountsだけから頻度閾値推定器を学習する処理と校正CLIを追加した。統計上の必要量をlabelごと8 queryと確認し、余剰2 queryを加えたlabelごと10、合計20 query・640 shotsをReset課題v0へ採用した。さらに、課題modeでcanonical estimatorの合格、perfect Reset・resource分離・定数推定器の不合格を確認してからseed bundleを発行する、最大試行回数付きpreflightを実装した。自由・研究用途は無選別seedを維持する。Reset関連の焦点test 82件が成功した。
2026-09-19のGit状態確認でもHEADは `699a110`。作業開始時の追跡済み変更は `.gitignore` と開発入口文書だった。

## 2026-10-01 監査指摘への対応

Reset modelのpost-stateとReadout値を、isolated resource割当を含む状態確定より前に検証するよう変更した。直接生成manifestのpolicy pathをtupleへ凍結し、設定入力とin-memory storeに暫定ホスト保護上限を設けた。Reset eventと利用者jobの文書上の混同、全体索引の`docs/`用途記述も修正し、Reset課題v0の二値解析値を独立式で照合するtestを追加した。近似の正式な受入指標、実機妥当性、公開APIの認可、物理計算量上限はこの変更では決定・検証していない。

第5団指摘を再確認した。Reset結果検証、manifestのpolicy path凍結、設定・manifest store上限について該当経路を修正済みと確認し、runtime/compiler焦点test 27件が成功した。一方、FIFO schedulerの受理job数、job/event履歴、`run_until_idle`が保持する完了結果には上限がない。host保護値は設定・manifestの直列化量を制限するもので、実メモリ量の保証ではない。runtime資源上限は未解決のまま。公開APIの認証・主体別結果投影も従来契約どおり未実装。

局所近似の機構別関係、領域選択、状態保持、比較指標を[共通契約案](local-physics-approximation-contract-proposal.md)へ整理した。これは提案中であり、実装済み挙動ではない。

局所近似案について二点を合意した。領域選択はevent時点の有効な影響関係だけでなく、状態保存機から引き継いだ過去の相関も考慮する。必要相関を保持できない、または資源上限を超える構成は、許可済み近似がない限り実行前preflightで拒否する。契約全体、相関の操作的定義、数値上限、誤差基準は引き続き提案・未確定である。

Reset・同時駆動クロストーク・相関Readoutを[三事例点検](local-physics-three-case-design-check.md)で同じ選択原則に通した。共通手順の候補は書けたが、自由モードでは未知の将来jobを開始時に検査できず、受付／割当前の再検査が必要。固定一領域の現行state store、FIFO、静的Readout primitiveだけでは三例を通したruntime検証はできない。これは設計点検であり、物理精度や性能の実証ではない。

実装分担のため、`core/attack_simulation/active_drive_relations.py`へ半開の装置時刻window、source drive区間、既存の`DriveCrosstalkTransfer`を使う選択関数の骨組みを追加した。重複・ゼロ係数・時刻境界を含む選択規則はユーザー実装待ちで、対応する2 testは明示的にskip。入力検証と既存Reset runtimeの焦点testは成功した。この選択器は共通schedulerやQPU状態計算へまだ接続していない。

## 2026-09-30 局所近似の探索実験

`scripts/experiment_local_isolation_approximation.py` を追加し、既存の内部Reset・駆動転送・残留ZZ・相関Readout primitiveを使って、少数量子ビットの結合計算と候補近似を比較した。係数は実機較正値ではなく差が見える探索条件であり、この実験は共通runtimeの物理近似を実装・検証したものではない。

- 不完全Reset（成功率0.9、成功時は|0⟩）: 被害者入力0/1に対する次利用者の`P(1)`は0/0.1。常に新鮮な|0⟩とみなす近似は入力1のケースで0.1の差を失う。
- 駆動転送（係数0.2、交換・ZZなし）: 2量子ビット結合計算と独立局所駆動の状態差ノルムは約`5.35e-16`。この限定条件では分離できる。
- 残留ZZ（蓄積位相`π/2`）: 相手が|0⟩または|1⟩ならX基底の測定確率について平均場と結合計算が一致。相手が|+⟩なら`P(+)`は厳密計算0.75、平均場0.85355で、二値分布の全変動距離は0.10355。位相を0からπまで変えると、試した点での距離は0〜0.11812。|+⟩の周辺量子ビットを増やした2／3／4量子ビットの星型結合では、同じ観測量の差は約0.104／0／0.229。πや3量子ビットでは差が偶然0になるため、単一点・単一観測量だけでは近似を承認できない。
- 相関Readout（両bitの同時反転確率0.12）: 真値00から観測11となる確率は結合チャネル0.12、各bitの周辺誤差だけを掛けた独立近似0.0144。二値対の全分布の距離は0.2112。Readoutの共同分布を独立誤差へ潰せない条件がある。
- 20×20の合成トポロジー、各8量子ビットの二ジョブ、1000 event×5反復では、1-hop近傍の索引は中央値約0.001〜0.0013秒、二つの独立8量子ビット**純粋状態**更新は約0.02〜0.03秒。保持した状態vectorは計8192 byte。一体化した16量子ビット密度行列だけなら理論上64 GiB。索引と純粋状態更新だけの試作であり、雑音・Reset・同時結合・スケジューラ・UIを含む性能値ではない。実測の最大RSSでもない。

焦点確認: `python -m unittest tests.test_local_isolation_approximation_experiment tests.test_experimental_isolation_physics` は15件成功。この探索だけでは近似の許容閾値、結合領域の数値上限、百量子ビット級での実行可否を確定しない。

### 雑音・時間・shotを含む追加比較

`scripts/experiment_noisy_isolation_approximation.py` で2量子ビットの結合Lindblad計算と、相手の励起確率を逐次更新する局所平均場を比較した。既存内部Hamiltonianと局所collapse演算子を使い、駆動は0.04〜0.10 μs、観測は0.04／0.10／0.20／0.30 μs。T1相当の下向き遷移、熱励起、Tφ相当の位相緩和は量子ビット別に設定し、雑音倍率0／1／4で比較した。数値は差の識別に用いた探索条件であり、特定の実機較正値ではない。

- 駆動転送のみ（ZZなし）: 雑音倍率0／1／4の全条件で、結合計算と局所計算の測定分布の全変動距離は最大`6e-9`程度（刻み0.002 μs）。局所Lindbladと駆動だけの系が分離可能という対照が成立した。
- ZZのみ（雑音倍率1）: X×X測定の共同分布の距離は0.10 μsで約0.0362、0.20 μsで約0.1047、0.30 μsで約0.1168。雑音を含めても局所平均場は結合相関を十分に再現しなかった。
- ZZ＋駆動転送（雑音倍率1）: 0.30 μsの共同分布の距離は約0.1000。最初の量子ビットのX基底`P(+)`は結合計算0.64086、局所平均場0.72379（差0.08292）。雑音倍率4でも共同分布の距離は約0.0956で、この条件では雑音を増やすだけで近似誤差は消えない。
- Reset後の待機（下向き0.4/μs、上向き0.02/μs）: 被害者入力による次利用者の`P(1)`差はReset直後0.1、1 μs後も約0.0657。後者では入力0／1に対する確率が約0.0163／0.0820で、熱励起による共通の床と情報残留を分けて扱う必要がある。
- 同じ結合＋駆動条件でRK4の最大刻みを0.004→0.002→0.001→0.0005 μsへ縮小すると、最終時刻の共同分布の距離は`0.10000893→0.10000890→0.10000890→0.10000890`に収束した。既存のdense time-dependent solverとの最終観測値の照合も通過した。
- 最後の`P(+)`から2モデルそれぞれ独立に二項sampleを2000対生成した場合（固定seed）、推定差の5〜95パーセンタイルは32 shots/modelで`-0.125〜0.281`、4096 shots/modelで`0.0659〜0.0999`。これは**模擬sample**であって実機測定値ではない。32 shots/caseの課題設定を検証用標本数と混同しない。

焦点testは `python -m unittest tests.test_noisy_isolation_approximation_experiment` で6件成功。結果は局所平均場を一般に採用できないことを示すが、2量子ビット・二準位・局所Markov雑音の範囲に限る。リーク状態、測定バックアクション、複数shotをまたぐ状態、ジョブスケジューラ、百量子ビットの実runtime負荷は未評価であり、結合領域の上限も決めない。

### 励起確率を1 percentage point以内へ抑える高速法の探索

`scripts/explore_excitation_probability_approximations.py` で、目標を標的量子ビットの励起確率`P(1)`の**絶対誤差0.01以内**と仮置きした。相対誤差1%とは異なる。測定前の確率を比較し、shot雑音を混ぜない。既存の内部ZZ Hamiltonianと局所Lindblad演算子を用い、標的・周辺量子ビットの初期状態、両者の駆動、ZZ係数、パルス長、量子ビット別雑音を変えた。参照計算は二量子ビット全密度行列、候補は局所平均場と「周辺の0/1別に標的の非正規化状態を保持する」条件付きブロック法。係数は探索用で実機較正値ではない。

- 周辺に横方向の駆動・交換結合がない場合、ZZと局所Markov雑音だけなら周辺の0/1別ブロックは閉じる。この条件では周辺の初期重ね合わせを含む設計格子96件と固定seedで生成した別条件48件すべてで`P(1)`が参照値と浮動小数点精度で一致した。これは当該縮約モデル内では近似より強い一致だが、2量子ビットでしか計測していない。solver部分の速度比は結合計算に対して約1.7倍（格子）、約2.7倍（別条件）。
- 局所平均場はさらに速いが、格子の周辺無駆動96件中4件、周辺駆動32件中3件で絶対誤差0.01を超えた。別条件の周辺駆動48件でも8件超過し、最大誤差約0.0302。よって「平均場を常に使う」は不採用。
- 周辺駆動時の経験的な簡易判定`|ζ|×パルス長 ≤ 0.5`では、別条件28件と境界ストレス48件に限り超過0件、最大絶対誤差約0.0052。ただし閾値0.75では境界ストレス96件中4件が0.01を超えた。0.5にも数学的保証はなく、未試験の強駆動・多準位・追加結合を許す判定器としては採用しない。

この段階の安全な経路案は「周辺が無駆動なら条件付きブロック法、駆動や交換結合があれば小結合領域の計算へ戻す」。高速化は対象条件付きであり、サービス全体の1%精度を保証したわけではない。ベンチマークはPythonのsolver部分だけ（Hamiltonian構築、ジョブ管理、測定shot、UIを除外）。複数周辺量子ビット・3準位リーク・実機由来の係数・速度／誤差の正式許容値は未検証。試験は`tests.test_excitation_probability_approximation_experiment`に置いた。

## 現在地

**物理基準版は固定済み。攻撃用物理プリミティブ、Reset校正、scenario compiler、単一QPU/FIFO、Reset漏洩課題orchestrator、集計限定graderの内部縦切りに加え、共通live世界・host_clock（1x）・WebSocket・所有者cookie・環境作成／破棄・ページ再読み込み復帰・設定catalog／resolverを、desktop／development限定のmountとして実装済み。攻撃画面は`AttackLivePage`（IDE型）で、分類タブの設定画面と端末から自由modeの製品profile（単一qubit較正／6-qubit）を操作できる。Reset課題の製品preset・採点接続、永続化・複数process復旧、公開proxy運用、未信頼コードsandboxは未実装。**

2026-10-09時点の整理（記述の同期。コード確認時のHEADは`1b658c6`、作業ツリーに攻撃系の未コミット差分あり）。
基準タグは `yuragi-strider-physical-baseline-v1`。以下の表の「確認した状態」には、
2026-09-13時点の記述が残る行がある場合、上の最新区切りを優先する。

## 実装済みの土台

| 範囲 | 確認した状態・根拠 |
| --- | --- |
| 物理モデルの固定 | [基準文書](../../../docs/validation/physical-model-baseline-v1.md)、[マニフェスト](../../../validation_results/physical_model_baseline_v1.json)、基準コミット・タグ |
| Gate-aware / Pulse API | [api/main.py](../../../api/main.py) の既存シミュレーション・解析エンドポイント |
| 量子ビット別T1/Tφ | [core/physical_environment.py](../../../core/physical_environment.py) の `compute_environment_rates_per_qubit` |
| 独立Readout | [api/pulse_models.py](../../../api/pulse_models.py) の `per_qubit` と割当確率。相関Readoutではない |
| 結合トランズモンネットワーク | 1〜4 transmons、局所2/3準位、交換結合、スケジュールされた局所駆動。詳細は[開発入口](../../README.md) |
| 相関した準静的デチューニング | [api/pulse_models.py](../../../api/pulse_models.py) の `quasi_static_detuning_*`。ジョブ間ドリフトや相関Readoutではない |
| 攻撃用物理プリミティブv0 | [モデル契約](../../../core/experimental_isolation_physics/MODEL_CONTRACT.md)に従い、不完全Reset、残留ZZ、駆動クロストーク、クラスタ相関Readoutを内部限定で追加。物理プリミティブ自体は共通基盤経由でのみ使われ、製品free profileからの利用範囲は[D62/D73の記録](#d73-グラフトポロジー領域選択qubit別ノイズ設定2026-10-09)に従う |
| Reset挙動校正基盤 | `reset_model.py`の共通境界・registry・既存CPTP adapterと、`reset_calibration.py`のprofile・相互情報量・Bayes最適精度を実装。model modeとvictim-dependent／fixed-zero controlを記録し、`scripts/calibrate_reset_leakage.py`からJSON出力可能。Reset課題専用v0 profileを`reset_leakage_settings.py`へ固定 |
| 共通scenario policy基盤 | `core/attack_simulation/scenario_policy.py`に三設定を持つ`ScenarioTemplate`、自由／課題overlay、field固定・秘匿、観測予算、evaluator指定を実装。両modeは同じtemplateを参照し、別シミュレーターへ分岐しない |
| 設定変更の成立前境界 | `ScenarioTemplate.free_override_fields`に自由modeの変更許可pathを集約。`ScenarioEnvironment`と`ScenarioCompiler`が未宣言の変更を拒否し、成立後の変更も拒否する。課題modeはpresetからの変更を拒否する。設定UI・永続profile registryは未実装 |
| 単一QPU/FIFO内部runtime | `qpu_state.py`にrevision付き唯一状態・原子的event commit、`jobs.py`にFIFO・状態機械・tenant別冪等性・queued取消、`shared_qpu_runtime.py`に所有者変更時のservice Resetと被害者→攻撃者一sampleを実装 |
| 共通コマンドclient・局所回路実行 | `api/attack_terminal.py`にdevice／program／job構文とactor別回路namespace、`circuit_program.py`に明示NISQ profileの最大8量子ビットcompilerを追加。配置・共同状態・bit列counts・領域別処理量へ一般化済み。HTTP／画面（desktop／development mount、WebSocket、IDE型端末）は接続済み。Reset既存runnerの主体非依存program移行と動的領域選択は未完了 |
| Reset漏洩scenario orchestrator | `observation_budget.py`でsession単位のquery・retry・shot予算を原子的に予約し、`reset_leakage_scenario.py`で各shotの全物理系列、既知label学習、未知bit観測、再試行を実装 **（2026-10-16削除: 製品経路は`SharedQpuRuntime`を通り、この単独経路は使われていなかった。[D74追記](decisions.md#d74-仮定過程変化不変の基盤を分ける合意2026-10-16)）** |
| Reset漏洩hidden grader | `reset_leakage_grader.py`に明示的評価policy、二値推定器境界、hidden case実行、aggregate score・合否だけの結果を実装。Reset課題専用v0の件数・閾値は設定化済み。未信頼コードsandboxは未実装 |
| Reset漏洩の利用者学習 | `reset_leakage_analysis.py`で公開training結果だけを受理し、両labelの集約頻度から閾値と方向を学習。内部真値へfallbackしない。labelごと10、合計20 queryのtraining予算を採用し、校正CLIと固定seedの内部E2E testへ接続 |
| Reset課題instance preflight | `reset_leakage_preflight.py`でmanager-private seed bundleを上限付き探索。candidate合格、perfect Reset／isolated resource／定数推定器不合格を同じhidden policyで検査し、選定policyと結果をprivate report化。永続manifest格納は共通基盤側で未実装 **（2026-10-16削除: 製品経路は`SharedQpuRuntime`を通り、この単独経路は使われていなかった。[D74追記](decisions.md#d74-仮定過程変化不変の基盤を分ける合意2026-10-16)）** |
| Reset漏洩access設定 | `ResetLeakageAccessConfig`でラベル付き学習・未知被害者観測をAttackerConfigから読み、自由modeの切替と課題modeの固定を実行時に強制 |
| Scenario compiler | `scenario_compiler.py`でRelation／Experimentを含む設定、仮定根拠、free override、Policyをcontent-addressed manifestへ解決。in-memory重複排除storeとconstraint hookを実装 |
| Reset漏洩RuntimePlan | manifestからaccess、予算、配置policy、Reset model ID・modeだけを抽出。同一qubitとisolated resourceを実行し、scheduler-dependentは拒否。orchestratorはauthoring treeを保持せず、job／eventへmanifest IDを記録 |
| Reset漏洩受入監査 | `reset_leakage_acceptance.py`で候補signal、perfect-reset／isolated対照、観測無効、定数推定器、hidden shot整合を明示閾値で判定。`scripts/audit_reset_leakage_acceptance.py`からJSON出力可能。Reset課題専用v0閾値を承認し、自動testへ接続 **（2026-10-16削除: 製品経路は`SharedQpuRuntime`を通り、この単独経路は使われていなかった。[D74追記](decisions.md#d74-仮定過程変化不変の基盤を分ける合意2026-10-16)）** |
| 既存UI | [frontend/src/App.tsx](../../../frontend/src/App.tsx) のGate-aware/Pulse領域に加え、[AttackLivePage.tsx](../../../frontend/src/pages/AttackLivePage.tsx)の攻撃IDE（分類タブ設定・コマンド端末・トポロジー／領域選択）を実装済み。desktop実画面での視覚確認は未実施 |

既存交換結合や多準位モデルがあることを、残留ZZ攻撃モデルの完成とは扱わない。
既存の同時局所駆動は一つのシミュレーション要求内の機能であり、異なるテナントの同時実行サービスではない。

## 未実装

- 独立した攻撃者のコード実行・ファイル・ツール環境（現端末は閉じたコマンドclientで、OS shellではない）。
- Reset既存runnerの主体非依存program移行。共通command／Application APIのHTTP・WebSocket・UI接続は、desktop／development限定で実装済み。Reset縦切りは役割別operationを内部型として使っている。
- 永続manifest store、profile registry、schema migration、compile cache、計算量見積り。
- 公開デプロイ向けの運用（公開proxy、複数worker／process間共有、管理者bootstrap）。環境ごとの所有者cookie認証、HTTP受付、内部FIFO状態機械、desktop／development限定mountは実装済み。
- 一般の被害者ワークロード自動実行と、複数scenarioを共有serviceへ割り当てるスケジューリング。最初のReset漏洩scenario内の同期実行だけは実装済み。
- 永続snapshot、server再起動復元、複数QPU、完全な仮想時計。in-memoryのjob間状態持越しとdevice timeは実装済み。
- 実装済み駆動クロストーク／残留ZZを、配置・時間・権限と結ぶ攻撃シナリオ契約。
- 実装済み相関Readoutを共有測定資源・攻撃者観測・推定処理へ接続する機構。
- 課題読み込み、提出・flag判定、課題改変の履歴、解説再生。
- 攻撃モードの一本道チュートリアル。演習予算の内部台帳は実装済みだが、予算のUI表示と永続化は未実装。
- Reset課題の製品preset・採点／提出接続（製品側ではunavailable）。

基準文書の除外項目と、`api/`・`core/`・`frontend/src/`・`tests/` の関連語検索、
APIおよび画面入口の確認に基づく。ネットワークサービス自身もクロストーク・伝達関数・較正モデルを非対応と記載している。

## 設計進捗

| 項目 | 状態 |
| --- | --- |
| 独立した高機能な攻撃者環境 | 方針合意。ランタイム・ツール・抽象化範囲は未決定 |
| 共通ワークスペースへの課題設定 | 最新方針として合意。二モード分離案を更新 |
| 課題中の操作入口 | 攻撃側画面のみとすることで合意。被害者・実機設定は上位の設定階層へ配置 |
| CTF型プリセット | 攻撃者・被害者・実機側の設定を割り当てる方針で合意。形式は未決定 |
| 体験の対象 | 量子コンピューターを通したセキュリティインシデント。量子暗号は対象外 |
| 被害者側から利用環境を設定 | 被害者は希望する利用条件を指定し、実際の共有方式・割当・保証はQPU serviceが所有することで合意 |
| 時間共有と同時共有の区別 | 設計上の前提として整理。参照サービス・課題仕様は未決定 |
| 時間、予算、統計判定 | Reset課題専用のshots、training query予算、hidden件数・合格閾値・決定論的受入閾値を採用済み。課題modeのseed preflightを内部実装し、自由／研究用途は無選別seedとして分離。一般scenarioの時間・予算・統計契約は未決定 |
| 最初の課題 | MiらCCS 2022を根拠に、同一物理量子ビットの順次再利用で被害者の非公開出力bitを推定するReset越し漏洩へ固定。profile値と内部受入は採用済みだが、共通service pipelineは未完了 |
| 主要な攻撃目的 | 最初の課題はO3型の「非公開出力の推定」。結果干渉・処理内容推定は後続課題 |
| 共通体験フロー | 設定→攻撃側へ移動→調査・操作→実行・観測→反復→課題別判定を整理。主体非依存catalog、能力の積集合、UI／terminalの共通Application APIまで合意。正式schemaと実装は未確定 |
| サービスごとの差 | 主体別三設定に加え、主体間関係と実験条件を分け、ScenarioPolicyで統制する方向へ更新。正式schemaと版管理は未決定 |
| 操作経路の候補 | ジョブだけでなく、パルス、仮想計測、指定部品編集を文献と対応付けた。採用・実装の決定ではない |
| 古典的手法との関係 | 量子固有の問題へ集中する意図を記録。開始条件として与える権限・情報は未決定 |
| レイヤー別整理 | [整理文書](layered-discussion.md)を作成。8分類は議論の見取り図であり、部品数や配置の決定ではない |
| 横断的な設計リスク | 目的と共有QPUの関係、攻撃因果の全経路、状態寿命、利用者の主体性、保護資産と被害をレビュー課題として記録 |
| 共通世界と計算範囲 | 製品全体では宣言された攻撃者→被害者の共有資源・経路を成立条件とする。現在の共有QPU型では、分離した利用者環境から同じ仮想QPU世界へ接続し、大規模トポロジー内の閉じた局所領域だけを計算する方針。切出し規則と上限は未決定。追加経路は未実装 |
| 時間と実行基盤の境界 | 三時間分離に加え、D57で成立原点・初期1倍・公開／実到達分離・判定保留・初回の整合復旧保証を採用。live接続、遅延／未処理量上限、復旧の具体手順・受入は未完了 |
| 観測経路 | 情報範囲だけでなく取得場所・権限・時点・根拠を設定する方針で合意。正式スキーマは未決定 |
| 自由実験と課題形式 | 同じ実行基盤上で`free`と`graded`のセッション方針を分ける方向へ更新。提出資格・再開始・解説後の扱いは未決定 |

## 検証履歴の扱い

基準版作成時の会話記録では、全803バックエンドテスト、フロントの契約検証・lint・buildが成功した。
2026-09-22には内部物理プリミティブの焦点テストを含むバックエンド全813件が400.095秒で成功した。ResetのCPTP性、相関Readoutのビット順と独立Readout一致、ZZ・複素駆動伝達、無効時の基準版完全一致を直接検証している。実機一致、攻撃成立、サービス/UI接続を検証した結果ではない。
2026-09-26にはReset境界・校正の7件、scenario policyの7件、共有QPU runtimeの7件を含む焦点テスト31件が成功した。revision競合、FIFO、tenant別冪等性、queued取消無影響、被害者→service Reset→攻撃者event順、内部失敗時の未確定Reset破棄、Readout誤分類と真の測定後状態の分離を検証した。例示parameterは採用profileではない。
同日の後続実装ではcompiler、orchestrator、grader、access、受入監査を含む焦点テスト64件が成功した。manifest、model parameter identity、同一qubit／isolated配置、model mode排他、負の対照、session close、hidden seed分離、FTQC manifestの共通compileとNISQ専用Reset runtimeでの拒否を検証した。直前の全599件走査では、現環境に`fastapi`／`pydantic`がないため31件がimport error、Rust拡張未配置により2件がfailureとなった。今回の焦点テストには失敗がない。

2026-10-05には、宣言workloadを一つも持たないactor-only live世界で、攻撃者のProgram受付、入力拒否、後続Program成功、返信、物理frontier確定までの受入試験を追加した。破棄後pump拒否も含め、`python -B -m unittest tests.test_attack_live_world`（24件）と`python -B -m unittest tests.test_attack_world tests.test_declared_controls`（37件）が成功。これはin-process受入であり、HTTP mount／ブラウザ経由の検証ではない。

2026-10-07には、desktop／明示development profileへ設定catalog・環境作成／破棄・WebSocketを組み立ててmountし、generic/publicではmountしない入口を追加した。製品free templateはD62の単一qubit較正profileと、既存物理モデルを使う6-qubit profileを分離し、Reset leakage課題はunavailableのまま。frontendの型は変更せずclient transportを実装し、credentialをclosure内だけで保持、HTTP CSRF header、手動WebSocket接続、reply通知、失敗対応を追加した。型ファイル`frontend/src/types/attackLive.ts`は今回の実装が依存する既存未追跡ファイルで、内容変更なし。D65 profile条件は未承認提案として保持し、一般契約へ昇格していない。

検証: `tests.test_attack_product_runtime` 3件、同テストを含む焦点回帰62 module／692件、`scripts/run_tests.py fast` 69件、frontend `npm run lint`、`npm run build`、`npm run validate:attack-live`が成功。desktop版をport 18877で起動し、既存ブラウザからアプリ本体を確認。実プロセスのcatalog GETはOrigin付きで200／`attack-settings-catalog-v1`を返した。ブラウザに攻撃UIがまだないため、ブラウザ上の作成→接続→送信→wait→reply→破棄は未実施。API経路はASGI統合試験で両free profileについて同じ流れを検証した。未決定: D65 profileの承認、UI接続、公開proxy／複数worker・永続化、Reset課題の判定器接続。

現行の小規模Reset profileでは、`ScenarioCompiler.compile()`は`timeit` 1000回×5反復の最良値で約45.5 us/回だった。これは設計上compile時だけに発生する参考値であり、将来の大規模profileに対する性能保証ではない。

## 次の設計作業と実装着手条件

2026-09-28に、Reset成功確率4条件、shot数4条件、hidden合格数5条件を比較する初回校正を実施した。最良count閾値推定器と固定case構成に対する厳密通過率、perfect-reset／isolated-resource対照の誤通過率を算出するCLIを追加した。標準候補はReset成功確率0.90、32 shots/case、hidden 20件中16件で、理論通過率82.29%、対照の最大誤通過率0.0156%だった。内部grader 100反復では78回通過、平均0.852秒だった。所有者は数値群をReset課題だけに限定したv0 profileとして承認した。詳細は[初回校正記録](reset-leakage-calibration-2026-09-28.md)を参照。

所有者は標準の攻撃者観測を32 shots/caseとし、preset生成時に変更可能とする方針を承認した。`ResetLeakageV0Settings`から標準値または上書き値を`ScenarioPolicy.observation_budget`へ解決し、compiler後のmanifestへ実効値を残す。hidden評価は0/1各10件の20件、16件以上正解で合格とする設定も承認した。grader policyはshot上書き値を共有し、case順・測定seedを明示して生成する。相互情報量0.45 bit/case以上・優位性0.30以上、負の対照は両指標1e-9以下の決定論的受入閾値も承認済みである。残るのは、model-derived閾値ではなく返却dataと正当に知るlabelから推定器を構成する体験上の校正である。
`scheduler_dependent`配置、永続profile registry、完全なrestart復元は後続契約とする。
Reset固有の内部経路は、公開trainingからの学習、hidden判定、負の対照、bounded preflightまで完了した。公開サービス化には永続snapshot・復元、Simulated QPU Service API、Application API、認証・結果認可、未信頼code sandbox、UIが残る。以後これらはReset専用機能として足さず、共通基盤として実装する。
