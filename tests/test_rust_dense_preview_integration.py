import unittest
from unittest.mock import patch

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig
from core.rust_dense_kernel import is_rust_kernel_available
from core.simulator import run_simulation


ABS_TOLERANCE = 1e-10
REL_TOLERANCE = 5e-8


class RustDensePreviewIntegrationTest(unittest.TestCase):
    def test_python_dense_still_does_not_use_rust_kernel(self) -> None:
        result = run_simulation(_one_qubit_h_config("python_dense"))

        self.assertFalse(result.diagnostics["rust_kernel_used"])
        self.assertEqual(result.diagnostics["rust_kernel_mode"], "none")
        self.assertEqual(result.diagnostics["rust_kernel_call_count"], 0.0)
        self.assertEqual(result.diagnostics["rust_kernel_actual_batch_count"], 0.0)
        self.assertFalse(result.diagnostics["rust_kernel_fallback_used"])
        self.assertEqual(result.diagnostics["rust_kernel_substep_count"], 0.0)
        self.assertGreater(result.diagnostics["python_kernel_substep_count"], 0.0)

    @unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
    def test_rust_dense_preview_uses_rust_kernel_when_available(self) -> None:
        result = run_simulation(_one_qubit_h_config("rust_dense_preview"))

        self.assertEqual(result.diagnostics["backend_requested"], "rust_dense_preview")
        self.assertEqual(result.diagnostics["backend_name"], "rust_dense_preview")
        self.assertFalse(result.diagnostics["backend_fallback_used"])
        self.assertTrue(result.diagnostics["rust_kernel_used"])
        self.assertEqual(result.diagnostics["rust_kernel_mode"], "sampled_cleaned_multi_output")
        self.assertGreater(result.diagnostics["rust_kernel_call_count"], 0.0)
        _assert_batchability_diagnostics_consistent(self, result.diagnostics)
        _assert_sampled_batch_diagnostics_consistent(self, result.diagnostics)
        self.assertFalse(result.diagnostics["rust_kernel_fallback_used"])
        self.assertGreater(result.diagnostics["rust_kernel_substep_count"], 0.0)

    @unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
    def test_rust_preview_call_count_is_less_than_substep_count(self) -> None:
        result = run_simulation(_bell_long_cnot_config("rust_dense_preview"))

        self.assertEqual(result.diagnostics["rust_kernel_mode"], "sampled_cleaned_multi_output")
        self.assertGreater(result.diagnostics["rust_kernel_substep_count"], 0.0)
        self.assertLess(
            result.diagnostics["rust_kernel_call_count"],
            result.diagnostics["rust_kernel_substep_count"],
        )
        _assert_batchability_diagnostics_consistent(self, result.diagnostics)

    @unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
    def test_rust_preview_matches_python_dense_for_required_cases(self) -> None:
        for builder in [
            _one_qubit_h_config,
            _one_qubit_hx_config,
            _finite_noise_1q_config,
            _bell_config,
            _bell_long_cnot_config,
            _bell_with_idle_config,
        ]:
            with self.subTest(builder=builder.__name__):
                _assert_results_close(
                    self,
                    run_simulation(builder("python_dense")),
                    run_simulation(builder("rust_dense_preview")),
                )

    @unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
    def test_batchability_diagnostics_do_not_change_sampling_series(self) -> None:
        python_result = run_simulation(_bell_with_idle_config("python_dense"))
        rust_result = run_simulation(_bell_with_idle_config("rust_dense_preview"))

        self.assertEqual(python_result.times, rust_result.times)
        self.assertEqual(len(python_result.fidelity), len(rust_result.fidelity))
        self.assertEqual(len(python_result.purity), len(rust_result.purity))
        _assert_batchability_diagnostics_consistent(self, rust_result.diagnostics)
        _assert_sampled_batch_diagnostics_consistent(self, rust_result.diagnostics)

    @unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
    def test_rust_preview_matches_python_dense_for_hamiltonian_only(self) -> None:
        _assert_results_close(
            self,
            run_simulation(_hamiltonian_only_config("python_dense")),
            run_simulation(_hamiltonian_only_config("rust_dense_preview")),
        )

    @unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
    def test_five_qubit_preview_uses_persistent_rust_session(self) -> None:
        python_result = run_simulation(_five_qubit_h_config("python_dense"))
        result = run_simulation(_five_qubit_h_config("rust_dense_preview"))

        self.assertEqual(result.diagnostics["backend_requested"], "rust_dense_preview")
        self.assertEqual(result.diagnostics["backend_name"], "rust_dense_preview")
        self.assertFalse(result.diagnostics["backend_fallback_used"])
        self.assertTrue(result.diagnostics["rust_kernel_used"])
        self.assertGreater(result.diagnostics["rust_kernel_session_count"], 0.0)
        self.assertGreater(result.diagnostics["rust_kernel_session_reuse_count"], 0.0)
        self.assertGreater(result.diagnostics["rust_kernel_piecewise_segment_count"], 0.0)
        self.assertLess(
            result.diagnostics["rust_kernel_sampled_returned_state_count"],
            len(result.times),
        )
        _assert_results_close(self, python_result, result)

    @unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
    def test_rust_rate_path_does_not_build_dense_python_collapse_operators(self) -> None:
        with patch(
            "core.simulator.multi_qubit_environment_collapse_operators",
            side_effect=AssertionError("dense Python collapse operators were built"),
        ):
            result = run_simulation(_five_qubit_h_config("rust_dense_preview"))

        self.assertEqual(result.issues, [])
        self.assertTrue(result.diagnostics["rust_kernel_used"])

    @unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
    def test_cleanup_policy_is_preserved(self) -> None:
        python_result = run_simulation(_finite_noise_1q_config("python_dense"))
        rust_result = run_simulation(_finite_noise_1q_config("rust_dense_preview"))

        self.assertLessEqual(rust_result.diagnostics["max_trace_error"], 1e-10)
        _assert_results_close(self, python_result, rust_result)

    def test_forced_rust_failure_falls_back_to_python_with_diagnostics(self) -> None:
        def fail_kernel(*args, **kwargs):
            raise RuntimeError("forced rust failure")

        with (
            patch(
                "core.simulator.RustDenseSession.evolve_piecewise_cleaned_samples",
                fail_kernel,
            ),
            patch(
                "core.simulator.RustDenseSession.evolve_paired_registered_compact",
                fail_kernel,
            ),
            patch("core.simulator.RustDenseSession.evolve_cleaned", fail_kernel),
        ):
            result = run_simulation(_finite_noise_1q_config("rust_dense_preview"))

        self.assertEqual(result.issues, [])
        self.assertEqual(result.diagnostics["backend_requested"], "rust_dense_preview")
        self.assertEqual(result.diagnostics["backend_name"], "python_dense_streaming_v1")
        self.assertTrue(result.diagnostics["backend_fallback_used"])
        self.assertFalse(result.diagnostics["rust_kernel_used"])
        self.assertEqual(result.diagnostics["rust_kernel_mode"], "fallback_python")
        self.assertEqual(result.diagnostics["rust_kernel_actual_batch_count"], 0.0)
        self.assertTrue(result.diagnostics["rust_kernel_fallback_used"])
        self.assertIn("forced rust failure", result.diagnostics["rust_kernel_fallback_reason"])
        self.assertGreater(result.diagnostics["rust_kernel_sampled_batch_fallback_count"], 0.0)
        self.assertGreater(result.diagnostics["python_kernel_substep_count"], 0.0)


