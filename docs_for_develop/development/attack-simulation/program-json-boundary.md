# 回路JSONから共通Program要求への境界

状態: 回路ファイルの読込み、共通送信要求、明示profileによる最大8量子ビットの局所コンパイル、ジョブ受付、in-processターミナルの`program submit`を実装。HTTP API・UI連携、他サービスのadapter、動的な物理領域選択は未実装。具体的な範囲は[共通コマンドclient](common-command-client.md)を参照。

D57の準備／継承を共通要求envelopeの`initialization=prepare|inherit`として実装し、compiler・認可・runtime・terminal・HTTP候補へ接続した。既定prepareのpayloadは維持し、継承は明示対応profileと追加grantを要求する。交代Resetは別に残す。旧課題presetの移行、UI選択、live時刻のevent列への統合は未完了。D58に従い、論理registerと物理ID集合、全体metadataと局所状態を分離する。[実装と検証](progress.md)を参照。

- Circuit Studioの`{version: 1, kind: "quantscope_circuit_config", circuit_config: ...}`と、既存インポータが許す生の`circuit_config`を入力とする。前者のversion/kindを確認し、現行エディター上限（64 KiB、8論理量子ビット、200列、800ゲート）と回路妥当性を再検査する。フロントの検査結果を信頼せず、バックエンドでも確認する。
- `CircuitProgram`は検証後の回路を不変スナップショットとして保持する。`MESSAGE`/`RECEIVED`の注釈は元の件数を記録するが、実行用回路からは明示的に除外する。`initial_states`、抽象ゲート、条件付き演算はこの段階では保持し、実機で実行可能と保証しない。
- `ProgramSubmitRequest`は回路に`device_id`、`shots`、`idempotency_key`、`initialization`（既定prepare）を組み合わせる。認証主体、tenant、資格情報、物理qubit割当はクライアントbodyから受けず、サービス側の認証・割当境界で決める。冪等性キーは要求メタデータであり、`to_application_payload()`のbodyには入れない。prepareのbodyは従来のままで、inheritだけ追加fieldを出す。
- 受理前に、選択されたサービスの命令集合、初期状態準備、条件付き演算、量子ビット上限、shots上限、トポロジー、配置を検査・コンパイルする必要がある。現行Reset runtimeの`VictimPrepareOperation`/`AttackerMeasureOperation`へ回路を黙って変換してはならない。未対応の回路は受付前に拒否し、ジョブ・予算・QPU状態を変えない。
- 現行エディター上限は入力互換の暫定値であり、全サービス・将来の実行方式の普遍的上限ではない。フロントの制限値を変える場合は、この境界と互換テストを同時に見直す。
- `LocalCircuitCompiler`は`circuit_execution`設定で明示的に有効化し、論理幅・対象operand・古典出力先を持つ主体非依存の`CircuitExecutionOperation`へ変換する。最大8量子ビット・32古典bit・終端測定blockを受理し、prepareでは不完全Reset＋準備gate、inheritでは準備を挿入しない。回路幅、serverが割り当てる`PhysicalQubitLayout`、保持する物理領域幅、計算量を分ける。全体topology metadataとstate bindingの照合を追加したが、gate routingや疎結合physics接続は未完了。既存Reset課題への移行は観測予算・被害者反復の接続後に行い、compilerを追加しただけで課題の送信経路を開放しない。
