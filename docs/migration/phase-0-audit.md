# Rust migration and shared security foundation: phase 0 audit

Audit date: 2026-10-28  
Audited revision: `b2cb244` (`Ui renew`) on `master`  
Scope: source and design-contract review; no physical hardware or external service was accessed.

## Summary

The repository already has a substantial Python attack-simulation foundation and an optional Rust numerical preview. The product is not yet Rust-centered: FastAPI, scenario compilation, service/runtime state, attack evaluation, and the main simulation orchestration remain Python. Rust was absent during the initial audit; after the user authorized tool installation, Rust 1.96.0 and the existing PyO3 extension were installed in this environment.

The migration should preserve the current Python contracts as the behavioral reference. In particular, `attack_scenario_manifest_v2`, frozen physical-model baselines, current numerical tolerances, and explicit statuses for experimental models must remain authoritative during the first Rust vertical slice.

The owner's target is a Rust standard core and runtime, with Python retained for optional extension features and independent testing or validation. Python extensions may implement substantial functionality and use scientific libraries; they are not limited to wrapper code. The standard Rust core must run without requiring a Python extension. When an extension is used, collect the information it needs and exchange it in coarse-grained batches through PyO3, avoiding repeated high-frequency conversions and calls. Long-lived simulation state should remain Rust-owned; transfer the requested data across the extension boundary. The precise extension hosting and distribution mechanism is not yet decided. Existing Python service, scheduling and standard physics code is transitional. React/TypeScript remains the UI, and observation and authorization boundaries must be preserved throughout migration.

## Current implementation inventory

| Area | Current state | Evidence |
| --- | --- | --- |
| UI | React/TypeScript | `frontend/`; API calls remain the existing interface |
| API and orchestration | FastAPI/Python remains the production server; a parallel Rust/Axum slice implements only `/api/health` | `api/`, `rust_kernels/yuragi_strider_api/`; attack environment, lifecycle and all other routes remain Python-owned |
| Physical simulator | Python dense solver remains the application default; strict Rust RK4/CPTP kernels are opt-in, with statevector and post-circuit paths still pending | `core/simulator.py`, `core/backend_boundary.py`, `core/rust_dense_kernel.py` |
| Rust | Interpreter-independent `yuragi_strider_core` library plus a PyO3/maturin extension crate; not yet a complete Rust runtime | `rust_kernels/yuragi_strider_core/`, `rust_kernels/yuragi_strider_rust/` |
| Independent numerical validation | QuTiP/SciPy validation paths and frozen artifacts | `validation_pulse/`, `validation_cptp/`, `validation_results/`, `tests/test_*qutip*` |
| Shared scenario model | Implemented in Python; immutable content-addressed manifest, schema `attack_scenario_manifest_v2` | `core/attack_simulation/scenario_compiler.py` |
| Authorization / observation limits | Implemented in Python | `job_authorization.py`, `scenario_policy.py`, `observation_budget.py`, `credential_policy.py` |
| Resources / concurrency | Physical resource binding, topology, declared crosstalk and shared runtime implemented for declared profiles | `resource_binding.py`, `qpu_topology.py`, `concurrency_policy.py`, `shared_qpu_runtime.py` |
| Persistence / multi-process recovery | Not complete | `docs_for_develop/development/attack-simulation/README.md`, `progress.md` |

### Rust boundary and data movement

`core/rust_dense_kernel.py` converts tuple-based complex matrices to Python `list[float]` in row-major interleaved real/imaginary form. PyO3 converts those lists to Rust `Vec<f64>`; results travel back as Python lists and are reconstructed into tuple matrices. This entails Python allocation and a Rust-owned copy on inputs, then a Rust-to-Python result conversion and Python reconstruction on outputs. It is evidence of conversion work, not evidence that the boundary is a performance bottleneck.

The call granularity is already better than one PyO3 round trip per RK4 timestep for session-based evolution: `RustDenseSession` holds state and collapse operators and submits a piecewise schedule to a batched Rust method. However, registering each distinct Hamiltonian uses a separate PyO3 method call, and the schedule and output states still cross the boundary. Stateless exponential/Lindblad helpers transfer inputs and outputs on each helper call. A future process boundary would also serialize data; it should be compared empirically before selection.

Initial audit check: the execution host had Python 3.12.14, no `rustc`/`cargo`, and no importable `yuragi_strider_rust` module, so the first benchmark attempt was unavailable. After the toolchain was installed, `cargo check` succeeded and the extension built for CPython 3.12. The existing crate also required PyO3's `extension-module` feature for a clean link in this Linux environment; that build configuration is now explicit.

