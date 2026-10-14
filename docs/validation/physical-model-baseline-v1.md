# Physical Model Baseline V1

## Decision

**PASS WITH RESTRICTIONS**

This baseline fixes the physical-model state that precedes shared-QPU
tenant-isolation work. It freezes physical semantics only; it does not add
tenants, attackers, job persistence, or mitigation UI.

```text
freeze ID: yuragi_strider_physical_baseline_v1
artifact: validation_results/physical_model_baseline_v1.json
```

## Included physical contract

The existing model identities remain unchanged:

- `gate_aware_hamiltonian_lindblad_v1`
- `driven_two_level_rwa_experimental_v1`
- `driven_transmon_qutrit_rwa_experimental_v1`
- `driven_coupled_transmon_network_rwa_experimental_v1`

| Input | Rule |
| --- | --- |
| Per-qubit coherence | `per_qubit` supplies one positive `t1_max_us` / `tphi_max_us` pair per logical qubit or transmon. |
| Input mode | Per-qubit coherence is accepted only in `physical` mode. |
| Shared fallback | Omitting `per_qubit` applies the historical shared T1/Tphi values to every subsystem. |
| Local dissipation | Each local coherence pair creates local relaxation, thermal-excitation, and dephasing collapse operators. |
| Per-qubit readout | One `(p10, p01)` pair per subsystem; every pair satisfies `p10 + p01 < 1`. |
| Readout boundary | Readout changes observed probabilities and counts only, never the simulated density matrix. |

The Gate-aware path supports this extension within its existing logical-qubit
limits. The coupled-transmon network supports one to four transmons at two or
three local levels.

## Excluded from this baseline

- shared-QPU scheduling, tenants, authorization, and cross-job state;
- Reset or imperfect Reset;
- unintended-drive crosstalk, residual ZZ, and control-line transfer effects;
- correlated readout, IQ samples, and joint measurement classification;
- calibration drift, time-varying parameters, and non-Markovian noise;
- calibrated real-device prediction.

These exclusions are the starting boundary for the tenant-isolation experiment
mode. Future work that adds one must use a new physical or attack-scenario
version and compare against this baseline.

## Evidence and regeneration

The JSON artifact records source hashes, model identifiers, environment
versions, and two direct smoke cases: a two-qubit Gate-aware request and a
two-transmon network request, each with distinct local T1/Tphi and readout
pairs.

```powershell
.\.venv\Scripts\python.exe scripts\freeze_physical_model_baseline.py
.\.venv\Scripts\python.exe -m unittest tests.test_physical_model_baseline
```

Before creating a release tag, also run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
cd frontend
npm.cmd run validate:pulse-lab
npm.cmd run lint
npm.cmd run build
```

Commit the implementation, tests, document, and artifact together, then create
an annotated `yuragi-strider-physical-baseline-v1` tag. Do not change physical
semantics for later tenant-isolation UI without a separately versioned contract.
