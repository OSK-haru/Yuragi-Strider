
> **Historical Streamlit-era implementation plan**
>
> The former Streamlit Expert UI is no longer active. Current model and
> diagnostic details are exposed through the React result drawers and State
> Explorer. Treat all `app/` paths below as obsolete.




---

# Phase 6: Expert Mode / Expert Inspector

## 目的

Phase 6の目的は、Beginner / Compare で動いているシミュレーション結果に対して、上級者・審査員向けの詳細情報を表示することです。

このPhaseでは、以下を実現します。

```text
現在の回路
  ↓
現在の環境条件
  ↓
SimulationResult
  ↓
Expert Inspector
  ↓
T1/T2, gamma, Lindblad operators, density matrix, diagnostics, assumptions
```

重要なのは、**Expert表示の値が実際の `SimulationResult` や `derived_parameters` と一致していること**です。F10でも「UI上の値とシミュレーション結果が一致している必要がある」と定義されています。

---

# Phase 6でやること

## 必達

```text
1. Expert Modeへ切り替え可能にする
2. 右側または折りたたみ式の Expert Inspector を作る
3. Overview / Noise / State / Assumptions を表示する
4. T1 / T2 を表示する
5. gamma1 / gammaphi を表示する
6. dominant decoherence source を表示する
7. final fidelity / final purity / effective operation time を表示する
8. モデル仮定・制約を表示する
9. 1〜2量子ビットの density matrix / output probabilities を表示する
10. trace / Hermiticity / minimum eigenvalue を表示する
```

## 標準目標

```text
1. Lindblad operators を折りたたみ表示
2. collapse operator matrix を表示
3. 検索ボックス
4. カテゴリフィルタ
5. Compare時のT1/T2/gamma A/B表示
```

## 後回し

```text
1. H_eff の本格実装
2. H_eff 固有値表示
3. no-jump発展との比較
4. reduced density matrix
5. QuTiP backend
6. 強結合開放系
7. Circuit QED inspired profile
```

H_effは表示枠だけ置き、未実装なら `not enabled` でよいです。UI要件でも H_eff は Plus/Expert拡張として扱い、未実装なら `not implemented` または `not enabled` と表示する方針です。

---

# 推奨ファイル構成

```text
app/
  ui/
    expert_mode.py
    expert_inspector.py
    expert_overview.py
    expert_noise.py
    expert_operators.py
    expert_state.py
    expert_assumptions.py

core/
  expert_data.py          # 推奨
  diagnostics.py          # 既存validationと分けてもよい
  operator_export.py      # 必要なら
```

ただし、最初は `app/ui/expert_inspector.py` と `core/expert_data.py` の2つだけでも十分です。

---

# Step 0: 作業前確認

```powershell
cd C:\Users\oshad\Quantum-sim
(Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned) ; (& .\.venv\Scripts\Activate.ps1)

git status
python -m unittest discover -s tests
python -m pre_commit run --all-files
```

Phase 5.5の修正がcommit済みなら、Phase 6用ブランチを作ります。

```powershell
git checkout -b phase6-expert-inspector
```

---

# Step 1: Expert用データ集約関数を作る

UIで直接 `result.derived_parameters` をバラバラに読むより、`core/expert_data.py` に集約関数を作る方が安全です。

## 作る関数

```python
def build_expert_inspector_data(result: SimulationResult) -> dict:
    ...
```

返す構造の例：

```python
{
    "overview": {
        "model": "weak_coupling_lindblad",
        "logical_qubits": 2,
        "hilbert_space_dimension": 4,
        "density_matrix_shape": [4, 4],
        "gate_count": 2,
        "circuit_depth": 2,
        "simulation_time_us": 20.0,
        "time_steps": 200,
        "final_fidelity": 0.91,
        "final_purity": 0.88,
        "effective_operation_time_us": 15.0,
    },
    "noise": {
        "temperature_parameter": 0.1,
        "magnetic_field_parameter": 0.1,
        "noise_level": 0.8,
        "T1_us": 12.5,
        "T2_us": 8.3,
        "gamma1_per_us": 0.08,
        "gammaphi_per_us": 0.11,
        "dominant_decoherence_source": "Dephasing",
    },
    "state": {
        "trace": 1.0,
        "hermiticity_error": 2.1e-12,
        "minimum_eigenvalue": -1.0e-10,
        "purity": 0.88,
        "output_probabilities": {"00": 0.48, "11": 0.47},
    },
    "assumptions": [
        "weak-coupling open quantum system",
        "Born-Markov approximation",
        "Lindblad-type master equation",
        "phenomenological T1/T2 noise",
        "normalized environment parameters",
        "no strict hardware calibration",
        "no strong-coupling memory effects",
    ],
}
```

