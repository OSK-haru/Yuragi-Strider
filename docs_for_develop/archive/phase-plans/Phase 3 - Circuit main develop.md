> **Historical Streamlit-era implementation plan**
>
> The active circuit editor is the React Circuit Studio and the former
> `app/` Streamlit tree has been removed. This document is retained only as
> development history. See `docs_for_develop/README.md` for current capabilities.


# Phase 3: 回路エディタ本開発

## 目的

Phase 3 の目的は、Yuragi-Strider の中核UIである **回路エディタ** を本開発用に整備することである。

Phase 1 では `CircuitConfig / SimulationConfig / SimulationResult / run_simulation(config)` を整備した。
Phase 2 では入力検証・数値異常検出・警告/エラーの基盤を整備した。

Phase 3 では、それらを前提として、利用者がUI上で量子回路を作成・編集できるようにする。

最終的には、以下の流れを成立させる。

```text
Gate Palette
    ↓
Circuit Editor
    ↓
CircuitState
    ↓
CircuitConfig
    ↓
SimulationConfig
    ↓
run_simulation(config)


```


## Phase 3で作るもの

Phase 3で作る主な成果物は以下である。

core/circuit_state.py
core/circuit_history.py
core/circuit_validation.py  または core/validation.py への追加
tests/test_circuit_state.py
tests/test_circuit_history.py
tests/test_circuit_to_config.py


UI側の簡易実装を含める場合は、以下も対象にする。

app/components/circuit_editor.py
app/components/gate_palette.py


将来のDrag & Drop UIにつなげるため、内部操作は以下のように抽象化しておく。

add_gate
remove_gate
move_gate
replace_gate
clear_circuit
undo
redo


# Step 1: `CircuitState` を作る

## 目的

編集中の回路状態を保持する。

`CircuitConfig` は保存・実行用の構造であり、`CircuitState` は編集用の構造である。

core/circuit_state.pyを作成

## 必要なクラス

@dataclass
class CircuitState:
    logical_qubits: int
    initial_states: list[str]
    columns: list[GateColumn]

既に `GateOperation`, `GateColumn`, `CircuitConfig` が `core/circuit_model.py` にあるなら、それを利用する。


## 必要なメソッド

```
add_gate(step, gate)
remove_gate(step, target)
replace_gate(step, gate)
move_gate(from_step, from_target, to_step, to_target)
clear()
to_config()
from_config(config)
copy()
```


## 最低限の仕様

### add_gate

指定した時間列にゲートを追加する。

```
add_gate(step=0, gate=H on q0)
```

結果:

```
q0: [H]
```

---

### remove_gate

指定したstepとtargetにあるゲートを削除する。

```
remove_gate(step=0, target=0)
```

---

### replace_gate

指定した位置にあるゲートを置き換える。

```
H → X
```

---

### move_gate

配置済みゲートを別セルへ移動する。

```
step 0 q0 の H を step 1 q0 へ移動
```

---

### clear

回路を全消去する。

```
columns = []
```

---

### to_config

`CircuitState` から `CircuitConfig` へ変換する。

```
config = state.to_config()
```

---

### from_config

保存済み・プリセット・テスト用の `CircuitConfig` から `CircuitState` を復元する。

```
state = CircuitState.from_config(config)
```

---

## 完了条件

- `CircuitState` を作成できる
- Hゲートを追加できる
- 追加したゲートを削除できる
- ゲートを置換できる
- ゲートを移動できる
- clearできる
- `CircuitConfig` に変換できる
- `CircuitConfig` から復元できる
-
# Step 2: 回路編集バリデーションを追加する

## 目的

不正なゲート配置を防ぐ。

Phase 2で `validate_simulation_config(config)` は作ったが、Phase 3では **編集中の操作単位**でも検証したい。


## 検証対象

```
logical_qubits >= 1
logical_qubits <= 6
target index が範囲内
control index が範囲内
CNOT control != target
未対応gateを拒否
同一step同一qubitへの衝突
Measure後の同一qubit操作
```
最後の `Measure後の同一qubit操作` は初期実装では warning でもよい。

## 対応ゲート

Phase 3で最低限扱うゲート:

I
H
X
Z
Measure


2量子ビット対応を入れる場合:

CNOT


## エラー例

INVALID_GATE_TYPE
GATE_TARGET_OUT_OF_RANGE
GATE_CONTROL_OUT_OF_RANGE
CNOT_REQUIRES_CONTROL
CNOT_CONTROL_EQUALS_TARGET
CELL_ALREADY_OCCUPIED
OPERATION_AFTER_MEASURE

## 実装方針

既存の `core/validation.py` に追加してよい。

または、新規に以下を作ってもよい。

```
core/circuit_validation.py
```

