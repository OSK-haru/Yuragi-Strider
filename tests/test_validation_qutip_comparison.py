"""VALIDATION-7 independent QuTiP comparisons for the production matrices."""

from __future__ import annotations

import math
import unittest

import numpy as np

from core.backend_boundary import PYTHON_DENSE_BACKEND
from core.circuit_model import GateColumn, GateOperation
from core.gates import (
    SIGMA_MINUS,
    SIGMA_PLUS,
    Z,
    column_unitary,
    effective_hamiltonian_from_involution,
    initial_density_matrix,
    multi_qubit_physical_collapse_operators,
    prepare_collapse_operators,
    zero_hamiltonian,
)
from core.physical_environment import compute_environment_rates
from core.results import EnvironmentConfig
from core.simulator import _KernelStats, _evolve_stable_with_substeps
from validation_pulse.qutip_adapter import QUTIP_AVAILABLE, as_qutip_operator, compare_density_matrices, run_qutip_constant_segment, run_qutip_piecewise_segments


QUANTA_STEP_US = 0.03125
IDLE_TIMES_US = (0.0, 2.5, 5.0, 10.0, 20.0, 50.0)
PHYSICALITY_TOLERANCE = 1e-10


def _quanta_constant(rho0, hamiltonian, collapse_ops, times_us, *, step_us=QUANTA_STEP_US):
    state = rho0
    snapshots = [state]
    for start, target in zip(times_us, times_us[1:]):
        duration = float(target - start)
        state = _evolve_stable_with_substeps(
            state,
            hamiltonian,
            prepare_collapse_operators(collapse_ops),
            duration,
            max(1, math.ceil(duration / step_us)),
            _KernelStats(PYTHON_DENSE_BACKEND),
            blocked_by_sampling=False,
        )
        snapshots.append(state)
    return snapshots


def _gate_columns(two_qubit: bool = False):
    columns = [GateColumn(step=0, gates=[GateOperation(type="H", targets=[0], params={"duration_us": 8.0})])]
    if two_qubit:
        columns.append(GateColumn(step=1, gates=[GateOperation(type="CNOT", controls=[0], targets=[1], params={"duration_us": 16.0})]))
    return tuple(columns)


def _gate_segments(columns, n_qubits):
    return [
        {
            "duration_us": float(column.gates[0].params["duration_us"]),
            "hamiltonian": effective_hamiltonian_from_involution(column_unitary(column, n_qubits), float(column.gates[0].params["duration_us"])),
        }
        for column in columns
    ]


def _quanta_piecewise(rho0, segments, collapse_ops, *, step_us=QUANTA_STEP_US):
    state = rho0
    snapshots = [state]
    cached = prepare_collapse_operators(collapse_ops)
    for segment in segments:
        duration = segment["duration_us"]
        state = _evolve_stable_with_substeps(
            state, segment["hamiltonian"], cached, duration,
            max(1, math.ceil(duration / step_us)), _KernelStats(PYTHON_DENSE_BACKEND),
            blocked_by_sampling=False, blocked_by_boundary=True,
        )
        snapshots.append(state)
    return snapshots


def _assert_metrics(testcase: unittest.TestCase, metrics: dict[str, float], tolerance: float) -> None:
    testcase.assertLessEqual(metrics["max_element_difference"], tolerance)
    testcase.assertLessEqual(metrics["trace_distance"], tolerance)
    for key in ("quanta_trace_error", "qutip_trace_error", "quanta_hermiticity_error", "qutip_hermiticity_error"):
        testcase.assertLessEqual(metrics[key], PHYSICALITY_TOLERANCE, msg=key)
    testcase.assertGreaterEqual(metrics["quanta_minimum_eigenvalue"], -PHYSICALITY_TOLERANCE)
    testcase.assertGreaterEqual(metrics["qutip_minimum_eigenvalue"], -PHYSICALITY_TOLERANCE)
    testcase.assertTrue(all(math.isfinite(value) for value in metrics.values()))


