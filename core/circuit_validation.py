"""Circuit-editor validation helpers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from core.capabilities import SUPPORTED_GATES, normalize_gate_type
from core.gate_specs import GATE_SPECS
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.errors import ValidationIssue


def validate_circuit_config(config: CircuitConfig) -> list[ValidationIssue]:
    """Validate a circuit config for editor-safe placement."""

    return _validate_circuit(
        logical_qubits=config.logical_qubits,
        classical_bits=config.classical_bits,
        columns=config.columns,
    )


def validate_circuit_state(state: Any) -> list[ValidationIssue]:
    """Validate a CircuitState-like object without importing CircuitState."""

    return _validate_circuit(
        logical_qubits=state.logical_qubits,
        classical_bits=getattr(state, "classical_bits", 0),
        columns=state.columns,
    )


def validate_gate_for_circuit(
    logical_qubits: int,
    gate: GateOperation,
    classical_bits: int = 0,
) -> list[ValidationIssue]:
    """Validate one gate without considering other gates in the same column."""

    issues: list[ValidationIssue] = []
    gate_type = normalize_gate_type(gate.type)

    if gate_type not in SUPPORTED_GATES:
        issues.append(_error(
            "INVALID_GATE_TYPE",
            "Gate type is not supported by the circuit editor.",
            f"Received gate type={gate.type!r}.",
            "Use one of I, H, X, Y, Z, S, T, RX, RY, RZ, CNOT, CZ, ECR, CP, CCX, SWAP, QFT, ORACLE, or Measure.",
        ))

    if gate.classical_targets and gate_type != "MEASURE":
        issues.append(_error(
            "CLASSICAL_TARGET_REQUIRES_MEASURE",
            "classical_targets are only valid for MEASURE gates.",
            f"Received classical_targets={gate.classical_targets!r}.",
            "Attach classical_targets only to MEASURE.",
        ))
    for classical_target in gate.classical_targets or []:
        if classical_target >= classical_bits:
            issues.append(_error(
                "CLASSICAL_TARGET_OUT_OF_RANGE",
                "Measurement classical target is outside the register.",
                f"Received classical target={classical_target}; classical_bits={classical_bits}.",
                "Increase classical_bits or choose an existing classical bit.",
            ))
    if gate_type == "MEASURE" and gate.classical_targets and len(
        gate.classical_targets
    ) != len(gate.targets):
        issues.append(_error(
            "MEASURE_CLASSICAL_TARGET_COUNT_MISMATCH",
            "Measurement classical targets must match measured targets.",
            "Provide one classical target per measured qubit.",
            "Set classical_targets to the same length as targets.",
        ))
    if gate.condition is not None and gate.condition.bit >= classical_bits:
        issues.append(_error(
            "CLASSICAL_CONDITION_OUT_OF_RANGE",
            "Classical condition bit is outside the register.",
            f"Received condition bit={gate.condition.bit}; classical_bits={classical_bits}.",
            "Increase classical_bits or choose an existing classical bit.",
        ))
    for condition in gate.conditions:
        if condition.bit >= classical_bits:
            issues.append(_error(
                "CLASSICAL_CONDITION_OUT_OF_RANGE",
                "Classical condition bit is outside the register.",
                f"Received condition bit={condition.bit}; classical_bits={classical_bits}.",
                "Increase classical_bits or choose an existing classical bit.",
            ))

    if not gate.targets:
        issues.append(_error(
            "GATE_REQUIRES_TARGET",
            "Gate must have at least one target.",
            f"Received targets={gate.targets!r}.",
            "Choose a target qubit for the gate.",
        ))
    elif (GATE_SPECS[gate_type].min_targets == GATE_SPECS[gate_type].max_targets == 1
          and GATE_SPECS[gate_type].min_controls == GATE_SPECS[gate_type].max_controls == 0
          and len(gate.targets) != 1):
        issues.append(_error(
            "GATE_REQUIRES_SINGLE_TARGET",
            f"{gate.type} requires exactly one target qubit.",
            f"Received targets={gate.targets!r}.",
            "Choose exactly one target qubit for this gate.",
        ))

    for target in gate.targets:
        if not _is_qubit_in_range(target, logical_qubits):
            issues.append(_error(
                "GATE_TARGET_OUT_OF_RANGE",
                "Gate target is outside the logical qubit range.",
                f"Received target={target}; logical_qubits={logical_qubits}.",
                "Use target indices from 0 to logical_qubits - 1.",
            ))

    for control in gate.controls or []:
        if not _is_qubit_in_range(control, logical_qubits):
            issues.append(_error(
                "GATE_CONTROL_OUT_OF_RANGE",
                "Gate control is outside the logical qubit range.",
                f"Received control={control}; logical_qubits={logical_qubits}.",
                "Use control indices from 0 to logical_qubits - 1.",
            ))

    if gate_type == "CNOT":
        if len(gate.controls or []) < 1:
            issues.append(_error(
                f"{gate_type}_REQUIRES_CONTROL",
                f"{gate_type} requires at least one control qubit.",
                f"Received controls={gate.controls!r}.",
                f"Set one or more control qubits for {gate_type}.",
            ))
        if len(gate.targets) != 1:
            issues.append(_error(
                f"{gate_type}_REQUIRES_TARGET",
                f"{gate_type} requires exactly one target qubit.",
                f"Received targets={gate.targets!r}.",
                f"Set exactly one target qubit for {gate_type}.",
            ))
        for control in gate.controls or []:
            if control in gate.targets:
                issues.append(_error(
                    f"{gate_type}_CONTROL_EQUALS_TARGET",
                    f"{gate_type} control and target must be different qubits.",
                    f"Received control={control}, targets={gate.targets!r}.",
                    f"Choose different qubits for {gate_type} control and target.",
                ))
        if len(set(gate.controls or [])) != len(gate.controls or []):
            issues.append(_error(
                "CNOT_CONTROLS_MUST_DIFFER",
                "CNOT control qubits must all be different.",
                f"Received controls={gate.controls!r}.",
                "Choose each control qubit once.",
            ))
    elif gate_type in {"CZ", "CP"}:
        if len(gate.controls or []) != 1:
            issues.append(_error(
                f"{gate_type}_REQUIRES_CONTROL",
                f"{gate_type} requires exactly one control qubit.",
                f"Received controls={gate.controls!r}.",
                f"Set exactly one control qubit for {gate_type}.",
            ))
        if len(gate.targets) != 1:
            issues.append(_error(
                f"{gate_type}_REQUIRES_TARGET",
                f"{gate_type} requires exactly one target qubit.",
                f"Received targets={gate.targets!r}.",
                f"Set exactly one target qubit for {gate_type}.",
            ))
        for control in gate.controls or []:
            if control in gate.targets:
                issues.append(_error(
                    f"{gate_type}_CONTROL_EQUALS_TARGET",
                    f"{gate_type} control and target must be different qubits.",
                    f"Received control={control}, targets={gate.targets!r}.",
                    f"Choose different qubits for {gate_type} control and target.",
                ))
    elif gate_type == "ECR":
        if gate.controls or len(gate.targets) != 2 or len(set(gate.targets)) != 2:
            issues.append(_error("INVALID_ECR_OPERANDS", "ECR requires two ordered target qubits.",
                                 f"Received targets={gate.targets!r}.",
                                 "Choose two different qubits in the backend-declared direction."))
    elif gate_type == "CCX":
        controls = list(gate.controls or [])
        if len(controls) != 2:
            issues.append(_error(
                "CCX_REQUIRES_TWO_CONTROLS",
                "CCX requires exactly two control qubits.",
                f"Received controls={controls!r}.",
                "Set exactly two control qubits for CCX.",
            ))
        if len(gate.targets) != 1:
            issues.append(_error(
                "CCX_REQUIRES_TARGET",
                "CCX requires exactly one target qubit.",
                f"Received targets={gate.targets!r}.",
                "Set exactly one target qubit for CCX.",
            ))
        if len(controls) == 2 and len(gate.targets) == 1 and len({*controls, gate.targets[0]}) != 3:
            issues.append(_error(
                "CCX_QUBITS_MUST_DIFFER",
                "CCX controls and target must be different qubits.",
                f"Received controls={controls!r}, targets={gate.targets!r}.",
                "Choose three different qubits for CCX.",
            ))
    elif gate_type == "SWAP":
        if gate.controls:
            issues.append(_error(
                "SWAP_REJECTS_CONTROLS",
                "SWAP does not accept control qubits.",
                f"Received controls={gate.controls!r}.",
                "Represent both SWAP operands as targets.",
            ))
        if len(gate.targets) != 2:
            issues.append(_error(
                "SWAP_REQUIRES_TWO_TARGETS",
                "SWAP requires exactly two target qubits.",
                f"Received targets={gate.targets!r}.",
                "Choose exactly two target qubits for SWAP.",
            ))
        elif gate.targets[0] == gate.targets[1]:
            issues.append(_error(
                "SWAP_TARGETS_MUST_DIFFER",
                "SWAP target qubits must be different.",
                f"Received targets={gate.targets!r}.",
                "Choose two different target qubits for SWAP.",
            ))
    elif gate_type in {"QFT", "ORACLE"}:
        if gate.controls:
            issues.append(_error(
                f"{gate_type}_REJECTS_CONTROLS",
                f"{gate_type} does not accept control qubits.",
                f"Received controls={gate.controls!r}.",
                f"List every {gate_type} qubit as a target instead.",
            ))
        if len(set(gate.targets)) != len(gate.targets):
            issues.append(_error(
                f"{gate_type}_TARGETS_MUST_DIFFER",
                f"{gate_type} target qubits must all be different.",
                f"Received targets={gate.targets!r}.",
                f"List each {gate_type} qubit exactly once, most significant first.",
            ))
        if gate_type == "ORACLE" and gate.targets:
            issues.extend(_oracle_marked_index_issues(gate))

    return issues


def _oracle_marked_index_issues(gate: GateOperation) -> list[ValidationIssue]:
    value = float((gate.params or {}).get("marked_index", 0.0))
    register_size = len(set(gate.targets))
    if value != int(value):
        return [_error(
            "ORACLE_MARKED_INDEX_NOT_INTEGER",
            "Oracle marked_index must be an integer.",
            f"Received marked_index={value!r}.",
            "Choose a whole basis-state index for the oracle to mark.",
        )]
    if not 0 <= int(value) < 2 ** register_size:
        return [_error(
            "ORACLE_MARKED_INDEX_OUT_OF_RANGE",
            "Oracle marked_index is outside the register range.",
            (
                f"Received marked_index={int(value)}; the register holds "
                f"{register_size} qubits, so it must be 0..{2 ** register_size - 1}."
            ),
            "Pick a basis state the oracle register can represent.",
        )]
    return []


def validate_gate_placement(
    logical_qubits: int,
    columns: Sequence[GateColumn],
    step: int,
    gate: GateOperation,
    classical_bits: int = 0,
) -> list[ValidationIssue]:
    """Validate a gate placement against existing gates in one step."""

    issues = validate_gate_for_circuit(logical_qubits, gate, classical_bits)
    candidate_qubits = _gate_qubits(gate)

    for column in columns:
        if column.step != step:
            continue
        for existing_gate in column.gates:
            overlap = candidate_qubits.intersection(_gate_qubits(existing_gate))
            if overlap:
                issues.append(_error(
                    "CELL_ALREADY_OCCUPIED",
                    "A qubit already has an operation in this step.",
                    (
                        f"Step {step} already uses qubit(s) "
                        f"{sorted(overlap)}."
                    ),
                    "Choose another step or remove the existing gate first.",
                ))

    return issues


def _validate_circuit(
    logical_qubits: int,
    classical_bits: int,
    columns: Iterable[GateColumn],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for column in columns:
        occupied: dict[int, GateOperation] = {}
        for gate in column.gates:
            issues.extend(validate_gate_for_circuit(logical_qubits, gate, classical_bits))
            for qubit in _gate_qubits(gate):
                if qubit in occupied:
                    issues.append(_error(
                        "CELL_ALREADY_OCCUPIED",
                        "A qubit has multiple operations in the same step.",
                        (
                            f"Step {column.step} has multiple operations "
                            f"touching qubit {qubit}."
                        ),
                        "Move one of the gates to a different step.",
                    ))
                occupied[qubit] = gate

    return issues


def _gate_qubits(gate: GateOperation) -> set[int]:
    return set(gate.targets).union(gate.controls or [])


def _is_qubit_in_range(index: int, logical_qubits: int) -> bool:
    return 0 <= index < logical_qubits


def _error(
    code: str,
    message: str,
    detail: str | None = None,
    suggestion: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        level="error",
        code=code,
        message=message,
        detail=detail,
        suggestion=suggestion,
    )
