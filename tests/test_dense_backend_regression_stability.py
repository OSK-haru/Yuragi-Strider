import unittest
from math import isfinite

from core.backend_boundary import PYTHON_DENSE_BACKEND
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.dense_numpy import (
    force_numpy_dense_execution,
    force_python_dense_execution,
    numpy_dense_available,
    should_use_numpy_dense,
)
from core.gates import (
    Matrix,
    adjoint,
    multi_qubit_environment_collapse_operators,
    output_probabilities,
    prepare_collapse_operators,
    trace,
)
from core.internal_profiling import enable_internal_profiling
from core.physical_environment import compute_environment_rates
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import (
    _KernelStats,
    _SimulationCaches,
    _max_environment_rate_per_us,
    _simulate_circuit_gate_aware_hamiltonian,
    run_simulation,
)
from core.ui_response import simulation_result_to_ui_response

try:
    import numpy as np
except ImportError:  # pragma: no cover - NumPy is an existing project dependency.
    np = None


ABS_TOL = 1e-10
REL_TOL = 5e-8
TRACE_TOL = 1e-9
HERMITICITY_TOL = 1e-9
PROB_SUM_TOL = 1e-9
PROB_NEG_TOL = -1e-12
EIGENVALUE_TOL = -1e-9


@unittest.skipUnless(numpy_dense_available(), "NumPy dense execution is unavailable")
class DenseBackendRegressionStabilityTest(unittest.TestCase):
    def test_numpy_and_python_are_equivalent_for_representative_cases(self) -> None:
        for name, config in _unit_cases():
            with self.subTest(name=name):
                numpy_result, numpy_state = _run_with_state(config, "numpy")
                python_result, python_state = _run_with_state(config, "python")

                _assert_results_close(self, python_result, numpy_result)
                _assert_matrices_close(self, python_state, numpy_state)
                _assert_density_sane(self, numpy_state)
                _assert_density_sane(self, python_state)

    def test_selected_cases_are_positive_semidefinite_with_tolerance(self) -> None:
        for name, config in (
            ("2q Bell", _config_bell(2.0, 31)),
            ("3q GHZ", _config_ghz_3q(1.0, 21)),
            ("4q multi-column", _config_4q_two_cnot(0.8, 21)),
        ):
            with self.subTest(name=name):
                _, state = _run_with_state(config, "numpy")
                minimum_eigenvalue = _minimum_eigenvalue(state)
                self.assertGreaterEqual(minimum_eigenvalue, EIGENVALUE_TOL)

    def test_repeated_runs_are_deterministic_and_contexts_do_not_leak(self) -> None:
        for name, config in (
            ("2q Bell", _config_bell(2.0, 31)),
            ("3q GHZ", _config_ghz_3q(1.0, 21)),
            ("4q empty default", _config_empty(4, 2.0, 51)),
            ("4q multi-column", _config_4q_two_cnot(0.8, 21)),
        ):
            with self.subTest(name=name):
                responses = [
                    simulation_result_to_ui_response(run_simulation(config))
                    for _ in range(3)
                ]
                first = responses[0]
                for response in responses[1:]:
                    self.assertEqual(
                        first["output_probabilities"].keys(),
                        response["output_probabilities"].keys(),
                    )
                    _assert_float_maps_close(
                        self,
                        first["output_probabilities"],
                        response["output_probabilities"],
                    )
                    _assert_timeline_close(self, first["timeline"], response["timeline"])
                    self.assertNotIn(
                        "core_internal_profiling_enabled",
                        response["diagnostics"],
                    )
                self.assertTrue(should_use_numpy_dense())

    def test_fallback_contexts_restore_default_and_can_be_nested(self) -> None:
        self.assertTrue(should_use_numpy_dense())
        with force_python_dense_execution():
            self.assertFalse(should_use_numpy_dense())
            with force_numpy_dense_execution():
                self.assertTrue(should_use_numpy_dense())
            self.assertFalse(should_use_numpy_dense())
        self.assertTrue(should_use_numpy_dense())

        with force_numpy_dense_execution():
            self.assertTrue(should_use_numpy_dense())
            with force_python_dense_execution():
                self.assertFalse(should_use_numpy_dense())
            self.assertTrue(should_use_numpy_dense())
        self.assertTrue(should_use_numpy_dense())

    def test_diagnostics_and_profiling_leakage_are_consistent(self) -> None:
        config = _config_empty(4, 0.5, 11)

        normal = simulation_result_to_ui_response(run_simulation(config))
        self.assertEqual(normal["diagnostics"]["core_dense_execution_engine"], "numpy_dense_v1")
        self.assertTrue(normal["diagnostics"]["core_zero_hamiltonian_fast_path_used"])
        self.assertNotIn("core_profile_rhs_call_count", normal["diagnostics"])

        with force_python_dense_execution():
            fallback = simulation_result_to_ui_response(run_simulation(config))
        self.assertEqual(fallback["diagnostics"]["core_dense_execution_engine"], "python_tuple_v1")
        self.assertNotIn("core_profile_rhs_call_count", fallback["diagnostics"])

        with enable_internal_profiling():
            profiled = simulation_result_to_ui_response(run_simulation(config))
        diagnostics = profiled["diagnostics"]
        self.assertTrue(diagnostics["core_internal_profiling_enabled"])
        self.assertGreater(diagnostics["core_profile_rhs_call_count"], 0)
        self.assertEqual(
            diagnostics["core_profile_rhs_call_count"]
            % diagnostics["core_profile_rk4_step_count"],
            0,
        )
        self.assertGreater(diagnostics["core_profile_zero_hamiltonian_skip_count"], 0)


