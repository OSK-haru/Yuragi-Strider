"""Compare NumPy and pure-Python dense simulation backends."""

from __future__ import annotations

import sys
import time
from math import isfinite
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.backend_boundary import PYTHON_DENSE_BACKEND
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.dense_numpy import force_numpy_dense_execution, force_python_dense_execution
from core.gates import (
    Matrix,
    adjoint,
    multi_qubit_environment_collapse_operators,
    output_probabilities,
    prepare_collapse_operators,
    trace,
)
from core.physical_environment import compute_environment_rates
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import (
    _KernelStats,
    _SimulationCaches,
    _max_environment_rate_per_us,
    _simulate_circuit_gate_aware_hamiltonian,
    run_simulation,
)

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - NumPy is an existing dependency.
    raise SystemExit(f"NumPy is required for this regression script: {exc}") from exc


ABS_TOL = 1e-10
REL_TOL = 1e-9
TRACE_TOL = 1e-9
HERMITICITY_TOL = 1e-9
EIGENVALUE_TOL = -1e-9


def main() -> int:
    headers = [
        "case",
        "qubits",
        "numpy_ms",
        "python_ms",
        "speedup",
        "max_rho_diff",
        "max_prob_diff",
        "trace_err",
        "herm_err",
        "min_eig",
        "status",
    ]
    print(" | ".join(headers))
    failures = 0
    max_rho_diff = 0.0
    max_prob_diff = 0.0
    max_trace_error = 0.0
    max_hermiticity_error = 0.0
    minimum_eigenvalue: float | None = None

    for case in _cases():
        numpy_elapsed_ms, numpy_result, numpy_state = _timed_run(case.config, "numpy")
        python_elapsed_ms, python_result, python_state = _timed_run(case.config, "python")
        rho_diff = _max_abs_matrix_difference(python_state, numpy_state)
        prob_diff = _max_probability_difference(
            python_result.output_probabilities,
            numpy_result.output_probabilities,
        )
        trace_error = abs(trace(numpy_state) - 1.0)
        hermiticity_error = _max_abs_matrix_difference(numpy_state, adjoint(numpy_state))
        min_eig = (
            _minimum_eigenvalue(numpy_state)
            if case.check_eigenvalues
            else float("nan")
        )
        status = "PASS"

        try:
            _assert_results_close(python_result, numpy_result)
            _assert_close(rho_diff, 0.0)
            _assert_density_sane(numpy_state)
            if case.check_eigenvalues and min_eig < EIGENVALUE_TOL:
                raise AssertionError(f"minimum eigenvalue too negative: {min_eig}")
        except AssertionError as exc:
            status = f"FAIL: {exc}"
            failures += 1

        max_rho_diff = max(max_rho_diff, rho_diff)
        max_prob_diff = max(max_prob_diff, prob_diff)
        max_trace_error = max(max_trace_error, float(abs(trace_error)))
        max_hermiticity_error = max(max_hermiticity_error, hermiticity_error)
        if case.check_eigenvalues:
            minimum_eigenvalue = (
                min_eig
                if minimum_eigenvalue is None
                else min(minimum_eigenvalue, min_eig)
            )

        speedup = python_elapsed_ms / numpy_elapsed_ms if numpy_elapsed_ms > 0 else 0.0
        print(" | ".join([
            case.name,
            str(case.config.circuit.logical_qubits),
            _format_ms(numpy_elapsed_ms),
            _format_ms(python_elapsed_ms),
            f"{speedup:.2f}x",
            f"{rho_diff:.3e}",
            f"{prob_diff:.3e}",
            f"{float(abs(trace_error)):.3e}",
            f"{hermiticity_error:.3e}",
            "n/a" if not case.check_eigenvalues else f"{min_eig:.3e}",
            status,
        ]))

    print(
        "summary | "
        f"max_rho_diff={max_rho_diff:.3e} | "
        f"max_prob_diff={max_prob_diff:.3e} | "
        f"max_trace_err={max_trace_error:.3e} | "
        f"max_herm_err={max_hermiticity_error:.3e} | "
        f"min_eig={_format_optional_scientific(minimum_eigenvalue)}"
    )
    return 1 if failures else 0


