"""Density-matrix metrics offered by the Gate-aware toolbox.

Every function takes a dense density matrix in the simulator's convention:
qubit 0 is the most significant bit of the basis index. Entropies are in bits.

These are the production implementations (NumPy only). QuTiP's
``entropy_vn``, ``concurrence``, ``negativity``, ``entropy_mutual`` and
``tracedist`` are the independent references they are tested against.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

PAULI_MATRICES: dict[str, np.ndarray] = {
    "I": np.eye(2, dtype=np.complex128),
    "X": np.array([[0.0, 1.0], [1.0, 0.0]], dtype=np.complex128),
    "Y": np.array([[0.0, -1.0j], [1.0j, 0.0]], dtype=np.complex128),
    "Z": np.array([[1.0, 0.0], [0.0, -1.0]], dtype=np.complex128),
}

# Eigenvalues below this are treated as zero when taking logarithms. It sits
# well above the round-off of a normalized dense density matrix and well
# below any population the UI could display.
_EIGENVALUE_FLOOR = 1e-14


def as_density_array(matrix: object, n_qubits: int) -> np.ndarray:
    """Return a square complex array of dimension 2**n_qubits."""

    array = np.asarray(matrix, dtype=np.complex128)
    dimension = 2 ** int(n_qubits)
    if array.shape != (dimension, dimension):
        raise ValueError(
            f"density matrix shape {array.shape} does not match {n_qubits} qubits"
        )
    return array


def validate_qubit_subset(
    qubits: Sequence[int],
    n_qubits: int,
    *,
    name: str,
    allow_all: bool = True,
) -> tuple[int, ...]:
    """Return the subset sorted, rejecting empty, duplicate or out-of-range input."""

    subset = tuple(int(qubit) for qubit in qubits)
    if not subset:
        raise ValueError(f"{name} must contain at least one qubit")
    if len(set(subset)) != len(subset):
        raise ValueError(f"{name} must not repeat a qubit")
    if any(qubit < 0 or qubit >= n_qubits for qubit in subset):
        raise ValueError(f"{name} contains a qubit outside 0..{n_qubits - 1}")
    if not allow_all and len(subset) == n_qubits:
        raise ValueError(f"{name} must leave at least one qubit outside it")
    return tuple(sorted(subset))


def partial_trace(rho: np.ndarray, n_qubits: int, keep: Sequence[int]) -> np.ndarray:
    """Trace out every qubit not in ``keep``; the kept order is ascending."""

    kept = validate_qubit_subset(keep, n_qubits, name="keep")
    if len(kept) == n_qubits:
        return np.array(rho, dtype=np.complex128, copy=True)
    tensor = np.asarray(rho, dtype=np.complex128).reshape([2] * (2 * n_qubits))
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    row = [letters[qubit] for qubit in range(n_qubits)]
    column = [
        letters[n_qubits + qubit] if qubit in kept else letters[qubit]
        for qubit in range(n_qubits)
    ]
    output = [row[qubit] for qubit in kept] + [column[qubit] for qubit in kept]
    reduced = np.einsum(f"{''.join(row)}{''.join(column)}->{''.join(output)}", tensor)
    size = 2 ** len(kept)
    return reduced.reshape(size, size)


def _hermitian_eigenvalues(matrix: np.ndarray) -> np.ndarray:
    hermitian = 0.5 * (matrix + matrix.conj().T)
    return np.linalg.eigvalsh(hermitian)


def von_neumann_entropy(rho: np.ndarray) -> float:
    """S(rho) = -Tr rho log2 rho, in bits."""

    eigenvalues = _hermitian_eigenvalues(rho)
    positive = eigenvalues[eigenvalues > _EIGENVALUE_FLOOR]
    return float(max(0.0, -np.sum(positive * np.log2(positive))))


def concurrence(rho: np.ndarray) -> float:
    """Wootters concurrence of a two-qubit density matrix.

    Uses the Hermitian form: the lambda_i are the square roots of the
    eigenvalues of sqrt(rho) rho_tilde sqrt(rho), which equal those of
    rho rho_tilde but avoid a non-Hermitian eigenproblem.
    """

    array = np.asarray(rho, dtype=np.complex128)
    if array.shape != (4, 4):
        raise ValueError("concurrence needs a two-qubit (4x4) density matrix")
    eigenvalues, eigenvectors = np.linalg.eigh(0.5 * (array + array.conj().T))
    sqrt_rho = (eigenvectors * np.sqrt(np.clip(eigenvalues, 0.0, None))) @ eigenvectors.conj().T
    spin_flip = np.kron(PAULI_MATRICES["Y"], PAULI_MATRICES["Y"])
    rho_tilde = spin_flip @ array.conj() @ spin_flip
    product = sqrt_rho @ rho_tilde @ sqrt_rho
    lambdas = np.sqrt(np.clip(_hermitian_eigenvalues(product), 0.0, None))[::-1]
    return float(max(0.0, lambdas[0] - lambdas[1] - lambdas[2] - lambdas[3]))


def partial_transpose(rho: np.ndarray, n_qubits: int, subsystem: Sequence[int]) -> np.ndarray:
    """Transpose the row/column indices of the qubits in ``subsystem``."""

    transposed = validate_qubit_subset(subsystem, n_qubits, name="subsystem")
    tensor = np.asarray(rho, dtype=np.complex128).reshape([2] * (2 * n_qubits))
    axes = list(range(2 * n_qubits))
    for qubit in transposed:
        axes[qubit], axes[n_qubits + qubit] = axes[n_qubits + qubit], axes[qubit]
    dimension = 2 ** n_qubits
    return tensor.transpose(axes).reshape(dimension, dimension)


def negativity(rho: np.ndarray, n_qubits: int, subsystem: Sequence[int]) -> float:
    """N = (||rho^{T_A}||_1 - Tr rho) / 2 for the bipartition A | rest."""

    validate_qubit_subset(subsystem, n_qubits, name="subsystem", allow_all=False)
    eigenvalues = _hermitian_eigenvalues(partial_transpose(rho, n_qubits, subsystem))
    trace = float(np.real(np.trace(rho)))
    return float(max(0.0, 0.5 * (np.sum(np.abs(eigenvalues)) - trace)))


def mutual_information(
    rho: np.ndarray,
    n_qubits: int,
    subsystem_a: Sequence[int],
    subsystem_b: Sequence[int],
) -> float:
    """I(A:B) = S(A) + S(B) - S(AB), in bits; qubits outside A and B are traced out."""

    a = validate_qubit_subset(subsystem_a, n_qubits, name="subsystem_a")
    b = validate_qubit_subset(subsystem_b, n_qubits, name="subsystem_b")
    if set(a) & set(b):
        raise ValueError("subsystem_a and subsystem_b must be disjoint")
    joint = tuple(sorted(a + b))
    value = (
        von_neumann_entropy(partial_trace(rho, n_qubits, a))
        + von_neumann_entropy(partial_trace(rho, n_qubits, b))
        - von_neumann_entropy(partial_trace(rho, n_qubits, joint))
    )
    return float(max(0.0, value))


def trace_distance(rho: np.ndarray, sigma: np.ndarray) -> float:
    """D(rho, sigma) = ||rho - sigma||_1 / 2."""

    difference = np.asarray(rho, dtype=np.complex128) - np.asarray(sigma, dtype=np.complex128)
    return float(0.5 * np.sum(np.abs(_hermitian_eigenvalues(difference))))


def validate_pauli_string(pauli: str, n_qubits: int) -> str:
    """Normalize a Pauli string whose first character acts on qubit 0."""

    normalized = str(pauli).strip().upper()
    if len(normalized) != n_qubits:
        raise ValueError(
            f"Pauli string {pauli!r} must have exactly {n_qubits} characters"
        )
    if any(character not in PAULI_MATRICES for character in normalized):
        raise ValueError(f"Pauli string {pauli!r} may only use I, X, Y, Z")
    return normalized


def pauli_weight(pauli: str) -> int:
    return sum(1 for character in pauli if character != "I")


def pauli_operator(pauli: str) -> np.ndarray:
    operator = np.ones((1, 1), dtype=np.complex128)
    for character in pauli:
        operator = np.kron(operator, PAULI_MATRICES[character])
    return operator


def pauli_expectation(rho: np.ndarray, pauli: str) -> float:
    """<P> = Tr(rho P). Pauli strings are Hermitian, so the value is real."""

    # Tr(rho P) = sum_ij rho_ij P_ji, which skips the O(d^3) product.
    operator = pauli_operator(pauli)
    return float(np.real(np.sum(np.asarray(rho, dtype=np.complex128) * operator.T)))
