"""Per-snapshot toolbox metrics for the Gate-aware state explorer.

The toolbox lets a user ask, before a run, for metrics the explorer does not
already show. Each one is evaluated on every density-matrix snapshot the run
kept, so the explorer can read it at the same time cursor as the Bloch sphere
and density matrix.

Only metrics that are not functions of what is already displayed are
accepted. That is why a one-qubit entropy and weight-one Pauli strings are
rejected: they are fixed by the Bloch vector length and the Bloch vector.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import numpy as np

from core.state_metrics import (
    as_density_array,
    concurrence,
    mutual_information,
    negativity,
    partial_trace,
    pauli_expectation,
    pauli_weight,
    trace_distance,
    validate_pauli_string,
    validate_qubit_subset,
    von_neumann_entropy,
)

TOOLBOX_SCHEMA_VERSION = "toolbox-v1"
MAX_TOOLBOX_OBSERVABLES = 8
# A snapshot is paired with its ideal counterpart only when both sit at the
# same simulated time; the ideal run uses the same snapshot plan, so a larger
# gap means the plans diverged and the pair would compare different instants.
_PAIRING_TIME_TOLERANCE_US = 1e-9


@dataclass(frozen=True)
class ToolboxSnapshotSpec:
    entropy_qubits: tuple[int, ...] | None = None
    concurrence_qubits: tuple[int, int] | None = None
    negativity_subsystem: tuple[int, ...] | None = None
    mutual_information_a: tuple[int, ...] | None = None
    mutual_information_b: tuple[int, ...] | None = None
    trace_distance: bool = False
    observables: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return (
            self.entropy_qubits is None
            and self.concurrence_qubits is None
            and self.negativity_subsystem is None
            and self.mutual_information_a is None
            and not self.trace_distance
            and not self.observables
        )


def validate_toolbox_spec(spec: ToolboxSnapshotSpec, n_qubits: int) -> ToolboxSnapshotSpec:
    """Return the spec normalized, or raise ValueError naming the bad field."""

    entropy_qubits = None
    if spec.entropy_qubits is not None:
        entropy_qubits = validate_qubit_subset(spec.entropy_qubits, n_qubits, name="entropy_qubits")
        if len(entropy_qubits) < 2:
            raise ValueError(
                "entropy_qubits needs at least two qubits; a one-qubit entropy "
                "is fixed by the Bloch vector length already shown"
            )

    concurrence_qubits = None
    if spec.concurrence_qubits is not None:
        pair = validate_qubit_subset(spec.concurrence_qubits, n_qubits, name="concurrence_qubits")
        if len(pair) != 2:
            raise ValueError("concurrence_qubits must name exactly two qubits")
        concurrence_qubits = (pair[0], pair[1])

    negativity_subsystem = None
    if spec.negativity_subsystem is not None:
        negativity_subsystem = validate_qubit_subset(
            spec.negativity_subsystem,
            n_qubits,
            name="negativity_subsystem",
            allow_all=False,
        )

    mutual_a = mutual_b = None
    if (spec.mutual_information_a is None) != (spec.mutual_information_b is None):
        raise ValueError("mutual_information_a and mutual_information_b must be given together")
    if spec.mutual_information_a is not None and spec.mutual_information_b is not None:
        mutual_a = validate_qubit_subset(spec.mutual_information_a, n_qubits, name="mutual_information_a")
        mutual_b = validate_qubit_subset(spec.mutual_information_b, n_qubits, name="mutual_information_b")
        if set(mutual_a) & set(mutual_b):
            raise ValueError("mutual_information_a and mutual_information_b must be disjoint")

    if len(spec.observables) > MAX_TOOLBOX_OBSERVABLES:
        raise ValueError(f"at most {MAX_TOOLBOX_OBSERVABLES} observables can be requested")
    observables: list[str] = []
    for raw in spec.observables:
        pauli = validate_pauli_string(raw, n_qubits)
        if pauli_weight(pauli) < 2:
            raise ValueError(
                f"observable {pauli} acts on fewer than two qubits; its value is "
                "a Bloch vector component or the trace, both already shown"
            )
        if pauli not in observables:
            observables.append(pauli)

    return ToolboxSnapshotSpec(
        entropy_qubits=entropy_qubits,
        concurrence_qubits=concurrence_qubits,
        negativity_subsystem=negativity_subsystem,
        mutual_information_a=mutual_a,
        mutual_information_b=mutual_b,
        trace_distance=bool(spec.trace_distance),
        observables=tuple(observables),
    )


def _snapshot_density(snapshot: Mapping[str, Any], n_qubits: int) -> np.ndarray:
    matrix = snapshot["density_matrix"]
    return as_density_array(
        np.asarray(matrix["real"], dtype=float) + 1j * np.asarray(matrix["imag"], dtype=float),
        n_qubits,
    )


def _paired_ideal(
    snapshot: Mapping[str, Any],
    position: int,
    ideal_snapshots: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if position >= len(ideal_snapshots):
        return None
    candidate = ideal_snapshots[position]
    if candidate.get("kind") != snapshot.get("kind"):
        return None
    if abs(float(candidate.get("time_us", 0.0)) - float(snapshot.get("time_us", 0.0))) > _PAIRING_TIME_TOLERANCE_US:
        return None
    return candidate


def compute_toolbox_snapshot_metrics(
    spec: ToolboxSnapshotSpec,
    n_qubits: int,
    state_snapshots: Sequence[Mapping[str, Any]],
    ideal_state_snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate the requested metrics on each serialized snapshot.

    Every series has one value per entry of ``state_snapshots``, in the same
    order, so the UI indexes it exactly as it indexes the snapshots.
    ``unavailable`` maps a requested key to a reason code the UI translates.
    """

    started_at = perf_counter()
    spec = validate_toolbox_spec(spec, n_qubits)
    response: dict[str, Any] = {
        "schema_version": TOOLBOX_SCHEMA_VERSION,
        "times_us": [float(snapshot.get("time_us", 0.0)) for snapshot in state_snapshots],
        "unavailable": {},
    }
    if not state_snapshots:
        for key in _requested_keys(spec):
            response["unavailable"][key] = "no_snapshots"
        response["elapsed_ms"] = round((perf_counter() - started_at) * 1000.0, 3)
        return response

    densities = [_snapshot_density(snapshot, n_qubits) for snapshot in state_snapshots]

    if spec.entropy_qubits is not None:
        qubits = spec.entropy_qubits
        response["entropy"] = {
            "qubits": list(qubits),
            "values": [von_neumann_entropy(partial_trace(rho, n_qubits, qubits)) for rho in densities],
        }

    if spec.concurrence_qubits is not None:
        pair = spec.concurrence_qubits
        response["concurrence"] = {
            "qubits": list(pair),
            "values": [concurrence(partial_trace(rho, n_qubits, pair)) for rho in densities],
        }

    if spec.negativity_subsystem is not None:
        subsystem = spec.negativity_subsystem
        response["negativity"] = {
            "subsystem": list(subsystem),
            "values": [negativity(rho, n_qubits, subsystem) for rho in densities],
        }

    if spec.mutual_information_a is not None and spec.mutual_information_b is not None:
        a, b = spec.mutual_information_a, spec.mutual_information_b
        response["mutual_information"] = {
            "a": list(a),
            "b": list(b),
            "values": [mutual_information(rho, n_qubits, a, b) for rho in densities],
        }

    if spec.trace_distance:
        if not ideal_state_snapshots:
            response["unavailable"]["trace_distance"] = "no_ideal_snapshots"
        else:
            values: list[float | None] = []
            for position, (snapshot, rho) in enumerate(zip(state_snapshots, densities)):
                ideal = _paired_ideal(snapshot, position, ideal_state_snapshots)
                values.append(
                    None if ideal is None else trace_distance(rho, _snapshot_density(ideal, n_qubits))
                )
            response["trace_distance"] = {"values": values}

    if spec.observables:
        response["observables"] = [
            {"pauli": pauli, "values": [pauli_expectation(rho, pauli) for rho in densities]}
            for pauli in spec.observables
        ]

    response["elapsed_ms"] = round((perf_counter() - started_at) * 1000.0, 3)
    return response


def _requested_keys(spec: ToolboxSnapshotSpec) -> list[str]:
    keys: list[str] = []
    if spec.entropy_qubits is not None:
        keys.append("entropy")
    if spec.concurrence_qubits is not None:
        keys.append("concurrence")
    if spec.negativity_subsystem is not None:
        keys.append("negativity")
    if spec.mutual_information_a is not None:
        keys.append("mutual_information")
    if spec.trace_distance:
        keys.append("trace_distance")
    if spec.observables:
        keys.append("observables")
    return keys
