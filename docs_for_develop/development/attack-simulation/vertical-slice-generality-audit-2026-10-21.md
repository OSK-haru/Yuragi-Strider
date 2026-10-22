# 縦切り由来の一般性喪失の点検（2026-10-21）

`DEVELOPMENT_PROTOCOL.md` 第11項に沿い、プロジェクトを7区間に分けて、縦切りを通すことを優先した設計になっていないかを点検した記録。

**位置付け**: 読取専用レビュー7本の報告を統合した所見であり、決定事項ではない。各レビューは一部のファイルを grep と抜粋で読んだもので、確度は中程度。未読範囲は「懸念なし」ではなく「判断保留」。指摘は2026-10-21時点の作業ツリー（backend中立化の未コミット差分を含む）に対するもので、最終形ではない。コードでの再確認前に実装判断の根拠にしない。

## 区間別の判定

| 区間 | 判定 | 主な懸念 |
|---|---|---|
| 1 汎用core | 軽微 | ゲート・backendが閉じた分岐、qubit数の閾値が散在 |
| 2 パルス | 軽微 | 旧経路の並存、`_build_envelope`の3重複、上限定数の出典が複数 |
| 3 攻撃の土台 | 要対処 | 役割の2値固定、`ResetResourceBinding`が共有契約に入っている |
| 4 課題・シナリオ | 要対処 | 評価器の名前分岐、2課題がコピペ構造 |
| 5 攻撃API | 要対処 | 評価器の直書き、凍結入力パーサ、領域サイズ6固定 |
| 6 frontend | 軽微〜中 | `InterleaveReference`/`EnvironmentMode`が型で閉じている、ibm_kingston固定 |
| 7 検証・基盤 | 軽微〜中 | テストグループが手書きでattack系が未収録、較正値の固定 |

## 横断的な所見（重要度順）

1. **評価器の選択が名前分岐（高）**: `api/attack_product_factory.py`の`evaluator_id == CROSSTALK_EVALUATOR_ID`分岐、`api/attack_challenge.py`の`challenge_evaluator`のif連鎖。3つ目の課題はfactory編集が必要。案: `evaluator_id`→builder(manifest, limits, live_context)の登録表。
2. **2課題がコピペ構造（高）**: `attack_challenge.py`と`attack_crosstalk_challenge.py`が同じ手順（`_thaw`→編集→lock→ScenarioPolicy→`_unique`）を複製。評価器クラス・結果型に共通基底がない。crosstalkがreset側のprivate関数`_thaw`/`_unique`をimport。案: 共通`ChallengeSpec`、提出台帳・結果のProtocol、共通部品の専用モジュール化。
3. **役割がvictim/attackerの2値固定（高）**: `job_authorization.py`、`job_service.py`、`shared_qpu_runtime.py`の文字列リテラル検査、`ObservationQueryKind.VICTIM`。frontendの領域所有者3種、端末の`--interleave`値域にも同じ語彙。案: 役割をpolicy側の登録制にし、2テナント前提はシナリオ層に限定。
4. **`ResetResourceBinding`が共有runtime configに入っている（高）**: `shared_qpu_runtime.py`、`resource_binding.py`、`job_service.py`。bindingの有無が実質「Resetモードか否か」のフラグ。案: `ResourceBinding`Protocol（役割→PhysicalQubitRef）へ一般化し、Reset用はscenario側の実装の1つへ。
5. **Kingston直結の散在（中）**: 既定snapshotがruntime・profiles・challengeの各所、frontendの見出しと座標、テスト9ファイル、実験スクリプト。案: 既定はruntime組立の1箇所、snapshot必須化、汎用`load_snapshot(device_id)`。
6. **閉じた集合に宣言がない（中）**: ゲート種別（5か所同期）、`SUPPORTED_TWO_QUBIT_GATES={"cz","ecr"}`、回路命令種別、`backend_boundary`の分岐、pulseのshape、`MAX_TRANSMON_COUNT`の二重定義、`simulator.py`のqubit数閾値。案: 宣言（registry）を1か所に置き、他は導出。
7. **足場の明記が薄い（中）**: 型単位で足場と明示されているのは`resource_binding.py`のdocstringと`job_service.py`のコメントなど少数。凍結入力パーサ（`shared_qpu_runtime_v3`、`reset_model`必須化、`prepare_basis_state_then_measure`）が最優先の明記候補。

