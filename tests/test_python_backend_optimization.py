import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gates import (
    H,
    Matrix,
    expand_single_qubit_gate,
    initial_density_matrix,
    lindblad_rhs,
    lindblad_rhs_cached,
    prepare_collapse_operators,
    rk4_step,
    rk4_step_cached,
    scale,
    zero_hamiltonian,
)
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from tests.physics_test_helpers import make_ideal_environment


class PythonBackendOptimizationTest(unittest.TestCase):
    def test_times_fidelity_and_purity_lengths_are_aligned(self) -> None:
        result = run_simulation(_one_qubit_h_config(_finite_environment()))

        self.assertEqual(len(result.times), 51)
        self.assertEqual(len(result.fidelity), len(result.times))
        self.assertEqual(len(result.purity), len(result.times))

    def test_final_output_probabilities_remain_correct_for_ideal_h(self) -> None:
        result = run_simulation(_one_qubit_h_config(make_ideal_environment()))

        self.assertAlmostEqual(result.output_probabilities["0"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["1"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.purity[-1], 1.0, delta=1e-10)

    def test_final_output_probabilities_remain_correct_for_ideal_bell(self) -> None:
        result = run_simulation(_bell_config(make_ideal_environment(), duration_us=0.22))

        self.assertAlmostEqual(result.output_probabilities["00"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["11"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["01"], 0.0, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["10"], 0.0, delta=1e-10)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.purity[-1], 1.0, delta=1e-10)

    def test_completion_and_final_match_when_no_idle_is_present(self) -> None:
        result = run_simulation(_bell_config(_finite_environment(), duration_us=0.22))

        self.assertAlmostEqual(result.diagnostics["idle_duration_us"], 0.0, delta=1e-12)
        self.assertAlmostEqual(
            result.diagnostics["completion_fidelity"],
            result.diagnostics["final_fidelity"],
            delta=1e-10,
        )

    def test_completion_and_final_can_differ_when_idle_is_present(self) -> None:
        result = run_simulation(_bell_config(_finite_environment(), duration_us=5.0))

        self.assertGreater(result.diagnostics["idle_duration_us"], 0.0)
        self.assertNotAlmostEqual(
            result.diagnostics["completion_fidelity"],
            result.diagnostics["final_fidelity"],
            delta=1e-6,
        )

    def test_phase_79_complexity_diagnostics_are_present(self) -> None:
        result = run_simulation(_bell_config(_finite_environment(), duration_us=5.0))

        for key in [
            "backend_name",
            "state_history_retained",
            "state_history_storage_mode",
            "complexity_total_segments",
            "complexity_gate_segment_count",
            "complexity_idle_segment_count",
            "complexity_total_rk4_substeps",
            "complexity_total_rhs_evaluations",
            "complexity_gate_rk4_substeps",
            "complexity_idle_rk4_substeps",
            "complexity_max_hamiltonian_scale_per_us",
            "complexity_max_environment_rate_per_us",
            "complexity_max_generator_scale_per_us",
            "complexity_estimated_work_units_segmented",
        ]:
            self.assertIn(key, result.diagnostics)

        self.assertEqual(result.diagnostics["backend_name"], "python_dense_streaming_v1")
        self.assertEqual(result.diagnostics["state_history_retained"], 0.0)

    def test_cached_and_uncached_lindblad_paths_match(self) -> None:
        rho = initial_density_matrix(["+"])
        hamiltonian = zero_hamiltonian(2)
        collapse_ops = [scale(0.1, expand_single_qubit_gate(H, 0, 1))]
        cached_ops = prepare_collapse_operators(collapse_ops)

        uncached_rhs = lindblad_rhs(rho, hamiltonian, collapse_ops)
        cached_rhs = lindblad_rhs_cached(rho, hamiltonian, cached_ops)
        uncached_step = rk4_step(rho, hamiltonian, collapse_ops, 0.01)
        cached_step = rk4_step_cached(rho, hamiltonian, cached_ops, 0.01)

        _assert_matrices_close(self, uncached_rhs, cached_rhs)
        _assert_matrices_close(self, uncached_step, cached_step)


def _finite_environment() -> EnvironmentConfig:
    return EnvironmentConfig(
        input_mode="physical",
        device_quality=1.0,
        temperature_mk=0.0,
        flux_noise_phi0=0.0,
        t1_max_us=10.0,
        tphi_max_us=10.0,
    )


def _one_qubit_h_config(environment: EnvironmentConfig) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])])
            ],
        ),
        environment=environment,
        duration_us=1.0,
        time_steps=51,
        fidelity_threshold=0.9,
    )


def _bell_config(
    environment: EnvironmentConfig,
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
                            params={"duration_us": 0.2},
                        )
                    ],
                ),
            ],
        ),
        environment=environment,
        duration_us=duration_us,
        time_steps=51,
        fidelity_threshold=0.9,
    )


def _assert_matrices_close(
    test_case: unittest.TestCase,
    left: Matrix,
    right: Matrix,
) -> None:
    test_case.assertEqual(len(left), len(right))
    for row in range(len(left)):
        for column in range(len(left[row])):
            test_case.assertAlmostEqual(
                left[row][column].real,
                right[row][column].real,
                delta=1e-12,
            )
            test_case.assertAlmostEqual(
                left[row][column].imag,
                right[row][column].imag,
                delta=1e-12,
            )


if __name__ == "__main__":
    unittest.main()