The reproducible benchmark entry point is `scripts/benchmark_backend_boundary.py`. It times the same one-qubit raw fixed-step Lindblad RK4 input through Python and the Rust wrapper, reports warm-ups/repeats, boundary call count, f64 payload sizes, separate Python flatten/reconstruction medians, and Python-traced peak allocations for both paths. `tracemalloc` excludes native Rust allocations, which remain unmeasured. It exits with an explicit unavailable result when the extension is missing. It intentionally does not claim numerical parity from timing.


Measured on this execution host (Python 3.12.14, Rust 1.96.0): dimension 2, zero Hamiltonian, one amplitude-damping collapse operator, `dt=0.0002`, 500 RK4 substeps, 2 warm-ups and 7 timed repetitions. Median Python end-to-end was 49.361 ms; Rust through PyO3 was 0.435 ms. Python flattening was 0.008212 ms and output reconstruction 0.000981 ms. The run used one PyO3 call per segment and accounted 256 bytes of flattened f64 payload including input and output. `tracemalloc` peaks were 4,608 bytes for the Python loop and 616 bytes of Python-traced allocation for the Rust wrapper; native Rust allocation peak was not measured. This is one narrow raw-kernel case; it does not establish speedup for full Gate-aware/Pulse simulations or attack scenarios.

## Behavior and support classification

| Feature | Classification | Audit finding |
| --- | --- | --- |
| Gate-aware | Implemented | Multiple declared models and explicit capacity limits are exposed through `core/capabilities.py`; model identity and limits must carry into Rust metadata. |
| Pulse | Implemented / experimental by model | Two-level and qutrit RWA models are marked available; coupled-transmon-network model is marked experimental. Preserve these labels and do not promote all declared models to stable support. |
| Reset leakage | Implemented for the declared training/challenge profile | The attack path is same physical qubit reuse with incomplete-reset state carryover; scenario settings define an observation budget and hidden evaluation policy. It is not a generic reset guarantee for all devices. |
| Concurrent drive crosstalk | Implemented for a declared, bounded profile | Two-region execution and explicit direction/strength declarations exist. Sample detuning/envelope values are assumptions, not calibration measurements. Do not imply arbitrary multi-region execution. |
| Persistence / recovery | Design / incomplete | In-memory scenario manifests and runtime state do not establish durable replay or multi-process restart recovery. |
| Physical hardware access | Not implemented in this simulator path | The attack environment is virtual and local to the application; this work does not add real-device access. |

## Scenario semantics to preserve

The existing common contract already separates attacker, victim, service, relation, and experiment configuration. A compiled manifest is immutable and content addressed, and carries schema version, assumptions, policy, allowed operations, observation budget, locked/hidden fields, evaluator identity, and submission settings.

Other semantics that must remain distinct in any Rust runtime/API migration:

- host computation time, simulated device time, and result publication time;
- event scheduling from event commitment and the established event ordering;
- program preparation, per-shot preparation, tenant/service reset, and explicit circuit reset;
- an actor's grants, scenario allowlist, service capabilities, authenticated tenant scope, and result projection;
- resource binding and scheduler/runtime capabilities;
- internal truth, actor-visible observations, and researcher-facing evaluation output;
- idempotent request receipts, retry accounting, observation budget reservations, and tenant-scoped result access.

The documented security boundary also includes credentials confined to the virtual service, hidden challenge configuration, and a bounded observation channel. A Rust API must reject unknown schema versions, unsupported model IDs, and unknown fields at its boundary; it must not fill them with defaults. Internal truth must never be returned as an attacker observation.

## Baseline tests attempted

Command:

```text
python -m unittest tests.test_backend_boundary tests.test_scenario_compiler \
  tests.test_attack_observation_budget tests.test_reset_leakage_analysis \
  tests.test_reset_leakage_grader tests.test_crosstalk_challenge \
  tests.test_concurrent_windows
```

Initial environment-limited result: 61 tests executed; 59 passed, 1 failed, and 1 test module could not import. The backend test requiring an installed Rust extension failed because the extension was absent. The crosstalk challenge test module could not import because FastAPI was missing. A subset without those dependencies ran 51 tests successfully. After installing the repository's pinned runtime dependencies and building the Rust extension, the same selected suite ran 81 tests successfully; the attack profile ran 338 tests successfully; the Rust/reset/physics subset ran 82 tests successfully.

At the start of the migration work, verification was focused because the Rust toolchain and runtime dependencies were absent. After the user authorized installing them, Rust 1.96.0, `rustfmt`, the project runtime/validation dependencies, and the editable PyO3 extension were installed. Once the reset kernel and its parity tests were added, `python scripts/run_tests.py full-audit` completed successfully: **1,804 tests passed** in 315.686 seconds. The focused reset/runtime set passed 83 tests. `cargo check`, `cargo fmt --check`, and `cargo test` also completed successfully; the crate currently defines no native Rust unit tests, so kernel behavior is covered by Python-to-Rust parity tests.

