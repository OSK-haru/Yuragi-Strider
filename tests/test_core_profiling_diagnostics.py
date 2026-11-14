import unittest
from math import isfinite

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.internal_profiling import enable_internal_profiling
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from core.ui_response import simulation_result_to_ui_response


class CoreProfilingDiagnosticsTest(unittest.TestCase):
    def test_simple_simulation_exposes_core_profiling_diagnostics(self) -> None:
        config = _simple_config()

        response = simulation_result_to_ui_response(run_simulation(config))
        diagnostics = response["diagnostics"]

        for key in (
            "core_dimension",
            "core_density_matrix_shape",
            "core_total_evolution_ms",
            "core_total_rk4_substeps",
            "core_collapse_operator_count",
        ):
            self.assertIn(key, diagnostics)

        self.assertTrue(diagnostics["core_has_gate_segments"])
        self.assertTrue(diagnostics["core_has_idle_after_circuit"])
        self.assertFalse(diagnostics["core_idle_only"])

    def test_internal_profiling_is_disabled_by_default(self) -> None:
        response = simulation_result_to_ui_response(run_simulation(_simple_config()))

        diagnostics = response["diagnostics"]

        self.assertNotIn("core_internal_profiling_enabled", diagnostics)
        self.assertNotIn("core_profile_rhs_call_count", diagnostics)
        self.assertNotIn("core_profile_matmul_call_count", diagnostics)

    def test_internal_profiling_exposes_function_level_diagnostics(self) -> None:
        with enable_internal_profiling():
            response = simulation_result_to_ui_response(run_simulation(_simple_config()))

        diagnostics = response["diagnostics"]

        self.assertTrue(diagnostics["core_internal_profiling_enabled"])
        self.assertGreater(diagnostics["core_profile_rhs_call_count"], 0)
        self.assertGreater(diagnostics["core_profile_rk4_step_count"], 0)
        self.assertGreaterEqual(diagnostics["core_profile_matmul_call_count"], 1)
        self.assertEqual(
            diagnostics["core_profile_rhs_call_count"],
            4 * diagnostics["core_profile_rk4_step_count"],
        )

        for key in (
            "core_profile_rk4_total_ms",
            "core_profile_rk4_average_ms",
            "core_profile_rhs_total_ms",
            "core_profile_rhs_average_ms",
            "core_profile_hamiltonian_term_ms",
            "core_profile_dissipator_total_ms",
            "core_profile_matrix_accumulation_ms",
            "core_profile_matmul_total_ms",
            "core_profile_adjoint_total_ms",
            "core_profile_matrix_add_scale_total_ms",
            "core_profile_collapse_adjoint_build_ms",
            "core_profile_ldagger_l_build_ms",
        ):
            self.assertIn(key, diagnostics)
            self.assertTrue(isfinite(diagnostics[key]))
            self.assertGreaterEqual(diagnostics[key], 0.0)

    def test_internal_profiling_does_not_change_outputs(self) -> None:
        baseline = simulation_result_to_ui_response(run_simulation(_simple_config()))
        with enable_internal_profiling():
            profiled = simulation_result_to_ui_response(run_simulation(_simple_config()))

        self.assertAlmostEqual(
            baseline["summary"]["final_fidelity"],
            profiled["summary"]["final_fidelity"],
            places=12,
        )
        self.assertAlmostEqual(
            baseline["summary"]["final_purity"],
            profiled["summary"]["final_purity"],
            places=12,
        )
        self.assertEqual(
            baseline["output_probabilities"],
            profiled["output_probabilities"],
        )


def _simple_config() -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[GateOperation(type="H", targets=[0])],
                )
            ],
        ),
        environment=EnvironmentConfig(t1_zero_temperature_us=10.0, tphi_us=10.0, temperature_mk=0.0),
        duration_us=1.0,
        time_steps=11,
        fidelity_threshold=0.9,
    )


if __name__ == "__main__":
    unittest.main()
