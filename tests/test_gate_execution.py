import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.comparison import ComparisonConfig, run_comparison
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from core.statevector import execute_statevector_branches


class GateExecutionTest(unittest.TestCase):
    def test_one_qubit_x_on_zero_outputs_one(self) -> None:
        result = run_simulation(_config_for_gate("X"))

        self.assertEqual(result.issues, [])
        self.assertAlmostEqual(result.output_probabilities["0"], 0.0, delta=1e-4)
        self.assertAlmostEqual(result.output_probabilities["1"], 1.0, delta=1e-4)

    def test_one_qubit_z_on_zero_keeps_zero(self) -> None:
        result = run_simulation(_config_for_gate("Z"))

        self.assertEqual(result.issues, [])
        self.assertAlmostEqual(result.output_probabilities["0"], 1.0, delta=1e-4)
        self.assertAlmostEqual(result.output_probabilities["1"], 0.0, delta=1e-4)

    def test_one_qubit_h_on_zero_outputs_balanced_distribution(self) -> None:
        result = run_simulation(_config_for_gate("H"))

        self.assertEqual(result.issues, [])
        self.assertAlmostEqual(result.output_probabilities["0"], 0.5, delta=1e-4)
        self.assertAlmostEqual(result.output_probabilities["1"], 0.5, delta=1e-4)

    def test_measure_preserves_computational_basis_state(self) -> None:
        result = run_simulation(_config_for_gate("Measure"))

        self.assertEqual(result.issues, [])
        self.assertAlmostEqual(result.output_probabilities["0"], 1.0, delta=1e-4)
        self.assertAlmostEqual(result.output_probabilities["1"], 0.0, delta=1e-4)

    def test_open_control_cnot_fires_when_control_is_zero(self) -> None:
        circuit = CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[GateColumn(step=0, gates=[GateOperation(
                type="CNOT",
                controls=[0],
                targets=[1],
                params={"control_value": 0.0},
            )])],
        )

        result = run_simulation(SimulationConfig(
            circuit=circuit,
            environment=_environment(),
            duration_us=0.001,
            time_steps=3,
            fidelity_threshold=0.9,
        ))

        self.assertEqual(result.issues, [])
        self.assertAlmostEqual(result.output_probabilities["01"], 1.0, delta=1e-4)

    def test_multi_control_x_supports_mixed_open_and_closed_controls(self) -> None:
        circuit = CircuitConfig(
            logical_qubits=3,
            initial_states=["0", "1", "0"],
            columns=[GateColumn(step=0, gates=[GateOperation(
                type="CNOT",
                controls=[0, 1],
                targets=[2],
                params={"control_state": 1.0},  # q0=0 (open), q1=1 (closed)
            )])],
        )

        result = run_simulation(SimulationConfig(
            circuit=circuit,
            environment=_environment(),
            duration_us=0.001,
            time_steps=3,
            fidelity_threshold=0.9,
        ))

        self.assertEqual(result.issues, [])
        self.assertAlmostEqual(result.output_probabilities["011"], 1.0, delta=1e-4)

    def test_four_qubit_grover_three_iterations_amplifies_marked_0100_state(self) -> None:
        all_qubits = [0, 1, 2, 3]
        columns = [GateColumn(0, [GateOperation("H", [qubit]) for qubit in all_qubits])]
        step = 1
        for iteration in range(3):
            columns.append(GateColumn(step, [GateOperation(
                "ORACLE", all_qubits, params={"marked_index": 4.0},
            )]))
            step += 1
            columns.extend([
                GateColumn(step, [GateOperation("H", [qubit]) for qubit in all_qubits]),
                GateColumn(step + 1, [GateOperation("X", [qubit]) for qubit in all_qubits]),
                GateColumn(step + 2, [GateOperation("H", [3])]),
                GateColumn(step + 3, [GateOperation(
                    "CNOT", [3], controls=[0, 1, 2], params={"control_state": 7.0},
                )]),
                GateColumn(step + 4, [GateOperation("H", [3])]),
                GateColumn(step + 5, [GateOperation("X", [qubit]) for qubit in all_qubits]),
                GateColumn(step + 6, [GateOperation("H", [qubit]) for qubit in all_qubits]),
            ])
            step += 7

        result = execute_statevector_branches(CircuitConfig(
            logical_qubits=4,
            initial_states=["0", "0", "0", "0"],
            columns=columns,
        ))

        self.assertGreater(result.output_probabilities["0100"], 0.95)

    def test_run_comparison_works_for_non_h_one_qubit_circuit(self) -> None:
        comparison = run_comparison(
            ComparisonConfig(
                circuit=_circuit_for_gate("X"),
                environment_a=_environment(),
                environment_b=EnvironmentConfig(
                    t1_zero_temperature_us=2.5118864315095797,
                    tphi_us=2.4727882168275728,
                    temperature_mk=82.0,
                ),
                duration_us=0.001,
                time_steps=3,
                fidelity_threshold=0.9,
                label_a="Low",
                label_b="High",
            )
        )

        self.assertTrue(comparison.result_a.times)
        self.assertTrue(comparison.result_b.times)
        self.assertIsNotNone(comparison.delta_final_fidelity)


def _config_for_gate(gate_type: str) -> SimulationConfig:
    return SimulationConfig(
        circuit=_circuit_for_gate(gate_type),
        environment=_environment(),
        duration_us=0.001,
        time_steps=3,
        fidelity_threshold=0.9,
    )


def _circuit_for_gate(gate_type: str) -> CircuitConfig:
    return CircuitConfig(
        logical_qubits=1,
        initial_states=["0"],
        columns=[
            GateColumn(
                step=0,
                gates=[
                    GateOperation(
                        type=gate_type,
                        targets=[0],
                        controls=[],
                        params={},
                    )
                ],
            )
        ],
    )


def _environment() -> EnvironmentConfig:
    return EnvironmentConfig(
        t1_zero_temperature_us=100.0,
        tphi_us=66.66666666666667,
        temperature_mk=10.0,
    )


if __name__ == "__main__":
    unittest.main()