When this work resumed, the environment had no Rust toolchain and only an older extension. After installing stable Rust 1.99.1 and `rustfmt`, the new binary-analysis function passed `cargo fmt --check`, `cargo check`, and `cargo test`; the PyO3 extension was rebuilt in editable mode. Python/Rust parity and native schema/source-boundary tests then ran successfully. The focused freeze, initialization and shared-analysis set ran 36 tests; 35 passed and one fallback-only test was skipped because the native kernel was available.

Before the Reset training adapter was added, `python scripts/run_tests.py full-audit` with the rebuilt extension ran **1,815 tests: 1,814 passed and 1 was skipped** in 511.986 seconds. The skip exercises the missing-native-kernel fallback path and occurs when the kernel is available. Both canonical freeze scripts report PASS WITH RESTRICTIONS against the refreshed sources.

After adding the Reset training adapter, the focused Reset analysis, shared statistics, grader and challenge suite ran 33 tests: 32 passed and the same fallback-only test was skipped. This includes authorized training-job results and the Rust analysis path. `python scripts/run_tests.py attack` also passed all 338 tests in 91.947 seconds. The complete full-audit suite was not rerun for this adapter-only change.

After moving Crosstalk evaluation state and scoring into Rust, the focused Crosstalk, shared analysis, Reset and freeze suite ran 60 tests: 59 passed and the fallback-only test was skipped. The attack profile passed 342 tests in 110.365 seconds. Rust formatting, `cargo check`, `cargo test`, editable extension rebuild and locked release wheel builds succeeded. A CPython 3.14.4 wheel was installed in an isolated environment and passed an import and evaluation smoke test. Both freeze scripts report PASS WITH RESTRICTIONS. The Docker image itself was not built because the host has no Docker or Podman executable; container verification remains outstanding.

After extracting the Reset core, its independent Cargo test passed, and 62 focused Reset, isolation, shared runtime, challenge and freeze tests passed. A CPython 3.14.4 release wheel was rebuilt and passed a reset-session smoke test. The CPTP source fingerprint now includes the Rust core and crate manifests so future changes to the extracted implementation are detected. Both freeze scripts report PASS WITH RESTRICTIONS. The complete full-audit suite and Docker image were not rerun for this slice.

The first register-runtime audit established the opt-in runner path and originally used a pre-measurement batch. A later slice moved measurement collapse into the Rust batch while retaining Python random draws, readout assignment, state commits and cross-shot state. The earlier full-audit evidence remains historical; the current default-backend change requires a fresh full audit before it can be considered complete. Rust core tests and the previous full suite passed before this change; those results do not validate the current default switch.

The first full-audit run exposed eight subtest errors in one test helper: it copied the immutable actor grant mapping, then tried to access an `ActorRole` key with a plain string. The helper now selects the typed role key before changing its grant set; all 17 program-initialization tests pass. It also exposed two stale source fingerprints in committed freeze reports. The canonical freeze scripts refreshed those audit records against the current sources and installed Rust backend. The frozen physical contract, comparison evidence, API defaults, and numerical tolerance values were preserved; the Rust CPTP smoke is now recorded because the extension is available.

## Migration implementation progress

| Phase | Status | Implemented in this branch |
| --- | --- | --- |
| 0: audit | Complete | Source inventory, support classification, risk record, benchmark harness and measured baseline. |
| 1: contract | Partial | Added strict `local_reset_sequence_v1` to the PyO3 call boundary. Unknown schema versions and unsupported backend IDs are rejected. Rust execution backend and schema version enter the reset model's `configuration_id`, while the default Python configuration ID stays unchanged. This does not yet deserialize or validate the full Python `attack_scenario_manifest_v2` in Rust. |
| 2: physics core | Partial | Extracted qubit/qutrit reset and a qubit register kernel into the interpreter-independent `yuragi_strider_core` crate. New shared-QPU profiles select complete Rust shot batches, including measurement collapse, while legacy and generic runtimes retain Python. Circuit Studio exposes strict `rust_dense_v1` RK4/CPTP kernels as an opt-in backend; statevector and post-circuit degradation paths fail closed until migrated. Python still owns circuit compilation, readout assignment, event commits, state snapshots, and API orchestration. See [the boundary contract](rust-register-contract.md). |
| 3: runtime/API | Partial | Crosstalk challenge baseline, attempt counter, readiness and scoring are Rust-owned in `CrosstalkEvaluationRuntime`. New shared-QPU profiles select Rust complete-shot batches, including final measurement collapse. A separate Axum API contract-tests `GET /api/health` and implements a parallel `POST /api/circuit/compile` route for both compilation modes and the three built-in presets, including all eight decomposition rules. FastAPI remains authoritative in production. General job state, authorization, time, budgets, result publication and all other HTTP routes stay Python-owned. |
| 4: security analysis foundation | Partial | `security_binary_contrast_v1`, Reset training analysis, and Crosstalk share Rust binary-count statistics. The Reset adapter sends validated labelled public observations in one coarse PyO3 call for pooling, contrast metrics, and threshold fitting; the Python implementation remains an explicit test/reference backend. Crosstalk sends the complete private workload to Rust and publishes only the existing decision. Reset standard error assumes independent shots; its public analysis is not the hidden grader's case-level score. Provenance and phase labels are not authentication. Reset runner/client/UI integration and a public Crosstalk analysis adapter remain unconnected. |

