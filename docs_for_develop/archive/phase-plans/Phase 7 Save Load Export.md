
> **Historical Streamlit-era implementation plan**
>
> Core config/result export helpers remain, and circuit JSON import/export is
> implemented in React. Streamlit paths and launch commands below are obsolete.



# Phase 7: Save / Load / Export に移行

Phase 7 の目的は、ここまで実装した **回路・環境条件・シミュレーション結果・比較結果・Expert情報** を、再利用・提出・検証できる形で外部化することです。

Phase 7 では、主に以下を作ります。

```text
.qscope.json          設定保存 / 読込
.qscope.result.json   結果保存
.csv                  時系列データ出力
.md                   簡易Markdownレポート出力
presets/              デモ用プリセット
```


---

# Phase 7でやること

## 対象機能

```text
1. 設定保存
2. 設定読込
3. プリセット読込
4. 結果JSON出力
5. 時系列CSV出力
6. Markdownレポート出力
7. UIへのSave / Open / Export接続
8. 読込時の検証
```

---


Phase 7は、まず **ローカルファイルとして保存・読込・出力できる状態** にするのが目的です。

---

# 推奨ファイル構成

```text
core/
  io/
    config_io.py
    result_export.py
    report_export.py
    schemas.py

data/
  presets/
    circuits/
      one_qubit_h.qscope.json
      one_qubit_x.qscope.json
      bell_state.qscope.json
    environments/
      low_noise.json
      high_noise.json
      almost_ideal.json
      strong_dephasing.json
    examples/
      one_qubit_h_low_high_compare.qscope.json
      bell_low_high_compare.qscope.json

exports/
  results/
    json/
    csv/
    markdown/

docs/
  architecture/
    config_format.md
    result_log_format.md
```

`core/io/` を作るのが嫌なら、最初は以下でもよいです。

```text
core/config_io.py
core/result_export.py
core/report_export.py
```

ただし、Phase 7以降は保存/出力系が増えるので、`core/io/` に分ける方が管理しやすいです。

---

# Phase 7の実装順序

## Step 0: 作業前確認

```powershell
cd C:\Users\oshad\Quantum-sim
(Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned) ; (& .\.venv\Scripts\Activate.ps1)

git status
python -m unittest discover -s tests
python -m pre_commit run --all-files
```

Phase 6がcommit済みなら、Phase 7用ブランチを作ります。

```powershell
git checkout -b phase7-save-load-export
```

---

# Step 1: `.qscope.json` 設定形式を確定する

## 目的

回路・環境条件・実行設定を保存し、あとで同じ条件を復元できるようにします。

## 保存対象

```text
schema_version
app_version
created_at
updated_at
circuit
environment
simulation
ui
metadata
```

## 推奨形式

```json
{
  "schema_version": "1.0",
  "app_version": "0.1.0",
  "kind": "qscope_config",
  "created_at": "2026-05-25T12:00:00Z",
  "updated_at": "2026-05-25T12:00:00Z",
  "metadata": {
    "title": "Bell state low noise demo",
    "description": "2-qubit Bell circuit with normalized low noise environment",
    "author": "",
    "tags": ["bell", "2-qubit", "low-noise"]
  },
  "circuit": {
    "logical_qubits": 2,
    "initial_states": ["0", "0"],
    "columns": [
      {
        "step": 0,
        "gates": [
          {
            "type": "H",
            "targets": [0],
            "controls": [],
            "params": {}
          }
        ]
      },
      {
        "step": 1,
        "gates": [
          {
            "type": "CNOT",
            "targets": [1],
            "controls": [0],
            "params": {}
          }
        ]
      }
    ]
  },
  "environment": {
    "mode": "normalized",
    "temperature": 0.1,
    "magnetic_field": 0.1,
    "noise_level": 0.2,
    "observation_strength": null,
    "observation_frequency": null
  },
  "simulation": {
    "model": "weak_coupling_lindblad",
    "duration_us": 20.0,
    "time_steps": 200,
    "fidelity_threshold": 0.9
  },
  "ui": {
    "display_level": "beginner",
    "workflow": "single_run"
  }
}
```

---

# Step 2: Config IOを実装する

## 作成ファイル

```text
core/io/config_io.py
```

## 必要関数

```python
def config_to_dict(config: SimulationConfig) -> dict:
    ...

def config_from_dict(data: dict) -> SimulationConfig:
    ...

def save_config(config: SimulationConfig, path: str, metadata: dict | None = None) -> None:
    ...

def load_config(path: str) -> SimulationConfig:
    ...
```

