"""Tensor-local operations preserving the full bounded register density matrix.

Qubit zero is the most-significant factor, as in core.gates. These kernels do
not trace away unaddressed qubits or approximate their correlations.
"""

from __future__ import annotations

import math
import random
import numpy as np

from core.gates import Matrix


def apply_operand_operator(state: np.ndarray, operator: Matrix, qubits: tuple[int, ...]) -> np.ndarray:
    n = state.shape[0].bit_length() - 1
    if state.shape != (1 << n, 1 << n) or len(set(qubits)) != len(qubits) or not qubits or any(q < 0 or q >= n for q in qubits):
        raise ValueError("invalid operator register or operands")
    matrix = np.asarray(operator, dtype=complex)
    if matrix.shape != (1 << len(qubits),) * 2:
        raise ValueError("operator dimension does not match operands")
    tensor = state.reshape((2,) * (2 * n))
    for axes, transform in ((qubits, matrix), (tuple(n + q for q in qubits), matrix.conj())):
        order = tuple(axes) + tuple(i for i in range(2 * n) if i not in axes)
        ordered = tensor.transpose(order)
        updated = transform @ ordered.reshape(len(matrix), -1)
        tensor = updated.reshape(ordered.shape).transpose(np.argsort(order))
    return tensor.reshape(state.shape)


def apply_local_replacement(state: np.ndarray, qubit: int, success: float, populations: tuple[float, float]) -> np.ndarray:
    """Exact E_q(rho)=(1-s)rho+s(tau_q tensor Tr_q rho), without embedded Kraus matrices."""
    if success == 0.0:
        return state
    n = state.shape[0].bit_length() - 1
    tensor = state.reshape((2,) * (2 * n))
    reduced = np.trace(tensor, axis1=qubit, axis2=n + qubit)
    replaced = np.zeros_like(tensor)
    for level, population in enumerate(populations):
        indices = [slice(None)] * (2 * n)
        indices[qubit] = indices[n + qubit] = level
        replaced[tuple(indices)] = population * reduced
    return (1.0 - success) * state + success * replaced.reshape(state.shape)


_PROJECTORS = (np.array([[1, 0], [0, 0]], dtype=complex), np.array([[0, 0], [0, 1]], dtype=complex))
_FLIP = np.array([[0, 1], [1, 0]], dtype=complex)


def apply_reset_with_feedback_kick(state: np.ndarray, qubit: int, success: float,
                                   populations: tuple[float, float], kick: Matrix) -> np.ndarray:
    """One measurement-based reset attempt with a rotation ``kick`` between its measurement and its correction.

    IBM's reset measures the qubit and then applies X on outcome 1. A rotation K that reaches the qubit
    after the measurement acts on the collapsed branch and is then flipped with it:

        E_K(rho) = (1 - s) K rho K^+ + s F(sum_b X^b K P_b rho P_b K^+ X^b),  F(x) = p0 x + p1 X x X.

    The failed branch only feels the rotation. With K = I this is ``apply_local_replacement`` exactly.
    Linear in ``state``, so it applies to any operator (e.g. a term of an operator-Schmidt sum).
    """
    operands = (qubit,)
    failed = apply_operand_operator(state, kick, operands)
    corrected = np.zeros_like(failed)
    for outcome, projector in enumerate(_PROJECTORS):
        branch = apply_operand_operator(apply_operand_operator(state, projector, operands), kick, operands)
        corrected = corrected + (apply_operand_operator(branch, _FLIP, operands) if outcome else branch)
    prepared = populations[0] * corrected + populations[1] * apply_operand_operator(corrected, _FLIP, operands)
    return (1.0 - success) * failed + success * prepared


def apply_local_bloch_map(state: np.ndarray, qubit: int, bloch: np.ndarray) -> np.ndarray:
    """A unital one-qubit channel given by its 3x3 Bloch matrix R (r -> R r), on ``qubit``.

    The identity component is kept and sigma_j -> sum_i R[i, j] sigma_i. Linear in ``state``, so it applies to any
    operator (e.g. a term of an operator-Schmidt sum).
    """
    n = state.shape[0].bit_length() - 1
    tensor = np.asarray(state, dtype=complex).reshape((2,) * (2 * n))
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


def apply_local_depolarizing_replacement(
    state: np.ndarray, qubits: tuple[int, ...], probability: float,
) -> np.ndarray:
    """Mix an operand subsystem with its maximally mixed state.

    ``probability`` is the explicit simulator channel weight, not an inferred
    conversion from an RB error metric. This is a local CPTP map on the named
    qubits and retains correlations with the rest of the register.
    """
    operands = tuple(qubits)
    if (not operands or len(set(operands)) != len(operands)
            or any(type(q) is not int or q < 0 or q >= state.shape[0].bit_length() - 1 for q in operands)
            or not np.isfinite(probability) or not 0.0 <= probability <= 1.0):
        raise ValueError("invalid local depolarizing replacement request")
    if probability == 0.0:
        return state
    mixed = state.copy()
    for qubit in operands:
        mixed = apply_local_replacement(mixed, qubit, 1.0, (0.5, 0.5))
    return (1.0 - probability) * state + probability * mixed


def nonselective_measurement(state: np.ndarray, qubits: tuple[int, ...]) -> np.ndarray:
    n = state.shape[0].bit_length() - 1
    indices = np.arange(len(state))
    mask = sum(1 << (n - 1 - q) for q in qubits)
    labels = indices & mask
    return np.where(labels[:, None] == labels[None, :], state, 0.0j)


def sample_measurement(state: np.ndarray, qubits: tuple[int, ...], generator: random.Random) -> tuple[np.ndarray, tuple[int, ...]]:
    n = state.shape[0].bit_length() - 1
    probabilities = np.diag(state).real
    if np.any(~np.isfinite(probabilities)) or np.min(probabilities) < -1e-10 or abs(float(probabilities.sum()) - 1.0) > 1e-10:
        raise ValueError("invalid measurement probability distribution")
    labels = np.arange(len(state))
    outcomes = np.zeros(len(state), dtype=int)
    for q in qubits:
        outcomes = (outcomes << 1) | ((labels >> (n - 1 - q)) & 1)
    marginal = np.bincount(outcomes, weights=np.maximum(probabilities, 0.0), minlength=1 << len(qubits))
    draw = generator.random() * float(marginal.sum())
    outcome = min(int(np.searchsorted(np.cumsum(marginal), draw, side="right")), len(marginal) - 1)
    probability = marginal[outcome]
    if probability <= 0:
        raise ValueError("sampled a zero-probability measurement branch")
    keep = outcomes == outcome
    collapsed = np.where(keep[:, None] & keep[None, :], state / probability, 0.0j)
    bits = tuple(int(bit) for bit in format(outcome, f"0{len(qubits)}b"))
    return collapsed, bits


def freeze_register(state: np.ndarray) -> Matrix:
    return tuple(tuple(row) for row in state.tolist())
