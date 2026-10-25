
> **Historical Streamlit-era implementation plan**
>
> This plan predates the React/FastAPI migration. Streamlit paths and
> implementation-status statements below are historical, not current.


Phase 5 の目的は、**同一回路に対して条件A/Bを適用し、環境条件の違いによる劣化差分を見える化すること**です。

Phase 4 で Beginner Mode から単一条件の実行ができるようになった前提で、Phase 5 では次を作ります。

```text
同一 CircuitConfig
  ↓
EnvironmentConfig A / EnvironmentConfig B
  ↓
run_simulation(config_A)
run_simulation(config_B)
  ↓
ComparisonResult
  ↓
Comparison Summary + A/B Graphs + Output Comparison
```


---

# Phase 5で作るもの

## 主な成果物

```text
core/comparison.py
tests/test_comparison.py

app/ui/compare_workflow.py
app/ui/comparison_summary.py
app/ui/comparison_drawers.py
```

既存UI構成に合わせるなら、ファイル名は多少変えて構いません。

---

# Phase 5でやること

## 1. Comparison用データ構造を作る

まず、UIより先に core 側で比較結果を標準化します。

推奨構造：

```python
@dataclass
class ComparisonConfig:
    circuit: CircuitConfig
    environment_a: EnvironmentConfig
    environment_b: EnvironmentConfig
    duration_us: float
    time_steps: int
    fidelity_threshold: float
    model: str = "weak_coupling_lindblad"
    label_a: str = "Condition A"
    label_b: str = "Condition B"
```

```python
@dataclass
class ComparisonResult:
    config: ComparisonConfig
    result_a: SimulationResult
    result_b: SimulationResult
    delta_final_fidelity: float | None
    delta_final_purity: float | None
    delta_effective_operation_time_us: float | None
    better_condition: str | None
    warnings: list
```

## 2. `run_comparison(config)` を作る

```python
def run_comparison(config: ComparisonConfig) -> ComparisonResult:
    ...
```

内部では、既存の `run_simulation(config)` を2回呼びます。

```text
SimulationConfig A → run_simulation
SimulationConfig B → run_simulation
```

重要なのは、**比較専用の物理計算を作らないこと**です。
既存の安定した `run_simulation(config)` を使い、比較はその上の集約層として実装します。

---

# Phase 5のスコープ

## 必達

```text
Low noise vs High noise comparison
同一回路・異なる環境条件の比較
State Fidelity A/B グラフ
Purity A/B グラフ
ΔFinal State Fidelity
ΔFinal Purity
ΔEffective Operation Time
Better Condition
```

## 標準目標

```text
Output Probability: Ideal vs A vs B
Condition Details Drawer
A/B warnings統合
Beginner + Compare UI
```

## 後回し

```text
Expert + Compare
T1/T2/gamma A/B差分
density matrix difference
多条件比較 A/B/C
パラメータスイープ
QuTiP backend比較
```

Expert表示時の T1/T2/gamma 差分は、Phase 6 Expert Inspector 以降でよいです。

---

# 実装順序

## Step 0: 作業前確認

```powershell
cd C:\Users\oshad\Quantum-sim
(Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned) ; (& .\.venv\Scripts\Activate.ps1)

git status
python -m unittest discover -s tests
```

Phase 4がcommit済みなら、Phase 5用ブランチを作ります。

```powershell
git checkout -b phase5-compare-workflow
```

---

## Step 1: `core/comparison.py` を作る

### 目的

比較処理をUIから切り離し、core側で再利用可能にする。

### 実装するもの

```text
ComparisonConfig
ComparisonResult
run_comparison(config)
```

### 受け入れ条件

```text
同一回路でCondition A/Bを実行できる
result_a, result_b が SimulationResult として返る
delta_final_fidelity が計算される
delta_final_purity が計算される
delta_effective_operation_time_us が計算される
better_condition が判定される
```

---

## Step 2: Low / High noise プリセット比較を作る

