
> **Historical Streamlit-era implementation plan**
>
> Circuit editing is now implemented in React with 2-8 qubit rows,
> drag-and-drop, deletion, Undo/Redo, and import/export. The Streamlit
> implementation details below are obsolete.


---


## Phase 5.5: Circuit Editor Completion Sprint

### 目的

Phase 5までで実装した Single Run / Compare を壊さず、回路エディタを提出版に近づける。

### このフェーズでやること

```text
1. H以外の基本ゲートをUIから配置可能にする
2. 1量子ビット回路で I/H/X/Z/Measure を扱う
3. 2量子ビット行を表示できるようにする
4. CNOTを内部表現・UI・検証に接続する
5. Bell回路プリセットを追加する
6. 表形式すぎる回路表示を「回路線 + ゲートカード」風に改善する
7. Drag & Drop完成版が難しい場合、まずは「ゲート選択 → セルクリック配置」にする
8. Undo/Redo/Clearが全ゲートで動くことを確認する
9. Compare Workflowが新しい回路エディタでも壊れないことを確認する
```

---

# Phase 5.5 のスコープ

## 必達

|項目|内容|
|---|---|
|基本ゲート配置|I / H / X / Z / Measure|
|2量子ビット表示|q0, q1 の行を表示|
|CNOT対応|`controls=[0]`, `targets=[1]` 形式|
|Bellプリセット|H + CNOT|
|Undo/Redo|すべての編集操作で動作|
|Clear Circuit|Clear後Undoで復元|
|回路表示改善|表ではなく回路線風|
|Single Run連携|新エディタから実行可能|
|Compare連携|新エディタからA/B比較可能|

## 後回し

```text
S/T/RX/RY/RZ
完全なDrag & Drop custom component
3〜4量子ビットUI
5〜6量子ビットUI
複数CNOTの高度編集
ゲートパラメータ編集
```

---

# 実装順序

## Step 1: 現在のPhase 5を固定

まず、Phase 5が動いている状態をcommitします。

```powershell
git status
git add -A
python -m pre_commit run --all-files
git add -A
python -m unittest discover -s tests
git commit -m "Complete Phase 5 compare workflow"
```

すでにcommit済みなら不要です。

---

## Step 2: Phase 5.5用ブランチ

```powershell
git checkout -b phase5-5-circuit-editor-hardening
```

---

## Step 3: 1量子ビット基本ゲートUIを完成

Phase 5.5開始時点でまずこれを確認します。

```text
I
H
X
Z
Measure
```

すべてについて、

```text
配置できる
削除できる
置換できる
Undoできる
Redoできる
SimulationConfigに変換できる
run_simulationできる
run_comparisonできる
```

を確認します。

---

## Step 4: 2量子ビット対応

最低限：

```text
logical_qubits = 2
initial_states = ["0", "0"]
q0行
q1行
```

をUIに出します。

CNOTは内部的にはこれで十分です。

```python
GateOperation(
    type="CNOT",
    controls=[0],
    targets=[1],
    params={},
)
```

Bellプリセット：

```text
q0 ──[H]──●──
          │
q1 ───────X──
```

---

## Step 5: 回路表示改善

完全DnDより先に、見た目を改善します。

目標：

```text
Before:
| step | q0 |
| 0    | H  |

After:
      t0    t1    t2
q0 ──[H]────────[M]──
q1 ───────[X]───[M]──
```

Streamlitなら最初は `st.markdown(..., unsafe_allow_html=True)` で十分です。
重要なのは、**回路らしく見えること**です。

---

## Step 6: Drag & Dropの暫定方針

本格DnDが重ければ、Phase 5.5では以下を採用します。

```text
1. ゲートパレットでゲートを選択
2. 回路セルをクリック
3. 選択中ゲートをそのセルに配置
```

これは厳密なDnDではありませんが、表形式よりかなり直感的です。
非機能要件上は最終的にDnD必須なので、これは **DnD前段階** として扱います。

---

# Codex指示文

