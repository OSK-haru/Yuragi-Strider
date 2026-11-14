import unittest

from core.circuit_model import CircuitConfig
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import _runtime_issues, run_simulation


class NoisyLargeQubitSupportTests(unittest.TestCase):
    def test_six_qubit_noisy_rk4_executes_as_density_matrix(self) -> None:
        result = run_simulation(_config(6))

        self.assertFalse(result.issues)
        self.assertEqual(result.diagnostics["execution_representation"], "density_matrix")
        self.assertEqual(result.diagnostics["density_matrix_qubit_limit"], 8)
        self.assertTrue(result.diagnostics["large_density_matrix_execution"])
        self.assertEqual(len(result.output_probabilities), 64)
        self.assertTrue(any("exact dense density-matrix" in warning for warning in result.warnings))

    def test_eight_qubit_noisy_rk4_passes_runtime_preflight(self) -> None:
        self.assertFalse(_runtime_issues(_config(8)))

    def test_nine_qubit_noisy_rk4_is_rejected(self) -> None:
        issues = _runtime_issues(_config(9))

        self.assertIn("UNSUPPORTED_QUBIT_COUNT", {issue.code for issue in issues})

    def test_six_qubit_noisy_explicit_cptp_is_rejected(self) -> None:
        config = _config(6)
        config.evolution_method = "explicit_cptp"

        issues = _runtime_issues(config)

        self.assertIn("UNSUPPORTED_EVOLUTION_METHOD", {issue.code for issue in issues})


def _config(logical_qubits: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=logical_qubits,
            initial_states=["0"] * logical_qubits,
            columns=[],
        ),
        environment=EnvironmentConfig(t1_zero_temperature_us=10.0, tphi_us=10.0, temperature_mk=0.0),
        duration_us=0.02,
        time_steps=2,
        simulation_backend="python_dense",
    )


if __name__ == "__main__":
    unittest.main()
