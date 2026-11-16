"""Tensor-local operations preserving the full bounded register density matrix.

Qubit zero is the most-significant factor, as in core.gates. These kernels do
not trace away unaddressed qubits or approximate their correlations.

A register's subsystems need not all have two levels (D91): ``dims`` gives each one's level count, in
register order, and basis labels are mixed-radix numbers with subsystem zero most significant. ``dims=None``
is a register of two-level qubits, its count read from the state's size. Levels 0 and 1 of a subsystem are
its qubit; anything above is leakage. An operator given on qubits acts on that computational subspace and
leaves leaked states alone (``embed_qubit_operator``).
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
import math
import random
import numpy as np

from core.gates import Matrix

MIN_LEVELS, MAX_LEVELS = 2, 4


def register_dims(dimension: int, dims: Sequence[int] | None = None) -> tuple[int, ...]:
    """The level count of every subsystem of a register with ``dimension`` basis states."""
    if dims is None:
        n = dimension.bit_length() - 1
        if dimension != 1 << n or n < 1:
            raise ValueError("a register without declared levels must hold two-level qubits")
        return (2,) * n
    dims = tuple(dims)
    if (not dims or any(type(level) is not int or not MIN_LEVELS <= level <= MAX_LEVELS for level in dims)
            or math.prod(dims) != dimension):
        raise ValueError("the declared levels do not match the register")
    return dims


def level_digits(dims: Sequence[int]) -> np.ndarray:
    """The level of every subsystem in every basis label, shape (dimension, subsystems); read-only."""
    return _digits(tuple(dims))


def level_outcomes(dims: Sequence[int], qubits: Sequence[int]) -> tuple[np.ndarray, int]:
    """Each basis label's joint level on ``qubits`` as one mixed-radix number, and how many values it takes."""
    return _outcomes(tuple(dims), tuple(qubits))


