
> **Historical Streamlit-era implementation plan**
>
> The active UI is React/Vite. Paths under `app/` and Streamlit launch
> commands below are obsolete and must not be used as current instructions.
> See `frontend/README.md`.



---



## 目的

Phase 4 の目的は、Yuragi-Strider の入門者向けUIを実装することです。

本Phaseでは、以下の画面・部品を作ります。

```text
Start Screen
Beginner Mode
Gate Palette
Circuit Editor UI
Environment Panel
Result Summary
Graph Drawer
Output Probabilities Drawer
Explanation Drawer
Error / Warning Display
```

Beginner Mode は、初学者が「何を操作すればよいか」「グラフが何を意味するか」「量子回路がなぜ壊れるか」を理解できる必要があります。Stakeholdersでも、初学者には用語説明、チュートリアル、プリセット、直感的なラベルが必要と整理されています。

---

# Phase 4で作る画面

## 1. Start Screen

起動直後に表示する画面です。

役割は以下です。

```text
アプリの目的を伝える
Beginner / Expert を選べる
デモを実行できる
チュートリアルを開始できる
保存済み設定を開ける
```

UI要件では、Start Screen には `Run Demo`、`Start Tutorial`、`Open Config` を置くことになっています。

---

## 2. Beginner Mode

入門者向けのメイン画面です。

必須構成は以下です。

```text
Header / Toolbar
Gate Palette
Circuit Editor
Environment Panel
Result Summary
Graphs Drawer
Output Probabilities Drawer
Explanation Drawer
```

Beginner Mode では、F03回路入力、F04環境条件入力、F05シミュレーション実行、F06可視化、F13エラー処理、F14サマリー表示を扱います。UI要件上も、Beginner Mode はこれらの主機能に対応づけられています。

---

# Phase 4でやらないこと

Phase 4では、以下は本格実装しません。

```text
Expert Inspector
Compare Workflow本実装
Save / Load本実装
Export本実装
QuTiP backend
Rust backend
Godot UI
FastAPI
H_eff
量子トラジェクトリー
Cavity QED
```

Compare、Expert、保存/出力は後続Phaseです。Plus Requirementsでも Godot、FastAPI、QuTiP optional backend、Rust backend などは本開発必達範囲ではなく将来拡張扱いです。

---

# 実装順序

## Step 0: 作業前確認

```powershell
cd C:\Users\oshad\Quantum-sim
(Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned) ; (& .\.venv\Scripts\Activate.ps1)

git status
python -m unittest discover -s tests
python -m pre_commit run --all-files
```

Phase 3がcommit済みで、`git status` が clean であることを確認します。

Phase 4用ブランチを作るなら：

```powershell
git checkout -b phase4-beginner-ui
```

---

## Step 1: UI構造を分割する

まず、`app/app.py` に全部書き続けるのを避けます。

推奨構成：

```text
app/
  app.py
  ui/
    start_screen.py
    beginner_mode.py
    gate_palette.py
    circuit_editor.py
    environment_panel.py
    result_summary.py
    result_drawers.py
    error_display.py
```

Phase 4では、UIを部品に分けることが重要です。非機能要件でも、core、UI、可視化、保存/出力、検証を分離することが保守性・拡張性の要件になっています。

---

## Step 2: Start Screenを実装する

### 必須要素

```text
Yuragi-Strider title
短い説明文
表示レベル選択: Beginner / Expert
Run Demo
Start Tutorial
Open Config
Recent / Status は任意
```

### 初期実装の方針

Expertはまだ未実装でもよいので、選択肢だけ置いても構いません。

```text
Beginner: enabled
Expert: coming soon
```

### 受け入れ条件

```text
起動直後にStart Screenが表示される
Beginner Modeへ遷移できる
Run Demoで1-qubit Hの結果を表示できる
Start Tutorialボタンが存在する
Open Configボタンが存在する
```

---

## Step 3: Beginner Modeのレイアウトを作る

Beginner Modeの基本レイアウトは以下です。