@unittest.skipUnless(QUTIP_AVAILABLE, "QuTiP is a validation-only optional dependency")
class QuTiPComparisonTest(unittest.TestCase):
    def test_basis_and_qubit_order_audit(self) -> None:
        ket_zero = np.array([1.0, 0.0])
        ket_one = np.array([0.0, 1.0])
        self.assertTrue(np.allclose(np.asarray(SIGMA_MINUS) @ ket_one, ket_zero))
        self.assertTrue(np.allclose(np.asarray(SIGMA_MINUS) @ ket_zero, 0.0))
        self.assertTrue(np.allclose(np.asarray(SIGMA_PLUS) @ ket_zero, ket_one))
        self.assertTrue(np.allclose(np.asarray(Z) @ ket_zero, ket_zero))
        self.assertTrue(np.allclose(np.asarray(Z) @ ket_one, -ket_one))
        qobj = as_qutip_operator(initial_density_matrix(["1", "0"]), 2)
        self.assertEqual(qobj.dims, [[2, 2], [2, 2]])
        self.assertEqual(int(np.argmax(np.diag(qobj.full()).real)), 2)  # |10>, q0 is MSB

    def test_v7_0_unitary_h_gate(self) -> None:
        self._constant_case("0", 0.0, 0.0, 0.0, zero_hamiltonian(2), tolerance=1e-8, gate=True)

    def test_v7_1_downward_relaxation(self) -> None:
        self._constant_case("1", 0.1, 0.0, 0.0, zero_hamiltonian(2), tolerance=1e-8)

    def test_v7_2_pure_dephasing(self) -> None:
        self._constant_case("+", 0.0, 0.0, 0.1, zero_hamiltonian(2), tolerance=1e-8)

    def test_v7_3_finite_temperature_relaxation(self) -> None:
        self._constant_case("1", 0.051, 0.049, 0.0, zero_hamiltonian(2), tolerance=1e-8)

    def test_v7_4_driven_one_qubit(self) -> None:
        self._piecewise_case(_gate_columns(), 1, 0.02, 0.003, 0.015, tolerance=1e-7)

    def test_v7_5_two_qubit_bell(self) -> None:
        self._piecewise_case(_gate_columns(True), 2, 0.02, 0.003, 0.015, tolerance=2e-7)

    def test_v7_6_physical_input_path(self) -> None:
        environment = EnvironmentConfig(
            t1_zero_temperature_us=100.0,
            tphi_zero_temperature_us=100.0,
            temperature_mk=100.0,
            qubit_frequency_ghz=5.0,
        )
        rates = compute_environment_rates(environment)
        self.assertGreater(rates.gamma_down_per_us, 0.0)
        self._constant_case("+", rates.gamma_down_per_us, rates.gamma_up_per_us, rates.gamma_phi_per_us, zero_hamiltonian(2), tolerance=1e-7)

    def _constant_case(self, initial, down, up, phi, hamiltonian, *, tolerance, gate=False) -> None:
        rho0 = initial_density_matrix([initial])
        if gate:
            columns = _gate_columns()
            segment = _gate_segments(columns, 1)[0]
            quanta = _quanta_piecewise(rho0, [segment], [])[-1]
            qutip = run_qutip_piecewise_segments(rho0, [segment], [], 1)[-1]
            _assert_metrics(self, compare_density_matrices(quanta, qutip), tolerance)
            return
        collapse_ops = multi_qubit_physical_collapse_operators(1, down, up, phi)
        quanta_states = _quanta_constant(rho0, hamiltonian, collapse_ops, IDLE_TIMES_US)
        qutip_states = run_qutip_constant_segment(rho0, hamiltonian, collapse_ops, 1, IDLE_TIMES_US[-1], IDLE_TIMES_US)
        for quanta, qutip in zip(quanta_states, qutip_states):
            _assert_metrics(self, compare_density_matrices(quanta, qutip), tolerance)

    def _piecewise_case(self, columns, n_qubits, down, up, phi, *, tolerance) -> None:
        segments = _gate_segments(columns, n_qubits)
        rho0 = initial_density_matrix(["0"] * n_qubits)
        collapse_ops = multi_qubit_physical_collapse_operators(n_qubits, down, up, phi)
        quanta_states = _quanta_piecewise(rho0, segments, collapse_ops)
        qutip_states = run_qutip_piecewise_segments(rho0, segments, collapse_ops, n_qubits)
        for quanta, qutip in zip(quanta_states, qutip_states):
            _assert_metrics(self, compare_density_matrices(quanta, qutip), tolerance)


if __name__ == "__main__":
    unittest.main()
