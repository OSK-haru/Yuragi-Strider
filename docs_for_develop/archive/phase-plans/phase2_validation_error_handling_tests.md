
> **Historical phase plan**
>
> Validation, structured issues, and regression tests are implemented. This
> document is retained as design history, not a pending task list.

## 目的

Phase 2 の目的は、Phase 1 で整備した

```
SimulationConfig    ↓run_simulation(config)    ↓SimulationResult
```

に対して、**入力検証・数値異常検出・エラー/警告の標準形式**を追加することです。

本開発では、UIやExpert表示を作る前に、まず以下を保証します。

```
不正な入力を検出できる計算結果が数値的に破綻していないか確認できる異常が起きたときにUIへ渡せる形で返せる
```

[[Non-functional Requirements]]では、trace、Hermiticity、positivity、purity、fidelity、probability、NaN/inf、time grid を検証対象にすると定義されています。特に NaN/inf は Fatal、trace/Hermiticity/probability は必須検証です


# Phase 2で作るもの

## 追加・整理するファイル

```
core/  errors.py  validation.py
```

## 追加・更新するテスト

```
tests/  test_validation.py  test_result_diagnostics.py
```

既存の `test_environment.py`, `test_evolution.py`, `test_run_simulation_api.py` は維持しす。



# Phase 2の成果物

## 1. `ValidationIssue`

入力エラー・警告・数値異常を統一的に表す構造です

@dataclass
class ValidationIssue:
    level: str
    code: str
    message: str
    detail: str | None = None
    suggestion: str | None = None



## 2. `validate_simulation_config(config)`

`SimulationConfig` が実行可能かを確認します。

検証対象:
logical_qubits
initial_states
gate targets
temperature
magnetic_field
noise_level
duration_us
time_steps
fidelity_threshold
model


## 3. `diagnose_simulation_result(result)`

`SimulationResult` が数値的に破綻していないか確認します。

検証対象:
NaN
inf
fidelity range
purity range
times length
probability sum
trace
Hermiticity

ただし、trace/Hermiticity は現在の `SimulationResult` に密度行列が入っていない場合、Phase 2では **可能な範囲だけ** 実装します。密度行列診断は、密度行列がresultに含まれるようになった段階で強化します。


# Phase 2でまだやらないこと

```
- UIのエラー表示実装- Expert Inspector実装- 保存/読込のschema検証- Drag & Drop UI- QuTiP backend- H_eff- quantum trajectory- 強結合開放系
```

Phase 2は、**core側の検証基盤**に限定します。

# 実装方針

## エラーレベル

F13[[Functional Requirements]]およびUI要件[[UI proto]]に合わせ、以下の4段階にします。

| level     | 意味       | 例               |
| --------- | -------- | --------------- |
| `info`    | 補足情報     | 実行条件の注記         |
| `warning` | 実行可能だが注意 | purityがわずかに範囲外  |
| `error`   | 実行不可     | noise_levelが範囲外 |
| `fatal`   | 計算破綻     | NaN/inf検出       |


# Step 1: `core/errors.py` を作る

## 目的

エラー・警告・数値異常をUIへ渡しやすい形式に統一します。

## 実装内容

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    code: str
    message: str
    detail: str | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }


想定コード例

ValidationIssue(
    level="error",
    code="INVALID_NOISE_LEVEL",
    message="Noise level must be between 0.0 and 1.0.",
    detail="Received noise_level=1.42",
    suggestion="Set noise_level to a value in the range [0.0, 1.0].",
)


# Step 2: `core/validation.py` を作る

## 目的

入力設定と実行結果を検証する関数をまとめます。

## 最低限必要な関数


def validate_simulation_config(config: SimulationConfig) -> list[ValidationIssue]:
    ...

def diagnose_simulation_result(result: SimulationResult) -> list[ValidationIssue]:
    ...

def has_blocking_issues(issues: list[ValidationIssue]) -> bool:
    ...


# Step 3: Config検証を実装する

## 検証対象

### 回路

logical_qubits >= 1
logical_qubits <= 6
initial_states の数 == logical_qubits
gate target が範囲内
gate control が範囲内
CNOT control != target
未対応gateを検出

Functional Requirementsでは、本開発の回路入力は論理量子ビット行と時間列のグリッドで扱い、MVPは1量子ビット、必達は2量子ビット、上限は6量子ビットと整理されています


### 環境条件

