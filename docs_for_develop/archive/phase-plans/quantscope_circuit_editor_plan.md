# Yuragi-Strider Circuit Editor Implementation Plan

> **Status: Completed and retained as implementation history**
>
> UI-3A through UI-3G and arbitrary `circuit_config` submission are now
> implemented. Circuit Studio supports 2-8 qubits, click and drag-and-drop
> placement, multi-qubit and register gates, movement, deletion, Clear,
> Undo/Redo, and JSON import/export. Statements below that describe a fixed
> Bell-only circuit or future API integration are obsolete.

## 目的

この文書は、Yuragi-Strider に **Drag & Drop と基本ゲートパレットを備えた回路編集機能**を追加するための実装計画である。

対象は React frontend の UI / state 管理であり、現時点では core physics や FastAPI の任意回路実行には踏み込まない。

まずは、現在固定表示されている Bell 回路プレビューを、React 側の編集可能な回路状態から描画できる構造へ移行する。その後、クリック配置、Drag & Drop、削除、Undo / Redo、CircuitConfig JSON 出力へ段階的に進める。

---

## 背景

現在の Yuragi-Strider は、以下の基本フローが成立している。

```text
ParameterPanel
  ↓
POST /api/simulate
  ↓
FastAPI
  ↓
run_simulation(config)
  ↓
SimulationResponse
  ↓
React UI
```

ただし、現時点の回路は `bell` preset に固定されている。

今後、ユーザーが自分で回路を構成できるようにするため、React 側に回路編集機能を追加する。

最終的には、React 側の Circuit Editor が `CircuitConfig` 互換 JSON を出力し、FastAPI / Python core 側に渡せるようにする。

---

## 守るべき設計原則

### 1. core physics を変更しない

このフェーズでは、以下を変更しない。

```text
core/
api/main.py
Lindblad equation
SimulationConfig
SimulationResult
SimulationResponse shape
```

Drag & Drop UI は frontend の機能であり、物理モデルの変更ではない。

---

### 2. React 側は CircuitConfig 互換構造を目指す

最終的な目標形式は、Python core の `CircuitConfig` と対応する JSON である。

```json
{
  "logical_qubits": 2,
  "initial_states": ["0", "0"],
  "columns": [
    {
      "step": 0,
      "gates": [
        { "type": "H", "targets": [0], "controls": [], "params": {} }
      ]
    },
    {
      "step": 1,
      "gates": [
        { "type": "CNOT", "targets": [1], "controls": [0], "params": {} }
      ]
    }
  ]
}
```

ただし、最初から API に送らない。

まずは React 内部で state と描画を安定させる。

---

### 3. Drag & Drop は段階的に導入する

いきなり完全な Drag & Drop を実装しない。

先に、以下を固める。

```text
1. Circuit editor state model
2. state から CircuitPreview を描画
3. click-to-place
4. Drag & Drop
5. delete / clear / undo / redo
6. CircuitConfig JSON export
7. arbitrary circuit API
```

---

### 4. UI を過密にしない

既に Result Drawers により、実験画面は整理された。

Circuit Editor 追加時も、次を維持する。

```text
常時見せる:
  Circuit editor
  ParameterPanel
  RunPanel
  SimulationSummary
  MetricTimeline

折りたたむ:
  Output probabilities
  Diagnostics
  API debug
  Model details
  Warnings / issues
```

回路編集のために、結果表示を圧迫しすぎない。

---

## 対象ゲート

### 初期対応ゲート

最初に対応するゲートは以下とする。

| Gate | 種類 | 対応方針 |
|---|---|---|
| I | 1 qubit | 任意の qubit に配置可能 |
| H | 1 qubit | 任意の qubit に配置可能 |
| X | 1 qubit | 任意の qubit に配置可能 |
| Z | 1 qubit | 任意の qubit に配置可能 |
| Measure | 1 qubit | 表示のみ。現段階では物理実行の扱いに注意 |
| CNOT | 2 qubit | control / target の2点配置が必要 |

### 後回しにするゲート

以下は後回し。

```text
S
T
RX / RY / RZ
CZ
SWAP
任意パラメータゲート
測定結果に依存する古典制御
```

---

## フェーズ分割

# UI-3A: Circuit editor state model

## 目的

React 側に編集可能な回路状態を作る。

この段階では UI 操作は最小でよい。

## やること

