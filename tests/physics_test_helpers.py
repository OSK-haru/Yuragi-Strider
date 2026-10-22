from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.expert_data import reconstruct_final_density_matrix
from core.gates import Matrix, column_unitary, identity_matrix, matmul
from core.physical_environment import INPUT_MODE_NORMALIZED, INPUT_MODE_PHYSICAL
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import run_simulation


TRACE_TOL = 1e-8
HERMITICITY_TOL = 1e-8
PROBABILITY_SUM_TOL = 1e-8
EIGENVALUE_LOWER_TOL = -1e-10
FIDELITY_PURITY_TOL = 1e-10


def make_ideal_environment() -> EnvironmentConfig:
    return EnvironmentConfig(
        input_mode=INPUT_MODE_PHYSICAL,
        ideal_reference=True,
        device_quality=1.0,
        temperature_mk=0.0,
        flux_noise_phi0=0.0,
    )


def circuit_unitary(circuit: CircuitConfig) -> Matrix:
    """Ordered product of the circuit's column unitaries (no noise)."""
    unitary = identity_matrix(2 ** circuit.logical_qubits)
    for column in sorted(circuit.columns, key=lambda item: item.step):
        unitary = matmul(column_unitary(column, circuit.logical_qubits), unitary)
    return unitary


def assert_matrix_close(actual, expected, tolerance: float = 1e-12) -> None:
    """Fail unless every entry differs by at most ``tolerance`` (absolute).

    Shapes must match exactly and NaN never compares equal, so a malformed or
    broken result cannot pass by broadcasting or by being all-NaN.
    """
    np.testing.assert_allclose(
        np.asarray(actual, dtype=np.complex128),
        np.asarray(expected, dtype=np.complex128),
        atol=tolerance,
        rtol=0.0,
        equal_nan=False,
    )


def make_normalized_environment(
    temperature: float = 0.0,
    flux_noise: float = 0.0,
    noise_level: float = 0.0,
) -> EnvironmentConfig:
    return EnvironmentConfig(
        input_mode=INPUT_MODE_NORMALIZED,
        temperature=temperature,
        magnetic_field=flux_noise,
        noise_level=noise_level,
    )


def make_physical_environment(
    device_quality: float = 1.0,
    temperature_mk: float = 0.0,
    flux_noise_phi0: float = 0.0,
    t1_max_us: float = 100.0,
    tphi_max_us: float = 100.0,
    ideal_reference: bool = False,
) -> EnvironmentConfig:
    return EnvironmentConfig(
        input_mode=INPUT_MODE_PHYSICAL,
        device_quality=device_quality,
        temperature_mk=temperature_mk,
        flux_noise_phi0=flux_noise_phi0,
        qubit_frequency_ghz=5.0,
        t1_max_us=t1_max_us,
        tphi_max_us=tphi_max_us,
        ideal_reference=ideal_reference,
    )


def make_one_qubit_h_config(
    environment: EnvironmentConfig | None = None,
    duration_us: float = 1.0,
    time_steps: int = 21,
) -> SimulationConfig:
    return make_one_qubit_gate_config(
        "H",
        environment=environment,
        duration_us=duration_us,
        time_steps=time_steps,
    )


def make_one_qubit_gate_config(
    gate_type: str,
    environment: EnvironmentConfig | None = None,
    initial_state: str = "0",
    duration_us: float = 1.0,
    time_steps: int = 21,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=[initial_state],
            columns=[
                GateColumn(
                    step=0,
                    gates=[GateOperation(type=gate_type, targets=[0])],
                )
            ],
        ),
        environment=environment or make_ideal_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def make_initial_state_config(
    initial_state: str,
    environment: EnvironmentConfig | None = None,
    duration_us: float = 1.0,
    time_steps: int = 21,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=[initial_state],
            columns=[],
        ),
        environment=environment or make_ideal_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def make_bell_config(
    environment: EnvironmentConfig | None = None,
    duration_us: float = 1.0,
    time_steps: int = 21,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[GateOperation(type="H", targets=[0])],
                ),
                GateColumn(
                    step=1,
                    gates=[GateOperation(type="CNOT", targets=[1], controls=[0])],
                ),
            ],
        ),
        environment=environment or make_ideal_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def run_and_reconstruct(config: SimulationConfig) -> tuple[SimulationResult, Matrix]:
    result = run_simulation(config)
    matrix = reconstruct_final_density_matrix(result)
    if matrix is None:
        raise AssertionError("final density matrix was not reconstructed")
    return result, matrix