BeginnerのCompareでは、自由設定よりも先に **Low noise vs High noise** を固定デモとして作ります。

例：

```python
LOW_NOISE_ENV = EnvironmentConfig(
    mode="normalized",
    temperature=0.1,
    magnetic_field=0.1,
    noise_level=0.1,
)

HIGH_NOISE_ENV = EnvironmentConfig(
    mode="normalized",
    temperature=0.8,
    magnetic_field=0.8,
    noise_level=0.8,
)
```

値は既存MVPや現在のプリセットに合わせて調整して構いません。

---

## Step 3: `tests/test_comparison.py` を追加する

最低限のテスト：

```text
1-qubit H回路でComparisonConfigを作れる
run_comparison(config)がComparisonResultを返す
result_a/result_bが空でない
delta_final_fidelityが計算される
delta_final_purityが計算される
delta_effective_operation_time_usが計算される
High noiseの方がfinal fidelityが低い、またはeffective timeが短い傾向を確認する
異なる回路同士は比較対象外
```

ただし、数値モデルによって差が小さい場合があるため、最初のテストでは「必ずHigh noiseが低い」と断定しすぎない方が安全です。
まずは `delta_*` が `None` でなく計算されることを優先し、安定してから単調性テストを入れます。

---

## Step 4: Beginner UIに Compare toggle を追加する

Phase 4のBeginner画面に、以下を追加します。

```text
Workflow:
  Single Run
  Compare A/B
```

またはボタンでよいです。

```text
[Run Simulation]
[Compare Low vs High Noise]
```

UI要件では、Compare Workflow は Beginner + Compare と Expert + Compare の両方を許容する構成ですが、Phase 5ではまず **Beginner + Compare** を対象にします。

---

## Step 5: Comparison Summary を表示する

表示項目：

```text
ΔFinal State Fidelity
ΔFinal Purity
ΔEffective Operation Time
Better Condition
ΔOutput Probability Distance
```

Phase 5初期では `ΔOutput Probability Distance` が未実装なら、`not available` 表示でよいです。

例：

```text
ΔFinal State Fidelity: -0.052
ΔFinal Purity: -0.084
ΔEffective Operation Time: -3.6 μs
Better Condition: Low noise
```

---

## Step 6: Comparison Graphs Drawerを作る

Drawer構成：

```text
Comparison Summary: 常時表示
Comparison Graphs: 初期展開
Output Probabilities: 初期折りたたみ
Condition Details: 初期折りたたみ
```

UI要件でも、Compare時は Comparison Summary を表示し、Comparison Graphs を開き、Output Probabilities と Condition Details は閉じる設計です。

### Graphs Drawer

必須：

```text
State Fidelity A/B over Time
Purity A/B over Time
```

A/Bを同じ軸に重ねます。

### Condition Details Drawer

表示：

```text
Condition A:
  temperature
  magnetic_field
  noise_level

Condition B:
  temperature
  magnetic_field
  noise_level
```

---

# Codexに渡す指示文

