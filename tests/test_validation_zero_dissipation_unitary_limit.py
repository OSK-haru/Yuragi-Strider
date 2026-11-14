from __future__ import annotations

import math
import unittest
from dataclasses import dataclass

from core.capabilities import GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gates import (
    apply_unitary_to_density,
    column_unitary,
    initial_density_matrix,
    multi_qubit_environment_collapse_operators,
    output_probabilities,
    trace,
)
from core.physical_environment import compute_environment_rates
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from tests.physics_test_helpers import hermitian_eigenvalues


MAX_ELEMENT_TOLERANCE = 1e-8
FROBENIUS_TOLERANCE = 1e-8
TRACE_DISTANCE_TOLERANCE = 1e-8
FIDELITY_TOLERANCE = 1e-8
TRACE_TOLERANCE = 1e-10
HERMITICITY_TOLERANCE = 1e-10


@dataclass(frozen=True)
class ValidationCase:
    name: str
    config: SimulationConfig
    expected_probabilities: dict[str, float]


class ZeroDissipationUnitaryLimitTest(unittest.TestCase):
    def test_representative_circuits_match_direct_unitary_reference(self) -> None:
        for case in validation_cases():
            with self.subTest(case=case.name):
                result = run_simulation(case.config)
                self.assertFalse(result.issues, msg=result.warnings)

                rates = compute_environment_rates(case.config.environment)
                self.assertEqual(rates.gamma_down_per_us, 0.0)
                self.assertEqual(rates.gamma_up_per_us, 0.0)
                self.assertEqual(rates.gamma_phi_per_us, 0.0)
                self.assertEqual(
                    multi_qubit_environment_collapse_operators(
                        case.config.circuit.logical_qubits,
                        rates,
                    ),
                    [],
                )

                simulated = _final_density_matrix(result)
                ideal = direct_unitary_reference(case.config)
                self.assertLessEqual(_max_element_error(simulated, ideal), MAX_ELEMENT_TOLERANCE)
                self.assertLessEqual(_frobenius_error(simulated, ideal), FROBENIUS_TOLERANCE)
                self.assertLessEqual(_trace_distance(simulated, ideal), TRACE_DISTANCE_TOLERANCE)
                self.assertLessEqual(1.0 - result.fidelity[-1], FIDELITY_TOLERANCE)

                probabilities = output_probabilities(
                    simulated,
                    case.config.circuit.logical_qubits,
                )
                for label, expected in case.expected_probabilities.items():
                    self.assertAlmostEqual(probabilities[label], expected, delta=MAX_ELEMENT_TOLERANCE)
                self.assertAlmostEqual(abs(trace(simulated) - 1.0), 0.0, delta=TRACE_TOLERANCE)
                self.assertLessEqual(_hermiticity_error(simulated), HERMITICITY_TOLERANCE)
                self.assertTrue(all(math.isfinite(abs(value)) for row in simulated for value in row))
                self.assertAlmostEqual(result.diagnostics["idle_duration_us"], 0.0)

    def test_phase_sensitive_sequence_has_expected_relative_phase(self) -> None:
        case = next(case for case in validation_cases() if case.name == "V1-3 1q H-Z")
        simulated = _final_density_matrix(run_simulation(case.config))

        self.assertAlmostEqual(simulated[0][1].real, -0.5, delta=MAX_ELEMENT_TOLERANCE)
        self.assertAlmostEqual(simulated[1][0].real, -0.5, delta=MAX_ELEMENT_TOLERANCE)


