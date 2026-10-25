
> **Historical analysis plan**
>
> Current complexity documentation is
> `docs_for_develop/architecture/complexity.md`. Statements below about future
> 3-4 qubit support predate the implemented 1-18 qubit core/API (noisy density
> matrix up to 8, explicit CPTP up to 5).

---

# Phase 7.8: Computational Complexity Analysis

## 目的

```text
1. 現在の計算量を数式で把握する
2. 何量子ビットまで現実的かを決める
3. どこがボトルネックかを特定する
4. Plus Requirements の Sweep / React / Algorithm Presets / 3〜6 qubits の前提を固める
```

---

# 1. 基本記号

量子ビット数を

[
n
]

Hilbert空間次元を

[
d = 2^n
]

密度行列サイズを

[
d \times d
]

とします。

したがって、密度行列の要素数は

[
d^2 = 4^n
]

です。

---

# 2. メモリ計算量

密度行列1個のメモリは、要素数で見ると

# [
O(d^2)

O(4^n)
]

です。

現在の実装では、simulation中に `states` と `ideal_states` を list として保持しています。
時系列点数を (T) とすると、状態保存のメモリは概ね

# [
O(Td^2)

O(T4^n)
]

です。

ただし、最終結果として必要なのは多くの場合、

```text
times
fidelity
purity
output_probabilities
derived_parameters
```

だけです。`SimulationResult` もそれらを保持する構造です。
したがって、内部実装を streaming 化すれば、状態保存メモリは

[
O(d^2)
]

まで削れます。

---

# 3. ゲート適用の計算量

現在のゲート適用は、

[
\rho \mapsto U\rho U^\dagger
]

です。

これは dense matrix multiplication を2回行います。

[
U\rho
]

[
(U\rho)U^\dagger
]

1回の dense matrix multiplication は

[
O(d^3)
]

なので、1ゲート適用は

# [
O(d^3)

O(8^n)
]

です。

`apply_gate_operation()` は、1量子ビットゲートや CNOT を全Hilbert空間上の行列に展開してから、`apply_unitary_to_density()` で (U\rho U^\dagger) を適用しています。

---

# 4. Lindblad RHS の計算量

Lindblad 方程式は、

# [
\frac{d\rho}{dt}

-i[H,\rho]
+
\sum_k
\mathcal{D}[L_k]\rho
]

です。

散逸項は、

# [
\mathcal{D}[L]\rho

## L\rho L^\dagger

\frac{1}{2}
(L^\dagger L\rho+\rho L^\dagger L)
]

です。

1つの collapse operator につき、dense matrix multiplication が複数回走ります。

量子ビットごとに、

[
L_{\downarrow,j},\quad L_{\uparrow,j},\quad L_{\phi,j}
]

を作るので、collapse operator 数は最大で

[
m = 3n
]

です。

したがって、Lindblad RHS 1回の計算量は概ね

# [
O(m d^3)

# O(n d^3)

O(n8^n)
]

です。

RK4 は RHS を4回呼びます。`rk4_step()` も (k_1,k_2,k_3,k_4) を計算しています。

したがって、RK4 1 step は

[
O(4n8^n)
]

定数を無視すると、

[
O(n8^n)
]

です。

---

# 5. Gate-aware Phase 7.7 の計算量

Phase 7.7 では、columnごとに

# [
\frac{d\rho}{dt}

-i[H_k,\rho]
+
\sum_j\mathcal{D}[L_j]\rho
]

を解きます。

column数を

[
C
]

各columnの内部substep数を

[
s_k
]

とすると、全体計算量は概ね

[
O\left(
\sum_{k=1}^{C}
s_k \cdot n8^n
\right)
]

です。

さらに idle evolution があるなら、idle区間のsubstep数を (s_{\mathrm{idle}}) として、

[
O\left(
(C_{\mathrm{eff}} + s_{\mathrm{idle}}) n8^n
\right)
]

に近い形になります。

より正確には、

[
O\left(
S_{\mathrm{total}} \cdot n8^n
\right)
]

です。

ここで、

# [
S_{\mathrm{total}}

\sum_k s_k + s_{\mathrm{idle}}
]

です。

---

# 6. なぜ急に重くなるか

最大の理由は、状態ベクトルではなく **密度行列**を使っているためです。