class Case:
    def __init__(
        self,
        name: str,
        config: SimulationConfig,
        *,
        check_eigenvalues: bool = False,
    ) -> None:
        self.name = name
        self.config = config
        self.check_eigenvalues = check_eigenvalues


def _cases() -> list[Case]:
    return [
        Case("2q empty", _config_empty(2, 0.5, 11)),
        Case("2q X(q0)", _config_single(2, "X", 0, 0.5, 11)),
        Case("2q H(q0)", _config_single(2, "H", 0, 0.5, 11)),
        Case("2q Bell", _config_bell(2.0, 31), check_eigenvalues=True),
        Case("2q H-CNOT-H", _config_2q_h_cnot_h(1.0, 21)),
        Case("3q empty", _config_empty(3, 2.0, 101)),
        Case("3q X(q2)", _config_single(3, "X", 2, 1.0, 21)),
        Case("3q H(q0)", _config_single(3, "H", 0, 1.0, 21)),
        Case("3q GHZ", _config_ghz_3q(1.0, 21), check_eigenvalues=True),
        Case("3q mixed", _config_3q_mixed(1.0, 21)),
        Case("4q empty light", _config_empty(4, 0.5, 11)),
        Case("4q empty default", _config_empty(4, 2.0, 101), check_eigenvalues=True),
        Case("4q H(q0) light", _config_single(4, "H", 0, 0.5, 11)),
        Case("4q H+CNOT", _config_4q_h_cnot(0.5, 11)),
        Case("4q two CNOT", _config_4q_two_cnot(0.8, 21), check_eigenvalues=True),
        Case("4q idle after", _config_single(4, "H", 0, 0.8, 21)),
    ]


def _timed_run(
    config: SimulationConfig,
    engine: str,
) -> tuple[float, SimulationResult, Matrix]:
    context = force_numpy_dense_execution() if engine == "numpy" else force_python_dense_execution()
    started_at = time.perf_counter()
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
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    return elapsed_ms, result, series.final_noisy_state


def _environment() -> EnvironmentConfig:
    # The rates of the earlier quality-0.8 default (D89), so the stored regression baseline still applies.
    return EnvironmentConfig(
        temperature_mk=15.0,
        qubit_frequency_ghz=5.0,
        t1_zero_temperature_us=39.810717055349734,
        tphi_us=33.20178309308936,
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
    )