```text
Task:
Implement Phase 5: Compare Workflow.

Goal:
Add a comparison workflow that runs the same circuit under two different environment conditions and displays A/B differences in Beginner Mode.

Required changes:

1. Add core/comparison.py
   - Define ComparisonConfig
   - Define ComparisonResult
   - Implement run_comparison(config: ComparisonConfig) -> ComparisonResult
   - Internally call run_simulation(config_a) and run_simulation(config_b)
   - Do not implement separate physics logic for comparison

2. ComparisonConfig:
   - circuit: CircuitConfig
   - environment_a: EnvironmentConfig
   - environment_b: EnvironmentConfig
   - duration_us
   - time_steps
   - fidelity_threshold
   - model
   - label_a
   - label_b

3. ComparisonResult:
   - config
   - result_a
   - result_b
   - delta_final_fidelity
   - delta_final_purity
   - delta_effective_operation_time_us
   - better_condition
   - warnings

4. Add tests:
   - tests/test_comparison.py
   - Test that a 1-qubit H circuit can run with Low noise vs High noise
   - Test that result_a and result_b are SimulationResult
   - Test that delta values are calculated
   - Test that warnings from both results are collected

5. Update Beginner UI:
   - Add Compare A/B workflow or Compare Low vs High Noise button
   - Build ComparisonConfig from current CircuitState
   - Use current circuit for both conditions
   - Use Low noise and High noise presets for first implementation
   - Display Comparison Summary
   - Add Comparison Graphs drawer
   - Add Output Probabilities drawer placeholder if needed
   - Add Condition Details drawer

6. Comparison Summary must show:
   - ΔFinal State Fidelity
   - ΔFinal Purity
   - ΔEffective Operation Time
   - Better Condition
   - ΔOutput Probability Distance if available, otherwise "not available"

7. Comparison Graphs:
   - Show State Fidelity A/B over time
   - Show Purity A/B over time
   - Use the same time axis when possible

Acceptance criteria:
- Existing tests still pass
- New comparison tests pass
- UI still starts
- Beginner Mode can run Single Run
- Beginner Mode can run Low noise vs High noise comparison
- Comparison Summary displays delta values
- Comparison Graphs display A/B fidelity and purity
- Condition Details drawer shows A/B environment values
- No Streamlit imports are added to core
- No physics model is changed

Constraints:
- Do not change physics model
- Do not change environment-to-T1/T2 mapping
- Do not change T1/T2-to-gamma mapping
- Do not change Lindblad evolution
- Do not change fidelity or purity definitions
- Do not implement Expert Inspector
- Do not implement Save/Load backend
- Do not implement QuTiP, Rust, FastAPI, or Godot
- Do not add external dependencies
```

---

# 実装後の確認手順

## 1. テスト

```powershell
python -m unittest discover -s tests
```

個別：

```powershell
python -m tests.test_comparison
python -m tests.test_run_simulation_api
python -m tests.test_validation
```

## 2. pre-commit

```powershell
git add -A
python -m pre_commit run --all-files
git add -A
python -m pre_commit run --all-files
```

## 3. 起動確認

```powershell
streamlit run app/app.py
```

## 4. 手動確認

```text
Start Screenが表示される
Beginner Modeに入れる
Hゲートを配置できる
Single Runが動く
Compare Low vs High Noiseが動く
Comparison Summaryが表示される
Fidelity A/Bグラフが表示される
Purity A/Bグラフが表示される
Condition Detailsが表示される
エラーが出ない
```

---

# Phase 5完了条件

```md
## Phase 5 Checklist

### Core

- [ ] core/comparison.py がある
- [ ] ComparisonConfig がある
- [ ] ComparisonResult がある
- [ ] run_comparison(config) がある
- [ ] run_comparison は run_simulation を内部で使う
- [ ] 比較専用の物理計算を新規実装していない

### Comparison Metrics

- [ ] delta_final_fidelity が計算される
- [ ] delta_final_purity が計算される
- [ ] delta_effective_operation_time_us が計算される
- [ ] better_condition が判定される
- [ ] warnings が統合される

### UI

- [ ] Beginner ModeにCompare操作がある
- [ ] Low noise vs High noise比較を実行できる
- [ ] Comparison Summaryが表示される
- [ ] Comparison Graphs Drawerがある
- [ ] Output Probabilities Drawerがある
- [ ] Condition Details Drawerがある

### Graphs

- [ ] State Fidelity A/B over Time が表示される
- [ ] Purity A/B over Time が表示される
- [ ] A/Bが同じ時間軸で表示される

### Tests

- [ ] 既存テストが通る
- [ ] tests/test_comparison.py が通る

### Safety

- [ ] coreにStreamlit依存がない
- [ ] 物理モデルを変更していない
- [ ] 外部依存を追加していない
- [ ] Save/LoadやExpertを勝手に実装していない
```

---
