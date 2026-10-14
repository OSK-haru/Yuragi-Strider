"""Process characterization of a Gate-aware circuit: average gate fidelity and chi.

The noisy process is the map from the initial state to the state at circuit
completion, simulated with the same Gate-aware open-system evolution as a
normal run. The reference is the circuit's ideal unitary U.

Two estimators share one definition, the entanglement (process) fidelity

    F_e = <<U| J |U>> / d^2 = (1/d^3) sum_k Tr(U P_k U^dag E(P_k)),

and report the average gate fidelity F_avg = (d F_e + 1) / (d + 1)
(Horodecki et al. 1999; Nielsen 2002).

* Exact (n <= EXACT_TOMOGRAPHY_MAX_QUBITS): simulate the 4^n product inputs
  {|0>, |1>, |+>, |+i>}^n, which span operator space, and invert linearly to
  the Choi matrix. This also yields the chi matrix in the Pauli basis.
* Monte Carlo (larger n): draw a Pauli P uniformly and one product eigenstate
  rho_s of P with sign s. The sample sgn(s) Tr(U P U^dag E(rho_s)) is an
  unbiased estimate of F_e because E(P) = sum_s sgn(s) E(rho_s) over the 2^n
  product eigenstates, and 4^n 2^n / d^3 = 1. Each sample is one simulation.
  The chi matrix is not estimated: it has d^4 entries.
"""

from __future__ import annotations

import itertools
import math
from time import perf_counter
from typing import Any

import numpy as np

from core.gate_compiler import compile_gate_aware_circuit
from core.gates import column_duration_us, gate_unitary
from core.results import SimulationConfig
from core.simulator import environment_is_ideal, run_simulation
from core.state_metrics import PAULI_MATRICES

PROCESS_SCHEMA_VERSION = "process-characterization-v1"
EXACT_TOMOGRAPHY_MAX_QUBITS = 3
MONTE_CARLO_MAX_QUBITS = 6
MIN_MONTE_CARLO_SAMPLES = 16
MAX_MONTE_CARLO_SAMPLES = 512

_PAULI_LABELS = ("I", "X", "Y", "Z")
_TOMOGRAPHY_INPUTS = ("0", "1", "+", "+i")
# Row k gives P_k as a combination of the tomography input projectors:
# I = |0><0| + |1><1|, X = 2|+><+| - I, Y = 2|+i><+i| - I, Z = |0><0| - |1><1|.
_PAULI_FROM_INPUTS = np.array(
    [
        [1.0, 1.0, 0.0, 0.0],
        [-1.0, -1.0, 2.0, 0.0],
        [-1.0, -1.0, 0.0, 2.0],
        [1.0, -1.0, 0.0, 0.0],
    ]
)
# The (+1, -1) eigenstates of each Pauli; I uses |0>, |1> with sign +1 for both.
_PAULI_EIGENSTATES = {
    "I": (("0", 1.0), ("1", 1.0)),
    "X": (("+", 1.0), ("-", -1.0)),
    "Y": (("+i", 1.0), ("-i", -1.0)),
    "Z": (("0", 1.0), ("1", -1.0)),
}
# Two-sided normal quantile for the reported 95% interval.
_Z_95 = 1.959963984540054


