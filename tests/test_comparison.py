import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.comparison import ComparisonConfig, ComparisonResult, run_comparison
from core.results import EnvironmentConfig, SimulationResult


class ComparisonTest(unittest.TestCase):
    def test_one_qubit_h_low_noise_vs_high_noise_runs(self) -> None:
        result = run_comparison(_comparison_config())

        self.assertIsInstance(result, ComparisonResult)
        self.assertIsInstance(result.result_a, SimulationResult)
        self.assertIsInstance(result.result_b, SimulationResult)
        self.assertTrue(result.result_a.times)
        self.assertTrue(result.result_b.times)

    def test_delta_values_are_calculated(self) -> None:
        result = run_comparison(_comparison_config())

        self.assertIsNotNone(result.delta_final_fidelity)
        self.assertIsNotNone(result.delta_final_purity)
        self.assertIsNotNone(result.delta_effective_operation_time_us)
        self.assertIn(result.better_condition, {"Low noise", "High noise", "Tie"})

    def test_warnings_from_both_results_are_collected(self) -> None:
        result = run_comparison(
            ComparisonConfig(
                circuit=CircuitConfig.one_qubit_h(),
                environment_a=EnvironmentConfig(t1_zero_temperature_us=-1.0),
                environment_b=EnvironmentConfig(tphi_us=0.0),
                label_a="A",
                label_b="B",
            )
        )

        self.assertGreaterEqual(len(result.warnings), 2)
        self.assertTrue(any("INVALID_COHERENCE_TIME" in warning for warning in result.warnings))

    def test_two_qubit_bell_circuit_can_compare(self) -> None:
        result = run_comparison(
            ComparisonConfig(
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
                environment_a=EnvironmentConfig(
                    t1_zero_temperature_us=100.0,
                    tphi_us=66.66666666666667,
                    temperature_mk=11.8,
                ),
                environment_b=EnvironmentConfig(
                    t1_zero_temperature_us=2.5118864315095797,
                    tphi_us=2.4807298729688254,
                    temperature_mk=11.8,
                ),
                duration_us=0.001,
                time_steps=3,
                fidelity_threshold=0.9,
                label_a="Low",
                label_b="High",
            )
        )

        self.assertTrue(result.result_a.times)
        self.assertTrue(result.result_b.times)
        self.assertIsNotNone(result.delta_final_fidelity)


def _comparison_config() -> ComparisonConfig:
    return ComparisonConfig(
        circuit=CircuitConfig.one_qubit_h(),
        environment_a=EnvironmentConfig(
            t1_zero_temperature_us=63.095734448,
            tphi_us=45.159858934213595,
            temperature_mk=19.0,
        ),
        environment_b=EnvironmentConfig(
            t1_zero_temperature_us=2.5118864315095797,
            tphi_us=2.4727882168275728,
            temperature_mk=82.0,
        ),
        duration_us=20.0,
        time_steps=101,
        fidelity_threshold=0.9,
        label_a="Low noise",
        label_b="High noise",
    )


if __name__ == "__main__":
    unittest.main()
