"""Cluster-local correlated assignment errors at the observation boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np


_STOCHASTIC_TOLERANCE = 1e-12


@dataclass(frozen=True)
class CorrelatedReadoutChannel:
    """A joint readout channel for an ordered subset of qubits.

    Matrix rows are observed labels, columns are prepared/true labels, and
    local labels follow binary integer order over ``qubits``.  Every column
    must therefore sum to one.  Arbitrary columns allow the error on one bit
    to depend on the true state of its cluster neighbours.
    """

    qubits: tuple[int, ...]
    confusion_matrix: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        qubits = tuple(int(qubit) for qubit in self.qubits)
        if not qubits:
            raise ValueError("a correlated readout cluster must contain a qubit")
        if any(qubit < 0 for qubit in qubits) or len(set(qubits)) != len(qubits):
            raise ValueError("readout cluster qubits must be distinct and non-negative")
        dimension = 1 << len(qubits)
        matrix = np.asarray(self.confusion_matrix, dtype=np.float64)
        if matrix.shape != (dimension, dimension):
            raise ValueError(
                "confusion_matrix must be 2^k by 2^k for the cluster"
            )
        if not np.all(np.isfinite(matrix)):
            raise ValueError("confusion_matrix entries must be finite")
        if np.min(matrix) < -_STOCHASTIC_TOLERANCE:
            raise ValueError("confusion_matrix entries must be non-negative")
        if np.max(matrix) > 1.0 + _STOCHASTIC_TOLERANCE:
            raise ValueError("confusion_matrix entries must not exceed one")
        if not np.allclose(
            matrix.sum(axis=0),
            np.ones(dimension),
            rtol=0.0,
            atol=_STOCHASTIC_TOLERANCE,
        ):
            raise ValueError("every confusion_matrix column must sum to one")
        object.__setattr__(self, "qubits", qubits)
        object.__setattr__(
            self,
            "confusion_matrix",
            tuple(tuple(float(value) for value in row) for row in matrix),
        )

    @classmethod
    def identity(cls, qubits: Sequence[int]) -> "CorrelatedReadoutChannel":
        ordered = tuple(int(qubit) for qubit in qubits)
        dimension = 1 << len(ordered)
        matrix = np.eye(dimension, dtype=np.float64)
        return cls(
            qubits=ordered,
            confusion_matrix=tuple(tuple(float(value) for value in row) for row in matrix),
        )


def apply_correlated_readout(
    probabilities: Mapping[str, float],
    qubit_count: int,
    channels: Sequence[CorrelatedReadoutChannel],
) -> dict[str, float]:
    """Apply disjoint local joint channels without building a global matrix.

    The cost is linear in the full probability vector for fixed cluster size.
    Overlapping clusters are rejected because their sequential order would
    otherwise become an undocumented part of the physical model.
    """

    count = int(qubit_count)
    if count < 1:
        raise ValueError("qubit_count must be positive")
    expected_labels = tuple(format(index, f"0{count}b") for index in range(1 << count))
    if set(probabilities) != set(expected_labels):
        raise ValueError("probabilities must contain every binary basis label once")
    values = np.asarray([float(probabilities[label]) for label in expected_labels])
    if not np.all(np.isfinite(values)) or np.min(values) < -_STOCHASTIC_TOLERANCE:
        raise ValueError("probabilities must be finite and non-negative")
    if not math.isclose(float(values.sum()), 1.0, rel_tol=0.0, abs_tol=1e-10):
        raise ValueError("probabilities must sum to one")
    if not channels:
        return {label: float(probabilities[label]) for label in expected_labels}

    occupied: set[int] = set()
    tensor = values.reshape((2,) * count)
    for channel in channels:
        if any(qubit >= count for qubit in channel.qubits):
            raise ValueError("readout cluster qubit is outside the register")
        overlap = occupied.intersection(channel.qubits)
        if overlap:
            raise ValueError("readout clusters must be disjoint")
        occupied.update(channel.qubits)

        cluster_size = len(channel.qubits)
        destinations = tuple(range(count - cluster_size, count))
        moved = np.moveaxis(tensor, channel.qubits, destinations)
        spectator_shape = moved.shape[:-cluster_size]
        rows = moved.reshape((-1, 1 << cluster_size))
        confusion = np.asarray(channel.confusion_matrix, dtype=np.float64)
        transformed = (rows @ confusion.T).reshape(
            (*spectator_shape, *((2,) * cluster_size))
        )
        tensor = np.moveaxis(transformed, destinations, channel.qubits)

    flattened = tensor.reshape(-1)
    return {
        label: _clean_probability(float(flattened[index]))
        for index, label in enumerate(expected_labels)
    }


def _clean_probability(value: float) -> float:
    if -_STOCHASTIC_TOLERANCE <= value < 0.0:
        return 0.0
    if 1.0 < value <= 1.0 + _STOCHASTIC_TOLERANCE:
        return 1.0
    return value