def characterize_process(
    config: SimulationConfig,
    *,
    monte_carlo_samples: int = 64,
    monte_carlo_seed: int = 0,
    include_process_matrix: bool = True,
) -> dict[str, Any]:
    """Characterize the circuit channel of ``config``; raise ValueError if undefined."""

    started_at = perf_counter()
    n_qubits = int(config.circuit.logical_qubits)
    _require_unitary_circuit(config, n_qubits)
    if not MIN_MONTE_CARLO_SAMPLES <= int(monte_carlo_samples) <= MAX_MONTE_CARLO_SAMPLES:
        raise ValueError(
            f"monte_carlo_samples must be between {MIN_MONTE_CARLO_SAMPLES} "
            f"and {MAX_MONTE_CARLO_SAMPLES}"
        )

    unitary = circuit_unitary(config)
    channel = _CircuitChannel(config, unitary)
    dimension = 2 ** n_qubits

    if n_qubits <= EXACT_TOMOGRAPHY_MAX_QUBITS:
        choi = _choi_from_product_tomography(channel, n_qubits)
        process_fidelity = float(np.real(_vec(unitary).conj() @ choi @ _vec(unitary))) / dimension ** 2
        response: dict[str, Any] = {
            "method": "exact_tomography",
            "average_gate_fidelity": _average_gate_fidelity(process_fidelity, dimension),
            "standard_error": 0.0,
            "confidence_interval_95": None,
            "simulation_count": channel.simulation_count,
        }
        if include_process_matrix:
            chi = chi_from_choi(choi, n_qubits)
            ideal_chi = chi_from_choi(np.outer(_vec(unitary), _vec(unitary).conj()), n_qubits)
            response["process_matrix"] = {
                "basis": pauli_basis_labels(n_qubits),
                "noisy": _serialize_complex(chi),
                "ideal": _serialize_complex(ideal_chi),
                "trace": float(np.real(np.trace(chi))),
                "minimum_eigenvalue": float(np.min(np.linalg.eigvalsh(0.5 * (chi + chi.conj().T)))),
            }
    else:
        if include_process_matrix:
            raise ValueError(
                f"the chi matrix needs exact tomography, limited to {EXACT_TOMOGRAPHY_MAX_QUBITS} qubits"
            )
        samples = _monte_carlo_entanglement_fidelity_samples(
            channel,
            unitary,
            n_qubits,
            int(monte_carlo_samples),
            int(monte_carlo_seed),
        )
        mean = float(np.mean(samples))
        standard_error = float(np.std(samples, ddof=1) / math.sqrt(len(samples)))
        scale = dimension / (dimension + 1)
        low = _average_gate_fidelity(mean - _Z_95 * standard_error, dimension)
        high = _average_gate_fidelity(mean + _Z_95 * standard_error, dimension)
        response = {
            "method": "monte_carlo",
            "average_gate_fidelity": _average_gate_fidelity(mean, dimension),
            "standard_error": scale * standard_error,
            "confidence_interval_95": [max(0.0, low), min(1.0, high)],
            "simulation_count": channel.simulation_count,
            "monte_carlo_samples": len(samples),
            "monte_carlo_seed": int(monte_carlo_seed),
        }

    response.update({
        "schema_version": PROCESS_SCHEMA_VERSION,
        "qubit_count": n_qubits,
        "circuit_duration_us": channel.duration_us,
        "noise_free_environment": channel.noise_free,
        "elapsed_ms": round((perf_counter() - started_at) * 1000.0, 3),
    })
    return response


def circuit_unitary(config: SimulationConfig) -> np.ndarray:
    """The logical circuit's unitary, gates applied in column then list order."""

    n_qubits = int(config.circuit.logical_qubits)
    unitary = np.eye(2 ** n_qubits, dtype=np.complex128)
    for column in sorted(config.circuit.columns, key=lambda column: column.step):
        for gate in column.gates:
            unitary = np.asarray(gate_unitary(gate, n_qubits), dtype=np.complex128) @ unitary
    return unitary


def chi_from_choi(choi: np.ndarray, n_qubits: int) -> np.ndarray:
    """chi_mn = <<P_m| J |P_n>> / d^2, so E(rho) = sum_mn chi_mn P_m rho P_n and Tr chi = 1."""

    dimension = 2 ** n_qubits
    vectors = np.array([_vec(pauli) for pauli in _pauli_basis(n_qubits)])
    return vectors.conj() @ choi @ vectors.T / dimension ** 2


def pauli_basis_labels(n_qubits: int) -> list[str]:
    return ["".join(labels) for labels in itertools.product(_PAULI_LABELS, repeat=n_qubits)]


class _CircuitChannel:
    """Runs the circuit from a product input state and returns the completion state."""

    def __init__(self, config: SimulationConfig, unitary: np.ndarray) -> None:
        self.unitary = unitary
        self.simulation_count = 0
        compiled = compile_gate_aware_circuit(
            config.circuit,
            config.compilation_mode,
            config.native_gate_durations_us,
        ).circuit
        # Summed exactly as the simulator sums it, so the run takes the
        # gate-aware path and ends at circuit completion with no idle tail.
        self.duration_us = float(sum(column_duration_us(column) for column in compiled.columns))
        self.noise_free = environment_is_ideal(config)
        base = config.to_dict()
        base.update({
            "duration_us": self.duration_us,
            "time_steps": 2,
            "measurement_shots": 1,
            # Readout error acts on observation, not on the state.
            "readout_error": None,
            "snapshot_options": {
                "enabled": True,
                "uniform_count": 0,
                "custom_times_us": [],
                "include_initial": False,
                "include_final": True,
                "include_column_boundaries": False,
                "include_after_circuit": False,
            },
        })
        self._base = base

    def __call__(self, initial_states: tuple[str, ...]) -> np.ndarray:
        if self.noise_free or self.duration_us <= 0.0:
            # No time or no environment: the Gate-aware evolution is U itself.
            return self.unitary @ _product_density(initial_states) @ self.unitary.conj().T
        data = dict(self._base)
        data["circuit"] = {**data["circuit"], "initial_states": list(initial_states)}
        result = run_simulation(SimulationConfig.from_dict(data))
        self.simulation_count += 1
        blocking = [issue for issue in result.issues if issue.level in {"error", "fatal"}]
        if blocking:
            raise ValueError(f"process simulation failed: {blocking[0].message}")
        finals = [snapshot for snapshot in result.state_snapshots if snapshot.kind == "final"]
        if not finals:
            raise ValueError("process simulation returned no completion state")
        return np.asarray(finals[-1].density_matrix, dtype=np.complex128)