```text
┌──────────────────────────────────────────────┐
│ Header / Toolbar                             │
├───────────────┬──────────────────────────────┤
│ Gate Palette  │ Circuit Editor               │
├───────────────┼──────────────────────────────┤
│ Environment   │ Result Summary               │
├───────────────┴──────────────────────────────┤
│ Drawers: Graphs / Output / Explanation       │
└──────────────────────────────────────────────┘
```

UI要件でも、Beginner Mode はこの構造で定義されています。

Streamlitなら最初は以下でよいです。

```python
left, main = st.columns([1, 2])

with left:
    render_gate_palette()
    render_environment_panel()

with main:
    render_circuit_editor()
    render_result_summary()
    render_result_drawers()
```

---

## Step 4: Gate Paletteを接続する

### Phase 4で表示するゲート

MVP対象は `I, H, X, Z`、本開発では `Measure` もBeginnerに出してよいです。Functional RequirementsではMVPゲートセットは `I, H, X, Z`、Phase 1以降で `CNOT, Measure, S/T, RX/RY/RZ` が拡張対象になっています。

Phase 4では以下で十分です。

```text
I
H
X
Z
Measure
```

CNOTはPhase 5以降または2量子ビットUI整備後でよいです。

### 最初のUI

Drag & Drop完成版でなくても、Phase 4ではまず簡易UIでよいです。

```text
Gate selectbox
Target selectbox
Step selectbox
Add Gate button
Remove Gate button
Undo
Redo
Clear
```

ただし、非機能要件では最終的に回路編集はDrag & Drop必須、Undo/Redo必須と定義されています。
そのため、Phase 4の簡易UIは **仮UI** と明記します。

---

## Step 5: Circuit Editor UIを接続する

Phase 3で作った `CircuitState` と `CircuitHistory` を使います。

### 必須操作

```text
Add Gate
Remove Gate
Undo
Redo
Clear Circuit
```

### 表示

```text
q0: [H] [ ] [Measure]
```

まずは表形式・テキスト形式でよいです。

理想は次の形です。

```text
step:  0    1    2
q0:   [H]  [ ]  [M]
```

### 受け入れ条件

```text
UIからHゲートを追加できる
Undoで戻せる
Redoで復元できる
Clear Circuitで消せる
Clear後Undoで復元できる
CircuitState.to_config()経由でrun_simulationに渡せる
```

---

## Step 6: Environment Panelを実装する

Beginnerでは、物理量を直接出しすぎません。

### 必須入力

```text
Temperature parameter
Magnetic field parameter
Noise level
```

これらは 0.0〜1.0 のスライダーです。Functional Requirementsでは、正規化パラメータから `T1/T2/gamma` へ変換する方針と、その変換式が定義されています。

### 必須プリセット

```text
Low noise
High noise
```

追加候補：

```text
Almost ideal
Strong dephasing
```

### 注意書き

必ず表示します。

```text
初期版では、temperature / magnetic_field / noise_level は正規化パラメータです。
実機デバイスの厳密な温度・磁場・材料特性を再現するものではありません。
```

モデル透明性は非機能要件でも要求されています。弱結合Lindblad、Born-Markov近似、正規化パラメータ、現象論的T1/T2モデルであることを明示する必要があります。

---

## Step 7: Run Simulationを接続する

UIから直接 `environment.py` や `evolution.py` を呼ばず、必ず以下の流れにします。

```text
CircuitState
  ↓ to_config()
CircuitConfig
  ↓
EnvironmentConfig
  ↓
SimulationConfig
  ↓
run_simulation(config)
  ↓
SimulationResult
```

非機能要件では、UIは `run_simulation(config)` を通じてcoreを呼び出すことが、移植性・バックエンド拡張性の制約として定義されています。

---

## Step 8: Result Summaryを実装する

Beginner Modeで常時表示する指標は以下です。

```text
State Fidelity
Purity
Effective Operation Time
Output Probability Distance
```

F06では、Fidelity/Purityの時系列グラフ、有効時間表示、最終指標サマリーがMVP可視化要素として定義されています。

