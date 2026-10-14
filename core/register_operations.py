"""Tensor-local operations preserving the full bounded register density matrix.

Qubit zero is the most-significant factor, as in core.gates. These kernels do
not trace away unaddressed qubits or approximate their correlations.
"""

from __future__ import annotations

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