def _config_single(
    qubits: int,
    gate_type: str,
    target: int,
    duration_us: float,
    time_steps: int,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=qubits,
            initial_states=["0"] * qubits,
            columns=[
                GateColumn(step=0, gates=[GateOperation(type=gate_type, targets=[target])])
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def _config_bell(duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(step=1, gates=[GateOperation(type="CNOT", targets=[1], controls=[0])]),
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def _config_2q_h_cnot_h(duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(step=1, gates=[GateOperation(type="CNOT", targets=[1], controls=[0])]),
                GateColumn(step=2, gates=[GateOperation(type="H", targets=[1])]),
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def _config_ghz_3q(duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=3,
            initial_states=["0", "0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(step=1, gates=[GateOperation(type="CNOT", targets=[1], controls=[0])]),
                GateColumn(step=2, gates=[GateOperation(type="CNOT", targets=[2], controls=[1])]),
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def _config_3q_mixed(duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=3,
            initial_states=["0", "0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(step=1, gates=[GateOperation(type="X", targets=[2])]),
                GateColumn(step=2, gates=[GateOperation(type="CNOT", targets=[1], controls=[0])]),
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def _config_4q_h_cnot(duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=4,
            initial_states=["0", "0", "0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(step=1, gates=[GateOperation(type="CNOT", targets=[1], controls=[0])]),
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def _config_4q_two_cnot(duration_us: float, time_steps: int) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=4,
            initial_states=["0", "0", "0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(step=1, gates=[GateOperation(type="CNOT", targets=[1], controls=[0])]),
                GateColumn(step=2, gates=[GateOperation(type="H", targets=[2])]),
                GateColumn(step=3, gates=[GateOperation(type="CNOT", targets=[3], controls=[2])]),
            ],
        ),
        environment=_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def _assert_results_close(expected: SimulationResult, actual: SimulationResult) -> None:
    _assert_sequences_close(expected.fidelity, actual.fidelity)
    _assert_sequences_close(expected.purity, actual.purity)
    _assert_close(expected.diagnostics["final_fidelity"], actual.diagnostics["final_fidelity"])
    _assert_close(expected.diagnostics["final_purity"], actual.diagnostics["final_purity"])
    _assert_close(
        expected.diagnostics["completion_fidelity"],
        actual.diagnostics["completion_fidelity"],
    )
    _assert_close(
        expected.diagnostics["completion_purity"],
        actual.diagnostics["completion_purity"],
    )
    _assert_float_maps_close(expected.output_probabilities, actual.output_probabilities)


def _assert_density_sane(matrix: Matrix) -> None:
    for row in matrix:
        for value in row:
            if not isfinite(value.real) or not isfinite(value.imag):
                raise AssertionError("density matrix contains a non-finite value")
    trace_error = abs(trace(matrix) - 1.0)
    if trace_error > TRACE_TOL:
        raise AssertionError(f"trace error {trace_error} exceeded {TRACE_TOL}")
    hermiticity_error = _max_abs_matrix_difference(matrix, adjoint(matrix))
    if hermiticity_error > HERMITICITY_TOL:
        raise AssertionError(
            f"Hermiticity error {hermiticity_error} exceeded {HERMITICITY_TOL}"
        )
    probabilities = output_probabilities(matrix, int(round(len(matrix).bit_length() - 1)))
    for probability in probabilities.values():
        if probability < -1e-12:
            raise AssertionError(f"probability below tolerance: {probability}")
    _assert_close(sum(probabilities.values()), 1.0, tolerance=1e-9)


def _assert_sequences_close(expected: list[float], actual: list[float]) -> None:
    if len(expected) != len(actual):
        raise AssertionError(f"sequence lengths differ: {len(expected)} != {len(actual)}")
    for expected_value, actual_value in zip(expected, actual):
        _assert_close(expected_value, actual_value)


def _assert_float_maps_close(expected: dict[str, float], actual: dict[str, float]) -> None:
    if expected.keys() != actual.keys():
        raise AssertionError("probability labels differ")
    for key, expected_value in expected.items():
        _assert_close(expected_value, actual[key])


def _assert_close(
    expected: float | complex,
    actual: float | complex,
    *,
    tolerance: float | None = None,
) -> None:
    allowed = tolerance if tolerance is not None else ABS_TOL + REL_TOL * abs(expected)
    if abs(expected - actual) > allowed:
        raise AssertionError(f"{expected!r} != {actual!r} within {allowed}")


def _max_probability_difference(left: dict[str, float], right: dict[str, float]) -> float:
    return max(abs(left[key] - right[key]) for key in left)


def _max_abs_matrix_difference(left: Matrix, right: Matrix) -> float:
    return max(
        abs(left[row][column] - right[row][column])
        for row in range(len(left))
        for column in range(len(left[row]))
    )


def _minimum_eigenvalue(matrix: Matrix) -> float:
    array = np.array(matrix, dtype=np.complex128)
    return float(np.linalg.eigvalsh(array).min())


def _format_ms(value: float) -> str:
    return f"{value:.3f}"


def _format_optional_scientific(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3e}"


if __name__ == "__main__":
    raise SystemExit(main())