状態ベクトルならサイズは

[
O(d)=O(2^n)
]

ですが、密度行列は

[
O(d^2)=O(4^n)
]

です。

さらに、dense matrix multiplication が入るので、

[
O(d^3)=O(8^n)
]

になります。

つまり、量子ビット数が1増えると、

```text
状態サイズ: 約4倍
dense matmul計算量: 約8倍
```

になります。

これは指数関数の木霊です。増え方がかわいくありません。

---

# 7. 目安表

|qubits (n)|(d=2^n)|density entries (d^2)|dense matmul scale (d^3)|評価|
|--:|--:|--:|--:|---|
|1|2|4|8|余裕|
|2|4|16|64|余裕|
|3|8|64|512|まだ軽い|
|4|16|256|4,096|Python tuple実装だと重くなり始める|
|5|32|1,024|32,768|Sweepでは注意|
|6|64|4,096|262,144|dense Pythonではかなり重い|
|7|128|16,384|2,097,152|現実的でない可能性が高い|
|8|256|65,536|16,777,216|非推奨|

理論上は6量子ビットでも行けそうに見えますが、現在の実装は NumPy 配列ではなく Python の tuple-of-tuples ベースです。
そのため、実効速度は理論的な dense linear algebra ライブラリよりかなり遅くなります。

---

# 8. 現在のボトルネック候補

優先度順に見ると、以下です。

|優先度|ボトルネック|理由|
|--:|---|---|
|1|`matmul()`|dense (O(d^3))、Pythonループ|
|2|Lindblad dissipator|collapse operatorごとに複数回 matmul|
|3|RK4|RHSを4回呼ぶ|
|4|`states` / `ideal_states` 保存|(O(Td^2)) メモリ|
|5|gate unitary expansion|columnごとに dense (d\times d) 行列を作る|
|6|Expert density reconstruction|結果後に再計算する可能性がある|

`expert_data.py` では final density matrix を再構成する処理もあります。これは便利ですが、重いケースでは追加コストになります。

---

# 9. 解析すべきメトリクス

Phase 7.8 では、以下を測るべきです。

```text
- qubits
- Hilbert dimension
- density matrix dimension
- number of columns
- number of gates
- number of collapse operators
- total gate duration
- idle duration
- total recorded time points
- integration substeps
- wall-clock runtime
- peak memory estimate
- final fidelity/purity
```

既存 `diagnostics` には `integration_substeps` などがあります。
ここを拡張するのが良いです。

---

# 10. 追加すべき complexity diagnostics

`SimulationResult.diagnostics` に以下を追加すると良いです。

```text
hilbert_dimension
density_matrix_entries
estimated_dense_matmul_cost
collapse_operator_count
recorded_state_count
estimated_state_memory_entries
total_rhs_evaluations
total_rk4_steps
total_gate_columns
total_gate_count
```

例えば：

[
d = 2^n
]

[
\text{density_matrix_entries}=d^2
]

[
\text{dense_matmul_cost}=d^3
]

[
\text{collapse_operator_count}=3n
]

[
\text{rhs_evaluations}=4\times \text{rk4_steps}
]

---

# 11. 実装方針

## Step 1: 理論計算量の関数を追加

```python
def estimate_complexity(config, diagnostics=None) -> dict[str, float]:
    ...
```

返すもの：

```text
n_qubits
hilbert_dimension
density_matrix_entries
dense_matmul_scale
collapse_operator_count
estimated_rhs_evaluations
estimated_rk4_steps
estimated_state_storage_entries
```

---

## Step 2: 実測プロファイルを追加

Python標準だけでやるなら、

```python
import time
import tracemalloc
```

を使えます。

```python
start = time.perf_counter()
tracemalloc.start()

result = run_simulation(config)

current, peak = tracemalloc.get_traced_memory()
elapsed = time.perf_counter() - start
tracemalloc.stop()
```

外部依存なしでできます。

---

## Step 3: benchmark script を作る

```text
scripts/benchmark_complexity.py
```

測るケース：

```text
1 qubit: H
2 qubit: Bell
3 qubit: GHZ-like
4 qubit: shallow circuit
5 qubit: shallow circuit
6 qubit: shallow circuit
```

