"""NumPy-backed dense evolution helpers for the Python simulator."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterator

from core.gates import CachedCollapseOperator, Matrix
from core.internal_profiling import active_internal_profile, elapsed_ms, timer_start

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised only without the existing dependency.
    np = None


_DENSE_EXECUTION_OVERRIDE: ContextVar[str | None] = ContextVar(
    "dense_execution_override",
    default=None,
)


@dataclass(frozen=True)
class NumpyEvolutionResult:
    state: Matrix
    zero_hamiltonian_fast_path_used: bool


@dataclass(frozen=True)
class _NumpyCachedCollapseOperator:
    operator: object
    operator_adjoint: object
    operator_adjoint_operator: object


def numpy_dense_available() -> bool:
    return np is not None


def should_use_numpy_dense() -> bool:
    override = _DENSE_EXECUTION_OVERRIDE.get()
    if override == "python":
        return False
    if override == "numpy":
        return numpy_dense_available()
    return numpy_dense_available()


@contextmanager
def force_python_dense_execution() -> Iterator[None]:
    token = _DENSE_EXECUTION_OVERRIDE.set("python")
    try:
        yield
    finally:
        _DENSE_EXECUTION_OVERRIDE.reset(token)


@contextmanager
def force_numpy_dense_execution() -> Iterator[None]:
    token = _DENSE_EXECUTION_OVERRIDE.set("numpy")
    try:
        yield
    finally:
        _DENSE_EXECUTION_OVERRIDE.reset(token)


def evolve_segment_numpy(
    state: Matrix,
    hamiltonian: Matrix,
    collapse_ops: Sequence[CachedCollapseOperator],
    dt: float,
    substeps: int,
) -> NumpyEvolutionResult:
    if np is None:
        raise RuntimeError("NumPy dense execution is not available")

    substeps = max(1, int(substeps))
    sub_dt = dt / substeps
    rho = _state_to_array(state)
    hamiltonian_array = _constant_matrix_to_array(hamiltonian)
    collapse_arrays = tuple(_collapse_to_array(collapse_op) for collapse_op in collapse_ops)
    hamiltonian_is_zero = bool(not np.any(hamiltonian_array))
    zero_fast_path_used = False

    for _ in range(substeps):
        rho, skipped_hamiltonian = _rk4_step_np(
            rho,
            hamiltonian_array,
            collapse_arrays,
            sub_dt,
            hamiltonian_is_zero,
        )
        zero_fast_path_used = zero_fast_path_used or skipped_hamiltonian
        rho = _clean_density_matrix_np(rho)

    return NumpyEvolutionResult(
        state=_array_to_matrix(rho),
        zero_hamiltonian_fast_path_used=zero_fast_path_used,
    )


def _rk4_step_np(
    rho,
    hamiltonian,
    collapse_ops: Sequence[_NumpyCachedCollapseOperator],
    dt: float,
    hamiltonian_is_zero: bool,
):
    profile = active_internal_profile()
    if profile is None:
        k1, skipped_hamiltonian = _rhs_np(
            rho,
            hamiltonian,
            collapse_ops,
            hamiltonian_is_zero,
        )
        k2, _ = _rhs_np(
            rho + 0.5 * dt * k1,
            hamiltonian,
            collapse_ops,
            hamiltonian_is_zero,
        )
        k3, _ = _rhs_np(
            rho + 0.5 * dt * k2,
            hamiltonian,
            collapse_ops,
            hamiltonian_is_zero,
        )
        k4, _ = _rhs_np(
            rho + dt * k3,
            hamiltonian,
            collapse_ops,
            hamiltonian_is_zero,
        )
        return rho + (dt / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4), skipped_hamiltonian

    started_at = timer_start()
    profile.rk4_step_count += 1
    try:
        k1, skipped_hamiltonian = _rhs_np(
            rho,
            hamiltonian,
            collapse_ops,
            hamiltonian_is_zero,
        )
        k2, _ = _rhs_np(
            _add_scale_np(rho, 0.5 * dt, k1),
            hamiltonian,
            collapse_ops,
            hamiltonian_is_zero,
        )
        k3, _ = _rhs_np(
            _add_scale_np(rho, 0.5 * dt, k2),
            hamiltonian,
            collapse_ops,
            hamiltonian_is_zero,
        )
        k4, _ = _rhs_np(
            _add_scale_np(rho, dt, k3),
            hamiltonian,
            collapse_ops,
            hamiltonian_is_zero,
        )
        weighted = _add_np(k1, _scale_np(2.0, k2), _scale_np(2.0, k3), k4)
        return _add_scale_np(rho, dt / 6.0, weighted), skipped_hamiltonian
    finally:
        profile.rk4_total_ms += elapsed_ms(started_at)


def _rhs_np(
    rho,
    hamiltonian,
    collapse_ops: Sequence[_NumpyCachedCollapseOperator],
    hamiltonian_is_zero: bool,
):
    profile = active_internal_profile()
    if profile is None:
        if hamiltonian_is_zero:
            derivative = np.zeros_like(rho)
            skipped_hamiltonian = True
        else:
            derivative = -1j * (_matmul_np(hamiltonian, rho) - _matmul_np(rho, hamiltonian))
            skipped_hamiltonian = False

        if collapse_ops:
            # sum_k L_k^dagger L_k is state-independent, so the anticommutator
            # costs two products per call instead of two per operator.
            decay = _sum_adjoint_operators(collapse_ops)
            derivative = derivative - 0.5 * (
                _matmul_np(decay, rho) + _matmul_np(rho, decay)
            )
        for collapse_op in collapse_ops:
            derivative = derivative + _matmul_np(
                _matmul_np(collapse_op.operator, rho), collapse_op.operator_adjoint
            )
        return derivative, skipped_hamiltonian

    rhs_started_at = timer_start()
    profile.rhs_call_count += 1
    try:
        started_at = timer_start()
        if hamiltonian_is_zero:
            derivative = np.zeros_like(rho)
            skipped_hamiltonian = True
            profile.zero_hamiltonian_skip_count += 1
        else:
            derivative = _scale_np(
                -1j,
                _subtract_np(
                    _matmul_np(hamiltonian, rho),
                    _matmul_np(rho, hamiltonian),
                ),
            )
            skipped_hamiltonian = False
        profile.hamiltonian_term_ms += elapsed_ms(started_at)

        if collapse_ops:
            started_at = timer_start()
            decay = _sum_adjoint_operators(collapse_ops)
            derivative = _subtract_np(
                derivative,
                _scale_np(
                    0.5,
                    _add_np(_matmul_np(decay, rho), _matmul_np(rho, decay)),
                ),
            )
            profile.dissipator_total_ms += elapsed_ms(started_at)

        for collapse_op in collapse_ops:
            profile.dissipator_operator_iterations += 1
            started_at = timer_start()
            jump = _matmul_np(
                _matmul_np(collapse_op.operator, rho),
                collapse_op.operator_adjoint,
            )
            profile.dissipator_total_ms += elapsed_ms(started_at)

            started_at = timer_start()
            derivative = _add_np(derivative, jump)
            profile.matrix_accumulation_ms += elapsed_ms(started_at)
        return derivative, skipped_hamiltonian
    finally:
        profile.rhs_total_ms += elapsed_ms(rhs_started_at)


def _sum_adjoint_operators(collapse_ops: Sequence[_NumpyCachedCollapseOperator]):
    total = collapse_ops[0].operator_adjoint_operator
    for collapse_op in collapse_ops[1:]:
        total = total + collapse_op.operator_adjoint_operator
    return total


def _matmul_np(left, right):
    profile = active_internal_profile()
    if profile is None:
        return left @ right

    started_at = timer_start()
    try:
        return left @ right
    finally:
        elapsed = elapsed_ms(started_at)
        profile.matmul_call_count += 1
        profile.matmul_total_ms += elapsed
        profile.numpy_matmul_call_count += 1
        profile.numpy_matmul_total_ms += elapsed


def _add_np(*matrices):
    profile = active_internal_profile()
    if profile is None:
        result = matrices[0]
        for matrix in matrices[1:]:
            result = result + matrix
        return result

    started_at = timer_start()
    try:
        result = matrices[0]
        for matrix in matrices[1:]:
            result = result + matrix
        return result
    finally:
        profile.matrix_add_scale_call_count += 1
        profile.matrix_add_scale_total_ms += elapsed_ms(started_at)


def _subtract_np(left, right):
    return _add_np(left, _scale_np(-1.0, right))


def _scale_np(value: complex, matrix):
    profile = active_internal_profile()
    if profile is None:
        return value * matrix

    started_at = timer_start()
    try:
        return value * matrix
    finally:
        profile.matrix_add_scale_call_count += 1
        profile.matrix_add_scale_total_ms += elapsed_ms(started_at)


def _add_scale_np(matrix, scale: complex, increment):
    profile = active_internal_profile()
    if profile is None:
        return matrix + scale * increment

    started_at = timer_start()
    try:
        return matrix + scale * increment
    finally:
        profile.matrix_add_scale_call_count += 1
        profile.matrix_add_scale_total_ms += elapsed_ms(started_at)


def _clean_density_matrix_np(rho):
    rho = 0.5 * (rho + rho.conj().T)
    trace_value = np.trace(rho)
    if abs(trace_value) == 0.0:
        raise ValueError("density matrix trace vanished during evolution")
    return rho / trace_value


def _state_to_array(matrix: Matrix):
    return _matrix_to_array(matrix, copy=True)


def _constant_matrix_to_array(matrix: Matrix):
    return _matrix_to_array_cached(matrix)


def _collapse_to_array(collapse_op: CachedCollapseOperator) -> _NumpyCachedCollapseOperator:
    return _NumpyCachedCollapseOperator(
        operator=_constant_matrix_to_array(collapse_op.operator),
        operator_adjoint=_constant_matrix_to_array(collapse_op.operator_adjoint),
        operator_adjoint_operator=_constant_matrix_to_array(
            collapse_op.operator_adjoint_operator
        ),
    )


def _matrix_to_array(matrix: Matrix, *, copy: bool):
    profile = active_internal_profile()
    if profile is None:
        return np.array(matrix, dtype=np.complex128, copy=copy)

    started_at = timer_start()
    try:
        return np.array(matrix, dtype=np.complex128, copy=copy)
    finally:
        profile.conversion_count += 1
        profile.conversion_total_ms += elapsed_ms(started_at)


@lru_cache(maxsize=512)
def _matrix_to_array_cached(matrix: Matrix):
    profile = active_internal_profile()
    if profile is None:
        return np.array(matrix, dtype=np.complex128)

    started_at = timer_start()
    try:
        return np.array(matrix, dtype=np.complex128)
    finally:
        profile.conversion_count += 1
        profile.conversion_total_ms += elapsed_ms(started_at)


def _array_to_matrix(array) -> Matrix:
    profile = active_internal_profile()
    if profile is None:
        return tuple(tuple(complex(entry) for entry in row) for row in array.tolist())

    started_at = timer_start()
    try:
        return tuple(tuple(complex(entry) for entry in row) for row in array.tolist())
    finally:
        profile.conversion_count += 1
        profile.conversion_total_ms += elapsed_ms(started_at)
