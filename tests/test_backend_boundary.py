import unittest

from core.backend_boundary import (
    DEFAULT_SIMULATION_BACKEND,
    RUST_DENSE_BACKEND,
    get_rust_backend_name,
    is_rust_backend_available,
    rust_backend_status,
)
from core.circuit_model import CircuitConfig
from core.io.config_io import config_from_dict, config_to_dict
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from scripts.benchmark_complexity import CSV_COLUMNS


class BackendBoundaryTest(unittest.TestCase):
    def test_default_backend_metadata(self) -> None:
        result = run_simulation(_config())

        self.assertEqual(DEFAULT_SIMULATION_BACKEND, "python_dense")
        self.assertEqual(result.diagnostics["backend_requested"], "python_dense")
        self.assertEqual(result.diagnostics["backend_name"], "python_dense_streaming_v1")
        self.assertFalse(result.diagnostics["backend_fallback_used"])
        self.assertEqual(result.diagnostics["backend_fallback_reason"], "")
        self.assertFalse(result.diagnostics["rust_kernel_used"])

    def test_python_dense_backend_matches_default(self) -> None:
        default = run_simulation(_config())
        explicit = run_simulation(_config(simulation_backend="python_dense"))

        for key, value in default.output_probabilities.items():
            self.assertAlmostEqual(value, explicit.output_probabilities[key], places=11)
        for values_a, values_b in ((default.fidelity, explicit.fidelity),
                                   (default.purity, explicit.purity)):
            self.assertEqual(len(values_a), len(values_b))
            for value_a, value_b in zip(values_a, values_b):
                self.assertAlmostEqual(value_a, value_b, places=10)

    @unittest.skipUnless(
        is_rust_backend_available(),
        "Rust extension is not built in this environment",
    )
    def test_rust_dense_preview_uses_rust_when_available(self) -> None:
        result = run_simulation(_config(simulation_backend="rust_dense_preview"))

        self.assertEqual(result.issues, [])
        self.assertEqual(result.diagnostics["backend_requested"], "rust_dense_preview")
        self.assertEqual(result.diagnostics["backend_name"], "rust_dense_preview")
        self.assertFalse(result.diagnostics["backend_fallback_used"])
        self.assertEqual(result.diagnostics["backend_fallback_reason"], "")
        self.assertTrue(result.diagnostics["rust_kernel_used"])
        self.assertEqual(result.diagnostics["simulation_model"], "gate_aware_open_system")
        self.assertEqual(
            result.diagnostics["evolution_mode"],
            "gate_aware_hamiltonian_lindblad_v1",
        )
        self.assertEqual(result.diagnostics["rust_kernel_mode"], "sampled_cleaned_multi_output")
        self.assertGreater(result.diagnostics["rust_kernel_call_count"], 0.0)
        self.assertGreater(result.diagnostics["rust_kernel_sampled_batch_count"], 0.0)
        self.assertFalse(result.diagnostics["rust_kernel_fallback_used"])
        self.assertIn("rust_module_available", result.diagnostics)
        self.assertIn("rust_module_name", result.diagnostics)
        self.assertIn("rust_module_error", result.diagnostics)

    def test_unknown_backend_rejected(self) -> None:
        result = run_simulation(_config(simulation_backend="mystery_backend"))

        self.assertEqual(result.times, [])
        self.assertIn(
            "UNSUPPORTED_SIMULATION_BACKEND",
            {issue.code for issue in result.issues},
        )
        self.assertEqual(result.diagnostics["backend_requested"], "mystery_backend")

    def test_cptp_mode_not_executable_yet(self) -> None:
        result = run_simulation(_config(simulation_backend="gate_aware_cptp_kraus"))

        self.assertEqual(result.times, [])
        self.assertIn(
            "UNSUPPORTED_SIMULATION_BACKEND",
            {issue.code for issue in result.issues},
        )
        self.assertEqual(result.diagnostics["simulation_model"], "gate_aware_open_system")
        self.assertEqual(
            result.diagnostics["evolution_mode"],
            "gate_aware_hamiltonian_lindblad_v1",
        )

    def test_legacy_config_without_backend_field(self) -> None:
        encoded = _config().to_dict()
        encoded.pop("simulation_backend")
        config = SimulationConfig.from_dict(encoded)

        result = run_simulation(config)

        self.assertEqual(config.simulation_backend, "python_dense")
        self.assertEqual(result.diagnostics["backend_requested"], "python_dense")

    def test_rust_backend_statevector_path_is_no_longer_classified_as_unsupported(self) -> None:
        config = _config(simulation_backend=RUST_DENSE_BACKEND)
        config.circuit = CircuitConfig(logical_qubits=6, initial_states=["0"] * 6, columns=[])
        result = run_simulation(config)
        issue_codes = {item.code for item in result.issues}
        self.assertNotIn("RUST_BACKEND_UNSUPPORTED_PATH", issue_codes)
        if is_rust_backend_available():
            self.assertEqual(issue_codes, set())
            self.assertTrue(result.diagnostics["rust_kernel_used"])
            self.assertEqual(result.diagnostics["rust_kernel_call_count"], 1)
        else:
            self.assertIn("STATEVECTOR_EXECUTION_FAILED", issue_codes)

    def test_qscope_envelope_round_trips_backend_field(self) -> None:
        config = _config(simulation_backend="rust_dense_preview")
        encoded = config_to_dict(config)

        self.assertEqual(
            encoded["simulation"]["simulation_backend"],
            "rust_dense_preview",
        )
        self.assertEqual(
            config_from_dict(encoded).simulation_backend,
            "rust_dense_preview",
        )

    def test_benchmark_output_contains_backend_metadata_columns(self) -> None:
        for column in [
            "backend_requested",
            "backend_name",
            "backend_fallback_used",
            "backend_fallback_reason",
            "rust_kernel_used",
            "rust_kernel_mode",
            "rust_kernel_fallback_used",
            "rust_kernel_call_count",
            "rust_kernel_segment_count",
            "rust_kernel_substep_count",
            "rust_kernel_batchable_interval_count",
            "rust_kernel_actual_batch_count",
            "rust_kernel_max_batch_substeps",
            "rust_kernel_mean_batch_substeps",
            "rust_kernel_batch_blocked_by_sampling_count",
            "rust_kernel_batch_blocked_by_boundary_count",
            "rust_kernel_sampled_batch_count",
            "rust_kernel_sampled_returned_state_count",
            "rust_kernel_max_sampled_batch_outputs",
            "rust_kernel_mean_sampled_batch_outputs",
            "rust_kernel_sampled_batch_fallback_count",
            "rust_kernel_sampled_batch_fallback_reason",
            "python_kernel_segment_count",
            "python_kernel_substep_count",
        ]:
            self.assertIn(column, CSV_COLUMNS)

    def test_rust_backend_availability_helper_never_raises(self) -> None:
        status = rust_backend_status()

        self.assertIn("available", status)
        self.assertIn("name", status)
        self.assertIn("reason", status)
        self.assertIsInstance(is_rust_backend_available(), bool)
        self.assertIsInstance(get_rust_backend_name(), str)


def _config(
    simulation_backend: str = DEFAULT_SIMULATION_BACKEND,
    model: str = "weak_coupling_lindblad",
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig.one_qubit_h(),
        environment=EnvironmentConfig(input_mode="physical", ideal_reference=True),
        duration_us=1.0,
        time_steps=21,
        fidelity_threshold=0.9,
        model=model,
        simulation_backend=simulation_backend,
    )


if __name__ == "__main__":
    unittest.main()
