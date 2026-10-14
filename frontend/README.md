# Yuragi-Strider Frontend

The frontend is a React 19 and TypeScript application built with Vite. It is
the current Yuragi-Strider UI; the former Streamlit UI is not part of the active
tree.

## Local Development

Start the FastAPI service from the repository root:

```powershell
$env:ATTACK_SIMULATOR_PROFILE = 'development'
.\.venv\Scripts\python.exe -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8001
```

Then start Vite:

```powershell
cd frontend
npm ci
npm run dev
```

Vite proxies `/api` HTTP and WebSocket requests to `http://127.0.0.1:8001`.
The attack-simulator routes are mounted only for the explicit `development`
profile (or by the desktop launcher); the generic/public API profile leaves
them disabled.

## Routes

The application uses a small History API based screen switcher without a
routing dependency:

```text
/                      Home
/simulate              Gate-aware simulation
/circuit-studio        Circuit editor
/state-explorer        Gate-aware density-matrix and Bloch-state inspection
/algorithm-library     Algorithm presets
/pulse-lab             Experimental two-level, qutrit, and coupled-transmon
                       pulse simulation
/pulse-circuit-studio  Pulse sequence and waveform editor
/pulse-state-explorer  Pulse trajectory inspection (no Bloch sphere)
/help                  Help / Q&A
```

Gate-aware and Pulse-level are separate workspaces: the navigation menu only
lists the screens of the workspace you are in, and the two never share state.

## Current Features

- Physical environment parameter editing.
- Editable gate-duration defaults.
- 2-8 qubit circuit editor.
- Gate palette: H, X, Y, Z, S, T, RX, RY, RZ, CNOT, CZ, CP, CCX, SWAP, QFT,
  ORACLE, MEASURE, and the MESSAGE/RECEIVED annotation pair.
- Click and drag-and-drop gate placement.
- Multi-qubit and variable-width register gate placement and movement.
- Delete, drag-out deletion, Clear, Undo, and Redo.
- Circuit JSON import/export.
- Arbitrary `circuit_config` submission to `POST /api/simulate`.
- Pre-run decomposed-circuit preview via `POST /api/circuit/compile`, which
  compiles without simulating.
- Summary, timeline, output probability, diagnostic, model, warning, and API
  detail views.
- State snapshot requests and State Explorer visualization.
- Separate Pulse Lab for the frozen two-level baseline and qutrit extension.
- Square and Gaussian envelopes, target-angle or peak-amplitude input, and
  physical or direct-rate environments.
- Qutrit leakage timelines and summaries, DRAG control, and a 3x3 final
  density-matrix heatmap.
- Pulse State Explorer: a Pulse-level trajectory viewer that mirrors the
  gate-aware State Explorer panels (physical time playback, metric timeline,
  probability comparison, population distribution, density matrix) behind one
  shared time cursor. It has no Bloch sphere, because reducing a qutrit or a
  coupled-transmon state onto a sphere hides the leakage the panel exists to
  show.
- Client-side validation and bounded-work checks before pulse requests.

The responsibility boundary is explicit:

- Circuit Studio and State Explorer belong to the gate-aware
  `POST /api/simulate` flow.
- Pulse Lab runs one control pulse through `POST /api/pulse/simulate`.
- Pulse Lab does not read `CircuitEditorState` or `circuit_config`.
- Pulse results stay in Pulse Lab; they are not loaded into the gate-aware
  State Explorer.

Pulse Extension B is frozen as
`driven_transmon_qutrit_rwa_experimental_v1` /
`pulse-extension-b-v1` with the restrictions documented in
[`../docs_for_develop/validation/pulse-extension-b-report.md`](../docs_for_develop/validation/pulse-extension-b-report.md).

## Checks

```powershell
cd frontend
npm.cmd run lint
npm.cmd run build
npm.cmd run validate:pulse-lab
npm.cmd run validate:attack-live
```

No external drag-and-drop, routing, or charting library is used.
