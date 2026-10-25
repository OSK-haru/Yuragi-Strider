
> **Historical phase plan**
>
> The normalized core data model and public `run_simulation` boundary are
> implemented. Use `docs/architecture/module_structure.md` for current
> structure.



phse1では、回路シュミレーターで中心となる機能が利用する
ゲート、回路全体の形式などを整理する、データ型の実装。

Beginner Mode
Compare Workflow
Expert Inspector
Save / Load
Export
FastAPI / Godot / QuTiP optional backend
で、統一的な扱いが可能に。

# Phase 1で作るもの

## 必須成果物

core/circuit_model.py
core/results.py
core/simulator.py の整理


## 1. `GateOperation`

1つのゲート操作を表す。


```

@dataclass
class GateOperation:
    type: str
    targets: list[int]
    controls: list[int] | None = None
    params: dict[str, float] | None = None

```


例:

```
{
  "type": "H",
  "targets": [0],
  "controls": [],
  "params": {}
}
```

CNOTなら:

{
  "type": "CNOT",
  "targets": [1],
  "controls": [0],
  "params": {}
}



---

## 2. `GateColumn`

同じ時間ステップに配置されたゲート群。

```
{
  "type": "CNOT",
  "targets": [1],
  "controls": [0],
  "params": {}
}
```

---

## 3. `CircuitConfig`

回路全体。

```
@dataclass
class CircuitConfig:
    logical_qubits: int
    initial_states: list[str]
    columns: list[GateColumn]
```

例:


```
{
  "logical_qubits": 1,
  "initial_states": ["0"],
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
    }
  ]
}
```

## 4. `EnvironmentConfig`

環境条件。

```
@dataclass
class EnvironmentConfig:
    mode: str
    temperature: float
    magnetic_field: float
    noise_level: float
    observation_strength: float | None = None
    observation_frequency: float | None = None
```

MVPでは `mode="normalized"` 固定でよいです。

---

## 5. `SimulationConfig`

シミュレーション実行設定。

```
@dataclass
class SimulationConfig:
    circuit: CircuitConfig
    environment: EnvironmentConfig
    duration_us: float
    time_steps: int
    fidelity_threshold: float
    model: str = "weak_coupling_lindblad"
```

最初はmodelを弱開放系にしておくこと
あとから拡張する際は、ここに注意
## 6. `SimulationResult`

実行結果。

```
@dataclass
class SimulationResult:
    config: SimulationConfig
    times: list[float]
    fidelity: list[float]
    purity: list[float]
    effective_operation_time_us: float | None
    output_probabilities: dict[str, float]
    derived_parameters: dict[str, float]
    diagnostics: dict[str, float]
    warnings: list[str]

```

最初は `output_probabilities` や `diagnostics` は簡易でもよいです。
重要なのは、**結果をこの形に集約すること**です。

---

# Phase 1でまだやらないこと

Phase 1では、以下はやりません。

```
- Drag & Drop UI- Expert Inspector UI- Compare Workflow本実装- 保存/読込本実装- QuTiP導入- FastAPI導入- Godot導入- H_eff実装- 物理モデル変更
```

Phase 1は、**内部APIを整えるだけ**です。
