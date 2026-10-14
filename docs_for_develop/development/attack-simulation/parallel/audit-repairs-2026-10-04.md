# 並列成果物の監査修正

2026-10-04。監査チャットで報告後、利用者の「修正を行って」により今回の実装修正を行った。通常の読み取り専用監査や他部隊の所有権を恒久的に変更するものではない。HTTP契約や未決定のホスト上限も採用していない。

## 変更

- `frontend/src/utils/attackWorkspace.ts`と`pages/AttackSimulatorPage.tsx`: 成立／棄却に対する未知の通信・応答処理エラーを状態不明として扱い、設定の再編集／重複成立を止める。明確に分類された拒否応答は区別する。状態照会・復旧APIは未実装のまま。
- 攻撃側エディター: 共通のparser、resize、provider、workspace、paletteへ最小幅を渡せるようにした。攻撃側は1〜8、既存Gate-awareは従来の2〜8を維持。JSON形式や最大8の制限は変えず、1 qubitはReset専用の特別な回路形式にしない。
- `api/attack_http.py`: 名前の文字数上限を256へ揃え、最終的な256 UTF-8 bytes・記号的namespace検査は既存の共通workspaceへ委ねる。二重JSON encodingを含むHTTP全体64 KiB上限は別に残す。
- `core/attack_simulation/jobs.py`: 公開job IDをUUID4へ変更。受付連番は内部だけに残し、FIFO・principal別の再送照合を維持する。旧未永続化IDの移行処理は不要で、既存のjob操作文法は変えない。
- `shared_qpu_runtime.py`: 現隔離adapterが扱えない複数qubit領域をruntime生成時に拒否する。大きい領域の隔離再割当を便宜的に実装したとは扱わない。1 qubit隔離対照と通常の複数qubit共有領域は維持する。
- `qpu_state.py`: snapshot／transitionの入口で可変行列を不変化する。既にbuiltinの複素tuple行列なら再コピーしない。状態の有限性・trace・Hermitian・PSD検査は維持する。

UI変更は上記に加え `frontend/src/App.tsx`、`context/CircuitContext.tsx`、`components/AttackCircuitEditor.tsx`、`components/CircuitWorkspace.tsx`、`components/GatePalette.tsx`、`utils/circuitEditing.ts`、`utils/circuitConfigTransfer.ts`。部隊が先に追加したページ／adapterや他の未コミット変更を今回新規作成した成果物として扱わない。

## 検証

追加した回帰は、通信応答喪失／明確な拒否、1 qubit入力／既存最小幅、公開ID／内部順序／再送、129〜256文字とUTF-8境界、可変行列の外部変更、非対応隔離領域の生成時拒否。`tests/test_attack_http_adapter.py`、`tests/test_local_program_runtime.py`、`tests/test_shared_qpu_runtime.py`、`frontend/src/validation/attackWorkspace.test.mjs`に置いた。

Python焦点回帰: 168件成功、36.487秒。既存Resetのtraining／hidden／対照／preflightと、認可・資格情報・manifest検査も含む。

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_local_program_runtime tests.test_common_command_pipeline tests.test_attack_terminal tests.test_attack_program_request tests.test_job_application_service tests.test_shared_qpu_runtime tests.test_reset_calibration tests.test_reset_leakage_scenario tests.test_reset_leakage_preflight tests.test_reset_leakage_grader tests.test_reset_leakage_analysis tests.test_reset_leakage_acceptance tests.test_experimental_isolation_physics tests.test_scenario_service_conditions tests.test_scenario_credential_lifecycle tests.test_attack_sessions tests.test_attack_http_adapter tests.test_attack_scenario_policy tests.test_scenario_compiler
```

`frontend/`からの検証: `node src/validation/attackWorkspace.test.mjs`は17件成功。`npm run lint`、`npm run build`、`npm run validate:result-export`、`npm run validate:pulse-lab`は成功。buildには500 kB超のchunk警告が残る（JS約687 kB）。`git diff --check -- frontend`も成功。

これはASGI・in-process・Node境界検査の証拠であり、ブラウザ実操作、公開HTTP、本番認証、最大RSS、実機精度、全test suiteの検証ではない。新依存・実ネットワーク接続・commit／pushは行っていない。