## その他（低〜中）

- 並行ポリシーが2領域前提で、離調`SAMPLE_DETUNING_RAD_PER_US`とパルス形状`SAMPLE_PULSE_SHAPE`がモジュール定数（宣言・manifestに出ない）。
- DeviceInfoの既定値`initialization_policy`/`gate_model`がReset縦切りの語彙で、`gate_model`は宣言でなく定数。
- 領域サイズ`MAX_RETAINED_QUBITS=6`と主・代替の2枠。freeの`idle_noise_mode`キー固定。
- crosstalk評価器がscheduler内部（`runtime.scheduler._jobs`）を直接読み、被害者を1プラン前提で扱う。
- `scripts/run_tests.py`・`verify_cached.py`のグループが手書きでattack系21ファイルが未収録。D62較正値（`0.992919921875`、`2.212`）を厳密固定するテスト。
- 環境保管が1プロセス・メモリ前提（README記載済み）で、保管用のseamがない。
- `hardware_calibrated: Literal[False]`/`experimental: Literal[True]`は較正済みモデル追加で応答契約が壊れる。足場としての記録は未確認。

## 一般性が保たれている点

`backend_declaration.py`、`execution_regime.py`、`placement.py`、`config_limits.py`、`scenario_compiler`（評価器IDを不透明に扱う）、`PulseEnvelope`、`process_characterization`、Rust境界（次元を引数に取る）、所有権profile、課題一覧と設定フォームの目録駆動、`AttackEnvironmentClient`境界。

## 解消の状況（Claude側、2026-10-21）

| 所見 | 状況 |
|---|---|
| 1 評価器の名前分岐 | 解消。`api/attack_evaluators.py`の登録表。未登録IDは拒否 |
| 2 2課題のコピペ | 一部。逆向き依存は`api/attack_challenge_common.py`で解消。`locked`の生成と評価器クラスの複製は、全ロックが方針であり3つ目の課題で確かめるまで共通化しない（意図的に残した） |
| 5 Kingston直結 | API側は`default_device_snapshot()`の1箇所に集約。frontendの見出しは`standard.device`から表示。座標レイアウト、テスト、実験スクリプトは残る |
| 6 閉じた集合 | パルスの`MAX_TRANSMON_COUNT`二重定義を解消。ほかは未着手 |
| 7 足場の明記 | factoryの凍結入力パーサ・出所一覧・interleaved victimに`SCAFFOLD`コメントを追加。foundation側は未 |
| テストグループ | `run_tests.py attack`を追加（ファイル名パターンで選択） |
| 3・4・6の残り・低の一部 | Codexへ申し送り |
| frontendの`EnvironmentMode`/`InterleaveReference` | 自由/課題の2モードは製品上の語彙として現状維持。interleave値域は課題の宣言から取る形が望ましいが未着手 |

## 担当と次の判断

- 3・4はfoundation層で、Codexのbackend中立化と重なる。Claudeはレビューのみ（申し送り）。
- 1・2・5（API側）・frontendの項目はClaude側の製品層。着手時は未コミット差分（`attack_product_factory.py`ほか）との衝突に注意する。
- 今決めること: 1・2の着手順序。試作で確かめること: 登録表の形（`live_context`の渡し方）。後回し可: 6の閉集合、低〜中の項目。

## Foundation対応の判断（2026-10-21）

以下はコードを再確認した上でのfoundation作業範囲。将来の第三backend・課題・役割を追加する際に共通契約を割らないことを基準にする。

### 今決めること

