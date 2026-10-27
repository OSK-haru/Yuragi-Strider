"""Phenomenological but CPTP imperfect-reset primitives.

An active reset attempt is represented as a classical mixture: with
``success_probability`` the local subsystem is replaced by a configured
diagonal target state, and otherwise its prior state is retained.  The failed
branch therefore preserves cross-job information instead of reducing reset to
an input-independent preparation error.
"""

from __future__ import annotations

from functools import lru_cache
import math
from dataclasses import dataclass
from functools import reduce

from core.backend_boundary import PYTHON_DENSE_BACKEND, RUST_DENSE_PREVIEW_BACKEND
from core.cptp import KrausChannel
from core.gates import Matrix, identity_matrix, tensor


@dataclass(frozen=True)
class ImperfectResetSpec:
    """One local reset attempt and its post-success target populations."""

    success_probability: float
    target_populations: tuple[float, ...] = (1.0, 0.0)

    def __post_init__(self) -> None:
        success = _probability(self.success_probability, "success_probability")
        populations = tuple(float(value) for value in self.target_populations)
        if len(populations) not in (2, 3):
            raise ValueError("target_populations must describe two or three levels")
        if any(not math.isfinite(value) or value < 0.0 for value in populations):
            raise ValueError("target_populations must be finite and non-negative")
        if not math.isclose(sum(populations), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("target_populations must sum to one")
        object.__setattr__(self, "success_probability", success)
        object.__setattr__(self, "target_populations", populations)

    @property
    def local_dimension(self) -> int:
        return len(self.target_populations)


def imperfect_reset_channel(spec: ImperfectResetSpec) -> KrausChannel:
    """Build the local CPTP reset channel described by ``spec``.

    For a local density matrix ``rho`` and diagonal target ``tau`` this is

        E(rho) = (1 - s) rho + s tau Tr(rho).

    The identity-only branch at ``s == 0`` is kept exact so disabling the
    extension is bit-for-bit identical to the input state.
    """

    dimension = spec.local_dimension
    success = spec.success_probability
    if success == 0.0:
        return KrausChannel(
            name="imperfect_reset_identity",
            operators=(identity_matrix(dimension),),
        )

    operators: list[Matrix] = []
    survival = 1.0 - success
    if survival > 0.0:
        operators.append(_scale(identity_matrix(dimension), math.sqrt(survival)))

    for output_level, population in enumerate(spec.target_populations):
        if population == 0.0:
            continue
        coefficient = math.sqrt(success * population)
        for input_level in range(dimension):
            rows = [[0.0j for _ in range(dimension)] for _ in range(dimension)]
            rows[output_level][input_level] = coefficient + 0.0j
            operators.append(tuple(tuple(row) for row in rows))

    return KrausChannel(
        name="imperfect_active_reset",
        operators=tuple(operators),
    )


def apply_local_imperfect_reset(
    state: Matrix,
    subsystem_count: int,
    target: int,
    spec: ImperfectResetSpec,
    kernel_backend: str = PYTHON_DENSE_BACKEND,
) -> Matrix:
    """Apply one reset attempt to a tensor-product qubit/qutrit register.

    Subsystem zero is the most-significant tensor factor, matching the pulse
    network and gate-aware basis conventions.
    """

    count = int(subsystem_count)
    if count < 1:
        raise ValueError("subsystem_count must be positive")
    if target < 0 or target >= count:
        raise ValueError("reset target is outside the register")
    dimension = spec.local_dimension
    expected = dimension ** count
    if len(state) != expected or any(len(row) != expected for row in state):
        raise ValueError("state dimension does not match the reset register")
    if kernel_backend not in (PYTHON_DENSE_BACKEND, RUST_DENSE_PREVIEW_BACKEND):
        raise ValueError(f"unsupported reset kernel backend: {kernel_backend}")
    if spec.success_probability == 0.0:
        return state

    if kernel_backend == RUST_DENSE_PREVIEW_BACKEND:
        from core.rust_dense_kernel import rust_apply_local_reset_sequence

        return rust_apply_local_reset_sequence(
            state,
            subsystem_count=count,
            local_dimension=dimension,
            targets=(target,),
            success_probabilities=(spec.success_probability,),
            target_populations=(spec.target_populations,),
            reset_count=1,
        )
    return _embedded_reset_channel(spec, target, count).apply(state)


@lru_cache(maxsize=256)
def _embedded_reset_channel(spec: ImperfectResetSpec, target: int, count: int) -> KrausChannel:
    """Build and audit each distinct embedded channel once; the result is immutable."""

    dimension = spec.local_dimension
    local_channel = imperfect_reset_channel(spec)
    embedded = tuple(
        _embed(operator, target, count, dimension)
        for operator in local_channel.operators
    )
    return KrausChannel(
        name=f"local_imperfect_reset_q{target}",
        operators=embedded,
    )


def _embed(
    operator: Matrix,
    target: int,
    subsystem_count: int,
    local_dimension: int,
) -> Matrix:
    factors = [identity_matrix(local_dimension) for _ in range(subsystem_count)]
    factors[target] = operator
    return reduce(tensor, factors)


def _scale(matrix: Matrix, scalar: float) -> Matrix:
    return tuple(tuple(scalar * value for value in row) for row in matrix)


def _probability(value: float, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0 or converted > 1.0:
        raise ValueError(f"{name} must be finite and between zero and one")
    return converted