この構造にしておくと、Streamlit UIだけでなく将来の保存/出力やGodot/FastAPIにも転用できます。

---

# Step 2: Expert Mode画面を作る

UI要件上、Expert Modeは以下の構成です。

```text
Left Panel: Environment / Simulation Settings
Main Workspace: Circuit + Results
Right Panel: Expert Inspector
```

ただし、Streamlitで最初から3カラムを凝りすぎる必要はありません。まずは以下でよいです。

```text
左: Environment + Simulation Settings
中央: Circuit Editor + Result Graphs
右: Expert Inspector
```

Expert Modeでも Beginnerと同じ回路編集・実行結果を使います。違いは、**右側の詳細情報が増えること**です。UI要件でも、Expert ModeのMain WorkspaceはBeginnerと同様に回路編集を行い、追加でHilbert space dimensionやModel nameなどを表示してよいとされています。

---

# Step 3: Expert Inspector タブを作る

## 初期タブ

```text
Overview
Noise
Operators
State
Assumptions
```

UI要件でこのタブ構成が指定されています。

---

## Overview タブ

表示：

```text
Model
Logical qubits
Hilbert space dimension
Density matrix size
Gate count
Circuit depth
Simulation time
Time steps
Final State Fidelity
Final Purity
Effective Operation Time
```

F10のE01でも、論理量子ビット数、Hilbert空間次元、密度行列サイズ、ゲート数、モデル、時間ステップ数、シミュレーション時間が対象です。

---

## Noise タブ

表示：

```text
Temperature parameter
Magnetic field parameter
Noise level
T1 relaxation time
T2 dephasing time
gamma1
gammaphi
gamma ratio
Dominant decoherence source
```

F10では、環境条件から導出されたT1/T2と、T1/T2から導かれるgamma1/gammaphiを表示することが要求されています。

---

## Operators タブ

表示：

```text
Lindblad operators
Collapse operators
Relaxation operator
Pure dephasing operator
Target qubit
Enabled / disabled
Operator matrix
```

行列は最初から全面表示しないでください。折りたたみ表示にします。UI要件でも Operators タブでは行列を折りたたみ、大きい行列はスクロール可能にする方針です。

初期実装で `SimulationResult` にcollapse operator matrixが入っていない場合は、次のどちらかにします。

```text
A. core側で再構成して表示する
B. "not available in current result" と表示する
```

推奨は A です。ただし、物理係数は既存実装から取り、勝手に変更しないでください。

---

## State タブ

表示：

```text
Density matrix
Re(ρ)
Im(ρ)
|ρ|
Trace
Hermiticity error
Minimum eigenvalue
Maximum eigenvalue
Purity
Output probabilities
```

F10のE06では density matrix、Re(ρ)、Im(ρ)、|ρ|、trace、Hermiticity error、minimum eigenvalue、final purity、final fidelity、output probability distribution が対象です。

もし現時点で `SimulationResult` に final density matrix がない場合は、Phase 6で追加してください。
ただし、JSON-safeを守るなら、外部公開用には以下の形式にします。

```python
"final_density_matrix_real": [[...]],
"final_density_matrix_imag": [[...]]
```

---

## Assumptions タブ

表示：

```text
weak-coupling open quantum system
Born-Markov approximation
Lindblad-type master equation
phenomenological T1/T2 noise
normalized environment parameters
no strict hardware calibration
no strong-coupling memory effects
no pulse-level control
not a research-grade full simulator
```

これはF10のE07にそのまま対応します。

また、非機能要件N03でも、弱結合開放系、Born-Markov近似、Lindblad型時間発展、正規化パラメータ、厳密な実機再現ではないことを明示する方針になっています。

---

# Step 4: 検索・フィルタを追加する

Phase 6の標準目標として、検索ボックスを追加します。

## 検索対象

```text
T1
T2
gamma1
gammaphi
fidelity
purity
Lindblad
collapse operator
relaxation
dephasing
density matrix
Hamiltonian
H_eff
threshold
trace
Hermiticity
approximation
limitation
```

F10でも検索対象としてこれらが定義されています。

最初は高度な全文検索でなくてよいです。

```text
search query に一致する項目だけ表示
```

で十分です。

---

# Step 5: Compare + Expert の最低限対応

Phase 5でCompareが入っているので、Expert側でも比較詳細を見られると強いです。

ただし、Phase 6の必達ではなく、軽く対応でよいです。

## 表示候補

```text
Condition A:
  T1
  T2
  gamma1
  gammaphi
  dominant source

Condition B:
  T1
  T2
  gamma1
  gammaphi
  dominant source

Delta:
  ΔT1
  ΔT2
  Δgamma1
  Δgammaphi
```