## 読込時の検証

最低限、以下を検証します。

```text
schema_version がある
kind == "qscope_config"
circuit がある
environment がある
simulation がある
logical_qubits が 1〜2、または現在対応範囲内
gate type が対応範囲内
temperature / magnetic_field / noise_level が 0.0〜1.0
duration_us > 0
time_steps >= 2
```

Phase 2で作った `validate_simulation_config(config)` を使ってください。

---

# Step 3: `.qscope.result.json` を実装する

## 目的

実行結果を、あとから検証・提出・再表示できる形で保存します。

## 保存対象

```text
schema_version
kind
created_at
model_version
input_config
summary
timeseries
output_probabilities
derived_parameters
diagnostics
warnings
```

## 推奨形式

```json
{
  "schema_version": "1.0",
  "kind": "qscope_result",
  "created_at": "2026-05-25T12:05:00Z",
  "model_version": "weak_coupling_lindblad_v1",
  "input_config": {},
  "summary": {
    "final_state_fidelity": 0.842,
    "final_purity": 0.797,
    "effective_operation_time_us": 15.0
  },
  "timeseries": {
    "time_us": [0.0, 0.1, 0.2],
    "state_fidelity": [1.0, 0.99, 0.98],
    "purity": [1.0, 0.995, 0.991]
  },
  "output_probabilities": {
    "00": 0.48,
    "01": 0.02,
    "10": 0.02,
    "11": 0.48
  },
  "derived_parameters": {
    "T1_us": 12.5,
    "T2_us": 8.3,
    "gamma1_per_us": 0.08,
    "gammaphi_per_us": 0.11
  },
  "diagnostics": {
    "trace": 1.0,
    "hermiticity_error": 2.1e-12,
    "minimum_eigenvalue": -1.0e-10
  },
  "warnings": []
}
```

---

# Step 4: CSV出力を実装する

## 目的

グラフや外部解析に使える時系列データを出力します。

## 出力ファイル

```text
result_timeseries.csv
```

## 列

```csv
time_us,state_fidelity,purity
0.0,1.0,1.0
0.1,0.995,0.998
0.2,0.990,0.996
```

Compare結果の場合はこうします。

```csv
time_us,fidelity_a,fidelity_b,purity_a,purity_b
0.0,1.0,1.0,1.0,1.0
0.1,0.998,0.991,0.999,0.995
0.2,0.995,0.982,0.997,0.989
```

---

# Step 5: Markdownレポート出力を実装する

## 目的

Obsidian管理・提出資料・開発記録に使える簡易レポートを生成します。

Plus Requirementsでも、Obsidian Markdownレポート出力は将来拡張候補に入っています。
ただし、Phase 7では高度なレポートではなく、**簡易Markdown出力** で十分です。

## 出力例

```md
# Yuragi-Strider Simulation Report

## Summary

- Final State Fidelity: 0.842
- Final Purity: 0.797
- Effective Operation Time: 15.0 us
- Model: weak_coupling_lindblad

## Circuit

- Logical qubits: 2
- Initial states: |0>, |0>
- Gates:
  - t0: H on q0
  - t1: CNOT q0 -> q1

## Environment

- Temperature parameter: 0.1
- Magnetic field parameter: 0.1
- Noise level: 0.8

## Derived Physical Quantities

- T1: 12.5 us
- T2: 8.3 us
- gamma1: 0.08 1/us
- gammaphi: 0.11 1/us

## Diagnostics

- Trace: 1.0
- Hermiticity error: 2.1e-12
- Minimum eigenvalue: -1.0e-10

## Model Assumptions

- Weak-coupling open quantum system
- Born-Markov approximation
- Lindblad-type master equation
- Normalized environment parameters
- No strict hardware calibration
```

---

# Step 6: プリセットを追加する

## 必須プリセット

```text
one_qubit_h.qscope.json
one_qubit_x.qscope.json
bell_state.qscope.json
```

## 環境プリセット

```text
low_noise.json
high_noise.json
almost_ideal.json
strong_dephasing.json
```

## 比較プリセット

```text
one_qubit_h_low_high_compare.qscope.json
bell_low_high_compare.qscope.json
```

U-22向けには、最低でも以下が必要です。

```text
1-qubit H + Low noise
1-qubit H + High noise
1-qubit X
2-qubit Bell
Low vs High comparison
```

---

# Step 7: UIに接続する

## Start Screen

`Open Config` を動作させます。

```text
Open Config
  ↓
.qscope.json 読込
  ↓
CircuitState / Environment / Simulation settings に復元
```

