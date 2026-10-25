
> **Historical test-plan document**
>
> Phase 8 was executed and later extended by V1-V7, 3-4 qubit, snapshot,
> dense-backend, and Pulse Baseline A validations. Streamlit commands and the
> old `validation/` path below are obsolete. Use `docs/validation/`,
> `validation_results/`, and `validation_pulse/`.



---


## 目的

Phase 8 の目的は、Yuragi-Strider の本開発版について、以下を検証することです。

```text
1. 主要機能が壊れていない
2. 1-qubit / 2-qubit シミュレーションが妥当な結果を返す
3. H / X / Z / CNOT / Bell 回路が期待通り動く
4. Beginner / Expert / Compare / Save / Load / Export が連携して動く
5. 数値異常を検出できる
6. 性能が許容範囲に収まっている
7. U-22提出前に破綻しやすい箇所を洗い出す
```

Phase 8は、**機能追加フェーズではなく、検証・安定化フェーズ**です。

---

# Phase 8でやること

## 必須項目

```text
1. 回帰テストの整理
2. 数値妥当性テスト
3. 物理的 sanity check
4. 性能測定
5. 保存/読込/出力の再現性確認
6. UI smoke test
7. 既知の制限事項の整理
8. バグ修正
```

---

# Phase 8でやらないこと

```text
- 新しい物理モデルの追加
- 強結合開放系
- QuTiP backend
- Rust backend
- Godot UI
- FastAPI
- H_eff / no-jump 本格実装
- 3D Bloch球
- 大規模量子回路対応
```

この段階で欲張ると、安定化が崩れます。

---

# 推奨ファイル構成

```text
tests/
  test_regression_core.py
  test_regression_ui_state.py
  test_numerical_sanity.py
  test_physical_sanity.py
  test_performance_basic.py
  test_export_regression.py
  test_presets_regression.py

docs/
  validation/
    phase8_validation_report.md
    numerical_sanity_checks.md
    performance_notes.md
    known_limitations.md
```

---

# Step 0: 作業前確認

```powershell
cd C:\Users\oshad\Quantum-sim
(Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned) ; (& .\.venv\Scripts\Activate.ps1)

git status
python -m unittest discover -s tests
python -m pre_commit run --all-files
```

Phase 7のバグ修正までcommit済みなら、Phase 8用ブランチを切ります。

```powershell
git checkout -b phase8-validation-regression
```

---

# Step 1: 回帰テスト対象を固定する

まず、Yuragi-Striderで絶対に壊してはいけない代表ケースを決めます。

## 必須回帰ケース

|ケース|回路|期待する確認|
|---|---|---|
|R01|1-qubit I|状態がほぼ変わらない|
|R02|1-qubit X on \|0>|出力が \|1> に寄る|
|R03|1-qubit Z on \|0>|出力が \|0> のまま|
|R04|1-qubit H on \|0>|出力が 0/1 約50%|
|R05|2-qubit Bell|出力が 00/11 中心|
|R06|Low vs High Compare|比較結果が返る|
|R07|Open Config|読み込んだ回路が実行できる|
|R08|Export Result|JSON/CSV/Markdownが出る|
|R09|Expert Data|T1/T2/gamma/diagnosticsが出る|
|R10|Qubit resize|不正ゲートが残らない|

---

# Step 2: 数値妥当性テスト

## 検証する数値条件

```text
- fidelity が NaN / inf でない
- purity が NaN / inf でない
- output probabilities が NaN / inf でない
- density matrix trace が 1 に近い
- density matrix が Hermitian に近い
- density matrix の固有値が大きく負にならない
- output probabilities の総和が 1 に近い
- fidelity が大きく [0, 1] を外れない
- purity が大きく [0, 1] を外れない
```

## 推奨許容誤差

```text
trace error: 1e-8 〜 1e-6
Hermiticity error: 1e-8 〜 1e-6
probability sum error: 1e-8 〜 1e-6
small negative eigenvalue tolerance: -1e-10 〜 -1e-8
fidelity / purity range tolerance: 1e-10 〜 1e-8
```

Pythonの数値実装や時間発展方式によって多少ズレるため、最初から過度に厳しくしすぎないでください。
まずは `1e-6` 程度でも構いません。

---

# Step 3: 物理的 sanity check

ここでは「厳密な物理検証」ではなく、**明らかにおかしくないか**を見ます。

## 必須チェック

### 1. Xゲート

```text
initial: |0>
gate: X
ideal output:
  P(0) ≈ 0
  P(1) ≈ 1
```

### 2. Hゲート

```text
initial: |0>
gate: H
ideal output:
  P(0) ≈ 0.5
  P(1) ≈ 0.5
```

### 3. Bell回路

```text
initial: |00>
gates:
  H on q0
  CNOT q0 -> q1

ideal output:
  P(00) ≈ 0.5
  P(11) ≈ 0.5
  P(01), P(10) ≈ 0
```

### 4. 長時間T1緩和

