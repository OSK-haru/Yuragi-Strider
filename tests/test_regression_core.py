import unittest

from core.comparison import ComparisonConfig, run_comparison
from core.expert_data import build_expert_inspector_data
from core.io.config_io import config_from_dict, config_to_dict
from core.results import EnvironmentConfig, SimulationResult
from core.simulator import run_simulation
from tests.phase8_helpers import bell_config, one_qubit_gate_config


class RegressionCoreTest(unittest.TestCase):
    def test_standard_gate_regressions_run(self) -> None:
        for gate in ["I", "X", "Z", "H"]:
            with self.subTest(gate=gate):
                result = run_simulation(one_qubit_gate_config(gate))

                self.assertIsInstance(result, SimulationResult)
                self.assertEqual(result.issues, [])
                self.assertTrue(result.times)

    def test_bell_regression_runs(self) -> None:
        result = run_simulation(bell_config())

        self.assertEqual(result.issues, [])
        self.assertEqual(set(result.output_probabilities), {"00", "01", "10", "11"})

    def test_low_vs_high_comparison_regression_runs(self) -> None:
        comparison = run_comparison(ComparisonConfig(
            circuit=one_qubit_gate_config("H").circuit,
            environment_a=EnvironmentConfig(t1_zero_temperature_us=100.0, tphi_us=100.0),
            environment_b=EnvironmentConfig(t1_zero_temperature_us=2.5, tphi_us=2.5),
            duration_us=0.001,
            time_steps=3,
            fidelity_threshold=0.9,
            label_a="Low",
            label_b="High",
        ))

        self.assertTrue(comparison.result_a.times)
        self.assertTrue(comparison.result_b.times)
        self.assertIsNotNone(comparison.delta_final_fidelity)

    def test_config_roundtrip_and_expert_data_regression(self) -> None:
        config = one_qubit_gate_config("H")
        loaded = config_from_dict(config_to_dict(config))
        result = run_simulation(loaded)
        expert_data = build_expert_inspector_data(result)

        self.assertEqual(loaded.to_dict(), config.to_dict())
        self.assertIn("overview", expert_data)
        self.assertIn("state", expert_data)


if __name__ == "__main__":
    unittest.main()
