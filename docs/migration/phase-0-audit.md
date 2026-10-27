# Rust migration and shared security foundation: phase 0 audit

Audit date: 2026-10-28  
Audited revision: `b2cb244` (`Ui renew`) on `master`  
Scope: source and design-contract review; no physical hardware or external service was accessed.

## Summary

The repository already has a substantial Python attack-simulation foundation and an optional Rust numerical preview. The product is not yet Rust-centered: FastAPI, scenario compilation, service/runtime state, attack evaluation, and the main simulation orchestration remain Python. The Rust extension is called through PyO3 and is not installed in this audit environment.

The migration should preserve the current Python contracts as the behavioral reference. In particular, `attack_scenario_manifest_v2`, frozen physical-model baselines, current numerical tolerances, and explicit statuses for experimental models must remain authoritative during the first Rust vertical slice.

## Current implementation inventory

| Area | Current state | Evidence |
| --- | --- | --- |
| UI | React/TypeScript | `frontend/`; API calls remain the existing interface |
| API and orchestration | FastAPI/Python | `api/`; the shared attack environment and its lifecycle are Python-owned |
| Physical simulator | Python/NumPy by default; optional Rust dense preview | `core/simulator.py`, `core/backend_boundary.py`, `core/rust_dense_kernel.py` |
| Rust | One PyO3/maturin extension crate, not a Rust workspace | `rust_kernels/yuragi_strider_rust/Cargo.toml`, `src/lib.rs` |
| Independent numerical validation | QuTiP/SciPy validation paths and frozen artifacts | `validation_pulse/`, `validation_cptp/`, `validation_results/`, `tests/test_*qutip*` |
| Shared scenario model | Implemented in Python; immutable content-addressed manifest, schema `attack_scenario_manifest_v2` | `core/attack_simulation/scenario_compiler.py` |
| Authorization / observation limits | Implemented in Python | `job_authorization.py`, `scenario_policy.py`, `observation_budget.py`, `credential_policy.py` |
| Resources / concurrency | Physical resource binding, topology, declared crosstalk and shared runtime implemented for declared profiles | `resource_binding.py`, `qpu_topology.py`, `concurrency_policy.py`, `shared_qpu_runtime.py` |
| Persistence / multi-process recovery | Not complete | `docs_for_develop/development/attack-simulation/README.md`, `progress.md` |

### Rust boundary and data movement

`core/rust_dense_kernel.py` converts tuple-based complex matrices to Python `list[float]` in row-major interleaved real/imaginary form. PyO3 converts those lists to Rust `Vec<f64>`; results travel back as Python lists and are reconstructed into tuple matrices. This entails Python allocation and a Rust-owned copy on inputs, then a Rust-to-Python result conversion and Python reconstruction on outputs. It is evidence of conversion work, not evidence that the boundary is a performance bottleneck.

The call granularity is already better than one PyO3 round trip per RK4 timestep for session-based evolution: `RustDenseSession` holds state and collapse operators and submits a piecewise schedule to a batched Rust method. However, registering each distinct Hamiltonian uses a separate PyO3 method call, and the schedule and output states still cross the boundary. Stateless exponential/Lindblad helpers transfer inputs and outputs on each helper call. A future process boundary would also serialize data; it should be compared empirically before selection.

No timing, memory, or numerical parity claim is made in this audit. The execution host has Python 3.12.14, no `rustc`/`cargo`, and no importable `yuragi_strider_rust` module. Therefore a same-input Python-vs-Rust benchmark and Rust parity run could not be performed here. This is the key Phase 0 verification gap.

The reproducible benchmark entry point is `scripts/benchmark_backend_boundary.py`. It times the same one-qubit raw fixed-step Lindblad RK4 input through Python and the Rust wrapper, reports warm-ups/repeats, boundary call count, f64 payload sizes, separate Python flatten/reconstruction medians, and Python-traced peak allocations for both paths. `tracemalloc` excludes native Rust allocations, which remain unmeasured. It exits with an explicit unavailable result when the extension is missing. It intentionally does not claim numerical parity from timing.

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

Result: 61 tests executed; 59 passed, 1 failed, and 1 test module could not import. The backend test requiring an installed Rust extension failed because the extension is absent. The crosstalk challenge test module could not import because FastAPI is not installed. The remaining selected scenario, budget, analysis, grader, and concurrency tests passed. A follow-up without the unavailable Rust-backend and FastAPI-dependent tests ran 51 tests successfully.

This is an environment-limited baseline, not a clean repository-wide test result. Full Python dependencies and Rust tooling need to be provisioned before claiming full acceptance.

## Migration risks

1. **Contract drift:** current scenario and authorization rules are rich Python dataclasses and policy code. A parallel Rust schema could silently diverge unless tested from shared golden JSON fixtures.
2. **Numerical drift:** model equations and published tolerances are already frozen in tests and validation artifacts. Port code without changing equations, units, integration order, or fallback behavior in the same change.
3. **False acceleration claims:** conversion overhead is visible in source, but no paired benchmark is available. Measure Python conversion, PyO3 call, kernel, reconstruction, and end-to-end time separately with the same input and precision.
4. **Secret/observation leakage:** keeping the compiler and actor projections in Python while moving only physics can preserve the current boundary. Moving API state requires explicit confidentiality and tenant-isolation contract tests.
5. **Over-broad support declarations:** capability constants include experimental models. The Rust backend must report exact model support and reject unsupported configurations before execution.
6. **Deployment risk:** PyO3 packaging differs by platform and Python ABI. A Rust service/IPC design would add process lifecycle, serialization, failure, and recovery contracts; it is not justified by this audit alone.

## Recommended next vertical slices

1. Define a versioned Rust experiment input/output contract compatible with `attack_scenario_manifest_v2` and current capability declarations. Validate unknown fields/model IDs explicitly; establish golden JSON fixtures from Python.
2. Move one bounded, stateless physics operation with frozen inputs and established tolerances, then add cross-language parity and invariant tests. Benchmark all transfer and compute phases before deciding whether to keep PyO3, batch more work, or use IPC.
3. Move a complete Reset-leakage experiment operation across one coarse Rust boundary while keeping Python API authorization and observation projection in place. Compare against existing Python scenario outputs and test that internal truth is absent from attacker-visible output.
4. Add the declared two-region crosstalk case only after the same schema can express its resource bindings, overlap window, crosstalk assumptions, and supported topology. Reject wider topologies until implemented.
5. Consider Rust ownership of runtime/API state only after the physics slice is stable and state persistence/replay requirements have a testable contract.

These are migration steps, not completed work. This audit does not claim the Rust migration or shared Rust security runtime is complete.