def _one_qubit_h_config(simulation_backend: str) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig.one_qubit_h(),
        environment=_finite_environment(),
        duration_us=1.0,
        time_steps=51,
        fidelity_threshold=0.9,
        simulation_backend=simulation_backend,
    )


def _five_qubit_h_config(simulation_backend: str) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=5,
            initial_states=["0"] * 5,
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
            ],
        ),
        environment=_finite_environment(),
        duration_us=0.02,
        time_steps=3,
        fidelity_threshold=0.9,
        simulation_backend=simulation_backend,
    )


def _hamiltonian_only_config(simulation_backend: str) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type="H",
                            targets=[0],
                            params={"duration_us": 1.0},
                        )
                    ],
                )
            ],
        ),
        environment=EnvironmentConfig(temperature_mk=15.0, ideal_reference=True),
        duration_us=1.0,
        time_steps=3,
        fidelity_threshold=0.9,
        simulation_backend=simulation_backend,
    )


def _one_qubit_hx_config(simulation_backend: str) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(step=1, gates=[GateOperation(type="X", targets=[0])]),
            ],
        ),
        environment=_finite_environment(),
        duration_us=1.0,
        time_steps=51,
        fidelity_threshold=0.9,
        simulation_backend=simulation_backend,
    )


def _finite_noise_1q_config(simulation_backend: str) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["+"],
            columns=[],
        ),
        environment=_finite_environment(),
        duration_us=2.0,
        time_steps=41,
        fidelity_threshold=0.9,
        simulation_backend=simulation_backend,
    )


def _bell_config(simulation_backend: str) -> SimulationConfig:
    return _bell_like_config(simulation_backend, cnot_duration_us=0.2, duration_us=1.0)