ただし現在の runtime guard が 2 qubits までなら、まずは 1〜2 qubits だけでよいです。`simulator.py` には現在 2 logical qubits までの runtime check があります。

---

# 12. Codex 指示文

```text
Task:
Implement Phase 7.8: Computational Complexity Analysis for Yuragi-Strider.

Goal:
Add theoretical and empirical complexity diagnostics for the current dense density-matrix Lindblad simulator. The purpose is to understand scaling before moving to Plus Requirements such as parameter sweeps, React UI, algorithm presets, and 3-6 qubit experimental support.

Background:
The simulator uses explicit density matrices of size d x d where d = 2^n. Gate application uses dense U rho U† operations. Lindblad evolution uses collapse operators and RK4. Therefore memory scales as O(4^n), and dense matrix operations scale approximately as O(8^n). Gate-aware Lindblad simulation also multiplies this by the number of RK4/substeps and circuit columns.

Required changes:

1. Add complexity estimation helper
   Create a module, for example:
     core/complexity.py

   Add:
     estimate_simulation_complexity(config, diagnostics=None, derived_parameters=None) -> dict[str, float]

   It should compute:
     - logical_qubits
     - hilbert_dimension = 2 ** n
     - density_matrix_entries = d ** 2
     - dense_matmul_scale = d ** 3
     - collapse_operator_count estimate:
         3 * n for unified physical rates
     - circuit_column_count
     - gate_count
     - configured_time_steps
     - estimated_recorded_state_count
     - estimated_state_storage_entries
     - estimated_rhs_evaluations
     - estimated_rk4_steps
     - estimated_matmul_dominant_work_units

2. Integrate complexity diagnostics
   Add these values to SimulationResult.diagnostics or derived_parameters under a clear prefix:
     complexity_hilbert_dimension
     complexity_density_matrix_entries
     complexity_dense_matmul_scale
     complexity_collapse_operator_count
     complexity_estimated_rhs_evaluations
     complexity_estimated_rk4_steps
     complexity_estimated_work_units

   Do not break existing diagnostics.

3. Add optional runtime profiling helper
   Add a script:
     scripts/benchmark_complexity.py

   Use only standard library:
     time.perf_counter
     tracemalloc

   It should run representative configs and print:
     - qubits
     - gates
     - columns
     - time_steps
     - duration_us
     - wall_time_seconds
     - peak_memory_bytes
     - final_fidelity
     - final_purity
     - estimated_work_units

4. Add benchmark cases
   Start with supported cases:
     - 1Q H
     - 1Q HX
     - 2Q Bell
     - 2Q Bell with longer CNOT duration
   If runtime support allows later:
     - 3Q GHZ-like
     - 4Q shallow
     - 5Q shallow
     - 6Q shallow

5. Add tests
   Add tests/test_complexity.py:
     - hilbert dimension is 2^n
     - density matrix entries are 4^n
     - dense matmul scale is 8^n
     - collapse operator count is 3n for unified physical rates
     - complexity diagnostics are present in run_simulation result
     - increasing qubits increases estimated work units
     - increasing time_steps or substeps increases estimated work units
     - increasing circuit columns increases estimated work units in gate-aware mode

6. Documentation
   Add docs/architecture/complexity.md with:
     - Definitions:
         n, d=2^n, T, C, S_total
     - Memory:
         O(d^2)=O(4^n) per density matrix
         O(T d^2) if all states are stored
     - Gate application:
         O(d^3)=O(8^n)
     - Lindblad RHS:
         O(n d^3)=O(n 8^n)
     - RK4:
         4 RHS evaluations per step
     - Gate-aware total:
         O(S_total n 8^n)
     - Practical note:
         current Python tuple dense matrices are much slower than optimized array libraries
     - Recommendation:
         keep default UI small, use 1-2 qubits stable, 3-4 qubits experimental, 5-6 qubits only with optimization or reduced sampling

7. Constraints
   - Do not change physics.
   - Do not change fidelity or purity definitions.
   - Do not add NumPy or external dependencies in this phase.
   - Do not implement sparse matrices in this phase.
   - Do not implement Rust in this phase.
   - Do not implement React in this phase.
   - Do not change run_simulation(config) public API.
   - Complexity diagnostics must be JSON-safe.
```

---