UI要件でも、Open Config は保存済み `.qscope.json` を読み込んで、回路・論理量子ビット数・初期状態・環境条件・シミュレーション設定・表示設定を復元する機能として定義されています。

## Beginner / Expert Toolbar

追加する操作：

```text
Save Config
Open Config
Export Result JSON
Export CSV
Export Markdown Report
```

## 注意

UIから直接JSONを組み立てすぎないでください。
必ず `core/io/` の関数を呼びます。

---

# Step 8: Compare結果のExport対応

Phase 5でCompareがあるため、CompareResultも保存できるようにします。

## 推奨形式

```json
{
  "schema_version": "1.0",
  "kind": "qscope_comparison_result",
  "created_at": "2026-05-25T12:10:00Z",
  "model_version": "weak_coupling_lindblad_v1",
  "comparison_summary": {
    "delta_final_fidelity": -0.052,
    "delta_final_purity": -0.084,
    "delta_effective_operation_time_us": -3.6,
    "better_condition": "Condition A"
  },
  "condition_a": {
    "label": "Low noise",
    "result": {}
  },
  "condition_b": {
    "label": "High noise",
    "result": {}
  }
}
```

---

# Codex指示文

```text
Task:
Implement Phase 7: Save, Load, and Export.

Goal:
Add local file-based persistence and export for Yuragi-Strider configurations, simulation results, comparison results, CSV time series, and Markdown reports. This phase must reuse existing SimulationConfig, SimulationResult, ComparisonResult, validation, and expert data structures.

Required changes:

1. Add config IO
   - Create core/io/config_io.py or equivalent
   - Implement:
     - config_to_dict(config)
     - config_from_dict(data)
     - save_config(config, path, metadata=None)
     - load_config(path)
   - Use .qscope.json format
   - Include schema_version, kind, metadata, circuit, environment, simulation, ui
   - Validate loaded configs using existing validation functions

2. Add result export
   - Create core/io/result_export.py or equivalent
   - Implement:
     - result_to_dict(result)
     - save_result_json(result, path)
     - export_result_csv(result, path)
   - Use .qscope.result.json format
   - Include:
     - schema_version
     - kind
     - created_at
     - model_version
     - input_config
     - summary
     - timeseries
     - output_probabilities
     - derived_parameters
     - diagnostics
     - warnings

3. Add comparison export
   - Support ComparisonResult if present
   - Implement:
     - comparison_result_to_dict(comparison_result)
     - save_comparison_result_json(comparison_result, path)
     - export_comparison_csv(comparison_result, path)
   - Include condition A/B labels, result summaries, delta metrics, and warnings

4. Add Markdown report export
   - Create core/io/report_export.py or equivalent
   - Implement:
     - export_markdown_report(result, path)
     - export_comparison_markdown_report(comparison_result, path)
   - Include summary, circuit, environment, derived parameters, diagnostics, model assumptions, warnings

5. Add presets
   - data/presets/circuits/one_qubit_h.qscope.json
   - data/presets/circuits/one_qubit_x.qscope.json
   - data/presets/circuits/bell_state.qscope.json
   - data/presets/environments/low_noise.json
   - data/presets/environments/high_noise.json
   - data/presets/environments/almost_ideal.json
   - data/presets/environments/strong_dephasing.json
   - data/presets/examples/one_qubit_h_low_high_compare.qscope.json
   - data/presets/examples/bell_low_high_compare.qscope.json

6. UI integration
   - Add Save Config button
   - Add Open Config button
   - Add Export Result JSON button
   - Add Export CSV button
   - Add Export Markdown Report button
   - Use core/io functions from UI
   - Do not build JSON manually in UI except for display

7. Documentation
   - Add docs/architecture/config_format.md
   - Add docs/architecture/result_log_format.md
   - Explain .qscope.json
   - Explain .qscope.result.json
   - Explain CSV columns
   - Explain Markdown report format

8. Tests
   - Add tests/test_config_io.py
   - Add tests/test_result_export.py
   - Add tests/test_comparison_export.py if ComparisonResult exists
   - Add tests/test_presets.py

Acceptance criteria:
   - Existing tests pass
   - Config can be saved and loaded
   - Loaded config can run through run_simulation(config)
   - 1-qubit H preset loads and runs
   - 2-qubit Bell preset loads and runs
   - Result JSON export contains summary, timeseries, derived parameters, diagnostics
   - CSV export contains time_us, state_fidelity, purity
   - Comparison CSV contains A/B fidelity and purity columns
   - Markdown report is generated
   - UI can save config
   - UI can open config
   - UI can export result JSON, CSV, and Markdown
   - Invalid config fails validation with clear error
   - No Streamlit imports are added to core
   - No physics model is changed

Constraints:
   - Do not change environment-to-T1/T2 mapping
   - Do not change T1/T2-to-gamma mapping
   - Do not change Lindblad evolution
   - Do not change fidelity or purity definitions
   - Do not add external dependencies unless absolutely necessary
   - Do not implement cloud storage
   - Do not implement FastAPI, Godot, Rust, or QuTiP backend
   - Do not implement authentication
```