最初は `Output Probability Distance` が未実装なら、次のようにしてよいです。

```text
Output Probability Distance: not available
```

ただし、UIの枠は先に作っておきます。

---

## Step 9: Graph Drawerを実装する

詳細グラフは常時全部表示せず、Drawer/Expander形式にします。

### 初期状態

```text
Graphs: open
Output Probabilities: closed
Explanation: closed
Condition Details: closed
```

UI要件でも、Single Run時は Summaryを表示し、Graphsを開き、Output ProbabilitiesとExplanationは閉じる構成になっています。

### Graph Drawerの中身

```text
State Fidelity over Time
Purity over Time
```

Fidelityグラフには以下を入れます。

```text
threshold line
Effective Operation Time marker
```

---

## Step 10: Output Probabilities Drawerを実装する

Phase 4では、まだ出力確率が未完成の場合があります。

その場合は、以下のどちらかでよいです。

```text
未実装表示を出す
```

または、

```text
SimulationResult.output_probabilities がある場合だけ表示する
```

必須表示の形：

```text
Ideal output probabilities
Noisy output probabilities
Output Probability Total Variation Distance
```

---

## Step 11: Explanation Drawerを実装する

Beginner向けの短い説明です。

対象：

```text
State Fidelity
Purity
Effective Operation Time
Output Probability Distance
Noise level
Temperature parameter
Magnetic field parameter
```

1項目あたり1〜2文で十分です。

例：

```text
State Fidelity:
理想状態にどれだけ近いかを表します。1.0に近いほど理想に近い状態です。
```

---

## Step 12: Error / Warning Displayを接続する

Phase 2の `ValidationIssue` や `SimulationResult.warnings` をUIに出します。

Beginnerでは短く表示します。

```text
Noise level は 0.0〜1.0 の範囲で指定してください。
```

Expert向け詳細はまだ不要です。

UI要件でも、Beginnerでは原因と修正方法を短く表示する方針です。

---

# Codexに渡す指示文

以下をそのまま使えます。

```text
Task:
Implement Phase 4: Beginner Mode UI.

Goal:
Connect the Phase 1-3 core API and circuit editor state to a usable Beginner UI. The UI should allow a beginner user to start from a Start Screen, create or load a simple circuit, set normalized environment parameters, run simulation, and inspect main results.

Required changes:

1. Create UI component files if appropriate:
   - app/ui/start_screen.py
   - app/ui/beginner_mode.py
   - app/ui/gate_palette.py
   - app/ui/circuit_editor.py
   - app/ui/environment_panel.py
   - app/ui/result_summary.py
   - app/ui/result_drawers.py
   - app/ui/error_display.py

2. Start Screen:
   - Show Yuragi-Strider title
   - Show short app description
   - Show display level choice: Beginner / Expert
   - Add Run Demo button
   - Add Start Tutorial button
   - Add Open Config button
   - Expert can be shown as coming soon if not implemented

3. Beginner Mode:
   - Show Gate Palette
   - Show Circuit Editor
   - Show Environment Panel
   - Show Run Simulation button
   - Show Result Summary
   - Show Graph Drawer
   - Show Output Probabilities Drawer
   - Show Explanation Drawer

4. Gate Palette:
   - Include I, H, X, Z, Measure
   - CNOT may be hidden or disabled until 2-qubit UI is ready

5. Circuit Editor:
   - Use CircuitState and CircuitHistory from core
   - Support Add Gate
   - Support Remove Gate
   - Support Undo
   - Support Redo
   - Support Clear Circuit
   - This can be selectbox/button based for Phase 4
   - Do not implement final Drag & Drop yet

6. Environment Panel:
   - Add sliders for:
     - temperature
     - magnetic_field
     - noise_level
   - Range must be 0.0 to 1.0
   - Add Low noise and High noise presets
   - Show note that these are normalized parameters, not strict hardware values

7. Run Simulation:
   - Convert CircuitState to CircuitConfig
   - Build EnvironmentConfig
   - Build SimulationConfig
   - Call run_simulation(config)
   - Do not call lower-level physics functions directly from UI

8. Result Summary:
   - Show State Fidelity
   - Show Purity
   - Show Effective Operation Time
   - Show Output Probability Distance if available
   - If a value is not available, show "not available"

9. Result Drawers:
   - Graphs drawer expanded by default
   - Output Probabilities drawer collapsed by default
   - Explanation drawer collapsed by default
   - Graphs drawer should show Fidelity and Purity over time
   - Fidelity graph should show threshold and effective time marker if possible

10. Error Display:
   - Show SimulationResult warnings or validation issues
   - Beginner messages should be short and actionable

Acceptance criteria:
- Existing tests still pass
- App starts without import errors
- Start Screen is visible on launch
- User can enter Beginner Mode
- User can add H gate to q0
- User can undo and redo circuit edits
- User can clear circuit
- User can set temperature, magnetic_field, noise_level
- User can run simulation through run_simulation(config)
- Result Summary displays fidelity, purity, and effective operation time
- Graph Drawer displays fidelity and purity curves
- Output Drawer exists
- Explanation Drawer exists
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
- Do not implement Compare Workflow
- Do not implement Save/Load backend
- Do not implement QuTiP, Rust, FastAPI, or Godot
```

