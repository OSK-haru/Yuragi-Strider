import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation


class TwoQubitSimulationTest(unittest.TestCase):
    def test_two_qubit_initial_zero_zero_outputs_all_basis_labels(self) -> None:
        result = run_simulation(_config(CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[],
        )))

        self.assertEqual(result.issues, [])
        self.assertEqual(
            set(result.output_probabilities),
            {"00", "01", "10", "11"},
        )
        self.assertAlmostEqual(result.output_probabilities["00"], 1.0, delta=1e-4)

    def test_cnot_with_control_equal_target_is_rejected(self) -> None:
        result = run_simulation(_config(CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type="CNOT",
                            targets=[0],
                            controls=[0],
                            params={},
                        )
                    ],
                )
            ],
        )))

        self.assertIn("CNOT_CONTROL_EQUALS_TARGET", {issue.code for issue in result.issues})
        self.assertEqual(result.times, [])

    def test_gate_target_out_of_range_is_rejected(self) -> None:
        result = run_simulation(_config(CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type="X",
                            targets=[2],
                            controls=[],
                            params={},
                        )
                    ],
                )
            ],
        )))

        self.assertIn("GATE_TARGET_OUT_OF_RANGE", {issue.code for issue in result.issues})
        self.assertEqual(result.times, [])


def _config(circuit: CircuitConfig) -> SimulationConfig:
    return SimulationConfig(
        circuit=circuit,
        environment=EnvironmentConfig(
            t1_zero_temperature_us=100.0,
            tphi_zero_temperature_us=66.66666666666667,
            temperature_mk=10.0,
        ),
        duration_us=0.001,
        time_steps=3,
        fidelity_threshold=0.9,
    )


if __name__ == "__main__":
    unittest.main()
