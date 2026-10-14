# Shared-QPU isolation physics v0

This package is an internal extension around Physical Baseline V1. It is not
registered in the public Gate-aware or Pulse API, and therefore cannot yet be
selected from the UI. Empty/disabled extensions reproduce the baseline
Hamiltonian or state exactly.

## Mechanisms

### Independent idle evolution (internal kernel only, 2026-10-04)

`IndependentIdleModel` takes a bounded tuple of `IdleQubitSpec`, an explicit
caller resource ceiling, a joint qubit density array and `elapsed_us`.
Time constants T1/Tphi and equilibrium excited population are required;
positive infinity disables a process. The population difference decays by
`exp(-dt/T1)` and coherence by `exp(-dt/(2*T1)-dt/Tphi)`.
The local thermal-relaxation channel is composed with pure dephasing, using
the same conventions as the existing generalized amplitude-damping and
phase-damping primitives. Population transfer uses `-expm1(-dt/T1)` for small
intervals. Qubit zero is the MSB factor.

The implementation updates diagonal/off-diagonal tensor blocks of the joint
state; it does not replace that state with a product of marginal states or
construct full embedded Kraus operators. Time is O(n*4^n), memory O(4^n),
independent of elapsed time or frame count. Inputs are not mutated, including
zero-duration and disabled evolution. The kernel checks size, finite values,
Hermiticity and unit trace but trusts caller-side PSD validation, avoiding a
cubic eigensolve on every idle update. A future public boundary must validate
PSD before using this internal kernel; it is not an untrusted-input API.

This opt-in model assumes independent, constant-rate, two-level dissipative
idle dynamics. It contains no Hamiltonian/ZZ, driven evolution, drift, QEC or
three-level leakage. Only use it where those dynamics are absent by declared
conditions; otherwise select a supporting model or reject the combination
before execution. Intervening physical events split intervals. No unsupported
effect is silently approximated by these local channels.

