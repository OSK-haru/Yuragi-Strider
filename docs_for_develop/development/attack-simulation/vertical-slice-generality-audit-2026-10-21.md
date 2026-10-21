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

## 担当と次の判断

- 3・4はfoundation層で、Codexのbackend中立化と重なる。Claudeはレビューのみ（申し送り）。
- 1・2・5（API側）・frontendの項目はClaude側の製品層。着手時は未コミット差分（`attack_product_factory.py`ほか）との衝突に注意する。
- 今決めること: 1・2の着手順序。試作で確かめること: 登録表の形（`live_context`の渡し方）。後回し可: 6の閉集合、低〜中の項目。

## 未読範囲（判断保留）

`jobs.py`本体、`qpu_state.py`本体、`request_receipts.py`、`virtual_credentials.py`、`credential_policy.py`、`live_*.py`、`world_event_frontier.py`、`event_commit_boundary.py`、`scenario_*.py`、`victim_workload.py`、`workload_config.py`、`pulse_shape.py`、`register_readout.py`、`api/attack_workloads.py`、`api/attack_sessions.py`、`api/attack_live_world.py`、`api/attack_world.py`、`attack_client/`、frontendのSimulatePage等、`validation_hardware/`、`packaging/`、`desktop_app.py`、testsの大半、baseline JSONの中身、Rust/cptp/ioの中身。
