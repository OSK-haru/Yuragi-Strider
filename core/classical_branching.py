"""Branch execution for mid-circuit measurement and classical control.

The default path is a fast logical-density preview.  When environment rates
are supplied, each branch is evolved through the same constant-GKSL gate
interval model as the Gate-aware runner before measurement projection.  This
keeps teleportation/feed-forward circuits physically meaningful without
simulating every requested shot as a separate density matrix.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any

from core.circuit_model import CircuitConfig, GateOperation
from core.gates import (
    Matrix,
    apply_unitary_to_density,
    computational_measurement_outcomes,
    effective_hamiltonian_from_unitary,
    gate_duration_us,
    gate_unitary,
    initial_density_matrix,
    multi_qubit_environment_collapse_operators,
    prepare_collapse_operators,
)
from core.evolution_methods import EXPLICIT_CPTP
from core.backend_boundary import PYTHON_DENSE_BACKEND, requests_rust_backend
from core.physical_environment import EnvironmentRates


MAX_CLASSICAL_BRANCHES = 4096


@dataclass
class ClassicalBranch:
    probability: float
    density_matrix: Matrix
    classical_bits: list[int]
    measurement_history: list[dict[str, Any]]


@dataclass
class ClassicalBranchingResult:
    output_probabilities: dict[str, float]
    branches: list[dict[str, Any]]
    diagnostics: dict[str, Any]


def execute_classical_branches(
    circuit: CircuitConfig,
    *,
    max_branches: int = MAX_CLASSICAL_BRANCHES,
    environment_rates: EnvironmentRates | Sequence[EnvironmentRates] | None = None,
    evolution_method: str = "fixed_step_rk4",
    simulation_backend: str = PYTHON_DENSE_BACKEND,
) -> ClassicalBranchingResult:
    """Execute gates while preserving measurement/classical branches.

    ``environment_rates=None`` retains the inexpensive logical preview.  With
    rates, gate intervals receive Gate-aware Lindblad/CPTP evolution.  Branch
    count is bounded before the next measurement so pathological adaptive
    circuits fail explicitly instead of exhausting memory.
    """

    collapse_ops = ()
    max_rate = 0.0
    cptp_evolver = None
    if environment_rates is not None:
        collapse_ops = prepare_collapse_operators(
            multi_qubit_environment_collapse_operators(
                circuit.logical_qubits,
                environment_rates,
            )
        )
        local_rates = (
            environment_rates
            if isinstance(environment_rates, Sequence)
            else (environment_rates,)
        )
        max_rate = max(
            max(
                float(rate.gamma_down_per_us),
                float(rate.gamma_up_per_us),
                float(rate.gamma_phi_per_us),
            )
            for rate in local_rates
        )
        if evolution_method == EXPLICIT_CPTP:
            from core.gate_aware_cptp import GateAwareCPTPEvolver
            cptp_evolver = GateAwareCPTPEvolver(
                backend="rust" if requests_rust_backend(simulation_backend) else "python"
            )

    state = initial_density_matrix(circuit.initial_states)
    branches = [ClassicalBranch(
        probability=1.0,
        density_matrix=state,
        classical_bits=[0] * circuit.classical_bits,
        measurement_history=[],
    )]

    for column in sorted(circuit.columns, key=lambda item: item.step):
        for gate in column.gates:
            next_branches: list[ClassicalBranch] = []
            for branch in branches:
                if not _condition_matches(gate, branch.classical_bits):
                    next_branches.append(branch)
                    continue

                if str(gate.type).upper() == "MEASURE":
                    outcomes = computational_measurement_outcomes(
                        branch.density_matrix,
                        gate.targets,
                        circuit.logical_qubits,
                    )
                    for outcome, (probability, density_matrix) in outcomes.items():
                        if probability <= 0.0:
                            continue
                        bits = list(branch.classical_bits)
                        for position, classical_target in enumerate(
                            gate.classical_targets or []
                        ):
                            bits[classical_target] = int(outcome[position])
                        next_branches.append(ClassicalBranch(
                            probability=branch.probability * probability,
                            density_matrix=density_matrix,
                            classical_bits=bits,
                            measurement_history=[
                                *branch.measurement_history,
                                {
                                    "column": int(column.step),
                                    "qubits": [int(target) for target in gate.targets],
                                    "outcome": outcome,
                                    "classical_targets": [
                                        int(target)
                                        for target in (gate.classical_targets or [])
                                    ],
                                },
                            ],
                        ))
                else:
                    unitary = gate_unitary(gate, circuit.logical_qubits)
                    evolved_state = _evolve_branch_gate(
                        branch.density_matrix,
                        unitary,
                        gate_duration_us(gate),
                        collapse_ops,
                        max_rate,
                        evolution_method,
                        simulation_backend,
                        cptp_evolver,
                    )
                    next_branches.append(ClassicalBranch(
                        probability=branch.probability,
                        density_matrix=evolved_state,
                        classical_bits=list(branch.classical_bits),
                        measurement_history=list(branch.measurement_history),
                    ))

            branches = next_branches
            if len(branches) > max_branches:
                raise ValueError(
                    "classical branching exceeded the configured branch limit "
                    f"({max_branches})"
                )

    output_probabilities: dict[str, float] = {}
    records: list[dict[str, Any]] = []
    branch_probability_total = sum(max(0.0, branch.probability) for branch in branches)
    branch_probability_scale = 1.0 / branch_probability_total if branch_probability_total > 0.0 else 1.0
    for branch in branches:
        for index in range(len(branch.density_matrix)):
            label = format(
                index,
                f"0{circuit.logical_qubits}b",
            )
            output_probabilities[label] = output_probabilities.get(label, 0.0) + (
                branch.probability
                * max(0.0, branch.density_matrix[index][index].real)
            )
        records.append({
            "probability": float(max(0.0, branch.probability) * branch_probability_scale),
            "classical_bits": list(branch.classical_bits),
            "measurements": list(branch.measurement_history),
        })

    total = sum(output_probabilities.values())
    if total > 0.0:
        output_probabilities = {
            label: probability / total
            for label, probability in output_probabilities.items()
        }
    diagnostics = {
        "branch_noise_applied": environment_rates is not None,
        "branch_evolution_method": evolution_method,
        "branch_simulation_backend": simulation_backend,
        "branch_max_count": int(max_branches),
        "branch_probability_sum": float(sum(record["probability"] for record in records)),
    }
    if cptp_evolver is not None:
        diagnostics.update({
            f"branch_{key}": value
            for key, value in cptp_evolver.diagnostics().items()
        })
    return ClassicalBranchingResult(output_probabilities, records, diagnostics)


def _evolve_branch_gate(
    state: Matrix,
    unitary: Matrix,
    duration_us: float,
    collapse_ops,
    max_rate: float,
    evolution_method: str,
    simulation_backend: str,
    cptp_evolver,
) -> Matrix:
    """Apply one gate and its physical interval to a branch."""

    duration = float(duration_us)
    if duration <= 0.0 or not collapse_ops:
        return apply_unitary_to_density(state, unitary)
    hamiltonian = effective_hamiltonian_from_unitary(unitary, duration)
    if evolution_method == EXPLICIT_CPTP:
        return cptp_evolver.evolve(state, hamiltonian, collapse_ops, duration)
    if requests_rust_backend(simulation_backend):
        try:
            from core.rust_dense_kernel import rust_rk4_evolve_segment_cleaned
            substeps = max(1, int(math.ceil(duration * max(max_rate, 1e-12))))
            return rust_rk4_evolve_segment_cleaned(
                state,
                hamiltonian,
                [item.operator for item in collapse_ops],
                duration / substeps,
                substeps,
            )
        except Exception as exc:
            if simulation_backend == "rust_dense_v1":
                raise RuntimeError("Rust classical-branch execution failed") from exc
            # The normal runner exposes the fallback in diagnostics; branch
            # execution remains correct by using the audited Python path.
            pass
    # Import lazily to avoid the simulator -> branching import cycle.
    from core.simulator import _evolve_stable
    return _evolve_stable(
        state,
        hamiltonian,
        collapse_ops,
        duration,
        max(max_rate, 1e-12),
        kernel_stats=None,
        profile=None,
    )


def _condition_matches(gate: GateOperation, classical_bits: list[int]) -> bool:
    conditions = gate.conditions or ([] if gate.condition is None else [gate.condition])
    return all(classical_bits[item.bit] == item.value for item in conditions)
