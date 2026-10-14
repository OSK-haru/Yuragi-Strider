# D53の時刻境界と遅延注入の並列作業

2026-10-04。正本は[実行境界 §4.3](../runtime-boundaries-and-resolutions.md#43-時間接続の前提検査と判断待ち2026-10-04)と[進捗](../progress.md)。本書はこの区切りの責務・検証範囲を記録する。

- 親側実装: `core/attack_simulation/world_event_frontier.py`、`tests/test_world_event_frontier.py`と文書同期。明示world入力からtarget・到着網羅済みwatermark・未解決ticket・confirmedを同期管理する。上限値・倍率・物理条件を既定化せず、QPU commitやHTTPへ未接続。
- 検証側: `tests/test_host_delay_invariance.py`だけを編集。既存session／service／runtimeの遅延比較、応答喪失・再送・結果投影待ちを検査し、productionの指摘は親へ返す。
- 独立監査側: コード・契約の読み取りと有限な再現検査だけを行い、既存受付と新時刻部品のrace・失敗・認可・上限を確認する。監査側は修正しない。

親の追加修正範囲: `api/attack_sessions.py`、`core/attack_simulation/job_service.py`、`shared_qpu_runtime.py`、`virtual_credentials.py`、`tests/test_session_ownership.py`。監査で再現された同一runtimeの並行二重所有と、生成失敗時の片側binding残留を修正する。検証側が確認した読み取り投影中のcontrol lock保持も、認証／生存再検査を残して解除する。新時刻部品の実装とこの既存不具合修正を区別する。

親が指摘を再現し、合意内の欠陥なら修正して再検証する。網羅性がないseal発行、QPU transactionと時刻確定のずれ、処理待ち中の被害者周期・物理発展を未合意の推測で接続しない。検証済みの範囲と残る判断は進捗へ残し、metadata検査をlive環境の完成と数えない。新しいチャット作成、自動継続の再開、ネットワーク、依存追加、Git履歴操作は行わない。

## 区切りの検証

親の焦点回帰237件／44.990秒と直接焦点77件／1.108秒が成功。独立検証49件が成功し、旧投影lock注入で有界TimeoutErrorを検出した。独立監査の最終43件も成功し、共有形態3種の36競合、代入直前2競合、rollback／再利用3条件、投影遅延3条件で具体的な残存欠陥・lock inversionの指摘なし。初回の時刻部品監査は756通りと16回の同時confirmを検査した。報告同士を証拠の代わりにせず、親がコードと遅延テストを再読・再実行して統合した。両担当の書込範囲は重複していない。

未検証は全suite、実ネットワーク、ブラウザ描画遅延、長時間負荷、復旧、live時計／QPU transactionの統合。時刻倍率、通信境界、被害者周期／待機物理、公開時刻・資源許容値はこの修正で暗黙に採用していない。

最終の文書／静的検査: PowerShellで変更15ファイルの末尾空白なし、更新文書のローカルMarkdownファイル参照79件の実在を確認した。これはリンク先headingの全検査やruntime検証ではない。内部担当2名は成果物の統合と再監査後に終了した。