- 共通認可は登録された`ActorRole`ごとのgrants表を参照し、tenant数や役割名を認可機構の前提にしない。現行manifestのattacker/victim入力とその組はscenario adapterの足場として残す。
- runtimeが受け取るresource bindingは`ResourceBinding`境界（roleから`PhysicalQubitRef`を解決）とし、Reset課題用bindingはscenario実装として分離する。bindingの有無をruntime種別の判定には使わず、runtime/scheduler capabilityを明示する。
- 並行干渉は宣言項目の列（source region、destination region、strength、detuning、pulse shape）から構成し、2領域サンプルはscenario用データにする。
- `DeviceInfo.gate_model`は既定語彙で埋めず、backend declaration由来の必須値とする。
- scheduler適合性は実装型名・「initial」ラベルで拒否せず、登録済みscheduler profile/capabilityを使う。
- schedulerにtenantで絞った完了jobの公開結果queryを設け、製品層はscheduler内部ストアを読まない。
- Kingston固定のloader呼出しは既定snapshot入口またはdevice ID指定の汎用loaderへ寄せる。Kingston固有の期待値を検証するテスト自体はfixture名と期待内容を明示した上で残せる。

### 試作で確かめること

- `GateSpec`を唯一のゲート宣言として、supported gate一覧・arity/operand制約・既定時間・unitary実装の導出が現行ゲートを保てるか。検証文言や複合ゲート(CNOT/CCX/QFT/ORACLE)の展開は宣言だけに押し込まず、個別意味論の境界を測る。
- circuit instruction kindを宣言・登録できる形にしつつ、現行compiler/runtimeが理解しない命令を明示的に拒否できるか。
- backend declarationの二重表現と二量子ビットgate検査が`BackendDeclaration`へ集約できるか。非対応backendへの拒否は維持する。
- concurrency declaration listの空・多領域・方向別の挙動を、現行2領域例と単純な第三領域で確認する。

### 後回しにできること／明示する足場

- 現在のscenario manifestがattacker/victim別設定を持つこと、interleaved victimが現行シナリオの主体・手順を表現することは、汎用core APIへ昇格させない範囲で足場として残す。
- 新しい攻撃操作・任意のcircuit instructionを動的pluginとしてロードする仕組み、backend外部配布、較正形式の汎用化は今回の契約に含めない。
- Kingston snapshotを基準にした物理係数・座標の数値的妥当性、第三backendの実機妥当性、第三scenarioでの評価品質は今回のコード試作では検証しない。
- 対象外のreset model、persistent/multiprocess runtime、pulse shape catalogの全域は後続課題とする。これらを実装済み・検証済みとは扱わない。

### Foundation実装の記録

- `ActorRole`とgrants registry、`ResourceBinding`、`CrosstalkDeclaration`、scheduler registry、`GateSpec`をfoundation側に追加した。現行attacker/victim設定、2領域lane、APIのcrosstalk入力はadapter/足場として互換維持している。
- `core/backend_boundary.py`は実行kernel選択（Python dense/Rust preview）、`BackendDeclaration`はhardware gate basis・directionality宣言で、責務が異なる二重宣言ではない。実行backend分岐はそれぞれ異なるfallback/診断契約を持つため今回統合しない。
- `CIRCUIT_INSTRUCTION_KINDS`は命令語彙の宣言になったが、実行器が扱える命令追加にはruntimeの実装と試作確認が別途必要。
- schedulerの公開queryは`completed_job_results_for_tenant(tenant_id)`。`api/attack_evaluators.py`の呼出側切替はClaude側の作業として残す。
- Kingston固定loaderの利用箇所を既定snapshot入口またはdevice ID指定loaderへ移した。Kingstonの物理値・レイアウトを使う既存テストは基準fixtureとして残し、汎用snapshot loader自体も直接使う。
- `concurrent_lanes.py`とruntimeの保管構造は現状2領域を要求する。第三regionの完全な実行は未対応で、scenario宣言形式の汎用化をその証拠と取り違えない。

## 未読範囲（判断保留）

`jobs.py`本体、`qpu_state.py`本体、`request_receipts.py`、`virtual_credentials.py`、`credential_policy.py`、`live_*.py`、`world_event_frontier.py`、`event_commit_boundary.py`、`scenario_*.py`、`victim_workload.py`、`workload_config.py`、`pulse_shape.py`、`register_readout.py`、`api/attack_workloads.py`、`api/attack_sessions.py`、`api/attack_live_world.py`、`api/attack_world.py`、`attack_client/`、frontendのSimulatePage等、`validation_hardware/`、`packaging/`、`desktop_app.py`、testsの大半、baseline JSONの中身、Rust/cptp/ioの中身。
