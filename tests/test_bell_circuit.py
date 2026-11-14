import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation


class BellCircuitTest(unittest.TestCase):
    def test_bell_circuit_runs_without_error(self) -> None:
        result = run_simulation(_bell_config())

        self.assertEqual(result.issues, [])
        self.assertTrue(result.times)

    def test_bell_circuit_outputs_high_probability_on_00_and_11(self) -> None:
        result = run_simulation(_bell_config())

        self.assertAlmostEqual(result.output_probabilities["00"], 0.5, delta=1e-4)
        self.assertAlmostEqual(result.output_probabilities["11"], 0.5, delta=1e-4)
        self.assertAlmostEqual(result.output_probabilities["01"], 0.0, delta=1e-4)
        self.assertAlmostEqual(result.output_probabilities["10"], 0.0, delta=1e-4)


def _bell_config() -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type="H",
                            targets=[0],
                            controls=[],
                            params={},
                        )
                    ],
                ),
                GateColumn(
                    step=1,
                    gates=[
                        GateOperation(
                            type="CNOT",
                            targets=[1],
                            controls=[0],
                            params={},
                        )
                    ],
                ),
            ],
        ),
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