The kernel reads no host clock and commits no state. It is not connected to
`SharedQpuRuntime`, world clock, scenario profiles or public Gate-aware/Pulse
selection. Parameters used by the benchmark are synthetic, not adopted
hardware/challenge defaults. Model integration must preserve the declared
physical event timeline and must not add host computation delays to dt.
Reference: [Qiskit Aer thermal relaxation channel](https://qiskit.github.io/qiskit-aer/stubs/qiskit_aer.noise.thermal_relaxation_error.html)
for the T1/T2/time/equilibrium-population parameterization; no Aer dependency
is added. Correspondence is `1/T2=1/(2*T1)+1/Tphi`.

Validation: `tests.test_idle_evolution` compares complex correlated 1-3 qubit
states against independently embedded existing Kraus channels, split intervals,
spectator marginals/coherence, long-time equilibrium, tiny/extreme ratios,
zero/disabled intervals, ownership, explicit bounds and clock independence.
`scripts/benchmark_idle_evolution.py` measures isolated-process kernel calls
and peak process RSS for 1/4/8 qubits. This excludes state-store PSD checking,
commit, scheduling, transport and rendering.

### Imperfect Reset

For reset success probability `s`, prior local state `rho`, and configured
diagonal reset target `tau`, the channel is

```text
E(rho) = (1 - s) rho + s tau Tr(rho).
```

This is CPTP in both the two- and three-level local spaces. Unlike a fixed
preparation error, its failed branch retains the previous job's state and can
therefore support a later cross-job information-retention experiment. It is a
phenomenological abstraction of active reset, not a reproduction of one
specific feedback or unconditional-reset circuit.

### Reset model boundary and calibration

`ResetModel` is the internal replaceable boundary used by reset scenarios.
`PhenomenologicalResetModel` adapts the existing CPTP primitive to that
boundary and remains explicitly labeled `behavioral_approximation`. Scenario
orchestration must select implementations through `ResetModelRegistry`; it
must not branch on paper names or hard-coded model identifiers. A later
measurement-instrument plus conditional-X model must implement the same
boundary.

For a bounded qubit register, `ResetModelRegistry.run_register()` accepts
`ResetModelRequest.subsystem_count` and explicit reset `targets`, and returns
`RegisterResetResult` (the retained joint state plus target-ordered independent
assignment channels). One-qubit models remain compatible through an adapter;
larger registers require an explicit model `run_register()` implementation.
The phenomenological adapter evaluates the same local Kraus channel via the
equivalent partial-trace replacement formula, without discarding spectator
coherence or correlations. This register
extension does not yet provide correlated readout, relaxation during gates,
or simultaneous cross-job coupling.

Every implementation declares a `ResetModelMode`. The stateful shared-QPU
runtime accepts only `state_transition_with_readout`, where the post-reset
state and readout assignment channel are explicit. A future
`effective_observation_only` implementation may provide a directly fitted
observation distribution, but it requires a separate runtime path and must
not be composed with another readout channel.

`derive_reset_profile` calculates `P(attacker=1 | victim=0)` and
`P(attacker=1 | victim=1)` from the selected simulator model. Profiles are
labeled `simulator_derived` and record the model version, a parameter-derived
`model_configuration_id`, model mode,
victim-dependent or fixed-zero control input, reset count, measurement
condition, seed policy, and generation time. They are not fitted to paper
curves or presented as hardware replication.

`run_reset_leakage_calibration` computes exact balanced-prior binomial metrics
for candidate shot budgets: per-shot mutual information, per-case mutual
information, Bayes-optimal binary accuracy, attacker advantage, and host
computation time. The shared
`core.attack_simulation.scenario_policy.ObservationBudget` separately limits
attacker-visible shots, training queries, victim observations, and retries;
grader case count is not an attacker budget.

### Residual ZZ

For an edge `(i, j)`, the added cross-Kerr Hamiltonian is

```text
H_ZZ = zeta_ij n_i n_j.
```

Within the computational subspace this differs from `zeta_ij Z_i Z_j / 4`
only by local-Z and global terms. The number-operator form naturally extends
to the existing three-level transmon truncation. `zeta_ij` is the conditional
frequency shift in radians per microsecond and may be signed.

### Unintended drive transfer

Each directed edge stores a dimensionless complex coefficient `c_ij`. An
intended source envelope `Omega_i = x_i + i y_i` adds
`c_ij Omega_i` to the target's existing X/Y drive operators. The graph is
sparse; no dense control-line transfer matrix is materialized. The current
model is instantaneous and frequency independent.

### Correlated Readout

A cluster of `k` qubits uses a `2^k x 2^k` column-stochastic confusion matrix,
with rows as observed labels and columns as true labels. Cluster axes are
transformed directly in the probability tensor, avoiding construction of the
global `2^n x 2^n` matrix. Clusters must be disjoint so application order does
not silently become part of the physical contract.

## Literature basis and adaptation

- Magnard et al., *Fast and Unconditional All-Microwave Reset of a
  Superconducting Qubit*, PRL 121, 060502 (2018),
  DOI `10.1103/PhysRevLett.121.060502`.
- Mi, Deng, and Szefer, *Securing Reset Operations in NISQ Quantum
  Computers*, CCS 2022, DOI `10.1145/3548606.3559380`.
- Ni et al., *Scalable Method for Eliminating Residual ZZ Interaction between
  Superconducting Qubits*, PRL 129, 040502 (2022), arXiv `2111.13292`.
- Wang et al., *Control and mitigation of microwave crosstalk effect with
  superconducting qubits* (2022), arXiv `2207.08416`.
- Seo and Bae, *Scalable quantum readout-error mitigation with state-dependent
  readout noise*, PRX Quantum 6, 010307 (2025),
  DOI `10.1103/PRXQuantum.6.010307`.
- Robertson and Song, *Categorizing Readout Error Correlations on Near Term
  Quantum Computers* (2021), arXiv `2104.04607`.

The sources justify the included mechanism classes, not universal parameter
values. Device-specific reset populations, ZZ rates, transfer coefficients,
and confusion matrices remain explicit scenario inputs and require separate
calibration or stated assumptions.

Magnard et al. supports the physical background of active reset. Mi et al.
supports the same-qubit sequential-reuse security scenario, but the current
success-replacement/failure-retention channel does not reproduce that paper's
measurement-plus-conditional-X mechanism or fitted `Q(theta)` error channel.
Paper-traceable use therefore requires the separate scenario contract and
equivalence tests documented in
`docs_for_develop/development/attack-simulation/reset-leakage-paper-reproduction-contract.md`.