- `CircuitEditorState` 型を作る
- `GatePlacement` 型を作る
- Bell 回路を state として初期化する
- `CircuitPreview` または新しい `CircuitEditor` を state から描画する
- 現在の固定 Bell 表示を state-driven 表示へ移行する

## 想定型

```ts
type GateType = "I" | "H" | "X" | "Z" | "CNOT" | "MEASURE";

type GatePlacement = {
  id: string;
  type: GateType;
  step: number;
  targets: number[];
  controls: number[];
  params?: Record<string, number>;
};

type CircuitEditorState = {
  logicalQubits: number;
  initialStates: string[];
  maxSteps: number;
  gates: GatePlacement[];
};
```

## やらないこと

- Drag & Drop
- click-to-place
- Undo / Redo
- API 送信
- core 変更

## 受け入れ条件

- `npm.cmd run build` が通る
- Bell 回路が state から表示される
- 表示が現状より大きく崩れない
- Run simulation はまだ既存の `bell` preset POST のままでよい

---

# UI-3B: Gate palette + click-to-place

## 目的

Drag & Drop の前段階として、クリックでゲート配置できるようにする。

## やること

- `GatePalette` を追加する
- ゲートをクリックして選択する
- グリッドセルをクリックすると選択中ゲートを配置する
- 1 qubit gate を配置可能にする
- 同じセルに既存ゲートがある場合の扱いを決める

## 初期ルール

```text
1 qubit gate:
  選択中ゲートをクリックしたセルに配置

既にゲートがあるセル:
  上書きする、または配置拒否する
  初期版では上書きでよい
```

## CNOT の扱い

CNOT はこの段階では以下のどちらかにする。

```text
Option A:
  CNOT は palette に表示するが disabled

Option B:
  CNOT は control qubit を選び、次に target qubit を選ぶ2クリック方式
```

推奨は Option A。CNOT は UI-3C 以降で扱う。

## 受け入れ条件

- H / X / Z / I を配置できる
- 配置後、CircuitEditor 表示が更新される
- Run simulation の挙動はまだ変えない
- build が通る

---

# UI-3C: Drag & Drop placement

## 目的

GatePalette から CircuitEditor へ Drag & Drop でゲートを配置できるようにする。

## 重要方針

外部ライブラリは追加しない。

HTML5 Drag and Drop API または Pointer events ベースで小さく実装する。

## やること

- GatePalette の gate item を draggable にする
- Circuit grid cell を drop target にする
- drop 時に `GatePlacement` を追加する
- 1 qubit gate の Drag & Drop を安定させる

## 初期対象

```text
I
H
X
Z
MEASURE
```

CNOT は後段でよい。

## 受け入れ条件

- GatePalette から H を q0 step0 に配置できる
- X / Z も同様に配置できる
- 不正な drop で画面が壊れない
- build が通る

---

# UI-3D: Gate delete / clear / undo / redo

## 目的

編集操作を安全に戻せるようにする。

## やること

- ゲート削除
- Clear circuit
- Undo
- Redo
- 操作履歴 state を追加

## 操作履歴の対象

```text
gate placement
gate deletion
gate overwrite
clear circuit
initial state change, later
qubit count change, later
```

## 推奨 state

```ts
type CircuitHistory = {
  past: CircuitEditorState[];
  present: CircuitEditorState;
  future: CircuitEditorState[];
};
```

## 受け入れ条件

- 配置後 Undo で戻る
- Undo 後 Redo で復元する
- Clear 後 Undo で復元する
- build が通る

---

# UI-3E: CNOT editing

## 目的

2量子ビットゲートである CNOT を編集可能にする。

## 候補UI

### Option A: 2クリック方式

```text
1. CNOT を選ぶ
2. control qubit のセルをクリック
3. target qubit のセルをクリック
4. 同じ step に CNOT を配置
```

### Option B: Drag control to target

```text
1. CNOT を選ぶ
2. control cell から target cell へ drag
```

### 推奨

初期実装は Option A。

Drag の高度な線描画は後回しにする。

## CNOT validation

```text
control != target
same step
logicalQubits >= 2
対象 step に競合ゲートがない
```

## 受け入れ条件

- q0 control, q1 target の CNOT を配置できる
- control と target が線で接続される
- control == target は拒否される
- build が通る

---

# UI-3F: CircuitConfig JSON output

## 目的

React の CircuitEditorState を Python core 互換の `CircuitConfig` JSON に変換する。

## やること

- `toCircuitConfig(state)` を実装する
- `fromCircuitConfig(config)` を実装する、可能なら
- JSON preview / export は後段でもよい