def _bell_long_cnot_config(simulation_backend: str) -> SimulationConfig:
    return _bell_like_config(
        simulation_backend,
        cnot_duration_us=20.0,
        duration_us=20.02,
    )


def _bell_with_idle_config(simulation_backend: str) -> SimulationConfig:
    return _bell_like_config(
        simulation_backend,
        cnot_duration_us=0.2,
        duration_us=5.22,
    )


def _bell_like_config(
    simulation_backend: str,
    cnot_duration_us: float,
    duration_us: float,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(
                    step=1,
                    gates=[
                        GateOperation(
                            type="CNOT",
                            targets=[1],
                            controls=[0],
                            params={"duration_us": cnot_duration_us},
                        )
                    ],
                ),
            ],
        ),
        environment=_finite_environment(),
        duration_us=duration_us,
        time_steps=41,
        fidelity_threshold=0.9,
        simulation_backend=simulation_backend,
    )


def _finite_environment() -> EnvironmentConfig:
    return EnvironmentConfig(t1_zero_temperature_us=10.0, tphi_zero_temperature_us=10.0, temperature_mk=0.0)


def _assert_results_close(test_case: unittest.TestCase, left, right) -> None:
    test_case.assertEqual(len(left.times), len(right.times))
    for left_value, right_value in zip(left.fidelity, right.fidelity):
        _assert_backend_close(test_case, left_value, right_value)
    for left_value, right_value in zip(left.purity, right.purity):
        _assert_backend_close(test_case, left_value, right_value)
    test_case.assertEqual(set(left.output_probabilities), set(right.output_probabilities))
    for state in left.output_probabilities:
        _assert_backend_close(
            test_case,
            left.output_probabilities[state],
            right.output_probabilities[state],
        )


def _assert_backend_close(
    test_case: unittest.TestCase,
    left: float,
    right: float,
) -> None:
    # Optimized SIMD GEMM can accumulate products in a different order from
    # NumPy. Keep the same mixed tolerance as the dense-backend regression
    # suite while retaining a tight absolute bound near zero.
    tolerance = ABS_TOLERANCE + REL_TOLERANCE * abs(left)
    test_case.assertAlmostEqual(left, right, delta=tolerance)


def _assert_batchability_diagnostics_consistent(
    test_case: unittest.TestCase,
    diagnostics,
) -> None:
    fields = [
        "rust_kernel_batchable_interval_count",
        "rust_kernel_actual_batch_count",
        "rust_kernel_max_batch_substeps",
        "rust_kernel_mean_batch_substeps",
        "rust_kernel_batch_blocked_by_sampling_count",
        "rust_kernel_batch_blocked_by_boundary_count",
    ]
    for field in fields:
        test_case.assertIn(field, diagnostics)
        test_case.assertGreaterEqual(diagnostics[field], 0.0)

    test_case.assertEqual(
        diagnostics["rust_kernel_actual_batch_count"],
        diagnostics["rust_kernel_call_count"],
    )
    test_case.assertEqual(
        diagnostics["rust_kernel_batchable_interval_count"],
        diagnostics["rust_kernel_actual_batch_count"],
    )
    test_case.assertLessEqual(
        diagnostics["rust_kernel_max_batch_substeps"],
        diagnostics["rust_kernel_substep_count"],
    )
    test_case.assertLessEqual(
        diagnostics["rust_kernel_mean_batch_substeps"],
        diagnostics["rust_kernel_max_batch_substeps"],
    )


def _assert_sampled_batch_diagnostics_consistent(
    test_case: unittest.TestCase,
    diagnostics,
) -> None:
    fields = [
        "rust_kernel_sampled_batch_count",
        "rust_kernel_sampled_returned_state_count",
        "rust_kernel_max_sampled_batch_outputs",
        "rust_kernel_mean_sampled_batch_outputs",
        "rust_kernel_sampled_batch_fallback_count",
    ]
    for field in fields:
        test_case.assertIn(field, diagnostics)
        test_case.assertGreaterEqual(diagnostics[field], 0.0)
    test_case.assertGreater(diagnostics["rust_kernel_sampled_batch_count"], 0.0)
    test_case.assertGreater(
        diagnostics["rust_kernel_sampled_returned_state_count"],
        0.0,
    )
    test_case.assertLessEqual(
        diagnostics["rust_kernel_sampled_batch_count"],
        diagnostics["rust_kernel_call_count"],
    )
    test_case.assertLessEqual(
        diagnostics["rust_kernel_mean_sampled_batch_outputs"],
        diagnostics["rust_kernel_max_sampled_batch_outputs"],
    )


if __name__ == "__main__":
    unittest.main()
