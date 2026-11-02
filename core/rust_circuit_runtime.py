"""Coarse-grained PyO3 boundary for Circuit Studio's native Rust runtime."""

from __future__ import annotations

import importlib
import math
from typing import Sequence

from core.circuit_model import CircuitConfig
from core.results import ReadoutErrorConfig
from core.rust_dense_kernel import unflatten_complex_matrix


def execute_ideal_circuit_rust(
    circuit: CircuitConfig,
    readout_error: ReadoutErrorConfig | None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Execute an ideal circuit and apply readout assignment in one PyO3 call."""

    logical_qubits = circuit.logical_qubits
    assignments: list[tuple[float, float]] = []
    if readout_error is not None and readout_error.is_enabled:
        assignments = readout_error.assignment_errors(logical_qubits)

    operations = []
    for column in sorted(circuit.columns, key=lambda item: item.step):
        for gate in column.gates:
            params = gate.params or {}
            marked = float(params.get("marked_index", 0.0))
            if not math.isfinite(marked) or not marked.is_integer() or marked < 0:
                raise ValueError("ORACLE marked_index must be a non-negative integer")
            control_value = float(params.get("control_value", 1.0))
            if control_value not in (0.0, 1.0):
                raise ValueError("CNOT control_value must be 0 or 1")
            raw_control_state = params.get("control_state")
            control_state = None
            if raw_control_state is not None:
                value = float(raw_control_state)
                if not math.isfinite(value) or not value.is_integer() or value < 0:
                    raise ValueError("CNOT control_state must be a non-negative integer")
                control_state = int(value)
            conditions = [
                (condition.bit, condition.value)
                for condition in (gate.conditions or [])
            ]
            operations.append((
                str(gate.type),
                list(gate.targets),
                list(gate.controls or []),
                _theta_parameter(gate),
                int(marked),
                int(control_value),
                control_state,
                conditions,
            ))

    module = importlib.import_module("yuragi_strider_rust")
    execute = getattr(module, "execute_ideal_circuit_v1", None)
    if execute is None:
        raise RuntimeError("Rust extension does not provide execute_ideal_circuit_v1")
    ideal, observed = execute(
        logical_qubits,
        circuit.classical_bits,
        [str(value) for value in circuit.initial_states],
        operations,
        assignments,
    )
    return _probability_map(ideal, logical_qubits), _probability_map(observed, logical_qubits)


def execute_post_circuit_gates_rust(circuit: CircuitConfig):
    """Apply a complete ideal gate sequence and nonselective measurements in Rust."""

    operations = []
    for column in sorted(circuit.columns, key=lambda item: item.step):
        for gate in column.gates:
            params = gate.params or {}
            marked = float(params.get("marked_index", 0.0))
            control_value = float(params.get("control_value", 1.0))
            raw_control_state = params.get("control_state")
            control_state = None
            if raw_control_state is not None:
                value = float(raw_control_state)
                if not math.isfinite(value) or not value.is_integer() or value < 0:
                    raise ValueError("CNOT control_state must be a non-negative integer")
                control_state = int(value)
            if not math.isfinite(marked) or not marked.is_integer() or marked < 0:
                raise ValueError("ORACLE marked_index must be a non-negative integer")
            if control_value not in (0.0, 1.0):
                raise ValueError("CNOT control_value must be 0 or 1")
            operations.append((
                int(column.step),
                str(gate.type),
                list(gate.targets),
                list(gate.controls or []),
                _theta_parameter(gate),
                int(marked),
                int(control_value),
                control_state,
            ))

    module = importlib.import_module("yuragi_strider_rust")
    execute = getattr(module, "execute_ideal_density_circuit_v1", None)
    if execute is None:
        raise RuntimeError("Rust extension does not provide execute_ideal_density_circuit_v1")
    flat = execute(
        circuit.logical_qubits,
        [str(value) for value in circuit.initial_states],
        operations,
    )
    return unflatten_complex_matrix(flat, 1 << circuit.logical_qubits)


def apply_readout_rust(
    probabilities: dict[str, float],
    logical_qubits: int,
    assignment_errors: Sequence[tuple[float, float]],
) -> dict[str, float]:
    """Apply a complete per-qubit assignment calibration using the Rust core."""

    dimension = 1 << logical_qubits
    values = [float(probabilities.get(format(index, f"0{logical_qubits}b"), 0.0))
              for index in range(dimension)]
    module = importlib.import_module("yuragi_strider_rust")
    apply_readout = getattr(module, "apply_binary_readout_v1", None)
    if apply_readout is None:
        raise RuntimeError("Rust extension does not provide apply_binary_readout_v1")
    return _probability_map(apply_readout(values, logical_qubits, list(assignment_errors)), logical_qubits)


def _probability_map(values: Sequence[float], logical_qubits: int) -> dict[str, float]:
    dimension = 1 << logical_qubits
    if len(values) != dimension:
        raise RuntimeError("Rust circuit runtime returned an invalid probability vector")
    return {
        format(index, f"0{logical_qubits}b"): float(value)
        for index, value in enumerate(values)
    }


def _theta_parameter(gate) -> float:
    params = gate.params or {}
    if str(gate.type).upper() == "MESSAGE":
        return float(params.get("animation_parameter_t", 0.0))
    return float(params.get("theta_rad", math.pi / 2.0))
