# Rust register execution boundary (migration preview)

`yuragi_strider_core::register::DensityRegister` owns a qubit density matrix and applies an ordered batch atomically. Qubit zero is the most significant tensor factor. Failed batches leave the register unchanged. This bounded numerical primitive is not an authorization boundary, a public state API, or the complete QPU runtime: scheduling, observation budgets, tenant isolation, event commits and replay remain in the existing service.

The temporary PyO3 `RegisterSession` accepts `register_state_v1` at construction and `register_batch_v1` at execution. A batch contains tuples `(kind, operands, data, scalar)`:

| Kind | Data | Scalar |
| --- | --- | --- |
| `unitary` | Operand operator as row-major interleaved real/imaginary numbers | `0.0` |
| `reset` | For each operand: success probability, target-zero population, target-one population | `0.0` |
| `depolarize` | Empty | Channel weight |
| `dephase` | Empty | `0.0` |
| `idle` | For each operand: T1 in microseconds, Tphi in microseconds, equilibrium excited population | Elapsed microseconds |
| `measure` | Empty | Caller-supplied uniform draw in `[0, 1)` |

Operands for an operator define the tensor order. A batch returns only measurement bit arrays, in measurement-operation order. `execute_with_checkpoints` can additionally return states after specified, strictly increasing operation indices. It is intended for trusted internal adapters that must retain existing validation checks. Raw state and checkpoints must not be exposed to an untrusted participant.

The runtime config may explicitly select `register_backend: rust_register_v1`; old manifests without the field retain their existing Python execution. The selected value is frozen in the manifest and needs a provenance record. Unknown backends, unavailable extensions and unsupported reset models fail explicitly. The selected path sends each pre-measurement shot as a batch, including applicable idle intervals, kicks, local reset and gate errors; it validates every reset checkpoint through the existing state authority. Final measurement sampling, state commits, scheduling and API ownership remain Python in this transition. It is not yet a Rust-owned full-job runtime, and the user-facing UI does not select this backend.

The core supports the existing phenomenological qubit reset models and the declared independent constant-rate idle channel. It does not silently replace other reset models or infer a device noise model. Measurement draws come from the existing caller's random generator; migration of that generator and replay is separate. Existing Python and QuTiP paths remain independent validation references during migration.
