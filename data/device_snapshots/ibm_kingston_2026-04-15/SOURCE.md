# ibm_kingston calibration snapshot (2026-04-15)

Per-qubit calibration data of the IBM device `ibm_kingston`, used as the
standard heterogeneous values of the attack simulator (decisions.md D66).

## Origin

The two JSON files are verbatim copies from the open-source package
`qiskit-ibm-runtime` 0.50.0 (`fake_provider/backends/kingston/`). They are
IBM's published snapshot of that device; no account, payment or job run was
involved in obtaining them. Copied on 2026-10-07. Neither file was modified.

| File | Source path in the wheel | SHA-256 |
| --- | --- | --- |
| `props_kingston.json` | `qiskit_ibm_runtime/fake_provider/backends/kingston/props_kingston.json` | `0ab157926fb5002a58ef1f5a240ffbe1adaf7facbebfb0f184f86d289785efa5` |
| `conf_kingston.json` | `qiskit_ibm_runtime/fake_provider/backends/kingston/conf_kingston.json` | `9947a3cf6902267afae47341b101029945d731b342ca88a1411c9ad11bfe8361` |
| `LICENSE.txt` | `qiskit_ibm_runtime-0.50.0.dist-info/licenses/LICENSE.txt` | `7b7de08c64b1824bdc2b9f4b530814a9d0b5905a38ad0e96a717f3dd46d2bbe7` |

Wheel: `qiskit_ibm_runtime-0.50.0-py3-none-any.whl`, SHA-256
`d49e0ede6d0500587b9afce25c83195c9ba4b588c337a690650e133198d8f42d`.

## License

Copyright 2021 IBM and its contributors. Licensed under the Apache License,
Version 2.0; a copy is in `LICENSE.txt`. Qiskit is not a dependency of this
project; only these data files are redistributed.

## Contents and limits

- `last_update_date` 2026-04-15T09:15:15+02:00. One point in time: the device
  drifts daily (qubit 150 had T1 300 µs on 2026-08-03 but 363 µs here).
- 156 qubits; the coupling map has 352 directed entries, i.e. 176 physical
  couplers. Basis gates `cz id rz sx x`.
- Per qubit: T1, T2, readout errors (`prob_meas0_prep1`, `prob_meas1_prep0`,
  `readout_error`), readout length. No qubit frequency. Qubit 146 has no T1/T2.
- Per gate: error and length for `sx`, `x`, `cz`, `measure`, `reset` and others.
- All T2 ≤ 2·T1. Some qubits are effectively faulty (readout error above 10%:
  1, 31, 92, 96, 99, 113, 127, 131, 132, 146; qubits on a CZ edge with error
  ≥ 0.5: 83, 96, 103, 112, 113, 120, 121, 130, 131, 145, 146, 147). How the
  simulator treats them is decided in D66, not here.