def _require_unitary_circuit(config: SimulationConfig, n_qubits: int) -> None:
    gates = [gate for column in config.circuit.columns for gate in column.gates]
    if any(str(gate.type).upper() == "MEASURE" for gate in gates):
        raise ValueError(
            "the circuit contains a measurement, so its ideal process is not unitary "
            "and the average gate fidelity is undefined"
        )
    if any(gate.condition is not None or gate.conditions for gate in gates):
        raise ValueError("classically conditioned gates have no single ideal unitary")
    if n_qubits > MONTE_CARLO_MAX_QUBITS:
        raise ValueError(
            f"process characterization supports at most {MONTE_CARLO_MAX_QUBITS} qubits"
        )


def _choi_from_product_tomography(channel: _CircuitChannel, n_qubits: int) -> np.ndarray:
    dimension = 2 ** n_qubits
    outputs = {
        labels: channel(labels)
        for labels in itertools.product(_TOMOGRAPHY_INPUTS, repeat=n_qubits)
    }
    paulis = _pauli_basis(n_qubits)
    images = []
    for pauli_indices in itertools.product(range(4), repeat=n_qubits):
        image = np.zeros((dimension, dimension), dtype=np.complex128)
        for input_indices in itertools.product(range(4), repeat=n_qubits):
            weight = math.prod(
                _PAULI_FROM_INPUTS[pauli_index, input_index]
                for pauli_index, input_index in zip(pauli_indices, input_indices)
            )
            if weight != 0.0:
                labels = tuple(_TOMOGRAPHY_INPUTS[index] for index in input_indices)
                image += weight * outputs[labels]
        images.append(image)
    pauli_images = np.array(images)
    # |i><j| = (1/d) sum_k (P_k)_{ji} P_k, so E(|i><j|) follows by linearity.
    unit_images = np.einsum("kji,kab->ijab", np.array(paulis), pauli_images) / dimension
    # J = sum_ij E(|i><j|) (x) |i><j|, output factor first.
    return unit_images.transpose(2, 0, 3, 1).reshape(dimension ** 2, dimension ** 2)


def _monte_carlo_entanglement_fidelity_samples(
    channel: _CircuitChannel,
    unitary: np.ndarray,
    n_qubits: int,
    sample_count: int,
    seed: int,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    samples = np.empty(sample_count)
    for index in range(sample_count):
        pauli = "".join(rng.choice(_PAULI_LABELS) for _ in range(n_qubits))
        labels: list[str] = []
        sign = 1.0
        for character in pauli:
            label, eigen_sign = _PAULI_EIGENSTATES[character][int(rng.integers(2))]
            labels.append(label)
            sign *= eigen_sign
        output = channel(tuple(labels))
        rotated = unitary @ _pauli_operator(pauli) @ unitary.conj().T
        samples[index] = sign * float(np.real(np.sum(rotated * output.T)))
    return samples


def _average_gate_fidelity(process_fidelity: float, dimension: int) -> float:
    return float((dimension * process_fidelity + 1.0) / (dimension + 1.0))


def _pauli_operator(pauli: str) -> np.ndarray:
    operator = np.ones((1, 1), dtype=np.complex128)
    for character in pauli:
        operator = np.kron(operator, PAULI_MATRICES[character])
    return operator


def _pauli_basis(n_qubits: int) -> list[np.ndarray]:
    return [_pauli_operator(label) for label in pauli_basis_labels(n_qubits)]


def _product_density(labels: tuple[str, ...]) -> np.ndarray:
    kets = {
        "0": np.array([1.0, 0.0], dtype=np.complex128),
        "1": np.array([0.0, 1.0], dtype=np.complex128),
        "+": np.array([1.0, 1.0], dtype=np.complex128) / math.sqrt(2.0),
        "-": np.array([1.0, -1.0], dtype=np.complex128) / math.sqrt(2.0),
        "+i": np.array([1.0, 1.0j], dtype=np.complex128) / math.sqrt(2.0),
        "-i": np.array([1.0, -1.0j], dtype=np.complex128) / math.sqrt(2.0),
    }
    ket = np.ones(1, dtype=np.complex128)
    for label in labels:
        ket = np.kron(ket, kets[label])
    return np.outer(ket, ket.conj())


def _vec(matrix: np.ndarray) -> np.ndarray:
    """|A>> = (A (x) I)|Omega>> = sum_ij A_ij |i>|j>, i.e. the row-major flattening."""

    return np.asarray(matrix, dtype=np.complex128).reshape(-1)


def _serialize_complex(matrix: np.ndarray) -> dict[str, list[list[float]]]:
    return {"real": np.real(matrix).tolist(), "imag": np.imag(matrix).tolist()}