def validation_cases() -> list[ValidationCase]:
    return [
        ValidationCase(
            "V1-1 1q X",
            _config(
                1,
                [
                    _column(0, _gate("X", [0], duration_us=0.20)),
                ],
                ["0"],
            ),
            {"0": 0.0, "1": 1.0},
        ),
        ValidationCase(
            "V1-2 1q H",
            _config(1, [_column(0, _gate("H", [0], duration_us=0.20))], ["0"]),
            {"0": 0.5, "1": 0.5},
        ),
        ValidationCase(
            "V1-3 1q H-Z",
            _config(
                1,
                [
                    _column(0, _gate("H", [0], duration_us=0.20)),
                    _column(1, _gate("Z", [0], duration_us=0.20)),
                ],
                ["0"],
            ),
            {"0": 0.5, "1": 0.5},
        ),
        ValidationCase(
            "V1-4 2q Bell",
            _config(
                2,
                [
                    _column(0, _gate("H", [0], duration_us=0.20)),
                    _column(1, _gate("CNOT", [1], controls=[0], duration_us=0.40)),
                ],
                ["0", "0"],
            ),
            {"00": 0.5, "01": 0.0, "10": 0.0, "11": 0.5},
        ),
        ValidationCase(
            "V1-5 3q GHZ",
            _config(
                3,
                [
                    _column(0, _gate("H", [0], duration_us=0.20)),
                    _column(1, _gate("CNOT", [1], controls=[0], duration_us=0.40)),
                    _column(2, _gate("CNOT", [2], controls=[1], duration_us=0.40)),
                ],
                ["0", "0", "0"],
            ),
            {"000": 0.5, "001": 0.0, "010": 0.0, "011": 0.0,
             "100": 0.0, "101": 0.0, "110": 0.0, "111": 0.5},
        ),
        ValidationCase(
            "V1-6 4q mixed",
            _config(
                4,
                [
                    _column(0, _gate("X", [3], duration_us=0.20)),
                    _column(1, _gate("H", [0], duration_us=0.20)),
                    _column(2, _gate("CNOT", [2], controls=[0], duration_us=0.40)),
                ],
                ["0", "0", "0", "0"],
            ),
            {label: (0.5 if label in {"0001", "1011"} else 0.0)
             for label in _basis_labels(4)},
        ),
        ValidationCase(
            "V1-7 same-column H-X",
            _config(
                2,
                [
                    _column(
                        0,
                        _gate("H", [0], duration_us=0.20),
                        _gate("X", [1], duration_us=0.20),
                    ),
                ],
                ["0", "0"],
            ),
            {"00": 0.0, "01": 0.5, "10": 0.0, "11": 0.5},
        ),
        ValidationCase(
            "V1-8 same-column two-CNOT",
            _config(
                4,
                [
                    _column(
                        0,
                        _gate("CNOT", [1], controls=[0], duration_us=0.40),
                        _gate("CNOT", [3], controls=[2], duration_us=0.40),
                    ),
                ],
                ["1", "0", "1", "0"],
            ),
            {label: (1.0 if label == "1111" else 0.0) for label in _basis_labels(4)},
        ),
    ]


def direct_unitary_reference(config: SimulationConfig):
    state = initial_density_matrix(config.circuit.initial_states)
    for column in sorted(config.circuit.columns, key=lambda item: item.step):
        state = apply_unitary_to_density(
            state,
            column_unitary(column, config.circuit.logical_qubits),
        )
    return state


def _config(
    logical_qubits: int,
    columns: list[GateColumn],
    initial_states: list[str],
) -> SimulationConfig:
    duration_us = sum(max(_gate_duration(gate) for gate in column.gates) for column in columns)
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=logical_qubits,
            initial_states=initial_states,
            columns=columns,
        ),
        environment=EnvironmentConfig(temperature_mk=0.0, ideal_reference=True),
        duration_us=duration_us,
        time_steps=101,
        fidelity_threshold=0.9,
        model=GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
    )


def _gate(
    gate_type: str,
    targets: list[int],
    *,
    controls: list[int] | None = None,
    duration_us: float,
) -> GateOperation:
    return GateOperation(
        type=gate_type,
        targets=targets,
        controls=controls,
        params={"duration_us": duration_us},
    )


def _column(step: int, *gates: GateOperation) -> GateColumn:
    return GateColumn(step=step, gates=list(gates))


def _gate_duration(gate: GateOperation) -> float:
    return float(gate.params["duration_us"])


def _final_density_matrix(result):
    final = next(snapshot for snapshot in reversed(result.state_snapshots) if snapshot.kind == "final")
    return final.density_matrix


def _basis_labels(qubits: int) -> list[str]:
    return [format(index, f"0{qubits}b") for index in range(2**qubits)]


def _max_element_error(left, right) -> float:
    return max(
        abs(left[row][column] - right[row][column])
        for row in range(len(left))
        for column in range(len(left))
    )


def _frobenius_error(left, right) -> float:
    return math.sqrt(sum(
        abs(left[row][column] - right[row][column]) ** 2
        for row in range(len(left))
        for column in range(len(left))
    ))


def _trace_distance(left, right) -> float:
    difference = [
        [left[row][column] - right[row][column] for column in range(len(left))]
        for row in range(len(left))
    ]
    hermitian = [
        [
            0.5 * (difference[row][column] + difference[column][row].conjugate())
            for column in range(len(left))
        ]
        for row in range(len(left))
    ]
    return 0.5 * sum(abs(value) for value in hermitian_eigenvalues(hermitian))


def _hermiticity_error(matrix) -> float:
    return max(
        abs(matrix[row][column] - matrix[column][row].conjugate())
        for row in range(len(matrix))
        for column in range(len(matrix))
    )
