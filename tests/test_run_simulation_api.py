import sys
import unittest

from core.circuit_model import CircuitConfig
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import run_simulation


class RunSimulationApiTest(unittest.TestCase):
    def test_run_simulation_returns_normalized_result_for_one_qubit_h(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig.one_qubit_h(),
            environment=EnvironmentConfig(
                t1_zero_temperature_us=95.49925860214358,
                tphi_zero_temperature_us=64.63587019060685,
                temperature_mk=11.8,
            ),
            duration_us=20.0,
            time_steps=101,
            fidelity_threshold=0.9,
        )

        result = run_simulation(config)

        self.assertIsInstance(result, SimulationResult)
        self.assertEqual(len(result.times), config.time_steps)
        self.assertEqual(len(result.fidelity), config.time_steps)
        self.assertEqual(len(result.purity), config.time_steps)
        self.assertIsInstance(result.effective_operation_time_us, float)
        self.assertIn("t1_effective_us", result.derived_parameters)
        self.assertIn("t2_effective_us", result.derived_parameters)
        self.assertIn("gamma_down_per_us", result.derived_parameters)
        self.assertIn("gamma_phi_per_us", result.derived_parameters)
        self.assertIn("final_fidelity", result.diagnostics)
        self.assertIn("final_purity", result.diagnostics)
        self.assertIsInstance(result.warnings, list)
        self.assertNotIn("streamlit", sys.modules)

    def test_run_simulation_uses_circuit_gate_sequence(self) -> None:
        environment = EnvironmentConfig(
            t1_zero_temperature_us=95.49925860214358,
            tphi_zero_temperature_us=64.63587019060685,
            temperature_mk=11.8,
        )
        config = SimulationConfig(
            circuit=CircuitConfig.one_qubit_h(),
            environment=environment,
            duration_us=20.0,
            time_steps=101,
            fidelity_threshold=0.9,
        )

        result = run_simulation(config)

        self.assertEqual(result.times[0], 0.0)
        self.assertEqual(len(result.times), config.time_steps)
        self.assertAlmostEqual(result.output_probabilities["0"], 0.5, delta=0.1)
        self.assertAlmostEqual(result.output_probabilities["1"], 0.5, delta=0.1)
        self.assertGreaterEqual(result.effective_operation_time_us, 0.0)


if __name__ == "__main__":
    unittest.main()