def matrix_trace(matrix: Matrix) -> complex:
    return sum(matrix[index][index] for index in range(len(matrix)))


def max_hermiticity_error(matrix: Matrix) -> float:
    return max(
        abs(matrix[row][column] - matrix[column][row].conjugate())
        for row in range(len(matrix))
        for column in range(len(matrix))
    )


def hermitian_eigenvalues(matrix: Matrix) -> list[float]:
    real_block = _complex_hermitian_to_real_symmetric(matrix)
    values = _jacobi_eigenvalues(real_block)
    values.sort()
    # The real representation duplicates every Hermitian eigenvalue.
    return values[::2]


def assert_no_nan_or_inf(values: Iterable[float]) -> None:
    for value in values:
        if not math.isfinite(float(value)):
            raise AssertionError(f"non-finite value: {value!r}")


def finite_numeric_derived_values(result: SimulationResult) -> list[float]:
    return [
        float(value)
        for value in result.derived_parameters.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]


def coherence_abs(matrix: Matrix, row: int = 0, column: int = 1) -> float:
    return abs(matrix[row][column])


def _complex_hermitian_to_real_symmetric(matrix: Matrix) -> list[list[float]]:
    dimension = len(matrix)
    block = [[0.0 for _ in range(2 * dimension)] for _ in range(2 * dimension)]
    for row in range(dimension):
        for column in range(dimension):
            value = matrix[row][column]
            block[row][column] = value.real
            block[row][column + dimension] = -value.imag
            block[row + dimension][column] = value.imag
            block[row + dimension][column + dimension] = value.real
    return block


def _jacobi_eigenvalues(matrix: list[list[float]]) -> list[float]:
    values = [row[:] for row in matrix]
    size = len(values)
    for _ in range(100 * size * size):
        pivot_row = 0
        pivot_col = 1
        max_offdiag = 0.0
        for row in range(size):
            for col in range(row + 1, size):
                candidate = abs(values[row][col])
                if candidate > max_offdiag:
                    max_offdiag = candidate
                    pivot_row = row
                    pivot_col = col
        if max_offdiag < 1e-14:
            break

        app = values[pivot_row][pivot_row]
        aqq = values[pivot_col][pivot_col]
        apq = values[pivot_row][pivot_col]
        angle = 0.5 * math.atan2(2.0 * apq, aqq - app)
        cosine = math.cos(angle)
        sine = math.sin(angle)

        for index in range(size):
            if index not in {pivot_row, pivot_col}:
                aip = values[index][pivot_row]
                aiq = values[index][pivot_col]
                values[index][pivot_row] = cosine * aip - sine * aiq
                values[pivot_row][index] = values[index][pivot_row]
                values[index][pivot_col] = sine * aip + cosine * aiq
                values[pivot_col][index] = values[index][pivot_col]

        values[pivot_row][pivot_row] = (
            cosine * cosine * app
            - 2.0 * sine * cosine * apq
            + sine * sine * aqq
        )
        values[pivot_col][pivot_col] = (
            sine * sine * app
            + 2.0 * sine * cosine * apq
            + cosine * cosine * aqq
        )
        values[pivot_row][pivot_col] = 0.0
        values[pivot_col][pivot_row] = 0.0

    return [values[index][index] for index in range(size)]