---

# 追加テスト

## `tests/test_config_io.py`

```text
save_configで.qscope.jsonを作れる
load_configでSimulationConfigに戻せる
保存→読込→run_simulationが通る
不正schema_versionでerror
必須キー欠損でerror
```

## `tests/test_result_export.py`

```text
SimulationResultをJSON化できる
.qscope.result.jsonを保存できる
CSVを保存できる
CSVにtime_us,state_fidelity,purity列がある
Markdownレポートを保存できる
```

## `tests/test_presets.py`

```text
one_qubit_h presetが読める
one_qubit_x presetが読める
bell_state presetが読める
各presetがvalidateを通る
各presetがrun_simulationで動く
```

---

# 実装後の確認手順

```powershell
python -m unittest discover -s tests
```

個別：

```powershell
python -m tests.test_config_io
python -m tests.test_result_export
python -m tests.test_presets
```

pre-commit：

```powershell
git add -A
python -m pre_commit run --all-files
git add -A
python -m pre_commit run --all-files
```

起動：

```powershell
streamlit run app/app.py
```

手動確認：

```text
1. Start ScreenからOpen Configできる
2. one_qubit_h.qscope.jsonを開ける
3. Run Simulationできる
4. Save Configできる
5. Result JSONを出力できる
6. CSVを出力できる
7. Markdown Reportを出力できる
8. bell_state.qscope.jsonを開ける
9. 2-qubit Bellが実行できる
10. Compare結果をexportできる
```

---

# Phase 7完了条件

```md
## Phase 7 Checklist

### Config Save / Load

- [ ] .qscope.json 形式が定義されている
- [ ] save_config がある
- [ ] load_config がある
- [ ] config_to_dict がある
- [ ] config_from_dict がある
- [ ] 読込時にvalidationされる
- [ ] 保存したconfigを再読込して実行できる

### Result Export

- [ ] .qscope.result.json 形式が定義されている
- [ ] result_to_dict がある
- [ ] save_result_json がある
- [ ] export_result_csv がある
- [ ] summary が保存される
- [ ] timeseries が保存される
- [ ] output_probabilities が保存される
- [ ] derived_parameters が保存される
- [ ] diagnostics が保存される
- [ ] warnings が保存される

### Comparison Export

- [ ] comparison_result_to_dict がある
- [ ] save_comparison_result_json がある
- [ ] export_comparison_csv がある
- [ ] A/B condition が保存される
- [ ] delta metrics が保存される
- [ ] warnings が統合保存される

### Markdown Report

- [ ] export_markdown_report がある
- [ ] Summary が出力される
- [ ] Circuit が出力される
- [ ] Environment が出力される
- [ ] Derived physical quantities が出力される
- [ ] Diagnostics が出力される
- [ ] Model assumptions が出力される

### Presets

- [ ] one_qubit_h preset がある
- [ ] one_qubit_x preset がある
- [ ] bell_state preset がある
- [ ] low_noise preset がある
- [ ] high_noise preset がある
- [ ] one_qubit_h_low_high_compare example がある
- [ ] bell_low_high_compare example がある

### UI

- [ ] Save Config ボタンがある
- [ ] Open Config ボタンがある
- [ ] Export Result JSON ボタンがある
- [ ] Export CSV ボタンがある
- [ ] Export Markdown Report ボタンがある
- [ ] UIがcore/io関数を使う
- [ ] UIで手動JSON構築を乱用していない

### Tests

- [ ] test_config_io.py が通る
- [ ] test_result_export.py が通る
- [ ] test_presets.py が通る
- [ ] 既存テストが通る

### Safety

- [ ] JSON読込で任意コード実行しない
- [ ] coreにStreamlit依存がない
- [ ] 物理モデルを変更していない
- [ ] 外部依存を不要に増やしていない
```

---


---

# Phase 7 commit

```powershell
git status
git add -A
python -m pre_commit run --all-files
git add -A
python -m unittest discover -s tests
git commit -m "Add save load and export support"
```

---
