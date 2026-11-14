import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.dense_numpy import (
    evolve_segment_numpy,
    force_python_dense_execution,
    numpy_dense_available,
)
from core.gates import (
    Matrix,
    adjoint,
    clean_density_matrix,
    initial_density_matrix,
    matmul,
    multi_qubit_environment_collapse_operators,
    output_probabilities,
    prepare_collapse_operators,
    rk4_step_cached,
    trace,
    zero_hamiltonian,
)
from core.physical_environment import compute_environment_rates
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import run_simulation


ABS_TOL = 1e-10
# NumPy and the pure-Python RK4 path use different summation orders. The
# observed backend drift is below 3e-8 for the representative dense cases;
# retain a strict bound without requiring bit-level equality across kernels.
REL_TOL = 5e-8


@unittest.skipUnless(numpy_dense_available(), "NumPy dense execution is unavailable")
class CoreDenseOptimizationTest(unittest.TestCase):
    def test_numpy_segment_matches_pure_python_segment(self) -> None:
        config = _config_empty(4, duration_us=0.5, time_steps=11)
        state = initial_density_matrix(config.circuit.initial_states)
        rates = compute_environment_rates(config.environment)
        collapse_ops = prepare_collapse_operators(
            multi_qubit_environment_collapse_operators(
                config.circuit.logical_qubits,
                rates,
            )
        )
        hamiltonian = zero_hamiltonian(len(state))
        dt = 0.05

        pure_state = clean_density_matrix(
            rk4_step_cached(state, hamiltonian, collapse_ops, dt)
        )
        numpy_state = evolve_segment_numpy(
            state,
            hamiltonian,
            collapse_ops,
            dt,
            1,
        ).state

        _assert_matrices_close(self, pure_state, numpy_state)
        _assert_density_matrix_is_physical(self, numpy_state)

    def test_optimized_and_fallback_results_match_for_regression_cases(self) -> None:
        for name, config in _regression_cases():
            with self.subTest(name=name):
                optimized = run_simulation(config)
                with force_python_dense_execution():
                    fallback = run_simulation(config)

                _assert_results_close(self, fallback, optimized)
                self.assertEqual(
                    optimized.diagnostics["core_dense_execution_engine"],
                    "numpy_dense_v1",
                )
                self.assertEqual(
                    fallback.diagnostics["core_dense_execution_engine"],
                    "python_tuple_v1",
                )

    def test_zero_hamiltonian_fast_path_is_reported_for_idle_case(self) -> None:
        result = run_simulation(_config_empty(4, duration_us=0.5, time_steps=11))

        self.assertTrue(result.diagnostics["core_zero_hamiltonian_fast_path_used"])
        self.assertEqual(result.diagnostics["core_dense_execution_engine"], "numpy_dense_v1")


def _regression_cases() -> list[tuple[str, SimulationConfig]]:
    return [
        ("2q Bell", _config_bell(duration_us=2.0, time_steps=31)),
        ("3q empty default", _config_empty(3, duration_us=2.0, time_steps=101)),
        ("4q empty light", _config_empty(4, duration_us=0.5, time_steps=11)),
        ("4q empty default", _config_empty(4, duration_us=2.0, time_steps=101)),
        ("4q H(q0) light", _config_h(4, duration_us=0.5, time_steps=11)),
        ("4q H+CNOT light", _config_h_cnot_4q(duration_us=0.5, time_steps=11)),
    ]


def _environment() -> EnvironmentConfig:
    return EnvironmentConfig(
        t1_zero_temperature_us=39.810717055349734,
        tphi_zero_temperature_us=33.20178309308936,
        temperature_mk=15.0,
        qubit_frequency_ghz=5.0,
    )


def _config_empty(
    qubits: int,
    *,
    duration_us: float,
    time_steps: int,
) -> SimulationConfig:
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
    )


def _config_h(
    qubits: int,
    *,
    duration_us: float,
    time_steps: int,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=qubits,
            initial_states=["0"] * qubits,
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])])
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def _config_bell(*, duration_us: float, time_steps: int) -> SimulationConfig:
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
    )


def _config_h_cnot_4q(*, duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=4,
            initial_states=["0", "0", "0", "0"],
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
    )


def _assert_results_close(
    test_case: unittest.TestCase,
    expected: SimulationResult,
    actual: SimulationResult,
) -> None:
    _assert_sequences_close(test_case, expected.times, actual.times)
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
    for label, value in expected.output_probabilities.items():
        test_case.assertAlmostEqual(
            value,
            actual.output_probabilities[label],
            delta=ABS_TOL,
        )
    test_case.assertLessEqual(actual.diagnostics["max_trace_error"], 1e-9)


def _assert_sequences_close(
    test_case: unittest.TestCase,
    expected: list[float],
    actual: list[float],
) -> None:
    test_case.assertEqual(len(expected), len(actual))
    for expected_value, actual_value in zip(expected, actual):
        allowed = ABS_TOL + REL_TOL * abs(expected_value)
        test_case.assertAlmostEqual(expected_value, actual_value, delta=allowed)


def _assert_matrices_close(
    test_case: unittest.TestCase,
    expected: Matrix,
    actual: Matrix,
) -> None:
    test_case.assertEqual(len(expected), len(actual))
    for row in range(len(expected)):
        for column in range(len(expected[row])):
            allowed = ABS_TOL + REL_TOL * abs(expected[row][column])
            test_case.assertAlmostEqual(
                expected[row][column].real,
                actual[row][column].real,
                delta=allowed,
            )
            test_case.assertAlmostEqual(
                expected[row][column].imag,
                actual[row][column].imag,
                delta=allowed,
            )


def _assert_density_matrix_is_physical(
    test_case: unittest.TestCase,
    matrix: Matrix,
) -> None:
    test_case.assertAlmostEqual(trace(matrix).real, 1.0, delta=1e-10)
    test_case.assertAlmostEqual(trace(matrix).imag, 0.0, delta=1e-10)
    _assert_matrices_close(test_case, matrix, adjoint(matrix))
    probabilities = output_probabilities(matrix, int(round(len(matrix).bit_length() - 1)))
    test_case.assertAlmostEqual(sum(probabilities.values()), 1.0, delta=1e-10)