@lru_cache(maxsize=256)
def _digits(dims: tuple[int, ...]) -> np.ndarray:
    """The level of every subsystem in every basis label, shape (dimension, subsystems)."""
    labels = np.arange(math.prod(dims))
    columns = []
    stride = labels.size
    for level in dims:
        stride //= level
        columns.append((labels // stride) % level)
    result = np.stack(columns, axis=1)
    result.setflags(write=False)
    return result


def _outcomes(dims: tuple[int, ...], qubits: Sequence[int]) -> tuple[np.ndarray, int]:
    """Each basis label's joint level on ``qubits`` as one mixed-radix number, and how many values it takes."""
    digits = _digits(dims)
    outcomes = np.zeros(len(digits), dtype=int)
    count = 1
    for q in qubits:
        outcomes = outcomes * dims[q] + digits[:, q]
        count *= dims[q]
    return outcomes, count


def embed_qubit_operator(operator: Matrix, operand_dims: Sequence[int]) -> np.ndarray:
    """A ``2^k`` operator on ``k`` operands as the operator on their full levels.

    It acts as given on the states where every operand is in level 0 or 1 and as the identity on the rest:
    a gate known on the qubit levels says nothing about a leaked state.
    """
    operand_dims = tuple(operand_dims)
    matrix = np.asarray(operator, dtype=complex)
    if matrix.shape != (1 << len(operand_dims),) * 2:
        raise ValueError("operator dimension does not match operands")
    if all(level == 2 for level in operand_dims):
        return matrix
    digits = _digits(operand_dims)
    inside = np.flatnonzero(np.all(digits < 2, axis=1))          # in the order of the qubit basis
    embedded = np.eye(len(digits), dtype=complex)
    embedded[np.ix_(inside, inside)] = matrix
    return embedded


def apply_operand_operator(state: np.ndarray, operator: Matrix, qubits: tuple[int, ...],
                           dims: Sequence[int] | None = None) -> np.ndarray:
    """``rho -> U rho U^+`` with ``U`` on the full levels of ``qubits`` (see ``embed_qubit_operator``)."""
    if state.ndim != 2 or state.shape[0] != state.shape[1]:
        raise ValueError("invalid operator register or operands")
    try:
        dims = register_dims(state.shape[0], dims)
    except ValueError:
        raise ValueError("invalid operator register or operands") from None
    n = len(dims)
    if len(set(qubits)) != len(qubits) or not qubits or any(q < 0 or q >= n for q in qubits):
        raise ValueError("invalid operator register or operands")
    matrix = np.asarray(operator, dtype=complex)
    if matrix.shape != (math.prod(dims[q] for q in qubits),) * 2:
        raise ValueError("operator dimension does not match operands")
    tensor = state.reshape(dims + dims)
    for axes, transform in ((qubits, matrix), (tuple(n + q for q in qubits), matrix.conj())):
        order = tuple(axes) + tuple(i for i in range(2 * n) if i not in axes)
        ordered = tensor.transpose(order)
        updated = transform @ ordered.reshape(len(matrix), -1)
        tensor = updated.reshape(ordered.shape).transpose(np.argsort(order))
    return tensor.reshape(state.shape)


def apply_local_replacement(state: np.ndarray, qubit: int, success: float, populations: tuple[float, ...],
                            dims: Sequence[int] | None = None) -> np.ndarray:
    """Exact E_q(rho)=(1-s)rho+s(tau_q tensor Tr_q rho), without embedded Kraus matrices.

    ``populations`` are the diagonal of ``tau_q``, one per level of the subsystem.
    """
    if success == 0.0:
        return state
    dims = register_dims(state.shape[0], dims)
    n = len(dims)
    if len(populations) != dims[qubit]:
        raise ValueError("replacement populations must give one value per level")
    tensor = state.reshape(dims + dims)
    reduced = np.trace(tensor, axis1=qubit, axis2=n + qubit)
    replaced = np.zeros_like(tensor)
    for level, population in enumerate(populations):
        indices = [slice(None)] * (2 * n)
        indices[qubit] = indices[n + qubit] = level
        replaced[tuple(indices)] = population * reduced
    return (1.0 - success) * state + success * replaced.reshape(state.shape)


_FLIP = np.array([[0, 1], [1, 0]], dtype=complex)


def apply_reset_with_feedback_kick(state: np.ndarray, qubit: int, success: float,
                                   populations: tuple[float, float], kick: Matrix,
                                   dims: Sequence[int] | None = None) -> np.ndarray:
    """One measurement-based reset attempt with a rotation ``kick`` between its measurement and its correction.

    IBM's reset measures the qubit and then applies X on outcome 1. A rotation K that reaches the qubit
    after the measurement acts on the collapsed branch and is then flipped with it:

        E_K(rho) = (1 - s) K rho K^+ + s F(sum_b X^b K P_b rho P_b K^+ X^b),  F(x) = p0 x + p1 X x X.

    The failed branch only feels the rotation. With K = I this is ``apply_local_replacement`` exactly.
    Linear in ``state``, so it applies to any operator (e.g. a term of an operator-Schmidt sum).

    On a subsystem with more than two levels the measurement tells every level apart and a level above 0
    is read as 1; X exchanges levels 0 and 1 only, so a leaked level passes through the reset unchanged
    (``populations`` are still the two values of ``F``, and ``kick`` is given on the qubit or on all levels).
    """
    dims = register_dims(state.shape[0], dims)
    levels = dims[qubit]
    if len(populations) != 2:
        raise ValueError("the reset's correction mixes the two qubit levels")
    operands = (qubit,)
    kick = np.asarray(kick, dtype=complex)
    if kick.shape == (2, 2):
        kick = embed_qubit_operator(kick, (levels,))
    flip = embed_qubit_operator(_FLIP, (levels,))
    failed = apply_operand_operator(state, kick, operands, dims)
    corrected = np.zeros_like(failed)
    for outcome in range(levels):
        projector = np.zeros((levels, levels), dtype=complex)
        projector[outcome, outcome] = 1.0
        branch = apply_operand_operator(apply_operand_operator(state, projector, operands, dims), kick, operands,
                                        dims)
        corrected = corrected + (apply_operand_operator(branch, flip, operands, dims) if outcome else branch)
    prepared = populations[0] * corrected + populations[1] * apply_operand_operator(corrected, flip, operands, dims)
    return (1.0 - success) * failed + success * prepared


def apply_local_bloch_map(state: np.ndarray, qubit: int, bloch: np.ndarray,
                          dims: Sequence[int] | None = None) -> np.ndarray:
    """A unital one-qubit channel given by its 3x3 Bloch matrix R (r -> R r), on ``qubit``.

    The identity component is kept and sigma_j -> sum_i R[i, j] sigma_i. Linear in ``state``, so it applies to any
    operator (e.g. a term of an operator-Schmidt sum). A Bloch matrix describes two levels only, so the
    subsystem must have two.
    """
    dims = register_dims(state.shape[0], dims)
    n = len(dims)
    if dims[qubit] != 2:
        raise ValueError("a Bloch map acts on a two-level subsystem")
    tensor = np.asarray(state, dtype=complex).reshape(dims + dims)
    axes = (qubit, n + qubit)
    order = axes + tuple(i for i in range(2 * n) if i not in axes)
    blocks = tensor.transpose(order).reshape(2, 2, -1)               # [row, column, rest]
    paulis = (np.array([[0, 1], [1, 0]], dtype=complex), np.array([[0, -1j], [1j, 0]], dtype=complex),
              np.array([[1, 0], [0, -1]], dtype=complex))
    identity = np.eye(2, dtype=complex)
    coefficient_i = 0.5 * np.einsum("ab,bar->r", identity, blocks)
    coefficients = 0.5 * np.einsum("kab,bar->kr", np.array(paulis), blocks)
    mapped = np.asarray(bloch, dtype=float) @ coefficients
    rebuilt = (np.einsum("ab,r->abr", identity, coefficient_i)
               + np.einsum("kab,kr->abr", np.array(paulis), mapped))
    restored = rebuilt.reshape(tensor.transpose(order).shape).transpose(np.argsort(order))
    return restored.reshape(state.shape)


def driven_dephasing_bloch(rotation: tuple[float, float, float], duration_us: float,
                           dephasing_per_us: float) -> np.ndarray:
    """Bloch matrix of a constant drive (net rotation vector ``rotation`` over ``duration_us``) under dephasing.

    dr/dt = Omega x r - gamma (r_x, r_y, 0), Omega = rotation / duration, solved exactly. A zero duration is the
    plain rotation; without a drive it is the coherence decay exp(-gamma t).
    """
    from scipy.linalg import expm
    omega = np.asarray(rotation, dtype=float)
    if duration_us <= 0.0:
        angle = float(np.linalg.norm(omega))
        if angle == 0.0:
            return np.eye(3)
        axis = omega / angle
        cross = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
        return np.eye(3) + math.sin(angle) * cross + (1 - math.cos(angle)) * cross @ cross
    rate = omega / duration_us
    generator = np.array([[-dephasing_per_us, -rate[2], rate[1]],
                          [rate[2], -dephasing_per_us, -rate[0]],
                          [-rate[1], rate[0], 0.0]])
    return expm(generator * duration_us)


def _mix_qubit_levels(state: np.ndarray, qubit: int, dims: tuple[int, ...]) -> np.ndarray:
    """Replace the qubit levels of a subsystem by their maximally mixed state, keeping what has leaked.

    The subsystem is first found, without reading which, in its qubit levels or above them; the qubit part
    becomes ``I/2`` with the same weight and the leaked part is left as it is.
    """
    n = len(dims)
    tensor = state.reshape(dims + dims)
    mixed = np.zeros_like(tensor)
    index = [slice(None)] * (2 * n)

    def block(row, column):
        index[qubit], index[n + qubit] = row, column
        return tuple(index)

    half = 0.5 * (tensor[block(0, 0)] + tensor[block(1, 1)])
    mixed[block(0, 0)] = half
    mixed[block(1, 1)] = half
    for row in range(2, dims[qubit]):
        for column in range(2, dims[qubit]):
            mixed[block(row, column)] = tensor[block(row, column)]
    return mixed.reshape(state.shape)


def apply_local_depolarizing_replacement(
    state: np.ndarray, qubits: tuple[int, ...], probability: float, dims: Sequence[int] | None = None,
) -> np.ndarray:
    """Mix an operand subsystem with its maximally mixed state.

    ``probability`` is the explicit simulator channel weight, not an inferred
    conversion from an RB error metric. This is a local CPTP map on the named
    qubits and retains correlations with the rest of the register. On a subsystem with more than two
    levels the mixing is of its qubit levels; a gate's calibrated error says nothing about a leaked state.
    """
    operands = tuple(qubits)
    dims = register_dims(state.shape[0], dims)
    if (not operands or len(set(operands)) != len(operands)
            or any(type(q) is not int or q < 0 or q >= len(dims) for q in operands)
            or not np.isfinite(probability) or not 0.0 <= probability <= 1.0):
        raise ValueError("invalid local depolarizing replacement request")
    if probability == 0.0:
        return state
    mixed = state.copy()
    for qubit in operands:
        mixed = (apply_local_replacement(mixed, qubit, 1.0, (0.5, 0.5)) if all(level == 2 for level in dims)
                 else _mix_qubit_levels(mixed, qubit, dims))
    return (1.0 - probability) * state + probability * mixed


def nonselective_measurement(state: np.ndarray, qubits: tuple[int, ...],
                             dims: Sequence[int] | None = None) -> np.ndarray:
    """Measure ``qubits`` in their level basis and forget the result."""
    labels, _ = _outcomes(register_dims(state.shape[0], dims), qubits)
    return np.where(labels[:, None] == labels[None, :], state, 0.0j)


def sample_measurement(state: np.ndarray, qubits: tuple[int, ...], generator: random.Random,
                       dims: Sequence[int] | None = None) -> tuple[np.ndarray, tuple[int, ...]]:
    """Draw the level of each of ``qubits`` and collapse onto it; the levels are returned in operand order.

    For two-level qubits the levels are the bits. What a readout assigns to a level above 1 is the caller's
    rule, not this function's.
    """
    dims = register_dims(state.shape[0], dims)
    probabilities = np.diag(state).real
    if np.any(~np.isfinite(probabilities)) or np.min(probabilities) < -1e-10 or abs(float(probabilities.sum()) - 1.0) > 1e-10:
        raise ValueError("invalid measurement probability distribution")
    outcomes, count = _outcomes(dims, qubits)
    marginal = np.bincount(outcomes, weights=np.maximum(probabilities, 0.0), minlength=count)
    draw = generator.random() * float(marginal.sum())
    outcome = min(int(np.searchsorted(np.cumsum(marginal), draw, side="right")), len(marginal) - 1)
    probability = marginal[outcome]
    if probability <= 0:
        raise ValueError("sampled a zero-probability measurement branch")
    keep = outcomes == outcome
    collapsed = np.where(keep[:, None] & keep[None, :], state / probability, 0.0j)
    levels = []
    for q in reversed(qubits):
        outcome, level = divmod(outcome, dims[q])
        levels.append(level)
    return collapsed, tuple(reversed(levels))


def freeze_register(state: np.ndarray) -> Matrix:
    return tuple(tuple(row) for row in state.tolist())