```
temperature: 0.0〜1.0magnetic_field: 0.0〜1.0noise_level: 0.0〜1.0observation_strength: None または 0.0〜1.0observation_frequency: None または 0以上
```

### シミュレーション設定

```
duration_us > 0time_steps >= 20.0 <= fidelity_threshold <= 1.0model == "weak_coupling_lindblad"
```

非機能要件でも、time grid は `duration > 0, steps >= 2` が必須条件です。

ただし、この環境条件は、正規化されたパラメタであることに注意すること。

# Step 4: Result診断を実装する

## 最低限の診断

```
times が空でない
fidelity が空でない
purity が空でない
times / fidelity / purity の長さが一致する
fidelity に NaN/inf がないpurity に NaN/inf がない
fidelity が 0〜1 付近にある
purity が 0〜1 付近にある
effective_operation_time_us が None または 0以上
output_probabilities の総和が1付近
```

## 許容誤差

非機能要件では以下が示されています。

```
trace error tolerance: 1e-8
Hermiticity error tolerance: 1e-8
probability sum tolerance: 1e-8
small negative eigenvalue tolerance: -1e-10
fidelity/purity clipping tolerance: 1e-10
```

Phase 2ではまず以下で実装します。

```
PROBABILITY_SUM_TOL = 1e-8
FIDELITY_RANGE_TOL = 1e-10
PURITY_RANGE_TOL = 1e-10
```


# Step 5: `run_simulation(config)` に検証を接続する

## 推奨方針

`run_simulation(config)` の最初で config 検証を行います。
```

issues = validate_simulation_config(config)
if has_blocking_issues(issues):
    return SimulationResult(
        config=config,
        times=[],
        fidelity=[],
        purity=[],
        effective_operation_time_us=None,
        output_probabilities={},
        derived_parameters={},
        diagnostics={},
        warnings=[issue.to_dict() for issue in issues],
    )


```


または、既存方針に合わせて `ValueError` を投げてもよいですが、UI連携を考えると **SimulationResultにissuesを含める形** が扱いやすいです。


## 注意

Phase 2では、`SimulationResult.warnings` が `list[str]` になっている可能性があります。
この場合は、無理に型を壊さず、まずは文字列化して入れます。

ただし将来的には、
```
warnings: list[ValidationIssue]
```
のようにすると好ましい

# Step 6: テストを追加する

## `tests/test_validation.py`

確認すること:
noise_level < 0 でerror
noise_level > 1 でerror
temperature > 1 でerror
magnetic_field < 0 でerror
duration_us <= 0 でerror
time_steps < 2 でerror
fidelity_threshold > 1 でerror
CNOT control == target でerror
gate target 範囲外でerror
unsupported gate でerror


## `tests/test_result_diagnostics.py`

確認すること:

```
NaN fidelity を fatal として検出inf purity を fatal として検出fidelity > 1 + tol を warning/errorとして検出purity < 0 - tol を warning/errorとして検出times/fidelity/purity の長さ不一致をerrorとして検出output_probabilities の総和ずれをwarningとして検出
```



## Phase 2 Checklist

### Core

- [ ] core/errors.py がある
- [ ] ValidationIssue がある
- [ ] core/validation.py がある
- [ ] validate_simulation_config がある
- [ ] diagnose_simulation_result がある
- [ ] has_blocking_issues がある

### Config Validation

- [ ] noise_level範囲外を検出できる
- [ ] temperature範囲外を検出できる
- [ ] magnetic_field範囲外を検出できる
- [ ] duration_us <= 0 を検出できる
- [ ] time_steps < 2 を検出できる
- [ ] fidelity_threshold範囲外を検出できる
- [ ] 不正gateを検出できる
- [ ] gate target範囲外を検出できる
- [ ] CNOT control == target を検出できる

### Result Diagnostics

- [ ] NaNを検出できる
- [ ] infを検出できる
- [ ] fidelity範囲外を検出できる
- [ ] purity範囲外を検出できる
- [ ] series長不一致を検出できる
- [ ] probability sumずれを検出できる

### run_simulation Integration

- [ ] invalid configでは物理計算を実行しない
- [ ] diagnostic warningsをSimulationResultに含める
- [ ] 既存MVPの正常ケースは壊れていない

### Tests

- [ ] 既存テストが通る
- [ ] test_validation.py が通る
- [ ] test_result_diagnostics.py が通る

### Safety

- [ ] 物理モデルを変更していない
- [ ] 依存関係を追加していない
- [ ] UIを変更していない