## 出力例

```json
{
  "logical_qubits": 2,
  "initial_states": ["0", "0"],
  "columns": [
    {
      "step": 0,
      "gates": [
        { "type": "H", "targets": [0], "controls": [], "params": {} }
      ]
    },
    {
      "step": 1,
      "gates": [
        { "type": "CNOT", "targets": [1], "controls": [0], "params": {} }
      ]
    }
  ]
}
```

## 受け入れ条件

- Bell 回路 state から正しい CircuitConfig JSON が出る
- 空回路でも valid JSON が出る
- build が通る

---

# UI-3G: Export / import circuit config

## 目的

作成した回路を `.qscope.json` または `.qscope.circuit.json` として保存・復元できるようにする。

## やること

- Export current circuit JSON
- Import circuit JSON
- JSON parse error 表示
- schema_version 付与

## 初期形式案

```json
{
  "schema_version": "0.1",
  "kind": "quantscope_circuit",
  "circuit": {
    "logical_qubits": 2,
    "initial_states": ["0", "0"],
    "columns": []
  }
}
```

## 注意

任意コード実行につながる形式は読まない。
JSON のみを対象にする。

---

# API-12: Arbitrary CircuitConfig simulation

## 目的

React で編集した回路を、FastAPI 経由で Python core に渡して実行する。

## 注意

これは UI-3 完了後に行う。

回路編集UIが未成熟なうちに API を拡張しない。

## 変更対象

```text
api/main.py
possibly api schema helper
frontend POST payload
```

## 新 payload 案

```json
{
  "circuit": {
    "logical_qubits": 2,
    "initial_states": ["0", "0"],
    "columns": []
  },
  "simulation_backend": "python_dense",
  "parameters": {
    "normalized_temperature": 0.02,
    "normalized_magnetic_field": 0.02,
    "noise_level": 0.2,
    "duration_us": 2.0,
    "time_steps": 11,
    "fidelity_threshold": 0.9
  }
}
```

## 後方互換

既存の `circuit_preset: "bell"` はしばらく残す。

```text
if circuit is provided:
  use arbitrary circuit
else if circuit_preset is provided:
  use preset
```

---

## 画面レイアウト案

```text
Simulation screen
├─ Header
│   ├─ Back / Home
│   └─ Help / Q&A
│
├─ Main experiment grid
│   ├─ Circuit editor area
│   │   ├─ Gate palette
│   │   ├─ Circuit grid
│   │   └─ Edit controls
│   │       ├─ Undo
│   │       ├─ Redo
│   │       └─ Clear
│   │
│   ├─ ParameterPanel
│   └─ RunPanel
│
├─ SimulationSummary
├─ MetricTimeline
└─ ResultDrawers
    ├─ Output probabilities
    ├─ Diagnostics
    ├─ Model details
    └─ API debug
```

---

## 実装順序まとめ

```text
UI-3A:
  Circuit editor state model

UI-3B:
  Gate palette + click-to-place for 1 qubit gates

UI-3C:
  Drag & Drop for 1 qubit gates

UI-3D:
  Delete / Clear / Undo / Redo

UI-3E:
  CNOT placement

UI-3F:
  CircuitConfig JSON output

UI-3G:
  Circuit config export / import

API-12:
  POST arbitrary CircuitConfig to /api/simulate
```

---

## Codex 共通注意事項

今後この計画を参照して実装する Codex は、以下を必ず守ること。

```text
Do not modify core physics unless the task explicitly says so.
Do not modify Lindblad equations.
Do not modify Rust backend behavior.
Do not change SimulationResponse shape casually.
Do not add external dependencies without explicit approval.
Do not implement arbitrary circuit API before the editor state is stable.
Do not combine Drag & Drop, Undo/Redo, CNOT, import/export, and API extension in one task.
Keep each task small and reviewable.
```

---

## 完了判定

この計画全体の最小完成は、以下が成立した状態とする。

```text
1. GatePalette から基本ゲートを配置できる
2. 回路グリッド上にゲートが表示される
3. 削除・Clear・Undo/Redo ができる
4. H + CNOT の Bell 回路をユーザーが構成できる
5. React state から CircuitConfig JSON を出力できる
6. 既存の API / ResultDrawers / Help / ParameterPanel が壊れていない
```

API に任意回路を送る機能は、この計画の後段であり、最小UI完成とは分けて評価する。