### Default backend migration status (2026-10-28)

New product QPU profiles select `rust_register_v1`, while new Circuit Studio requests, generic QPU runtimes, and legacy `.qscope` files remain on Python. `rust_dense_v1` is available as a strict opt-in and rejects Rust execution failures rather than silently running Python. A full-suite trial of making it the Circuit Studio default exposed unsupported statevector, ideal-gate, post-circuit-degradation, and related conditional paths, so the application default was restored to preserve existing behavior. Those paths and several UI/API orchestration steps remain Python migration work. This is an intermediate Rust migration, not a Rust-default or Rust-only core; re-evaluate the default after those execution paths have moved and the full audit passes.

Earlier reset-kernel measurements (`scripts/benchmark_reset_kernel.py`) showed Python/NumPy and Rust/PyO3 at 0.174/0.172 ms for 4 qubits, 0.458/1.917 ms for 6 qubits, and 4.538/31.400 ms for 8 qubits. Those measurements preceded the core extraction. In the separate `scripts/benchmark_reset_session.py` comparison, four reset sequences over 8 qubits took a median 140.306 ms when the state crossed the Python boundary after every sequence, and 40.589 ms when one Rust-owned session executed the batch and returned the final state; 2 warm-ups and 7 repetitions, maximum output difference `0.0`. Both timings include initial and final Python conversions. This is one bounded benchmark and does not measure a complete job or native Rust memory.

## Migration risks

1. **Contract drift:** current scenario and authorization rules are rich Python dataclasses and policy code. A parallel Rust schema could silently diverge unless tested from shared golden JSON fixtures.
2. **Numerical drift:** model equations and published tolerances are already frozen in tests and validation artifacts. Port code without changing equations, units, integration order, or fallback behavior in the same change.
3. **False acceleration claims:** conversion overhead is visible in source, but no paired benchmark is available for the new Crosstalk runtime. Measure Python conversion, PyO3 call, kernel, reconstruction, and end-to-end time separately with the same input and precision.
4. **Secret/observation leakage:** keeping the compiler and actor projections in Python while moving only physics can preserve the current boundary. Moving API state requires explicit confidentiality and tenant-isolation contract tests.
5. **Over-broad support declarations:** capability constants include experimental models. The Rust backend must report exact model support and reject unsupported configurations before execution.
6. **Deployment risk:** PyO3 packaging differs by platform and Python ABI. A Rust service/IPC design would add process lifecycle, serialization, failure, and recovery contracts; it is not justified by this audit alone.

## Recommended next vertical slices

1. Extend the new batch contract from one prepared shot to a Rust-owned job/session lifecycle with retained cross-shot state, random-stream/replay rules and transactional event commits. Preserve existing tenant isolation and observation projection tests before changing defaults.
2. Wire an operator-controlled selection into the existing UI/product profile only after its product contract can freeze backend identity and provenance; keep the preview opt-in until whole-job parity and performance are measured.
3. Move a complete Reset-leakage experiment operation across one coarse Rust boundary. Keep existing authorization and observation projection during the transition, then move their ownership to Rust with matching isolation tests. Compare scenario outputs and verify that internal truth is absent from attacker-visible output.
4. Add the declared two-region crosstalk case only after the shared schema can express resource bindings, overlap windows, assumptions, and supported topology. Reject wider topologies until implemented.
5. Move general runtime/API state to Rust in stages, with explicit persistence, replay, authorization and isolation acceptance tests. The Crosstalk evaluator and pre-measurement register batch are bounded slices, not the general service runtime.

These are migration steps, not completed work. This audit does not claim the Rust migration or shared Rust security runtime is complete.