```text
Bell状態 + 長時間 + 緩和あり
予想:
  purity が一度下がってから上がる可能性
  fidelity は低下
  final state は |00> に寄る可能性
```

これは、先ほど見つけた面白い挙動を **デモ候補** として検証します。

---

# Step 4: 性能測定

## 測定対象

```text
1-qubit H
1-qubit X
2-qubit Bell
1-qubit Compare Low/High
2-qubit Compare Low/High
Save/Load
Export JSON/CSV/Markdown
Expert data generation
```

## 計測する値

```text
elapsed time [s]
logical_qubits
dimension
time_steps
duration_us
gate_count
workflow
```

## 性能目標

|対象|目標|
|---|--:|
|1-qubit Single Run|1秒以内|
|2-qubit Single Run|1〜2秒以内|
|1-qubit Compare|5秒以内|
|2-qubit Compare|5秒以内を目標|
|Save/Load|1秒以内|
|Export|1秒以内|

厳密に失敗扱いにするより、まずは `docs/validation/performance_notes.md` に記録してください。

---

# Step 5: 保存/読込/出力の再現性確認

Phase 7で保存/読込を入れたので、ここは重要です。

## テストする流れ

```text
1. CircuitStateでBell回路を作る
2. SimulationConfigに変換
3. save_config
4. load_config
5. run_simulation
6. 結果が元と同じ構造になる
7. result JSON export
8. CSV export
9. Markdown export
```

## Compareでも確認

```text
1. Bell circuit
2. Low noise vs High noise comparison
3. save comparison result JSON
4. export comparison CSV
5. markdown comparison report
```

---

# Step 6: UI smoke test

自動化が難しい場合は、手動チェックリストで構いません。

## Beginner Mode

```text
- 起動する
- Beginnerに入れる
- I/H/X/Z/Measureを置ける
- 2qubitにできる
- CNOTを置ける
- Bellプリセットが動く
- Single Runできる
- Compareできる
- Open Configが反映される
- Save/Exportできる
```

## Expert Mode

```text
- Expertに入れる
- Expert Inspectorが表示される
- Overviewが出る
- Noiseが出る
- Stateが出る
- Assumptionsが出る
- 2-qubit Bellで Hilbert dimension = 4 になる
- Open Config後もInspectorが更新される
```

## Export

```text
- Result JSONを保存できる
- CSVを保存できる
- Markdownを保存できる
- 保存したConfigを再読込できる
```

---

# Step 7: known limitations を書く

Phase 8では、できないことを明示するのも重要です。

```md
# Known Limitations

## Current simulation scope

- Supports small density-matrix simulations.
- Main target: 1-2 logical qubits.
- 3-4 logical qubits are experimental.
- 5-6 logical qubits are not guaranteed for interactive use.

## Physical model limitations

- Uses weak-coupling Lindblad-type dynamics.
- Uses normalized environment parameters.
- Does not represent exact hardware temperature or magnetic field.
- Does not implement strong-coupling open systems.
- Does not implement non-Markovian memory effects.
- Does not implement pulse-level hardware calibration.

## UI limitations

- Drag-and-drop may be approximated by click-to-place.
- Expert Inspector is diagnostic, not a full research-grade solver interface.
- H_eff/no-jump mode is not enabled unless explicitly implemented.

## Backend limitations

- No QuTiP backend by default.
- No Rust backend.
- No Godot frontend.
- No FastAPI service.
```

これは弱点を晒すというより、**モデルの透明性**として強みになります。

---

# Codex指示文