def _unit_cases() -> list[tuple[str, SimulationConfig]]:
    return [
        ("2q empty", _config_empty(2, 0.5, 11)),
        ("2q Bell", _config_bell(2.0, 31)),
        ("3q GHZ", _config_ghz_3q(1.0, 21)),
        ("3q mixed", _config_3q_mixed(1.0, 21)),
        ("4q empty light", _config_empty(4, 0.5, 11)),
        ("4q idle-after", _config_h(4, 0.8, 21)),
        ("4q two CNOT", _config_4q_two_cnot(0.8, 21)),
    ]


def _run_with_state(
    config: SimulationConfig,
    engine: str,
) -> tuple[SimulationResult, Matrix]:
    context = force_numpy_dense_execution() if engine == "numpy" else force_python_dense_execution()
    with context:
        result = run_simulation(config)
        rates = compute_environment_rates(config.environment)
        collapse_ops = prepare_collapse_operators(
            multi_qubit_environment_collapse_operators(
                config.circuit.logical_qubits,
                rates,
            )
        )
        series = _simulate_circuit_gate_aware_hamiltonian(
            config=config,
            duration_us=config.duration_us,
            time_steps=config.time_steps,
            collapse_ops=collapse_ops,
            max_environment_rate_per_us=_max_environment_rate_per_us(rates),
            caches=_SimulationCaches.empty(),
            kernel_stats=_KernelStats(PYTHON_DENSE_BACKEND),
        )
    return result, series.final_noisy_state


def _environment() -> EnvironmentConfig:
    return EnvironmentConfig(
        input_mode="physical",
        device_quality=0.8,
        temperature_mk=15.0,
        flux_noise_phi0=1e-6,
        qubit_frequency_ghz=5.0,
        t1_max_us=100.0,
        tphi_max_us=100.0,
    )


def _config_empty(qubits: int, duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=qubits,
            initial_states=["0"] * qubits,
            columns=[],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
        simulation_backend="python_dense",
    )


def _config_h(qubits: int, duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=qubits,
            initial_states=["0"] * qubits,
            columns=[GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])])],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
        simulation_backend="python_dense",
    )


def _config_bell(duration_us: float, time_steps: int) -> SimulationConfig:
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
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
        simulation_backend="python_dense",
    )


def _config_ghz_3q(duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=3,
            initial_states=["0", "0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(
                    step=1,
                    gates=[GateOperation(type="CNOT", targets=[1], controls=[0])],
                ),
                GateColumn(
                    step=2,
                    gates=[GateOperation(type="CNOT", targets=[2], controls=[1])],
                ),
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
        simulation_backend="python_dense",
    )