---

# 実装後の確認手順

## 1. テスト

```powershell
python -m unittest discover -s tests
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

以下を手で確認します。

```text
Start Screenが表示される
Beginner Modeに入れる
Hゲートを追加できる
Undoできる
Redoできる
Clearできる
temperature/magnetic_field/noise_levelを変更できる
Run Simulationできる
State Fidelityが表示される
Purityが表示される
Effective Operation Timeが表示される
Graphs Drawerが開く
Output Drawerが存在する
Explanation Drawerが存在する
```

---

# Phase 4完了条件

```md
## Phase 4 Checklist

### Start Screen

- [ ] Yuragi-Striderタイトルが表示される
- [ ] アプリ説明が表示される
- [ ] Beginner / Expert を選択できる
- [ ] Run Demo ボタンがある
- [ ] Start Tutorial ボタンがある
- [ ] Open Config ボタンがある

### Beginner Mode

- [ ] Gate Palette が表示される
- [ ] Circuit Editor が表示される
- [ ] Environment Panel が表示される
- [ ] Result Summary が表示される
- [ ] Graph Drawer が表示される
- [ ] Output Probabilities Drawer が表示される
- [ ] Explanation Drawer が表示される

### Circuit UI

- [ ] Hゲートを追加できる
- [ ] I/X/Z/Measureを選べる
- [ ] Undoできる
- [ ] Redoできる
- [ ] Clear Circuitできる
- [ ] CircuitStateからCircuitConfigへ変換して実行できる

### Environment

- [ ] temperature slider がある
- [ ] magnetic_field slider がある
- [ ] noise_level slider がある
- [ ] Low noise preset がある
- [ ] High noise preset がある
- [ ] 正規化パラメータである注記がある

### Simulation

- [ ] UIがrun_simulation(config)を呼ぶ
- [ ] lower-level physics関数をUIから直接呼ばない
- [ ] SimulationResultを受け取れる

### Results

- [ ] State Fidelity が表示される
- [ ] Purity が表示される
- [ ] Effective Operation Time が表示される
- [ ] Fidelity graph が表示される
- [ ] Purity graph が表示される
- [ ] threshold line または effective time marker が表示される
- [ ] warnings/error を表示できる

### Safety

- [ ] coreにStreamlit依存がない
- [ ] 物理モデルを変更していない
- [ ] 新しい外部依存を追加していない
- [ ] 既存テストが通る
```

---



---


Phase 4の核心は、**Beginnerにとって迷わない最小画面を作ること**です。


```text
Start Screen
↓
Beginner Mode layout
↓
CircuitState接続
↓
Environment sliders
↓
run_simulation(config)
↓
Result Summary
↓
Graph Drawer
↓
Output / Explanation Drawer
```