```text
Task:
Implement Phase 8: performance, numerical sanity, and regression testing.

Goal:
Stabilize the application after Phase 1-7. Add regression tests, numerical sanity checks, performance measurements, export/load reproducibility tests, and documentation for known limitations. Do not add new physics features.

Required changes:

1. Add regression tests
   - tests/test_regression_core.py
   - tests/test_numerical_sanity.py
   - tests/test_physical_sanity.py
   - tests/test_performance_basic.py
   - tests/test_export_regression.py
   - tests/test_presets_regression.py

2. Regression cases
   Test:
   - 1-qubit I
   - 1-qubit X on |0>
   - 1-qubit Z on |0>
   - 1-qubit H on |0>
   - 2-qubit Bell circuit
   - Low vs High comparison
   - config save -> load -> run
   - result JSON export
   - CSV export
   - Markdown export
   - expert data generation

3. Numerical sanity checks
   Check:
   - no NaN/inf in times/fidelity/purity/output probabilities
   - fidelity within expected tolerance
   - purity within expected tolerance
   - probability sum close to 1
   - trace close to 1 if density matrix is available
   - Hermiticity error small if density matrix is available
   - minimum eigenvalue not strongly negative

4. Physical sanity checks
   - X on |0> should produce output probability near |1>
   - Z on |0> should preserve |0> probabilities
   - H on |0> should produce approximately 50/50 output probabilities
   - Bell circuit should produce probability support mainly on 00 and 11 in low/no noise
   - Long-time relaxation behavior may be documented, not strictly asserted

5. Performance checks
   - Measure elapsed time for:
     - 1-qubit H
     - 1-qubit X
     - 2-qubit Bell
     - 1-qubit comparison
     - 2-qubit comparison
     - save/load/export
     - expert data generation
   - Do not make tests flaky by enforcing overly strict timing on slow machines
   - Write results to docs/validation/performance_notes.md or print them in test logs

6. Export/load reproducibility
   - Save config
   - Load config
   - Run loaded config
   - Export result JSON
   - Export CSV
   - Export Markdown
   - Verify files exist and contain expected keys/columns

7. Documentation
   Add:
   - docs/validation/phase8_validation_report.md
   - docs/validation/numerical_sanity_checks.md
   - docs/validation/performance_notes.md
   - docs/validation/known_limitations.md

8. Optional diagnostic improvement
   - If final_purity > 0.95 and final_fidelity < threshold, add a diagnostic warning:
     "High purity with low fidelity: final state may be pure but not close to the ideal target."
   - Do not change physics calculations.

Acceptance criteria:
   - Existing tests pass
   - New tests pass
   - 1-qubit gate sanity checks pass
   - 2-qubit Bell sanity check passes
   - Save/load/export regression passes
   - Compare regression passes
   - Expert data generation regression passes
   - No NaN/inf appears in standard test cases
   - Known limitations document exists
   - Performance notes document exists
   - No Streamlit imports are added to core
   - No physics model is changed

Constraints:
   - Do not change environment-to-T1/T2 mapping
   - Do not change T1/T2-to-gamma mapping
   - Do not change Lindblad evolution
   - Do not change fidelity or purity definitions
   - Do not add external dependencies
   - Do not implement QuTiP, Rust, FastAPI, Godot, or strong-coupling systems
   - Do not rewrite the UI
```

---

# Phase 8 完了チェックリスト

```md
## Phase 8 Checklist

### Regression

- [ ] 1-qubit I regression test
- [ ] 1-qubit X regression test
- [ ] 1-qubit Z regression test
- [ ] 1-qubit H regression test
- [ ] 2-qubit Bell regression test
- [ ] Compare regression test
- [ ] Expert data regression test
- [ ] Save/Load regression test
- [ ] Export regression test

### Numerical sanity

- [ ] NaN/infを検出できる
- [ ] fidelityが妥当範囲内
- [ ] purityが妥当範囲内
- [ ] probability sumが1付近
- [ ] traceが1付近
- [ ] Hermiticity errorが小さい
- [ ] minimum eigenvalueが大きく負でない

### Physical sanity

- [ ] X on |0> が |1> へ寄る
- [ ] Z on |0> が |0> を保つ
- [ ] H on |0> が50/50に近い
- [ ] Bellが00/11中心になる
- [ ] 長時間緩和挙動を説明できる

### Performance

- [ ] 1-qubit Single Runを測定
- [ ] 2-qubit Single Runを測定
- [ ] 1-qubit Compareを測定
- [ ] 2-qubit Compareを測定
- [ ] Save/Load/Exportを測定
- [ ] performance_notes.mdに記録

### Export / Reproducibility

- [ ] .qscope.json保存/読込
- [ ] .qscope.result.json出力
- [ ] CSV出力
- [ ] Markdown出力
- [ ] 保存したconfigを再実行できる

### Documentation

- [ ] phase8_validation_report.md
- [ ] numerical_sanity_checks.md
- [ ] performance_notes.md
- [ ] known_limitations.md

### Safety

- [ ] coreにStreamlit依存がない
- [ ] 物理モデルを変更していない
- [ ] 外部依存を追加していない
- [ ] 既存UIを破壊していない
```

---

# 実装後の確認コマンド

```powershell
python -m unittest discover -s tests
```

個別に：

```powershell
python -m tests.test_regression_core
python -m tests.test_numerical_sanity
python -m tests.test_physical_sanity
python -m tests.test_performance_basic
python -m tests.test_export_regression
python -m tests.test_presets_regression
```

pre-commit：

```powershell
git add -A
python -m pre_commit run --all-files
git add -A
python -m pre_commit run --all-files
```

起動確認：

```powershell
streamlit run app/app.py
```

commit：

```powershell
git status
git add -A
git commit -m "Add Phase 8 validation and regression tests"
```

---

# Phase 8で特に見るべきバグ

## 1. Open Config後の古い結果残留

今回直したバグの再発確認です。

```text
Open Config
↓
CircuitState更新
↓
Run
↓
新しいconfigで実行されているか
```

## 2. 2-qubit Bellの保存/読込

2量子ビット・CNOTは壊れやすいので重点確認。

## 3. Compare + Export

ComparisonResultのJSON/CSV出力が欠けやすいです。

## 4. Expert Inspector更新

Open Config後にHilbert dimensionやT1/T2が古いまま残らないか確認。

## 5. H以外のゲート反映

X/Z/MeasureがUIだけでなく計算に反映されているか再確認。

---