UI要件でも Expert + Compare では T1/T2 A/B、gamma1/gammaphi A/B、dominant source A/B、density matrix difference、model parameter difference が対象です。

Phase 6では、density matrix difference は後回しでよいです。

---

# Codex指示文

```text
Task:
Implement Phase 6: Expert Mode and Expert Inspector.

Goal:
Add an Expert Mode that displays detailed physical quantities, numerical diagnostics, model assumptions, and internal simulation information for the current circuit and environment. The implementation must reuse existing SimulationResult data and must not change the physics model.

Required changes:

1. Expert Mode UI
   - Add an Expert Mode option if not already present
   - Expert Mode should reuse the current circuit editor and simulation workflow
   - Add a right-side or expandable Expert Inspector panel
   - Keep Beginner Mode unchanged

2. Expert Inspector tabs
   Implement the following tabs:
   - Overview
   - Noise
   - Operators
   - State
   - Assumptions

3. Overview tab
   Display:
   - Model
   - Logical qubits
   - Hilbert space dimension
   - Density matrix size
   - Gate count
   - Circuit depth
   - Simulation time
   - Time steps
   - Final State Fidelity
   - Final Purity
   - Effective Operation Time

4. Noise tab
   Display:
   - Temperature parameter
   - Magnetic field parameter
   - Noise level
   - T1 relaxation time
   - T2 dephasing time
   - gamma1
   - gammaphi
   - gamma ratio
   - Dominant decoherence source

5. Operators tab
   Display:
   - Lindblad operators if available
   - Collapse operators if available
   - Relaxation operator
   - Pure dephasing operator
   - Target qubit
   - Enabled/disabled
   - Operator matrix in collapsed/expandable form
   If operator matrices are not available in SimulationResult, reconstruct them using existing core conventions without changing physical coefficients. If reconstruction is not safe, show "not available in current result".

6. State tab
   Display:
   - Final density matrix if available
   - Re(rho)
   - Im(rho)
   - |rho|
   - Trace
   - Hermiticity error
   - Minimum eigenvalue
   - Maximum eigenvalue
   - Final purity
   - Final state fidelity
   - Output probability distribution

7. Assumptions tab
   Display:
   - weak-coupling open quantum system
   - Born-Markov approximation
   - Lindblad-type master equation
   - phenomenological T1/T2 noise
   - normalized environment parameters
   - no strict hardware calibration
   - no strong-coupling memory effects
   - no pulse-level control
   - not a research-grade full simulator

8. Expert data aggregation
   - Add core/expert_data.py or similar
   - Implement build_expert_inspector_data(result)
   - Return JSON-safe dictionaries where possible
   - Do not expose numpy arrays directly to UI if avoidable
   - Keep core independent from Streamlit

9. Search/filter
   - Add a simple search box in Expert Inspector
   - Search should filter displayed expert fields by keyword
   - Keywords include T1, T2, gamma, fidelity, purity, Lindblad, density matrix, trace, Hermiticity, approximation, limitation

10. H_eff handling
   - Do not implement full no-jump simulation in this phase
   - If H_eff is not implemented, show "not enabled"
   - Add note that H_eff/no-jump evolution is distinct from Lindblad ensemble-averaged evolution

11. Compare integration
   - If current result is a ComparisonResult, show A/B expert summary if straightforward
   - At minimum show T1/T2/gamma for Condition A and B if available
   - Density matrix difference may be deferred

Acceptance criteria:
   - Existing tests pass
   - Beginner Mode still works
   - Compare Workflow still works
   - Expert Mode can be selected
   - Expert Inspector is visible in Expert Mode
   - Overview tab displays current simulation metadata
   - Noise tab displays T1/T2/gamma values
   - State tab displays diagnostics and output probabilities
   - Assumptions tab displays model assumptions and limitations
   - Operators tab displays Lindblad/collapse info or clearly says not available
   - H_eff is shown as not enabled if not implemented
   - Search/filter works at least for visible field labels
   - No Streamlit imports are added to core
   - No physics coefficients are changed

Constraints:
   - Do not change environment-to-T1/T2 mapping
   - Do not change T1/T2-to-gamma mapping
   - Do not change Lindblad evolution
   - Do not change fidelity or purity definitions
   - Do not add external dependencies
   - Do not implement Save/Load backend
   - Do not implement QuTiP, Rust, FastAPI, or Godot
   - Do not implement strong-coupling open system
   - Do not implement full no-jump trajectory simulation
```

---

# 追加テスト

```text
tests/test_expert_data.py
tests/test_expert_inspector_smoke.py  # UIテストが難しければ省略可
```

## `test_expert_data.py`

確認項目：