おすすめは、Phase 3では以下。

```
core/circuit_validation.py
```

理由:

- 回路編集用の検証とSimulationConfig全体検証を分けられる
- UIエディタから呼びやすい
- 後でDrag & Drop配置時に使いやすい

# Step 3: Undo/Redo履歴を作る

## 目的

回路編集の安全装置を実装する。

core/circuit_history.pyを作成する

必要なクラス

@dataclass
class CircuitHistory:
    current: CircuitState
    undo_stack: list[CircuitState]
    redo_stack: list[CircuitState]
    history_limit: int = 50

必要なメソッド

apply(operation)
undo()
redo()
can_undo()
can_redo()
clear_history()

## Redoの仕様

```
現在stateをundo_stackへ積む
redo_stackからstateを取り出す
currentに復元する
```


## 履歴上限

```
history_limit = 50
```

50を超えたら古い履歴から削除する。

## 完了条件

- add後にundoできる
- undo後にredoできる
- remove後にundoできる
- clear後にundoで復元できる
- 新しい操作後はredo_stackがクリアされる
- 履歴上限が守られる

# Step 4: `CircuitState -> SimulationConfig` の接続確認

## 目的

回路エディタで作った回路をシミュレーションに渡せるようにする。

---

## 手順

1. `CircuitState` で1-qubit H回路を作る
2. `CircuitConfig` に変換する
3. `EnvironmentConfig` と組み合わせる
4. `SimulationConfig` を作る
5. `run_simulation(config)` を呼ぶ
6. `SimulationResult` が返ることを確認する

---

## 完了条件

以下が成立する。


```

 state = CircuitState(logical_qubits=1, initial_states=["0"], columns=[])
 state.add_gate(0, GateOperation(type="H", targets=[0], controls=[], params={}))

 circuit_config = state.to_config()

 sim_config = SimulationConfig(
     circuit=circuit_config,
     environment=environment,
     duration_us=20.0,
     time_steps=101,
     fidelity_threshold=0.9,
 )

 result = run_simulation(sim_config)
```
# Step 5: テストを追加する

## 追加テスト

tests/test_circuit_state.py
tests/test_circuit_history.py
tests/test_circuit_to_config.py

## `test_circuit_state.py`

確認すること:

```
CircuitStateを作れる
Hゲートを追加できる
Xゲートに置換できる
ゲートを削除できる
ゲートを移動できる
clearできる
CircuitConfigに変換できる
CircuitConfigから復元できる
```

## `test_circuit_history.py`

確認すること:

```
add_gate後にundoできる
undo後にredoできる
remove_gate後にundoできる
clear後にundoできる
新規操作後にredo_stackが消える
history_limitが有効
```

## `test_circuit_to_config.py`

確認すること:

```
CircuitStateで作った1-qubit H回路をSimulationConfigにできる
run_simulation(config)でSimulationResultが返る
```



# Step 6: 簡易UIへの接続

## 目的

本格Drag & Dropの前に、簡易UIから `CircuitState` を触れるようにする。

---




## 初期UI案

Streamlitの場合、最初は以下でよい。

Gate selectbox:
  I / H / X / Z / Measure

Target selectbox:
  q0

Step selectbox:
  0〜9

Buttons:
  Add Gate
  Remove Gate
  Undo
  Redo
  Clear





## Phase 3 Checklist

### CircuitState

- [ ] CircuitState がある
- [ ] add_gate がある
- [ ] remove_gate がある
- [ ] replace_gate がある
- [ ] move_gate がある
- [ ] clear がある
- [ ] to_config がある
- [ ] from_config がある
- [ ] copy がある

### CircuitHistory

- [ ] CircuitHistory がある
- [ ] undo がある
- [ ] redo がある
- [ ] can_undo がある
- [ ] can_redo がある
- [ ] clear_circuit がundo可能
- [ ] 新規操作後にredo_stackが消える
- [ ] history_limitが守られる

### Validation

- [ ] 未対応gateを検出できる
- [ ] gate target範囲外を検出できる
- [ ] gate control範囲外を検出できる
- [ ] CNOT control == target を検出できる
- [ ] 同一step同一qubit衝突を検出できる

### Simulation Connection

- [ ] CircuitStateからCircuitConfigへ変換できる
- [ ] CircuitConfigからSimulationConfigを作れる
- [ ] run_simulation(config)でSimulationResultが返る

### Tests

- [ ] 既存テストが通る
- [ ] test_circuit_state.py が通る
- [ ] test_circuit_history.py が通る
- [ ] test_circuit_to_config.py が通る

### Safety

- [ ] 物理モデルを変更していない
- [ ] 外部依存を追加していない
- [ ] UIを大幅変更していない
