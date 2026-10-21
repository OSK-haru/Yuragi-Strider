"""Per-point toolbox metrics for the Pulse-level state explorer.

The Pulse explorer already shows purity, leakage, the reference overlap, the
populations and the raw density matrix. The toolbox adds metrics that are not
functions of those, evaluated on every density-matrix point of a finished run
so they can be read at the explorer's time cursor.

A Pulse state lives on ``transmon_count`` transmons with ``local_levels``
levels each (2 or 3), with transmon 0 as the most significant tensor factor.
Subsets are therefore subsets of *transmons*, and the entanglement metrics act
on the full local spaces, leakage levels included.

Metrics that would only restate a displayed value are rejected, the same rule
the Gate-aware toolbox follows:

* A one-transmon two-level state has a Bloch-vector entropy that is fixed by
  the purity already shown.
* A Pauli string that has no X or Y factor and acts on a single transmon is a
  linear function of the populations already shown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import numpy as np

# MIN/MAX_TRANSMON_COUNT come from the network model that defines what can be simulated.
from core.pulse_transmon_network import MAX_TRANSMON_COUNT, MIN_TRANSMON_COUNT
from core.state_metrics import (
    PAULI_MATRICES,
    concurrence,
    von_neumann_entropy,
)

PULSE_TOOLBOX_SCHEMA_VERSION = "pulse-toolbox-v1"
SUPPORTED_LOCAL_LEVELS = (2, 3)
MAX_PULSE_TOOLBOX_OBSERVABLES = 8


@dataclass(frozen=True)
class PulseToolboxSpec:
    entropy_transmons: tuple[int, ...] | None = None
    concurrence_transmons: tuple[int, int] | None = None
    negativity_subsystem: tuple[int, ...] | None = None
    mutual_information_a: tuple[int, ...] | None = None
    mutual_information_b: tuple[int, ...] | None = None
    observables: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return (
            self.entropy_transmons is None
            and self.concurrence_transmons is None
            and self.negativity_subsystem is None
            and self.mutual_information_a is None
            and not self.observables
        )


def _validate_geometry(local_levels: int, transmon_count: int) -> None:
    if local_levels not in SUPPORTED_LOCAL_LEVELS:
        raise ValueError(f"local_levels must be one of {SUPPORTED_LOCAL_LEVELS}")
    if not MIN_TRANSMON_COUNT <= transmon_count <= MAX_TRANSMON_COUNT:
        raise ValueError(
            f"transmon_count must be {MIN_TRANSMON_COUNT}..{MAX_TRANSMON_COUNT}"
        )


def _validate_subset(
    transmons: Sequence[int],
    transmon_count: int,
    *,
    name: str,
    allow_all: bool = True,
) -> tuple[int, ...]:
    subset = tuple(int(transmon) for transmon in transmons)
    if not subset:
        raise ValueError(f"{name} must contain at least one transmon")
    if len(set(subset)) != len(subset):
        raise ValueError(f"{name} must not repeat a transmon")
    if any(transmon < 0 or transmon >= transmon_count for transmon in subset):
        raise ValueError(f"{name} contains a transmon outside 0..{transmon_count - 1}")
    if not allow_all and len(subset) == transmon_count:
        raise ValueError(f"{name} must leave at least one transmon outside it")
    return tuple(sorted(subset))


def validate_pulse_observable(pauli: str, transmon_count: int) -> str:
    """Normalize a Pauli string whose first character acts on transmon 0."""

    normalized = str(pauli).strip().upper()
    if len(normalized) != transmon_count:
        raise ValueError(
            f"Pauli string {pauli!r} must have exactly {transmon_count} characters"
        )
    if any(character not in PAULI_MATRICES for character in normalized):
        raise ValueError(f"Pauli string {pauli!r} may only use I, X, Y, Z")
    weight = sum(1 for character in normalized if character != "I")
    has_coherence = any(character in "XY" for character in normalized)
    if weight == 0 or (not has_coherence and weight < 2):
        raise ValueError(
            f"observable {normalized} is a function of the populations already "
            "shown; use a string with an X or Y factor, or one acting on two "
            "or more transmons"
        )
    return normalized


def validate_pulse_toolbox_spec(
    spec: PulseToolboxSpec,
    local_levels: int,
    transmon_count: int,
) -> PulseToolboxSpec:
    """Return the spec normalized, or raise ValueError naming the bad field."""

    _validate_geometry(local_levels, transmon_count)

    entropy_transmons = None
    if spec.entropy_transmons is not None:
        entropy_transmons = _validate_subset(
            spec.entropy_transmons, transmon_count, name="entropy_transmons"
        )
        if local_levels == 2 and transmon_count == 1:
            raise ValueError(
                "a single two-level transmon has an entropy fixed by the purity "
                "already shown"
            )

    concurrence_transmons = None
    if spec.concurrence_transmons is not None:
        if local_levels != 2:
            raise ValueError(
                "concurrence is defined for two-level transmons; "
                "use negativity for three-level transmons"
            )
        pair = _validate_subset(
            spec.concurrence_transmons, transmon_count, name="concurrence_transmons"
        )
        if len(pair) != 2:
            raise ValueError("concurrence_transmons must name exactly two transmons")
        concurrence_transmons = (pair[0], pair[1])

    negativity_subsystem = None
    if spec.negativity_subsystem is not None:
        negativity_subsystem = _validate_subset(
            spec.negativity_subsystem,
            transmon_count,
            name="negativity_subsystem",
            allow_all=False,
        )

    mutual_a = mutual_b = None
    if (spec.mutual_information_a is None) != (spec.mutual_information_b is None):
        raise ValueError(
            "mutual_information_a and mutual_information_b must be given together"
        )
    if spec.mutual_information_a is not None and spec.mutual_information_b is not None:
        mutual_a = _validate_subset(
            spec.mutual_information_a, transmon_count, name="mutual_information_a"
        )
        mutual_b = _validate_subset(
            spec.mutual_information_b, transmon_count, name="mutual_information_b"
        )
        if set(mutual_a) & set(mutual_b):
            raise ValueError(
                "mutual_information_a and mutual_information_b must be disjoint"
            )

    if len(spec.observables) > MAX_PULSE_TOOLBOX_OBSERVABLES:
        raise ValueError(
            f"at most {MAX_PULSE_TOOLBOX_OBSERVABLES} observables can be requested"
        )
    observables: list[str] = []
    for raw in spec.observables:
        pauli = validate_pulse_observable(raw, transmon_count)
        if pauli not in observables:
            observables.append(pauli)

    return PulseToolboxSpec(
        entropy_transmons=entropy_transmons,
        concurrence_transmons=concurrence_transmons,
        negativity_subsystem=negativity_subsystem,
        mutual_information_a=mutual_a,
        mutual_information_b=mutual_b,
        observables=tuple(observables),
    )


def as_pulse_density_array(
    matrix: object,
    local_levels: int,
    transmon_count: int,
) -> np.ndarray:
    """Return a square complex array of dimension ``local_levels**transmon_count``."""

    array = np.asarray(matrix, dtype=np.complex128)
    dimension = local_levels**transmon_count
    if array.shape != (dimension, dimension):
        raise ValueError(
            f"density matrix shape {array.shape} does not match "
            f"{transmon_count} transmon(s) with {local_levels} levels"
        )
    if not np.all(np.isfinite(array)):
        raise ValueError("density matrix contains a non-finite value")
    return array


def density_from_upper_triangle(
    upper_real: Sequence[float],
    upper_imag: Sequence[float],
    dimension: int,
) -> np.ndarray:
    """Rebuild a Hermitian matrix from its row-major upper triangle.

    The lower triangle is the conjugate of the upper one and the diagonal is
    real, so the imaginary part of a diagonal entry is ignored.
    """

    size = dimension * (dimension + 1) // 2
    if len(upper_real) != size or len(upper_imag) != size:
        raise ValueError(
            f"an upper triangle of a {dimension}x{dimension} matrix has {size} entries"
        )
    rows, columns = np.triu_indices(dimension)
    matrix = np.zeros((dimension, dimension), dtype=np.complex128)
    matrix[rows, columns] = np.asarray(upper_real, dtype=float) + 1j * np.asarray(
        upper_imag, dtype=float
    )
    lower = np.tril_indices(dimension, -1)
    matrix[lower] = matrix.conj().T[lower]
    matrix[np.diag_indices(dimension)] = matrix.diagonal().real
    return matrix


_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def pulse_partial_trace(
    rho: np.ndarray,
    local_levels: int,
    transmon_count: int,
    keep: Sequence[int],
) -> np.ndarray:
    """Trace out every transmon not in ``keep``; the kept order is ascending."""

    kept = _validate_subset(keep, transmon_count, name="keep")
    if len(kept) == transmon_count:
        return np.array(rho, dtype=np.complex128, copy=True)
    tensor = np.asarray(rho, dtype=np.complex128).reshape(
        [local_levels] * (2 * transmon_count)
    )
    row = [_LETTERS[index] for index in range(transmon_count)]
    column = [
        _LETTERS[transmon_count + index] if index in kept else _LETTERS[index]
        for index in range(transmon_count)
    ]
    output = [row[index] for index in kept] + [column[index] for index in kept]
    reduced = np.einsum(f"{''.join(row)}{''.join(column)}->{''.join(output)}", tensor)
    size = local_levels ** len(kept)
    return reduced.reshape(size, size)


def pulse_negativity(
    rho: np.ndarray,
    local_levels: int,
    transmon_count: int,
    subsystem: Sequence[int],
) -> float:
    """N = (||rho^{T_A}||_1 - Tr rho) / 2 for the bipartition A | rest."""

    transposed = _validate_subset(
        subsystem, transmon_count, name="subsystem", allow_all=False
    )
    tensor = np.asarray(rho, dtype=np.complex128).reshape(
        [local_levels] * (2 * transmon_count)
    )
    axes = list(range(2 * transmon_count))
    for transmon in transposed:
        axes[transmon], axes[transmon_count + transmon] = (
            axes[transmon_count + transmon],
            axes[transmon],
        )
    dimension = local_levels**transmon_count
    partial = tensor.transpose(axes).reshape(dimension, dimension)
    eigenvalues = np.linalg.eigvalsh(0.5 * (partial + partial.conj().T))
    trace = float(np.real(np.trace(rho)))
    return float(max(0.0, 0.5 * (np.sum(np.abs(eigenvalues)) - trace)))


def pulse_mutual_information(
    rho: np.ndarray,
    local_levels: int,
    transmon_count: int,
    subsystem_a: Sequence[int],
    subsystem_b: Sequence[int],
) -> float:
    """I(A:B) = S(A) + S(B) - S(AB) in bits; transmons outside A and B are traced out."""

    a = _validate_subset(subsystem_a, transmon_count, name="subsystem_a")
    b = _validate_subset(subsystem_b, transmon_count, name="subsystem_b")
    if set(a) & set(b):
        raise ValueError("subsystem_a and subsystem_b must be disjoint")
    joint = tuple(sorted(a + b))
    value = (
        von_neumann_entropy(pulse_partial_trace(rho, local_levels, transmon_count, a))
        + von_neumann_entropy(pulse_partial_trace(rho, local_levels, transmon_count, b))
        - von_neumann_entropy(
            pulse_partial_trace(rho, local_levels, transmon_count, joint)
        )
    )
    return float(max(0.0, value))


def pulse_pauli_operator(pauli: str, local_levels: int) -> np.ndarray:
    """Embed a Pauli string into the full space.

    X, Y and Z act on the computational levels {0, 1} of their transmon and are
    zero on level 2, so a population in the leakage level contributes nothing to
    them. ``I`` is the full identity on that transmon.
    """

    operator = np.ones((1, 1), dtype=np.complex128)
    for character in pauli:
        local = np.zeros((local_levels, local_levels), dtype=np.complex128)
        if character == "I":
            local = np.eye(local_levels, dtype=np.complex128)
        else:
            local[:2, :2] = PAULI_MATRICES[character]
        operator = np.kron(operator, local)
    return operator


def pulse_pauli_expectation(rho: np.ndarray, operator: np.ndarray) -> float:
    """<P> = Tr(rho P). The embedded Pauli string is Hermitian, so the value is real."""

    return float(np.real(np.sum(np.asarray(rho, dtype=np.complex128) * operator.T)))


def compute_pulse_toolbox_metrics(
    spec: PulseToolboxSpec,
    local_levels: int,
    transmon_count: int,
    times_us: Sequence[float],
    density_matrices: Sequence[object],
) -> dict[str, Any]:
    """Evaluate the requested metrics on each density-matrix point.

    Every series has one value per entry of ``times_us``, in the same order.
    """

    started_at = perf_counter()
    spec = validate_pulse_toolbox_spec(spec, local_levels, transmon_count)
    if len(times_us) != len(density_matrices):
        raise ValueError("times_us and density_matrices must have the same length")
    densities = [
        as_pulse_density_array(matrix, local_levels, transmon_count)
        for matrix in density_matrices
    ]
    response: dict[str, Any] = {
        "schema_version": PULSE_TOOLBOX_SCHEMA_VERSION,
        "local_levels": local_levels,
        "transmon_count": transmon_count,
        "times_us": [float(time) for time in times_us],
    }

    def reduced(rho: np.ndarray, keep: Sequence[int]) -> np.ndarray:
        return pulse_partial_trace(rho, local_levels, transmon_count, keep)

    if spec.entropy_transmons is not None:
        keep = spec.entropy_transmons
        response["entropy"] = {
            "transmons": list(keep),
            "values": [von_neumann_entropy(reduced(rho, keep)) for rho in densities],
        }

    if spec.concurrence_transmons is not None:
        pair = spec.concurrence_transmons
        response["concurrence"] = {
            "transmons": list(pair),
            "values": [concurrence(reduced(rho, pair)) for rho in densities],
        }

    if spec.negativity_subsystem is not None:
        subsystem = spec.negativity_subsystem
        response["negativity"] = {
            "subsystem": list(subsystem),
            "values": [
                pulse_negativity(rho, local_levels, transmon_count, subsystem)
                for rho in densities
            ],
        }

    if spec.mutual_information_a is not None and spec.mutual_information_b is not None:
        a, b = spec.mutual_information_a, spec.mutual_information_b
        response["mutual_information"] = {
            "a": list(a),
            "b": list(b),
            "values": [
                pulse_mutual_information(rho, local_levels, transmon_count, a, b)
                for rho in densities
            ],
        }

    if spec.observables:
        operators = {
            pauli: pulse_pauli_operator(pauli, local_levels)
            for pauli in spec.observables
        }
        response["observables"] = [
            {
                "pauli": pauli,
                "values": [pulse_pauli_expectation(rho, operators[pauli]) for rho in densities],
            }
            for pauli in spec.observables
        ]

    response["elapsed_ms"] = round((perf_counter() - started_at) * 1000.0, 3)
    return response