```text
Task:
Implement Phase 5.5: Circuit Editor Completion Sprint.

Goal:
Improve the circuit editor after Phase 5 Compare Workflow. Keep existing Single Run and Compare functionality working, while adding basic multi-gate and 2-qubit circuit editing support.

Required changes:

1. Gate support in UI
   - Ensure I, H, X, Z, Measure can be selected and placed from the UI
   - Do not limit the UI to H only
   - Keep existing CircuitState / CircuitHistory as the source of truth

2. Editing operations
   - Add gate
   - Remove gate
   - Replace gate
   - Undo
   - Redo
   - Clear Circuit
   - Clear Circuit must be undoable

3. 2-qubit support
   - Allow logical_qubits = 2 in the UI
   - Show q0 and q1 rows
   - Allow 1-qubit gates on q0 or q1
   - Add CNOT support with one control and one target
   - Validate CNOT control != target

4. Bell preset
   - Add a Bell circuit preset:
     - q0: H then CNOT control
     - q1: CNOT target
   - The preset must convert to CircuitConfig
   - The preset must run through run_simulation(config)
   - The preset must work with Compare Workflow if supported by current simulator

5. Circuit rendering
   - Improve rendering from table-like view to circuit-line / gate-card style
   - Show q0, q1 rows
   - Show time steps
   - Show gates as compact cards such as [H], [X], [Z], [M]
   - Show CNOT with visual connection if feasible
   - If full visual CNOT connection is difficult, show CNOT(control=0,target=1) clearly

6. Placement interaction
   - If full Drag & Drop is difficult, implement click-to-place:
     - select gate from palette
     - select target qubit
     - select or click step
     - place gate
   - Do not implement a large custom Drag & Drop component yet unless simple

7. Preserve Phase 5
   - Single Run must still work
   - Compare Low vs High Noise must still work
   - Existing tests must pass
   - Add or update tests for 2-qubit circuit state and CNOT validation

Acceptance criteria:
- Existing tests still pass
- I/H/X/Z/Measure can be placed from UI
- H-only limitation is removed
- User can switch between 1 and 2 logical qubits
- User can create a 2-qubit circuit
- User can place CNOT with distinct control and target
- Bell preset exists
- Undo/Redo works for all supported gates
- Clear Circuit can be undone
- Circuit rendering looks like circuit lines/gate cards rather than a raw data table
- Single Run still works
- Compare Workflow still works for supported circuits
- No Streamlit imports are added to core
- No physics model is changed

Constraints:
- Do not change physics model
- Do not change environment-to-T1/T2 mapping
- Do not change T1/T2-to-gamma mapping
- Do not change Lindblad evolution
- Do not change fidelity or purity definitions
- Do not add external dependencies
- Do not implement Expert Inspector
- Do not implement Save/Load backend
- Do not implement QuTiP, Rust, FastAPI, or Godot
```

---

# 完了チェックリスト

```md
## Phase 5.5 Checklist

### Gate Support

- [ ] I をUIから配置できる
- [ ] H をUIから配置できる
- [ ] X をUIから配置できる
- [ ] Z をUIから配置できる
- [ ] Measure をUIから配置できる
- [ ] H専用UIではなくなっている

### Editing

- [ ] Add Gateできる
- [ ] Remove Gateできる
- [ ] Replace Gateできる
- [ ] Undoできる
- [ ] Redoできる
- [ ] Clear Circuitできる
- [ ] Clear後Undoで復元できる

### 2-qubit

- [ ] logical_qubits=2を選べる
- [ ] q0, q1行が表示される
- [ ] q0に1量子ビットゲートを置ける
- [ ] q1に1量子ビットゲートを置ける
- [ ] CNOTを置ける
- [ ] CNOT control != target が検証される

### Preset

- [ ] Bell presetがある
- [ ] Bell presetがCircuitConfigへ変換できる
- [ ] Bell presetでSingle Runできる
- [ ] Bell presetでCompareできる、または未対応なら明確に警告する

### Rendering

- [ ] 表データそのものではなく回路線風に表示される
- [ ] q0, q1ラベルがある
- [ ] time stepが分かる
- [ ] gate card表示がある
- [ ] CNOTが分かる形で表示される

### Regression

- [ ] Phase 4 Beginner UIが壊れていない
- [ ] Phase 5 Compare Workflowが壊れていない
- [ ] 既存テストが通る
- [ ] pre-commitが通る

### Safety

- [ ] coreにStreamlit依存がない
- [ ] 物理モデルを変更していない
- [ ] 外部依存を追加していない
```

---

# その後のロードマップ修正

Phase 5まで完了済みなら、今後はこうです。

```text
Phase 5.5:
  Circuit Editor Completion Sprint

Phase 6:
  Expert Mode / Expert Inspector

Phase 7:
  Save / Load / Export

Phase 8:
  性能・数値妥当性・回帰テスト

Phase 9:
  U-22提出向け仕上げ
```

3〜4量子ビット対応は Phase 8 以降で構いません。
5〜6量子ビットは上限目標・Expert実験枠のままでよいです。

---
