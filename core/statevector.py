"""Local-gate statevector execution for ideal and low-noise circuits.

This deliberately avoids constructing a ``2**n x 2**n`` matrix.  It is the
first adaptive representation behind the simulator: noisy density/CPTP paths
remain authoritative, while ideal circuits can use this O(2**n) state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.circuit_model import CircuitConfig, GateOperation
from core.gates import (
    H,
    I,
    S,
    T,
    X,
    Y,
    Z,
    oracle_marked_index,
    oracle_register,
    qft_register,
    rx_matrix,
    ry_matrix,
    rz_matrix,
)


Vector = tuple[complex, ...]


@dataclass
class StatevectorBranch:
    probability: float
    amplitudes: Vector
    classical_bits: list[int]
    measurement_history: list[dict[str, Any]]


@dataclass
class StatevectorExecutionResult:
    output_probabilities: dict[str, float]
    branches: list[dict[str, Any]]


def execute_statevector_branches(
    circuit: CircuitConfig,
    *,
    max_branches: int = 4096,
) -> StatevectorExecutionResult:
    """Execute a circuit without ever materializing a full density matrix."""

    branches = [StatevectorBranch(
        probability=1.0,
        amplitudes=_initial_statevector(circuit.initial_states),
        classical_bits=[0] * circuit.classical_bits,
        measurement_history=[],
    )]
    for column in sorted(circuit.columns, key=lambda item: item.step):
        for gate in column.gates:
            next_branches: list[StatevectorBranch] = []
            for branch in branches:
                if not _condition_matches(gate, branch.classical_bits):
                    next_branches.append(branch)
                    continue
                if str(gate.type).upper() == "MEASURE":
                    next_branches.extend(_measure_branch(branch, gate, circuit.logical_qubits, column.step))
                    continue
                next_branches.append(StatevectorBranch(
                    probability=branch.probability,
                    amplitudes=_apply_gate(branch.amplitudes, gate, circuit.logical_qubits),
                    classical_bits=list(branch.classical_bits),
                    measurement_history=list(branch.measurement_history),
                ))
            if len(next_branches) > max_branches:
                raise ValueError(
                    f"statevector branching exceeded the configured branch limit ({max_branches})"
                )
            branches = next_branches

    probabilities = {
        format(index, f"0{circuit.logical_qubits}b"): 0.0
        for index in range(2 ** circuit.logical_qubits)
    }
    records: list[dict[str, Any]] = []
    branch_probability_total = sum(max(0.0, branch.probability) for branch in branches)
    branch_probability_scale = 1.0 / branch_probability_total if branch_probability_total > 0.0 else 1.0
    for branch in branches:
        for index, amplitude in enumerate(branch.amplitudes):
            label = format(index, f"0{circuit.logical_qubits}b")
            probabilities[label] += branch.probability * abs(amplitude) ** 2
        records.append({
            "probability": float(max(0.0, branch.probability) * branch_probability_scale),
            "classical_bits": list(branch.classical_bits),
            "measurements": list(branch.measurement_history),
        })
    total = sum(probabilities.values())
    if total > 0.0:
        probabilities = {label: value / total for label, value in probabilities.items()}
    result = StatevectorExecutionResult(probabilities, records)
    # Keep the branch amplitudes private but available for the endpoint purity
    # diagnostic, which must reflect an unread measurement as a mixed state.
    result._branches_internal = tuple(branches)  # type: ignore[attr-defined]
    return result


def statevector_branch_purity(result: StatevectorExecutionResult) -> float:
    branches = getattr(result, "_branches_internal", ())
    if not branches:
        return 1.0
    if len(branches) == 1:
        return 1.0
    dimension = len(branches[0].amplitudes)
    rho = [[0.0j for _ in range(dimension)] for _ in range(dimension)]
    for branch in branches:
        for row, amplitude_row in enumerate(branch.amplitudes):
            for column, amplitude_column in enumerate(branch.amplitudes):
                rho[row][column] += branch.probability * amplitude_row * amplitude_column.conjugate()
    purity = sum(rho[row][column] * rho[column][row] for row in range(dimension) for column in range(dimension))
    return max(0.0, min(1.0, float(purity.real)))


def _initial_statevector(initial_states: list[str]) -> Vector:
    vector: Vector = (1.0 + 0.0j,)
    for state in initial_states:
        if state == "0":
            local = (1.0 + 0.0j, 0.0 + 0.0j)
        elif state == "1":
            local = (0.0 + 0.0j, 1.0 + 0.0j)
        elif state == "+":
            local = (2.0 ** -0.5, 2.0 ** -0.5)
        elif state == "-":
            local = (2.0 ** -0.5, -(2.0 ** -0.5))
        elif state == "+i":
            local = (2.0 ** -0.5, 1.0j * 2.0 ** -0.5)
        elif state == "-i":
            local = (2.0 ** -0.5, -1.0j * 2.0 ** -0.5)
        else:
            raise ValueError(f"unsupported initial state: {state!r}")
        vector = tuple(
            value * local[bit]
            for value in vector
            for bit in range(2)
        )
    return vector


def _apply_gate(vector: Vector, gate: GateOperation, n_qubits: int) -> Vector:
    gate_type = str(gate.type).upper()
    if gate_type in {"I", "H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ"}:
        matrix = {
            "I": I, "H": H, "X": X, "Y": Y, "Z": Z, "S": S, "T": T,
            "RX": rx_matrix(float((gate.params or {}).get("theta_rad", 3.141592653589793 / 2))),
            "RY": ry_matrix(float((gate.params or {}).get("theta_rad", 3.141592653589793 / 2))),
            "RZ": rz_matrix(float((gate.params or {}).get("theta_rad", 3.141592653589793 / 2))),
        }[gate_type]
        return _apply_single(vector, matrix, gate.targets[0], n_qubits)
    if gate_type == "CNOT":
        params = gate.params or {}
        raw_control_value = params.get("control_value", 1.0)
        raw_control_state = params.get("control_state")
        if raw_control_value not in {0.0, 1.0}:
            raise ValueError("CNOT control_value must be 0 or 1")
        if raw_control_state is not None and (
            raw_control_state != int(raw_control_state)
            or raw_control_state < 0
            or raw_control_state >= 2 ** len(gate.controls)
        ):
            raise ValueError("CNOT control_state is outside the control-bit range")
        control_value = int(raw_control_value)
        return _apply_controlled_x(
            vector,
            gate.controls,
            gate.targets[0],
            n_qubits,
            control_value,
            None if raw_control_state is None else int(raw_control_state),
        )
    if gate_type == "CZ":
        return _apply_controlled_phase(vector, gate.controls[0], gate.targets[0], n_qubits, -1.0)
    if gate_type == "SWAP":
        return _apply_swap(vector, gate.targets[0], gate.targets[1], n_qubits)
    if gate_type == "CCX":
        return _apply_toffoli(vector, gate.controls[0], gate.controls[1], gate.targets[0], n_qubits)
    if gate_type == "QFT":
        return _apply_qft(vector, qft_register(gate.targets), n_qubits)
    if gate_type == "ORACLE":
        register = oracle_register(gate.targets)
        return _apply_phase_oracle(
            vector,
            register,
            oracle_marked_index(gate.params, len(register)),
            n_qubits,
        )
    if gate_type in {"CP"}:
        import cmath
        theta = float((gate.params or {}).get("theta_rad", 3.141592653589793 / 2))
        phase = cmath.exp(1j * theta)
        return _apply_controlled_phase(vector, gate.controls[0], gate.targets[0], n_qubits, phase)
    raise ValueError(f"unsupported statevector gate: {gate.type}")


def _apply_single(vector: Vector, matrix, target: int, n_qubits: int) -> Vector:
    mask = 1 << (n_qubits - 1 - target)
    values = list(vector)
    for index in range(len(vector)):
        if index & mask:
            continue
        partner = index | mask
        a, b = vector[index], vector[partner]
        values[index] = matrix[0][0] * a + matrix[0][1] * b
        values[partner] = matrix[1][0] * a + matrix[1][1] * b
    return tuple(values)


def _apply_controlled_x(vector, controls, target, n_qubits, control_value=1, control_state=None):
    if not controls:
        raise ValueError("CNOT requires at least one control")
    if control_value not in {0, 1}:
        raise ValueError("CNOT control_value must be 0 or 1")
    if control_state is None:
        control_state = control_value if len(controls) == 1 else 2 ** len(controls) - 1
    if control_state < 0 or control_state >= 2 ** len(controls):
        raise ValueError("CNOT control_state is outside the control-bit range")
    control_masks = [1 << (n_qubits - 1 - control) for control in controls]
    target_mask = 1 << (n_qubits - 1 - target)
    values = list(vector)
    for index in range(len(vector)):
        control_is_active = all(
            bool(index & mask) == bool((control_state >> (len(control_masks) - 1 - position)) & 1)
            for position, mask in enumerate(control_masks)
        )
        if control_is_active and not index & target_mask:
            partner = index | target_mask
            values[index], values[partner] = vector[partner], vector[index]
    return tuple(values)


def _apply_controlled_phase(vector, control, target, n_qubits, phase):
    control_mask = 1 << (n_qubits - 1 - control)
    target_mask = 1 << (n_qubits - 1 - target)
    return tuple(
        amplitude * phase if index & control_mask and index & target_mask else amplitude
        for index, amplitude in enumerate(vector)
    )


def _apply_swap(vector, first, second, n_qubits):
    first_mask = 1 << (n_qubits - 1 - first)
    second_mask = 1 << (n_qubits - 1 - second)
    values = list(vector)
    for index in range(len(vector)):
        first_bit = bool(index & first_mask)
        second_bit = bool(index & second_mask)
        if first_bit != second_bit:
            partner = index ^ first_mask ^ second_mask
            values[index] = vector[partner]
    return tuple(values)


def _apply_qft(vector, register, n_qubits):
    """Apply the QFT ladder in place of a 2**n x 2**n matrix multiplication."""

    import cmath

    size = len(register)
    for position, target in enumerate(register):
        vector = _apply_single(vector, H, target, n_qubits)
        for offset in range(1, size - position):
            phase = cmath.exp(1j * cmath.pi / float(2 ** offset))
            vector = _apply_controlled_phase(
                vector, register[position + offset], target, n_qubits, phase,
            )
    for index in range(size // 2):
        vector = _apply_swap(vector, register[index], register[size - 1 - index], n_qubits)
    return vector


def _apply_phase_oracle(vector, register, marked_index, n_qubits):
    """Negate every amplitude whose register bits equal the marked value."""

    size = len(register)
    mask = 0
    marked_mask = 0
    for position, qubit in enumerate(register):
        bit = 1 << (n_qubits - 1 - qubit)
        mask |= bit
        if (marked_index >> (size - 1 - position)) & 1:
            marked_mask |= bit
    return tuple(
        -amplitude if (index & mask) == marked_mask else amplitude
        for index, amplitude in enumerate(vector)
    )


def _apply_toffoli(vector, control_a, control_b, target, n_qubits):
    mask_a = 1 << (n_qubits - 1 - control_a)
    mask_b = 1 << (n_qubits - 1 - control_b)
    target_mask = 1 << (n_qubits - 1 - target)
    values = list(vector)
    for index in range(len(vector)):
        if index & mask_a and index & mask_b and not index & target_mask:
            partner = index | target_mask
            values[index], values[partner] = vector[partner], vector[index]
    return tuple(values)


def _measure_branch(branch: StatevectorBranch, gate: GateOperation, n_qubits: int, column_step: int):
    targets = list(gate.targets)
    outcomes: list[StatevectorBranch] = []
    for outcome_index in range(2 ** len(targets)):
        outcome = format(outcome_index, f"0{len(targets)}b")
        selected = [
            index for index in range(len(branch.amplitudes))
            if all(((index >> (n_qubits - 1 - target)) & 1) == int(outcome[position])
                   for position, target in enumerate(targets))
        ]
        probability = sum(abs(branch.amplitudes[index]) ** 2 for index in selected)
        if probability <= 1e-15:
            continue
        amplitudes = tuple(
            branch.amplitudes[index] / probability ** 0.5 if index in selected else 0.0j
            for index in range(len(branch.amplitudes))
        )
        bits = list(branch.classical_bits)
        for position, target in enumerate(gate.classical_targets or []):
            bits[target] = int(outcome[position])
        outcomes.append(StatevectorBranch(
            probability=branch.probability * probability,
            amplitudes=amplitudes,
            classical_bits=bits,
            measurement_history=[
                *branch.measurement_history,
                {"column": int(column_step), "qubits": targets, "outcome": outcome,
                 "classical_targets": list(gate.classical_targets or [])},
            ],
        ))
    return outcomes


def _condition_matches(gate: GateOperation, classical_bits: list[int]) -> bool:
    return all(
        classical_bits[item.bit] == item.value
        for item in (gate.conditions or ([] if gate.condition is None else [gate.condition]))
    )