```text
1-qubit H結果からexpert dataを作れる
2-qubit Bell結果からexpert dataを作れる
overview.logical_qubits が正しい
overview.hilbert_space_dimension が 2^n になる
noise.T1_us / T2_us / gamma1 / gammaphi が存在する
state.trace が存在する
state.output_probabilities が存在する
assumptions が空でない
```

---

# 手動確認

```powershell
python -m unittest discover -s tests
python -m pre_commit run --all-files
streamlit run app/app.py
```

UIで確認：

```text
1. Beginner Mode が壊れていない
2. Compare Workflow が壊れていない
3. Expert Mode に切り替えられる
4. 1-qubit HでExpert Inspectorが表示される
5. X/Z/Measureでも値が変に壊れない
6. 2-qubit BellでHilbert dimension = 4 と出る
7. NoiseタブにT1/T2/gammaが出る
8. Stateタブにtrace/Hermiticity/output probabilitiesが出る
9. Assumptionsタブにモデル限界が出る
10. H_effが未実装ならnot enabledと出る
```

---

# Phase 6完了条件

```md
## Phase 6 Checklist

### Expert Mode

- [ ] Expert Modeを選択できる
- [ ] Expert Modeで通常の回路実行ができる
- [ ] Expert ModeでCompare結果を壊さない
- [ ] Beginner Modeが壊れていない

### Expert Inspector

- [ ] Overviewタブがある
- [ ] Noiseタブがある
- [ ] Operatorsタブがある
- [ ] Stateタブがある
- [ ] Assumptionsタブがある
- [ ] 初期表示が過密でない

### Overview

- [ ] Modelを表示できる
- [ ] Logical qubitsを表示できる
- [ ] Hilbert space dimensionを表示できる
- [ ] Density matrix sizeを表示できる
- [ ] Gate countを表示できる
- [ ] Circuit depthを表示できる
- [ ] Final Fidelity / Purity / Effective Timeを表示できる

### Noise

- [ ] Temperature parameterを表示できる
- [ ] Magnetic field parameterを表示できる
- [ ] Noise levelを表示できる
- [ ] T1を表示できる
- [ ] T2を表示できる
- [ ] gamma1を表示できる
- [ ] gammaphiを表示できる
- [ ] dominant decoherence sourceを表示できる

### Operators

- [ ] Lindblad operatorsを表示またはnot available表示できる
- [ ] relaxation operatorを表示またはnot available表示できる
- [ ] pure dephasing operatorを表示またはnot available表示できる
- [ ] operator matrixは折りたたみ表示になっている
- [ ] H_effは未実装ならnot enabledと表示する

### State

- [ ] density matrixを表示またはnot available表示できる
- [ ] Re(ρ), Im(ρ), |ρ| を表示できる
- [ ] traceを表示できる
- [ ] Hermiticity errorを表示できる
- [ ] minimum eigenvalueを表示できる
- [ ] output probabilitiesを表示できる

### Assumptions

- [ ] weak-coupling open quantum systemを表示
- [ ] Born-Markov approximationを表示
- [ ] Lindblad-type master equationを表示
- [ ] phenomenological T1/T2 noiseを表示
- [ ] normalized environment parametersを表示
- [ ] no strict hardware calibrationを表示
- [ ] no strong-coupling memory effectsを表示
- [ ] not a research-grade full simulatorを表示

### Search / Filter

- [ ] T1で検索できる
- [ ] gammaで検索できる
- [ ] Lindbladで検索できる
- [ ] density matrixで検索できる
- [ ] approximationまたはlimitationで検索できる

### Safety

- [ ] coreにStreamlit依存がない
- [ ] 物理モデルを変更していない
- [ ] 外部依存を追加していない
- [ ] 既存テストが通る
```

---

# Phase 6での注意点

## 1. Expertだからといって物理モデルを増やさない

Phase 6は **表示フェーズ** です。
新しい物理を追加するフェーズではありません。

## 2. H_effは慎重に扱う

H_effは理論的には魅力的ですが、Lindblad平均発展と no-jump 条件付き発展は別物です。資料でも、連続測定下の量子トラジェクトリーと、測定結果を平均したLindbladマスター方程式は質的に異なると説明されています。
そのため、Phase 6では `not enabled` と明示し、説明だけ置くのが安全です。

## 3. 画面を過密にしない

Expert Modeは「全部を一画面に出す」ではなく、「必要な情報へ到達できる」画面です。UI要件でも、Expert Modeではすべてを常時表示せず、タブ・折りたたみ・引き出しで段階的に表示する方針です。

---

# Phase 6 commit

```powershell
git status
git add -A
python -m pre_commit run --all-files
git add -A
python -m unittest discover -s tests
git commit -m "Add Expert Mode inspector"
```

---



---