def _config_3q_mixed(duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=3,
            initial_states=["0", "0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(step=1, gates=[GateOperation(type="X", targets=[2])]),
                GateColumn(
                    step=2,
                    gates=[GateOperation(type="CNOT", targets=[1], controls=[0])],
                ),
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
        simulation_backend="python_dense",
    )


def _config_4q_two_cnot(duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=4,
            initial_states=["0", "0", "0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(
                    step=1,
                    gates=[GateOperation(type="CNOT", targets=[1], controls=[0])],
                ),
                GateColumn(step=2, gates=[GateOperation(type="H", targets=[2])]),
                GateColumn(
                    step=3,
                    gates=[GateOperation(type="CNOT", targets=[3], controls=[2])],
                ),
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
        simulation_backend="python_dense",
    )


def _assert_results_close(
    test_case: unittest.TestCase,
    expected: SimulationResult,
    actual: SimulationResult,
) -> None:
    _assert_sequences_close(test_case, expected.fidelity, actual.fidelity)
    _assert_sequences_close(test_case, expected.purity, actual.purity)
    test_case.assertAlmostEqual(
        expected.diagnostics["final_fidelity"],
        actual.diagnostics["final_fidelity"],
        delta=ABS_TOL,
    )
    test_case.assertAlmostEqual(
        expected.diagnostics["final_purity"],
        actual.diagnostics["final_purity"],
        delta=ABS_TOL,
    )
    test_case.assertAlmostEqual(
        expected.diagnostics["completion_fidelity"],
        actual.diagnostics["completion_fidelity"],
        delta=ABS_TOL,
    )
    test_case.assertAlmostEqual(
        expected.diagnostics["completion_purity"],
        actual.diagnostics["completion_purity"],
        delta=ABS_TOL,
    )
    _assert_float_maps_close(
        test_case,
        expected.output_probabilities,
        actual.output_probabilities,
    )


def _assert_sequences_close(
    test_case: unittest.TestCase,
    expected: list[float],
    actual: list[float],
) -> None:
    test_case.assertEqual(len(expected), len(actual))
    for expected_value, actual_value in zip(expected, actual):
        _assert_float_close(test_case, expected_value, actual_value)


def _assert_timeline_close(
    test_case: unittest.TestCase,
    expected: list[dict[str, float | None]],
    actual: list[dict[str, float | None]],
) -> None:
    test_case.assertEqual(len(expected), len(actual))
    for expected_row, actual_row in zip(expected, actual):
        _assert_float_close(test_case, expected_row["fidelity"], actual_row["fidelity"])
        _assert_float_close(test_case, expected_row["purity"], actual_row["purity"])


def _assert_float_maps_close(
    test_case: unittest.TestCase,
    expected: dict[str, float],
    actual: dict[str, float],
) -> None:
    test_case.assertEqual(expected.keys(), actual.keys())
    for key, expected_value in expected.items():
        _assert_float_close(test_case, expected_value, actual[key])


def _assert_float_close(
    test_case: unittest.TestCase,
    expected: float | None,
    actual: float | None,
) -> None:
    test_case.assertIsNotNone(expected)
    test_case.assertIsNotNone(actual)
    assert expected is not None
    assert actual is not None
    allowed = ABS_TOL + REL_TOL * abs(expected)
    test_case.assertAlmostEqual(expected, actual, delta=allowed)


def _assert_matrices_close(
    test_case: unittest.TestCase,
    expected: Matrix,
    actual: Matrix,
) -> None:
    test_case.assertEqual(len(expected), len(actual))
    for row in range(len(expected)):
        for column in range(len(expected[row])):
            _assert_float_close(
                test_case,
                expected[row][column].real,
                actual[row][column].real,
            )
            _assert_float_close(
                test_case,
                expected[row][column].imag,
                actual[row][column].imag,
            )


def _assert_density_sane(test_case: unittest.TestCase, matrix: Matrix) -> None:
    for row in matrix:
        for value in row:
            test_case.assertTrue(isfinite(value.real))
            test_case.assertTrue(isfinite(value.imag))

    test_case.assertAlmostEqual(abs(trace(matrix) - 1.0), 0.0, delta=TRACE_TOL)
    test_case.assertLessEqual(_max_abs_matrix_difference(matrix, adjoint(matrix)), HERMITICITY_TOL)

    probabilities = output_probabilities(matrix, int(round(len(matrix).bit_length() - 1)))
    for probability in probabilities.values():
        test_case.assertTrue(isfinite(probability))
        test_case.assertGreaterEqual(probability, PROB_NEG_TOL)
    test_case.assertAlmostEqual(sum(probabilities.values()), 1.0, delta=PROB_SUM_TOL)

    purity = trace(_matmul(matrix, matrix)).real
    fidelity_like = max(probabilities.values())
    test_case.assertTrue(isfinite(purity))
    test_case.assertTrue(isfinite(fidelity_like))
    test_case.assertGreaterEqual(purity, -1e-10)
    test_case.assertLessEqual(purity, 1.0 + 1e-10)
    test_case.assertGreaterEqual(fidelity_like, -1e-10)
    test_case.assertLessEqual(fidelity_like, 1.0 + 1e-10)


def _minimum_eigenvalue(matrix: Matrix) -> float:
    array = np.array(matrix, dtype=np.complex128)
    return float(np.linalg.eigvalsh(array).min())


def _max_abs_matrix_difference(left: Matrix, right: Matrix) -> float:
    return max(
        abs(left[row][column] - right[row][column])
        for row in range(len(left))
        for column in range(len(left[row]))
    )


def _matmul(left: Matrix, right: Matrix) -> Matrix:
    return tuple(
        tuple(
            sum(left[row][inner] * right[inner][column] for inner in range(len(right)))
            for column in range(len(right[0]))
        )
        for row in range(len(left))
    )


if __name__ == "__main__":
    unittest.main()
