# Yuragi-Strider Config Format

Yuragi-Strider circuit files use `.qscope.json`.

## Envelope

Required top-level fields:

- `schema_version`: format version, currently `1.1`
- `kind`: `yuragi_strider.config`
- `metadata`: user-facing name, description, or tags
- `circuit`: serialized `CircuitConfig`
- `environment`: serialized `EnvironmentConfig`
- `simulation`: duration, time steps, threshold, and model
- `ui`: optional UI hints

## Circuit

`circuit` contains:

- `logical_qubits`: 1 through 18 in the core config format
- `initial_states`: one of `0`, `1`, `+`, `-` per qubit
- `columns`: ordered gate columns with `step` and `gates`

Supported gate types are `I`, `H`, `X`, `Y`, `Z`, `S`, `T`, `RX`, `RY`, `RZ`,
`CNOT`, `CZ`, `CP`, `CCX`, `SWAP`, `QFT`, `ORACLE`, `MEASURE`, and `MESSAGE`.
For basis labels, `q0` is the most significant bit.

`QFT` and `ORACLE` are the gates with a variable operand count. They take no
controls and list their whole register in `targets`, most significant first, so
the qubits they span need be neither contiguous nor ascending. Their default
`duration_us` is `0.2` per spanned qubit rather than a single fixed value.

`ORACLE` additionally carries `params.marked_index`: the single computational
basis state whose phase it flips, given as an integer in `0 .. 2**len(targets)-1`
and read against the register's own bit order. Marking several states is done by
placing several oracles in sequence, since diagonal gates commute.

The React Circuit Studio import/export format is intentionally narrower:
2-8 qubits and initial states `0` or `1`. The editor stops at 8 because that is
the core's noisy density-matrix ceiling, so anything editable can also be run
under the gate-aware model. The FastAPI `circuit_config`
boundary accepts the same 1-18 range and `0`/`1`/`+`/`-` initial states as
the core config format; noisy density-matrix evolution is limited to 8
qubits, explicit CPTP to 5, and ideal measurement-free circuits above 5 qubits
use the statevector path instead.

## Environment

`environment` uses the unified environment model:

- `model`: `generic_superconducting_open_system_v1`
- `input_mode`: `normalized` or `physical`
- `normalized`: beginner-friendly normalized controls
- `physical`: expert physical-unit controls

Normalized input contains:

- `temperature_parameter`
- `magnetic_field_parameter` legacy key, interpreted as normalized flux-noise strength
- `noise_level`

Physical input contains:

- `device_quality`
- `temperature_mk`
- `flux_noise_phi0`
- `qubit_frequency_ghz`
- `t1_max_us`
- `tphi_max_us`
- `per_qubit` (optional physical-mode list; one entry per logical qubit, each
  containing `t1_max_us` and `tphi_max_us`)
- `ideal_reference`

Older `1.0` configs with `environment_model` values such as
`normalized_phenomenological_v1` or `superconducting_qubit_profile_v1` are
accepted and migrated to the unified model when loaded.

## Validation

Loaded configs are converted into `SimulationConfig` and validated through the
core validation layer before they are accepted.
