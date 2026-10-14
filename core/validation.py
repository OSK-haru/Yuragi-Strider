"""Validation and numerical diagnostics for the normalized core API."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

from core.backend_boundary import SUPPORTED_SIMULATION_BACKENDS
from core.capabilities import (
    DEFAULT_SIMULATION_MODEL,
    MAX_LOGICAL_QUBITS,
    SUPPORTED_GATES,
    SUPPORTED_SIMULATION_MODELS,
    normalize_gate_type,
)
from core.errors import ValidationIssue
from core.physical_environment import (
    INPUT_MODE_NORMALIZED,
    INPUT_MODE_PHYSICAL,
    SUPPORTED_ENVIRONMENT_MODELS,
    SUPPORTED_INPUT_MODES,
    UNIFIED_ENVIRONMENT_MODEL,
)
from core.results import SimulationConfig, SimulationResult
from core.simulation_backends import registered_simulation_models


BLOCKING_LEVELS = {"error", "fatal"}
FIDELITY_RANGE_TOL = 1e-10
PURITY_RANGE_TOL = 1e-10
PROBABILITY_SUM_TOL = 1e-8
PROBABILITY_NEGATIVE_TOL = 1e-8
# The API still restricts users to the first four; +i/-i exist for internal
# process characterization (see core.process_characterization).
SUPPORTED_INITIAL_STATES = frozenset({"0", "1", "+", "-", "+i", "-i"})


def validate_simulation_config(config: SimulationConfig) -> list[ValidationIssue]:
    """Return standardized issues for a simulation configuration."""

    issues: list[ValidationIssue] = []
    circuit = config.circuit
    environment = config.environment

    logical_qubits = _as_number(circuit.logical_qubits)
    if not _is_finite_number(logical_qubits) or int(logical_qubits) != logical_qubits:
        issues.append(_error(
            "INVALID_LOGICAL_QUBITS",
            "logical_qubits must be an integer.",
            f"Received logical_qubits={circuit.logical_qubits!r}",
            f"Set logical_qubits to an integer from 1 to {MAX_LOGICAL_QUBITS}.",
        ))
    else:
        qubit_count = int(logical_qubits)
        if qubit_count < 1:
            issues.append(_error(
                "INVALID_LOGICAL_QUBITS",
                "logical_qubits must be at least 1.",
                f"Received logical_qubits={qubit_count}",
                f"Set logical_qubits to a value in the range [1, {MAX_LOGICAL_QUBITS}].",
            ))
        if qubit_count > MAX_LOGICAL_QUBITS:
            issues.append(_error(
                "TOO_MANY_LOGICAL_QUBITS",
                f"logical_qubits must be {MAX_LOGICAL_QUBITS} or less.",
                f"Received logical_qubits={qubit_count}",
                (
                    f"Use at most {MAX_LOGICAL_QUBITS} logical qubits for "
                    "the lightweight simulator."
                ),
            ))

    if len(circuit.initial_states) != circuit.logical_qubits:
        issues.append(_error(
            "INITIAL_STATE_COUNT_MISMATCH",
            "initial_states must match logical_qubits.",
            (
                f"Received {len(circuit.initial_states)} initial states for "
                f"{circuit.logical_qubits} logical qubits."
            ),
            "Provide exactly one initial state per logical qubit.",
        ))

    if circuit.classical_bits < 0 or circuit.classical_bits > 32:
        issues.append(_error(
            "INVALID_CLASSICAL_BITS",
            "classical_bits must be between 0 and 32.",
            f"Received classical_bits={circuit.classical_bits!r}",
            "Set classical_bits to the number of available classical register bits.",
        ))

    for column in circuit.columns:
        for gate in column.gates:
            gate_type = normalize_gate_type(gate.type)
            if gate_type not in SUPPORTED_GATES:
                issues.append(_error(
                    "UNSUPPORTED_GATE",
                    "Gate type is not supported.",
                    f"Received gate type={gate.type!r} at step {column.step}.",
                    "Use one of I, H, X, Y, Z, S, T, RX, RY, RZ, CNOT, CZ, CP, CCX, SWAP, QFT, ORACLE, or Measure.",
                ))
            elif gate_type in {"I", "H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ", "MEASURE"} and len(gate.targets) != 1:
                issues.append(_error(
                    "GATE_REQUIRES_SINGLE_TARGET",
                    f"{gate.type} requires exactly one target qubit.",
                    (
                        f"Gate {gate.type} at step {column.step} has "
                        f"targets={gate.targets!r}."
                    ),
                    "Set exactly one target qubit for this gate.",
                ))

            for target in gate.targets:
                if not _is_qubit_index_in_range(target, circuit.logical_qubits):
                    issues.append(_error(
                        "GATE_TARGET_OUT_OF_RANGE",
                        "Gate target is outside the logical qubit range.",
                        (
                            f"Gate {gate.type} at step {column.step} targets "
                            f"qubit {target}; logical_qubits={circuit.logical_qubits}."
                        ),
                        "Set targets to qubit indices from 0 to logical_qubits - 1.",
                    ))

            for control in gate.controls or []:
                if not _is_qubit_index_in_range(control, circuit.logical_qubits):
                    issues.append(_error(
                        "GATE_CONTROL_OUT_OF_RANGE",
                        "Gate control is outside the logical qubit range.",
                        (
                            f"Gate {gate.type} at step {column.step} controls "
                            f"qubit {control}; logical_qubits={circuit.logical_qubits}."
                        ),
                        "Set controls to qubit indices from 0 to logical_qubits - 1.",
                    ))

            if gate_type == "CNOT":
                if len(gate.controls or []) < 1:
                    issues.append(_error(
                        f"{gate_type}_REQUIRES_CONTROL",
                        f"{gate_type} requires at least one control qubit.",
                        (
                            f"Gate {gate_type} at step {column.step} has controls "
                            f"{gate.controls!r}."
                        ),
                        f"Set one or more control qubits for {gate_type}.",
                    ))
                if len(gate.targets) != 1:
                    issues.append(_error(
                        f"{gate_type}_REQUIRES_TARGET",
                        f"{gate_type} requires exactly one target qubit.",
                        (
                            f"Gate {gate_type} at step {column.step} has targets "
                            f"{gate.targets!r}."
                        ),
                        f"Set exactly one target qubit for {gate_type}.",
                    ))
                for control in gate.controls or []:
                    if control in gate.targets:
                        issues.append(_error(
                            f"{gate_type}_CONTROL_EQUALS_TARGET",
                            f"{gate_type} control and target must be different qubits.",
                            (
                                f"Gate {gate_type} at step {column.step} uses qubit "
                                f"{control} as both control and target."
                            ),
                            f"Choose different qubit indices for {gate_type} control and target.",
                        ))
                if len(set(gate.controls or [])) != len(gate.controls or []):
                    issues.append(_error(
                        "CNOT_CONTROLS_MUST_DIFFER",
                        "CNOT control qubits must all be different.",
                        f"Gate CNOT at step {column.step} has controls {gate.controls!r}.",
                        "Choose each control qubit once.",
                    ))
            elif gate_type in {"CZ", "CP"}:
                if len(gate.controls or []) != 1:
                    issues.append(_error(
                        f"{gate_type}_REQUIRES_CONTROL",
                        f"{gate_type} requires exactly one control qubit.",
                        f"Gate {gate_type} at step {column.step} has controls {gate.controls!r}.",
                        f"Set exactly one control qubit for {gate_type}.",
                    ))
                if len(gate.targets) != 1:
                    issues.append(_error(
                        f"{gate_type}_REQUIRES_TARGET",
                        f"{gate_type} requires exactly one target qubit.",
                        f"Gate {gate_type} at step {column.step} has targets {gate.targets!r}.",
                        f"Set exactly one target qubit for {gate_type}.",
                    ))
                for control in gate.controls or []:
                    if control in gate.targets:
                        issues.append(_error(
                            f"{gate_type}_CONTROL_EQUALS_TARGET",
                            f"{gate_type} control and target must be different qubits.",
                            f"Gate {gate_type} at step {column.step} uses qubit {control} as both control and target.",
                            f"Choose different qubit indices for {gate_type} control and target.",
                        ))
            elif gate_type == "CCX":
                controls = list(gate.controls or [])
                if len(controls) != 2:
                    issues.append(_error(
                        "CCX_REQUIRES_TWO_CONTROLS",
                        "CCX requires exactly two control qubits.",
                        f"Gate CCX at step {column.step} has controls {controls!r}.",
                        "Set two different control qubits for CCX.",
                    ))
                if len(gate.targets) != 1:
                    issues.append(_error(
                        "CCX_REQUIRES_TARGET",
                        "CCX requires exactly one target qubit.",
                        f"Gate CCX at step {column.step} has targets {gate.targets!r}.",
                        "Set one target qubit for CCX.",
                    ))
                if len(controls) == 2 and len(gate.targets) == 1 and len({*controls, gate.targets[0]}) != 3:
                    issues.append(_error(
                        "CCX_QUBITS_MUST_DIFFER",
                        "CCX controls and target must be different qubits.",
                        f"Gate CCX at step {column.step} uses controls={controls!r}, targets={gate.targets!r}.",
                        "Choose three different qubit indices for CCX.",
                    ))
            elif gate_type == "SWAP":
                if gate.controls:
                    issues.append(_error(
                        "SWAP_REJECTS_CONTROLS",
                        "SWAP does not accept control qubits.",
                        f"Gate SWAP at step {column.step} has controls {gate.controls!r}.",
                        "Represent both SWAP operands as targets.",
                    ))
                if len(gate.targets) != 2:
                    issues.append(_error(
                        "SWAP_REQUIRES_TWO_TARGETS",
                        "SWAP requires exactly two target qubits.",
                        f"Gate SWAP at step {column.step} has targets {gate.targets!r}.",
                        "Set two different target qubits for SWAP.",
                    ))
                elif gate.targets[0] == gate.targets[1]:
                    issues.append(_error(
                        "SWAP_TARGETS_MUST_DIFFER",
                        "SWAP target qubits must be different.",
                        f"Gate SWAP at step {column.step} has targets {gate.targets!r}.",
                        "Choose two different target qubits for SWAP.",
                    ))
            elif gate_type in {"QFT", "ORACLE"}:
                if gate.controls:
                    issues.append(_error(
                        f"{gate_type}_REJECTS_CONTROLS",
                        f"{gate_type} does not accept control qubits.",
                        f"Gate {gate_type} at step {column.step} has controls {gate.controls!r}.",
                        f"List every {gate_type} qubit as a target instead.",
                    ))
                if not gate.targets:
                    issues.append(_error(
                        f"{gate_type}_REQUIRES_TARGET",
                        f"{gate_type} requires at least one target qubit.",
                        f"Gate {gate_type} at step {column.step} has targets {gate.targets!r}.",
                        f"List the {gate_type} register qubits, most significant first.",
                    ))
                if len(set(gate.targets)) != len(gate.targets):
                    issues.append(_error(
                        f"{gate_type}_TARGETS_MUST_DIFFER",
                        f"{gate_type} target qubits must all be different.",
                        f"Gate {gate_type} at step {column.step} has targets {gate.targets!r}.",
                        f"List each {gate_type} qubit exactly once, most significant first.",
                    ))
                if gate_type == "ORACLE" and gate.targets:
                    marked = float((gate.params or {}).get("marked_index", 0.0))
                    register_size = len(set(gate.targets))
                    if marked != int(marked):
                        issues.append(_error(
                            "ORACLE_MARKED_INDEX_NOT_INTEGER",
                            "Oracle marked_index must be an integer.",
                            f"Gate ORACLE at step {column.step} has marked_index={marked!r}.",
                            "Choose a whole basis-state index for the oracle to mark.",
                        ))
                    elif not 0 <= int(marked) < 2 ** register_size:
                        issues.append(_error(
                            "ORACLE_MARKED_INDEX_OUT_OF_RANGE",
                            "Oracle marked_index is outside the register range.",
                            (
                                f"Gate ORACLE at step {column.step} marks {int(marked)} "
                                f"with a {register_size}-qubit register."
                            ),
                            "Pick a basis state the oracle register can represent.",
                        ))

            for classical_target in gate.classical_targets or []:
                if classical_target >= circuit.classical_bits:
                    issues.append(_error(
                        "CLASSICAL_TARGET_OUT_OF_RANGE",
                        "Measurement classical target is outside the register.",
                        (
                            f"Gate {gate.type} at step {column.step} targets classical "
                            f"bit {classical_target}; classical_bits={circuit.classical_bits}."
                        ),
                        "Increase classical_bits or choose an existing classical bit.",
                    ))
            if gate.classical_targets and gate_type != "MEASURE":
                issues.append(_error(
                    "CLASSICAL_TARGET_REQUIRES_MEASURE",
                    "classical_targets are only valid for MEASURE gates.",
                    f"Gate {gate.type} at step {column.step} has classical_targets.",
                    "Attach classical_targets only to MEASURE.",
                ))
            if gate_type == "MEASURE" and gate.classical_targets and len(
                gate.classical_targets
            ) != len(gate.targets):
                issues.append(_error(
                    "MEASURE_CLASSICAL_TARGET_COUNT_MISMATCH",
                    "Measurement classical targets must match measured targets.",
                    (
                        f"MEASURE at step {column.step} has {len(gate.targets)} "
                        f"quantum targets and {len(gate.classical_targets)} classical targets."
                    ),
                    "Provide one classical target per measured qubit.",
                ))
            if gate.condition is not None and gate.condition.bit >= circuit.classical_bits:
                issues.append(_error(
                    "CLASSICAL_CONDITION_OUT_OF_RANGE",
                    "Classical condition bit is outside the register.",
                    (
                        f"Gate {gate.type} at step {column.step} reads classical bit "
                        f"{gate.condition.bit}; classical_bits={circuit.classical_bits}."
                    ),
                    "Increase classical_bits or choose an existing classical bit.",
                ))

    for index, initial_state in enumerate(circuit.initial_states):
        if initial_state not in SUPPORTED_INITIAL_STATES:
            issues.append(_error(
                "UNSUPPORTED_INITIAL_STATE",
                "Initial state is not supported.",
                f"Received initial_states[{index}]={initial_state!r}.",
                "Use one of '0', '1', '+', '-', '+i', or '-i'.",
            ))

    if environment.model not in SUPPORTED_ENVIRONMENT_MODELS:
        issues.append(_error(
            "UNSUPPORTED_ENVIRONMENT_MODEL",
            "Environment model is not supported.",
            f"Received model={environment.model!r}",
            f"Use {UNIFIED_ENVIRONMENT_MODEL!r}.",
        ))
    if environment.input_mode not in SUPPORTED_INPUT_MODES:
        issues.append(_error(
            "UNSUPPORTED_ENVIRONMENT_INPUT_MODE",
            "Environment input_mode is not supported.",
            f"Received input_mode={environment.input_mode!r}",
            "Use 'normalized' or 'physical'.",
        ))
    elif environment.input_mode == INPUT_MODE_NORMALIZED:
        if environment.per_qubit:
            issues.append(_error(
                "PER_QUBIT_ENVIRONMENT_REQUIRES_PHYSICAL_MODE",
                "Per-qubit T1/Tphi settings require physical input mode.",
                "Normalized mode does not expose calibrated coherence times.",
                "Use physical input mode or remove environment.per_qubit.",
            ))
        issues.extend(_range_issue(
            environment.temperature,
            "temperature",
            "INVALID_TEMPERATURE",
            0.0,
            1.0,
        ))
        issues.extend(_range_issue(
            environment.magnetic_field,
            "magnetic_field",
            "INVALID_MAGNETIC_FIELD",
            0.0,
            1.0,
        ))
        issues.extend(_range_issue(
            environment.noise_level,
            "noise_level",
            "INVALID_NOISE_LEVEL",
            0.0,
            1.0,
        ))
    elif environment.input_mode == INPUT_MODE_PHYSICAL:
        issues.extend(_range_issue(
            environment.device_quality,
            "device_quality",
            "INVALID_DEVICE_QUALITY",
            0.0,
            1.0,
        ))
        if not _is_non_negative_finite(environment.temperature_mk):
            issues.append(_error(
                "INVALID_TEMPERATURE_MK",
                "temperature_mk must be non-negative.",
                f"Received temperature_mk={environment.temperature_mk!r}",
                "Set temperature_mk to zero or a positive physical temperature.",
            ))
        if not _is_non_negative_finite(environment.flux_noise_phi0):
            issues.append(_error(
                "INVALID_FLUX_NOISE_PHI0",
                "flux_noise_phi0 must be non-negative.",
                f"Received flux_noise_phi0={environment.flux_noise_phi0!r}",
                "Set flux_noise_phi0 to zero or a positive amplitude.",
            ))
        if not _is_positive_finite(environment.qubit_frequency_ghz):
            issues.append(_error(
                "INVALID_QUBIT_FREQUENCY_GHZ",
                "qubit_frequency_ghz must be greater than 0.",
                f"Received qubit_frequency_ghz={environment.qubit_frequency_ghz!r}",
                "Set qubit_frequency_ghz to a positive frequency.",
            ))
        if not _is_positive_finite(environment.t1_max_us):
            issues.append(_error(
                "INVALID_T1_MAX_US",
                "t1_max_us must be greater than 0.",
                f"Received t1_max_us={environment.t1_max_us!r}",
                "Set t1_max_us to a positive coherence-time maximum.",
            ))
        if not _is_positive_finite(environment.tphi_max_us):
            issues.append(_error(
                "INVALID_TPHI_MAX_US",
                "tphi_max_us must be greater than 0.",
                f"Received tphi_max_us={environment.tphi_max_us!r}",
                "Set tphi_max_us to a positive dephasing-time maximum.",
            ))
        if environment.per_qubit and len(environment.per_qubit) != circuit.logical_qubits:
            issues.append(_error(
                "PER_QUBIT_ENVIRONMENT_COUNT_MISMATCH",
                "Per-qubit coherence settings must match logical_qubits.",
                (
                    f"Received {len(environment.per_qubit)} entries for "
                    f"{circuit.logical_qubits} logical qubits."
                ),
                "Provide one T1/Tphi pair per logical qubit or use the shared values.",
            ))
    if environment.observation_strength is not None:
        issues.extend(_range_issue(
            environment.observation_strength,
            "observation_strength",
            "INVALID_OBSERVATION_STRENGTH",
            0.0,
            1.0,
        ))
    if environment.observation_frequency is not None:
        frequency = _as_number(environment.observation_frequency)
        if not _is_finite_number(frequency) or frequency < 0.0:
            issues.append(_error(
                "INVALID_OBSERVATION_FREQUENCY",
                "observation_frequency must be non-negative.",
                f"Received observation_frequency={environment.observation_frequency!r}",
                "Set observation_frequency to None or a value greater than or equal to 0.",
            ))

    duration = _as_number(config.duration_us)
    if not _is_finite_number(duration) or duration <= 0.0:
        issues.append(_error(
            "INVALID_DURATION_US",
            "duration_us must be greater than 0.",
            f"Received duration_us={config.duration_us!r}",
            "Set duration_us to a positive value.",
        ))

    time_steps = _as_number(config.time_steps)
    if (
        not _is_finite_number(time_steps)
        or int(time_steps) != time_steps
        or time_steps < 2
    ):
        issues.append(_error(
            "INVALID_TIME_STEPS",
            "time_steps must be an integer greater than or equal to 2.",
            f"Received time_steps={config.time_steps!r}",
            "Set time_steps to at least 2.",
        ))

    threshold = _as_number(config.fidelity_threshold)
    if not _is_finite_number(threshold) or not 0.0 <= threshold <= 1.0:
        issues.append(_error(
            "INVALID_FIDELITY_THRESHOLD",
            "fidelity_threshold must be between 0.0 and 1.0.",
            f"Received fidelity_threshold={config.fidelity_threshold!r}",
            "Set fidelity_threshold to a value in the range [0.0, 1.0].",
        ))

    if config.model not in _supported_simulation_models():
        issues.append(_error(
            "UNSUPPORTED_MODEL",
            "Simulation model is not supported.",
            f"Received model={config.model!r}",
            f"Set model to {DEFAULT_SIMULATION_MODEL!r}.",
        ))

    if config.simulation_backend not in SUPPORTED_SIMULATION_BACKENDS:
        issues.append(_error(
            "UNSUPPORTED_SIMULATION_BACKEND",
            "Simulation backend is not supported.",
            f"Received simulation_backend={config.simulation_backend!r}",
            "Use 'python_dense' or 'rust_dense_preview'.",
        ))

    return issues


def diagnose_simulation_result(result: SimulationResult) -> list[ValidationIssue]:
    """Return numerical diagnostics for a simulation result."""

    issues: list[ValidationIssue] = []

    if not result.times:
        issues.append(_error(
            "EMPTY_TIMES",
            "times must not be empty.",
            "Received an empty times series.",
            "Return at least one simulation time sample.",
        ))
    if not result.fidelity:
        issues.append(_error(
            "EMPTY_FIDELITY",
            "fidelity must not be empty.",
            "Received an empty fidelity series.",
            "Return one fidelity value per time sample.",
        ))
    if not result.purity:
        issues.append(_error(
            "EMPTY_PURITY",
            "purity must not be empty.",
            "Received an empty purity series.",
            "Return one purity value per time sample.",
        ))

    if not (len(result.times) == len(result.fidelity) == len(result.purity)):
        issues.append(_error(
            "SERIES_LENGTH_MISMATCH",
            "times, fidelity, and purity must have the same length.",
            (
                f"Received lengths: times={len(result.times)}, "
                f"fidelity={len(result.fidelity)}, purity={len(result.purity)}."
            ),
            "Return one fidelity and purity value for each time sample.",
        ))

    issues.extend(_finite_series_issues(result.times, "times", "TIME_VALUE_NOT_FINITE"))
    issues.extend(_finite_series_issues(
        result.fidelity,
        "fidelity",
        "FIDELITY_VALUE_NOT_FINITE",
    ))
    issues.extend(_finite_series_issues(
        result.purity,
        "purity",
        "PURITY_VALUE_NOT_FINITE",
    ))
    issues.extend(_probability_series_issues(
        result.fidelity,
        "fidelity",
        "FIDELITY_OUT_OF_RANGE",
        FIDELITY_RANGE_TOL,
    ))
    issues.extend(_probability_series_issues(
        result.purity,
        "purity",
        "PURITY_OUT_OF_RANGE",
        PURITY_RANGE_TOL,
    ))

    effective_time = result.effective_operation_time_us
    if effective_time is not None:
        value = _as_number(effective_time)
        if not _is_finite_number(value) or value < 0.0:
            issues.append(_error(
                "INVALID_EFFECTIVE_OPERATION_TIME",
                "effective_operation_time_us must be None or non-negative.",
                f"Received effective_operation_time_us={effective_time!r}",
                "Use None or a value greater than or equal to 0.",
            ))

    if result.output_probabilities:
        probability_sum = 0.0
        for state, probability in result.output_probabilities.items():
            value = _as_number(probability)
            if not _is_finite_number(value):
                issues.append(_fatal(
                    "OUTPUT_PROBABILITY_NOT_FINITE",
                    "Output probability contains NaN or infinity.",
                    f"Received output_probabilities[{state!r}]={probability!r}",
                    "Return only finite probability values.",
                ))
                continue
            probability_sum += value
            if value < -PROBABILITY_NEGATIVE_TOL:
                issues.append(_error(
                    "NEGATIVE_OUTPUT_PROBABILITY",
                    "Output probability is negative beyond tolerance.",
                    f"Received output_probabilities[{state!r}]={probability!r}",
                    "Return output probabilities greater than or equal to 0.",
                ))

        if abs(probability_sum - 1.0) > PROBABILITY_SUM_TOL:
            issues.append(_warning(
                "OUTPUT_PROBABILITY_SUM_MISMATCH",
                "Output probabilities should sum to 1.",
                f"Received probability sum={probability_sum!r}",
                "Normalize output probabilities before exposing them.",
            ))

    return issues


def has_blocking_issues(issues: Iterable[ValidationIssue]) -> bool:
    """Return True when issues should prevent physics execution."""

    return any(issue.level in BLOCKING_LEVELS for issue in issues)


def _range_issue(
    value: Any,
    field_name: str,
    code: str,
    minimum: float,
    maximum: float,
) -> list[ValidationIssue]:
    number = _as_number(value)
    if _is_finite_number(number) and minimum <= number <= maximum:
        return []
    return [_error(
        code,
        f"{field_name} must be between {minimum} and {maximum}.",
        f"Received {field_name}={value!r}",
        f"Set {field_name} to a value in the range [{minimum}, {maximum}].",
    )]


def _supported_simulation_models() -> set[str]:
    return set(SUPPORTED_SIMULATION_MODELS).union(registered_simulation_models())


def _finite_series_issues(
    values: Sequence[Any],
    name: str,
    code: str,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, value in enumerate(values):
        number = _as_number(value)
        if not _is_finite_number(number):
            issues.append(_fatal(
                code,
                f"{name} contains NaN or infinity.",
                f"Received {name}[{index}]={value!r}",
                f"Ensure all {name} values are finite numbers.",
            ))
    return issues


def _probability_series_issues(
    values: Sequence[Any],
    name: str,
    code: str,
    tolerance: float,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for index, value in enumerate(values):
        number = _as_number(value)
        if not _is_finite_number(number):
            continue
        if number < -tolerance or number > 1.0 + tolerance:
            issues.append(_error(
                code,
                f"{name} values must stay within [0.0, 1.0].",
                f"Received {name}[{index}]={value!r}",
                f"Check the simulation path that produced {name}.",
            ))
    return issues


def _is_qubit_index_in_range(index: Any, logical_qubits: int) -> bool:
    number = _as_number(index)
    return (
        _is_finite_number(number)
        and int(number) == number
        and 0 <= int(number) < logical_qubits
    )


def _as_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _is_finite_number(value: float) -> bool:
    return math.isfinite(value)


def _is_positive_finite(value: Any) -> bool:
    number = _as_number(value)
    return _is_finite_number(number) and number > 0.0


def _is_non_negative_finite(value: Any) -> bool:
    number = _as_number(value)
    return _is_finite_number(number) and number >= 0.0


def _error(
    code: str,
    message: str,
    detail: str | None = None,
    suggestion: str | None = None,
) -> ValidationIssue:
    return ValidationIssue("error", code, message, detail, suggestion)


def _fatal(
    code: str,
    message: str,
    detail: str | None = None,
    suggestion: str | None = None,
) -> ValidationIssue:
    return ValidationIssue("fatal", code, message, detail, suggestion)


def _warning(
    code: str,
    message: str,
    detail: str | None = None,
    suggestion: str | None = None,
) -> ValidationIssue:
    return ValidationIssue("warning", code, message, detail, suggestion)
