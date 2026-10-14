"""Adapters from core simulation results to React UI response dictionaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.circuit_model import GateColumn, GateOperation
from core.gates import column_duration_us
from core.gates import reduced_density_matrix
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import run_simulation
from core.state_snapshots import serialize_complex_matrix, serialize_state_snapshots


def simulation_result_to_ui_response(result: SimulationResult) -> dict[str, Any]:
    """Convert a SimulationResult into the minimal JSON shape used by React."""

    if not isinstance(result, SimulationResult):
        raise TypeError("result must be a SimulationResult")

    warnings = [str(warning) for warning in result.warnings]
    timeline, timeline_warnings = _timeline(result)
    warnings.extend(timeline_warnings)

    state_snapshots, snapshot_serialization_ms = serialize_state_snapshots(
        result.state_snapshots
    )
    ideal_timeline, ideal_state_snapshots = _ideal_reference_data(result)
    circuit_probes = _circuit_probes(
        result.config,
        state_snapshots,
        ideal_state_snapshots,
    )
    diagnostics = _diagnostics_response(result)
    diagnostics["state_snapshot_serialization_ms"] = _safe_float(
        round(snapshot_serialization_ms, 3)
    )
    state_transfer = _state_transfer_response(result.config, state_snapshots, ideal_state_snapshots)

    return {
        "circuit": _circuit_response(result.config),
        "parameters": _parameters_response(result.config),
        "rates": _rates_response(result),
        "diagnostics": diagnostics,
        "summary": _summary_response(result),
        "timeline": timeline,
        "physical_timeline": dict(result.physical_timeline),
        "output_probabilities": _output_probabilities(result),
        "measurement": _measurement_response(result),
        "state_snapshots": state_snapshots,
        "circuit_probes": circuit_probes,
        "state_transfer": state_transfer,
        "run": {
            **_run_response(result),
            "comparison": {
                "ideal_timeline": ideal_timeline,
                "ideal_state_snapshots": ideal_state_snapshots,
            },
        },
        "warnings": warnings,
        "issues": [_issue_to_dict(issue) for issue in result.issues],
    }


def _state_transfer_response(
    config: SimulationConfig,
    noisy_snapshots: list[dict[str, Any]],
    ideal_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Expose declared Message/Receive roles and actual snapshot checkpoints."""
    message = None
    for column_index, column in enumerate(config.circuit.columns):
        for gate_index, gate in enumerate(column.gates):
            if str(gate.type).upper() == "MESSAGE":
                message = {"column_index": column_index, "gate_index": gate_index, "qubit": int(gate.targets[0]), "operation": "MESSAGE"}
                break
        if message is not None:
            break
    annotation = next((item for item in config.circuit.annotations if item.kind == "RECEIVED"), None)
    if message is None or annotation is None:
        return {"schema_version": "state_transfer_v1", "available": False,
                "reason": "requires one physical MESSAGE gate and one explicit RECEIVED annotation",
                "message": None, "receive": None, "checkpoints": []}
    receive_qubit = int(annotation.qubits[0]) if annotation.qubits else None
    checkpoints = []
    for role, column_index, qubit in (("message", message["column_index"], message["qubit"]), ("receive", annotation.column_index, receive_qubit)):
        noisy_index = _snapshot_index_for_column(noisy_snapshots, column_index)
        ideal_index = _snapshot_index_for_column(ideal_snapshots, column_index)
        snapshot = noisy_snapshots[noisy_index] if noisy_index is not None else None
        noisy_reduced = _reduced_snapshot(snapshot, qubit, config.circuit.logical_qubits)
        ideal_snapshot = ideal_snapshots[ideal_index] if ideal_index is not None else None
        ideal_reduced = _reduced_snapshot(ideal_snapshot, qubit, config.circuit.logical_qubits)
        checkpoints.append({"role": role, "column_index": int(column_index), "qubit": qubit,
                           "noisy_snapshot_index": noisy_index, "ideal_snapshot_index": ideal_index,
                           "time_us": None if snapshot is None else _safe_float(snapshot.get("time_us", 0.0)),
                           "available": noisy_index is not None,
                           "noisy_reduced_density_matrix": serialize_complex_matrix(noisy_reduced) if noisy_reduced is not None else None,
                           "ideal_reduced_density_matrix": serialize_complex_matrix(ideal_reduced) if ideal_reduced is not None else None,
                           "delta_rho_ideal_frobenius": _matrix_distance(noisy_reduced, ideal_reduced)})
    noisy_message, noisy_receive = (checkpoints[0], checkpoints[1])
    ideal_message, ideal_receive = (checkpoints[0], checkpoints[1])
    return {"schema_version": "state_transfer_v1", "available": True, "reason": None,
            "message": message,
            "receive": {"annotation_id": annotation.id, "source_id": annotation.source_id,
                        "column_index": int(annotation.column_index), "qubit": receive_qubit},
            "checkpoints": checkpoints,
            "metrics": {
                "noisy_message_to_receive_frobenius": _matrix_distance(
                    _deserialize_matrix(noisy_message.get("noisy_reduced_density_matrix")),
                    _deserialize_matrix(noisy_receive.get("noisy_reduced_density_matrix")),
                ),
                "ideal_message_to_receive_frobenius": _matrix_distance(
                    _deserialize_matrix(ideal_message.get("ideal_reduced_density_matrix")),
                    _deserialize_matrix(ideal_receive.get("ideal_reduced_density_matrix")),
                ),
            }}


def _reduced_snapshot(snapshot: dict[str, Any] | None, qubit: int | None, qubit_count: int) -> tuple[tuple[complex, ...], ...] | None:
    if snapshot is None or qubit is None or qubit < 0 or qubit >= qubit_count:
        return None
    matrix_data = snapshot.get("density_matrix")
    if not isinstance(matrix_data, Mapping):
        return None
    try:
        matrix = tuple(tuple(complex(real, imag) for real, imag in zip(row_real, row_imag))
                       for row_real, row_imag in zip(matrix_data["real"], matrix_data["imag"]))
        return reduced_density_matrix(matrix, qubit_count, qubit)
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _matrix_distance(left: tuple[tuple[complex, ...], ...] | None, right: tuple[tuple[complex, ...], ...] | None) -> float | None:
    if left is None or right is None or len(left) != len(right):
        return None
    total = 0.0
    for row_left, row_right in zip(left, right):
        if len(row_left) != len(row_right):
            return None
        total += sum(abs(a - b) ** 2 for a, b in zip(row_left, row_right))
    return _safe_float(total ** 0.5)


def _deserialize_matrix(data: Mapping[str, Any] | None) -> tuple[tuple[complex, ...], ...] | None:
    if data is None:
        return None
    try:
        return tuple(tuple(complex(real, imag) for real, imag in zip(row_real, row_imag))
                     for row_real, row_imag in zip(data["real"], data["imag"]))
    except (KeyError, TypeError):
        return None


def _snapshot_index_for_column(snapshots: list[dict[str, Any]], column_index: int) -> int | None:
    for index, snapshot in enumerate(snapshots):
        if snapshot.get("column_index") == column_index and str(snapshot.get("kind", "")) == "column_boundary":
            return index
    return None


def _circuit_probes(
    config: SimulationConfig,
    noisy_snapshots: list[dict[str, Any]],
    ideal_snapshots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose deterministic logical-boundary references without duplicating matrices."""

    ideal_by_key = {
        _probe_snapshot_key(snapshot): index
        for index, snapshot in enumerate(ideal_snapshots)
    }
    probes: list[dict[str, Any]] = []
    for index, snapshot in enumerate(noisy_snapshots):
        kind = str(snapshot.get("kind", ""))
        if kind == "initial":
            boundary = "before"
            column_index = None
            probe_id = "probe-before-column-0"
        elif kind == "column_boundary":
            column_index = snapshot.get("column_index")
            if column_index is None:
                continue
            column_index = int(column_index)
            boundary = "after"
            probe_id = f"probe-after-column-{column_index}"
        elif kind == "after_circuit":
            boundary = "completion"
            column_index = None
            probe_id = "probe-completion"
        elif kind == "final":
            boundary = "final"
            column_index = None
            probe_id = "probe-final"
        else:
            continue
        key = _probe_snapshot_key(snapshot)
        probes.append({
            "id": probe_id,
            "circuit_position": {
                "column_index": column_index,
                "boundary": boundary,
            },
            "noisy_snapshot_index": index,
            "ideal_snapshot_index": ideal_by_key.get(key),
            "time_us": _safe_float(snapshot.get("time_us", 0.0)),
        })
    # A final column boundary and ``after_circuit`` can share one timestamp;
    # snapshot de-duplication intentionally keeps only the higher-priority
    # semantic event. Alias that retained state back to the final logical
    # column without duplicating its density matrix.
    if probes and config.circuit.columns:
        last_column_index = len(config.circuit.columns) - 1
        has_last_column = any(
            probe["circuit_position"].get("column_index") == last_column_index
            for probe in probes
        )
        completion_probe = next(
            (probe for probe in probes if probe["circuit_position"].get("boundary") == "completion"),
            None,
        )
        if not has_last_column and completion_probe is not None:
            probes.insert(
                probes.index(completion_probe),
                {
                    **completion_probe,
                    "id": f"probe-after-column-{last_column_index}",
                    "circuit_position": {
                        "column_index": last_column_index,
                        "boundary": "after",
                    },
                },
            )
    return probes


def _probe_snapshot_key(snapshot: Mapping[str, Any]) -> tuple[str, int | None, float]:
    return (
        str(snapshot.get("kind", "")),
        None if snapshot.get("column_index") is None else int(snapshot["column_index"]),
        round(float(snapshot.get("time_us", 0.0)), 12),
    )


def _ideal_reference_data(
    result: SimulationResult,
) -> tuple[list[dict[str, float | None]], list[dict[str, Any]]]:
    """Run the same circuit with environment noise disabled for UI comparison."""

    if not result.times:
        return [], []

    config_data = result.config.to_dict()
    environment = dict(config_data.get("environment") or {})
    environment["ideal_reference"] = True
    physical = dict(environment.get("physical") or {})
    physical["ideal_reference"] = True
    environment["physical"] = physical
    config_data["environment"] = environment

    try:
        ideal_result = run_simulation(SimulationConfig.from_dict(config_data))
    except (TypeError, ValueError):
        return [], []

    ideal_timeline, _ = _timeline(ideal_result)
    ideal_snapshots, _ = serialize_state_snapshots(ideal_result.state_snapshots)
    return ideal_timeline, ideal_snapshots


def _rates_response(result: SimulationResult) -> dict[str, Any]:
    """Expose rate diagnostics with canonical names and one legacy read alias."""

    derived = result.derived_parameters
    gamma_down = _safe_float_or_none(derived.get("gamma_down_per_us"))
    legacy_gamma1 = _safe_float_or_none(derived.get("gamma1_per_us"))

    return {
        "gamma0_per_us": _safe_float_or_none(derived.get("gamma0_per_us")),
        "gamma_down_per_us": gamma_down,
        "gamma_up_per_us": _safe_float_or_none(derived.get("gamma_up_per_us")),
        "gamma_population_relaxation_per_us": _safe_float_or_none(
            derived.get("gamma_population_relaxation_per_us")
        ),
        "gamma_phi_per_us": _safe_float_or_none(derived.get("gamma_phi_per_us")),
        "t1_base_us": _safe_float_or_none(derived.get("t1_base_us")),
        "t1_effective_us": _safe_float_or_none(derived.get("t1_effective_us")),
        "tphi_base_us": _safe_float_or_none(derived.get("tphi_base_us")),
        "t2_effective_us": _safe_float_or_none(derived.get("t2_effective_us")),
        # Compatibility only: gamma1 historically meant the downward rate, not total T1 decay.
        "gamma1_per_us": legacy_gamma1 if legacy_gamma1 is not None else gamma_down,
        "gamma1_per_us_deprecation": (
            "Legacy alias for gamma_down_per_us; it is not the population relaxation rate."
        ),
        "per_qubit": list(derived.get("per_qubit") or []),
        "per_qubit_environment_enabled": bool(
            derived.get("per_qubit_environment_enabled", False)
        ),
    }


def _circuit_response(config: SimulationConfig) -> dict[str, Any]:
    circuit = config.circuit
    return {
        "qubit_count": int(circuit.logical_qubits),
        "classical_bit_count": int(circuit.classical_bits),
        "annotations": [annotation.to_dict() for annotation in circuit.annotations],
        "columns": [
            _column_response(column)
            for column in sorted(circuit.columns, key=lambda column: column.step)
        ],
    }


def _column_response(column: GateColumn) -> dict[str, Any]:
    return {
        "id": f"step-{column.step}",
        "step": int(column.step),
        "duration_us": _safe_float_or_none(column_duration_us(column)),
        "gates": [
            ui_gate
            for gate in column.gates
            for ui_gate in _gate_response_entries(gate)
        ],
    }


def _gate_response_entries(gate: GateOperation) -> list[dict[str, Any]]:
    gate_type = str(gate.type)
    normalized_type = gate_type.upper()

    if normalized_type in {"CNOT", "CZ", "CP", "CCX"}:
        entries: list[dict[str, Any]] = []
        for control in gate.controls or []:
            entries.append({
                "label": normalized_type,
                "type": gate_type,
                "qubits": [int(control)],
                "kind": "control",
                **_classical_gate_metadata(gate),
            })
        for target in gate.targets:
            entries.append({
                "label": normalized_type,
                "type": gate_type,
                "qubits": [int(target)],
                "kind": "target",
                **_classical_gate_metadata(gate),
            })
        return entries

    if normalized_type == "SWAP":
        return [
            {
                "label": "SWAP",
                "type": gate_type,
                "qubits": [int(target)],
                "kind": "target",
                **_classical_gate_metadata(gate),
            }
            for target in gate.targets
        ]

    if normalized_type in {"QFT", "ORACLE"}:
        # Register gates are order sensitive, so each cell also carries its
        # position in the register (0 = most significant, matching the targets
        # index) and the bit weight k it holds, which is what the editor draws.
        register_size = len(gate.targets)
        marked_index = (
            int((gate.params or {}).get("marked_index", 0.0))
            if normalized_type == "ORACLE"
            else None
        )
        return [
            {
                "label": normalized_type,
                "type": gate_type,
                "qubits": [int(target)],
                "kind": "target",
                "register_position": position,
                "register_bit_weight": register_size - 1 - position,
                **({} if marked_index is None else {"marked_index": marked_index}),
                **_classical_gate_metadata(gate),
            }
            for position, target in enumerate(gate.targets)
        ]

    kind = "measure" if normalized_type == "MEASURE" else "single"
    label = "M" if normalized_type == "MEASURE" else gate_type
    return [
        {
            "label": label,
            "type": gate_type,
            "qubits": [int(target)],
            "kind": kind,
            **_classical_gate_metadata(gate),
        }
        for target in gate.targets
    ]


def _classical_gate_metadata(gate: GateOperation) -> dict[str, Any]:
    return {
        "classical_targets": [
            int(target) for target in (gate.classical_targets or [])
        ],
        "condition": (
            None
            if gate.condition is None
            else {
                "bit": int(gate.condition.bit),
                "value": int(gate.condition.value),
            }
        ),
        "conditions": [
            {"bit": int(item.bit), "value": int(item.value)}
            for item in gate.conditions
        ],
    }


def _parameters_response(config: SimulationConfig) -> dict[str, Any]:
    environment = config.environment
    is_physical = _is_physical_mode(environment)
    is_normalized = _is_normalized_mode(environment)

    return {
        "environment_model": str(environment.model),
        "input_mode": str(environment.input_mode),
        "temperature_k": (
            _safe_float_or_none(environment.temperature_mk / 1000.0)
            if is_physical
            else None
        ),
        "temperature_mk": (
            _safe_float_or_none(environment.temperature_mk)
            if is_physical
            else None
        ),
        "normalized_temperature": (
            _safe_float_or_none(environment.temperature)
            if is_normalized
            else None
        ),
        "qubit_frequency_ghz": (
            _safe_float_or_none(environment.qubit_frequency_ghz)
            if is_physical
            else None
        ),
        "device_quality": (
            _safe_float_or_none(environment.device_quality)
            if is_physical
            else None
        ),
        "flux_noise_phi0": (
            _safe_float_or_none(environment.flux_noise_phi0)
            if is_physical
            else None
        ),
        "duration_us": _safe_float(config.duration_us),
        "time_steps": int(config.time_steps),
        "fidelity_threshold": _safe_float(config.fidelity_threshold),
        "simulation_backend": str(config.simulation_backend),
    }


def _diagnostics_response(result: SimulationResult) -> dict[str, Any]:
    diagnostics = dict(result.diagnostics)
    config = result.config

    diagnostics["simulation_backend"] = str(
        diagnostics.get(
            "simulation_backend",
            diagnostics.get("backend_requested", config.simulation_backend),
        )
    )

    for key in (
        "simulation_model",
        "evolution_mode",
        "backend_name",
        "rust_kernel_mode",
    ):
        diagnostics[key] = str(diagnostics.get(key, ""))

    for key in ("rust_kernel_call_count", "rust_kernel_sampled_batch_count"):
        diagnostics[key] = _safe_float(diagnostics.get(key, 0.0))

    for key in ("backend_fallback_used", "rust_kernel_fallback_used"):
        diagnostics[key] = bool(diagnostics.get(key, False))

    return {str(key): _json_safe_value(value) for key, value in diagnostics.items()}


def _summary_response(result: SimulationResult) -> dict[str, Any]:
    diagnostics = result.diagnostics
    return {
        "final_fidelity": _first_number_or_none(
            diagnostics.get("final_fidelity"),
            result.fidelity[-1] if result.fidelity else None,
        ),
        "final_purity": _first_number_or_none(
            diagnostics.get("final_purity"),
            result.purity[-1] if result.purity else None,
        ),
        "completion_fidelity": _safe_float_or_none(
            diagnostics.get("completion_fidelity")
        ),
        "completion_purity": _safe_float_or_none(
            diagnostics.get("completion_purity")
        ),
        "effective_time_us": _safe_float_or_none(
            result.effective_operation_time_us
        ),
    }


def _timeline(result: SimulationResult) -> tuple[list[dict[str, float | None]], list[str]]:
    length = min(len(result.times), len(result.fidelity), len(result.purity))
    warnings: list[str] = []
    if length != len(result.times) or length != len(result.fidelity) or length != len(result.purity):
        warnings.append(
            "Timeline arrays had different lengths; the UI response used the shortest length."
        )

    return [
        {
            "time_us": _safe_float(result.times[index]),
            "fidelity": _safe_float_or_none(result.fidelity[index]),
            "purity": _safe_float_or_none(result.purity[index]),
        }
        for index in range(length)
    ], warnings


def _output_probabilities(result: SimulationResult) -> dict[str, float]:
    return {
        str(label): _safe_float(value)
        for label, value in result.output_probabilities.items()
    }


def compilation_response(diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    """Shape gate-compiler diagnostics into the UI's `run.compilation` block.

    The pre-run preview endpoint compiles without simulating, so it produces the
    same diagnostics dict and reuses this adapter to stay byte-identical with
    what the run path later reports.
    """

    return {
        "mode": str(diagnostics.get("compilation_mode", "logical_direct")),
        "native_gate_set_id": str(
            diagnostics.get("native_gate_set_id", "gate_aware_hxyzst_rz_cnot_v3")
        ),
        "logical_gate_count": int(diagnostics.get("logical_gate_count", 0)),
        "compiled_gate_count": int(diagnostics.get("compiled_gate_count", 0)),
        "logical_depth": int(diagnostics.get("logical_depth", 0)),
        "compiled_depth": int(diagnostics.get("compiled_depth", 0)),
        "logical_duration_us": _safe_float_or_none(
            diagnostics.get("logical_declared_duration_us")
        ),
        "compiled_duration_us": _safe_float_or_none(
            diagnostics.get("compiled_duration_us")
        ),
        "decomposition_rules_used": list(
            diagnostics.get("decomposition_rules_used", [])
        ),
        "source_map": list(diagnostics.get("source_map", [])),
        "compiled_circuit": dict(diagnostics.get("compiled_circuit", {})),
    }


def _run_response(result: SimulationResult) -> dict[str, Any]:
    selected_backend = str(
        result.diagnostics.get(
            "backend_requested",
            result.config.simulation_backend,
        )
    )
    compilation = compilation_response(result.diagnostics)
    return {
        "status": (
            "Completed with issues"
            if _has_fatal_or_error_issue(result.issues)
            else "Completed"
        ),
        "selected_backend": selected_backend,
        "last_run_label": "Latest simulation",
        "compilation": compilation,
    }


def _measurement_response(result: SimulationResult) -> dict[str, Any]:
    shots = int(result.config.measurement_shots)
    explicit_targets = sorted({
        int(target)
        for column in result.config.circuit.columns
        for gate in column.gates
        if str(gate.type).upper() == "MEASURE"
        for target in gate.targets
    })
    explicit_measurement_count = sum(
        1
        for column in result.config.circuit.columns
        for gate in column.gates
        if str(gate.type).upper() == "MEASURE"
    )
    explicit_measurement_bindings = [
        {
            "qubit": int(qubit),
            "classical_bit": int(classical_bit),
        }
        for column in result.config.circuit.columns
        for gate in column.gates
        if str(gate.type).upper() == "MEASURE"
        for qubit, classical_bit in zip(
            gate.targets,
            gate.classical_targets or [],
        )
    ]
    has_classical_conditions = any(
        gate.condition is not None or bool(gate.conditions)
        for column in result.config.circuit.columns
        for gate in column.gates
    )
    conditional_operations = [
        {
            "gate": str(gate.type).upper(),
            "targets": [int(target) for target in gate.targets],
            "conditions": [
                {"bit": int(condition.bit), "value": int(condition.value)}
                for condition in (gate.conditions or ([] if gate.condition is None else [gate.condition]))
            ],
            "column_index": int(column.step),
        }
        for column in result.config.circuit.columns
        for gate in column.gates
        if gate.condition is not None or gate.conditions
    ]
    branch_probability_sum = sum(
        float(record.get("probability", 0.0))
        for record in result.classical_branch_records
    )
    return {
        "mode": "final_computational_basis_shots_v1",
        "shots": shots,
        "seed": int(result.config.measurement_seed),
        "counts": {
            str(label): int(count)
            for label, count in result.measurement_counts.items()
        },
        "frequencies": {
            str(label): _safe_float(count / shots)
            for label, count in result.measurement_counts.items()
        },
        "explicit_measurement_mode": "non_selective_computational_basis_v1",
        "explicit_measurement_count": explicit_measurement_count,
        "explicit_measurement_targets": explicit_targets,
        "explicit_measurement_bindings": explicit_measurement_bindings,
        "classical_register_bits": int(result.config.circuit.classical_bits),
        "classical_register_mode": (
            "gate_aware_noisy_branching_v1"
            if has_classical_conditions
            else "schema_only_v2"
        ),
        "classical_conditioning_supported": has_classical_conditions,
        "classical_branch_count": len(result.classical_branch_records),
        "classical_branching_noise_applied": bool(
            result.diagnostics.get("classical_branching_noise_applied", False)
        ),
        "classical_branches": list(result.classical_branch_records),
        "classical_shot_preview": list(result.classical_shot_preview),
        "branch_probability_sum": _safe_float(branch_probability_sum),
        "branch_probability_normalized": abs(branch_probability_sum - 1.0) <= 1e-9
        if result.classical_branch_records else None,
        "conditional_operations": conditional_operations,
        "branch_state_representation": "probability_and_classical_bits_only_v1",
    }
def _is_physical_mode(environment: EnvironmentConfig) -> bool:
    return str(environment.input_mode).lower() == "physical"


def _is_normalized_mode(environment: EnvironmentConfig) -> bool:
    return str(environment.input_mode).lower() == "normalized"


def _has_fatal_or_error_issue(issues: list[Any]) -> bool:
    for issue in issues:
        level = getattr(issue, "level", None)
        if isinstance(issue, Mapping):
            level = issue.get("level")
        if str(level).lower() in {"error", "fatal"}:
            return True
    return False


def _issue_to_dict(issue: Any) -> dict[str, Any]:
    if hasattr(issue, "to_dict"):
        return {
            str(key): _json_safe_value(value)
            for key, value in issue.to_dict().items()
        }
    if isinstance(issue, Mapping):
        return {
            str(key): _json_safe_value(value)
            for key, value in issue.items()
        }
    return {
        "level": "warning",
        "code": "UNSTRUCTURED_ISSUE",
        "message": str(issue),
        "detail": None,
        "suggestion": None,
    }


def _first_number_or_none(*values: Any) -> float | None:
    for value in values:
        converted = _safe_float_or_none(value)
        if converted is not None:
            return converted
    return None


def _safe_float(value: Any) -> float:
    converted = _safe_float_or_none(value)
    if converted is None:
        raise ValueError("value must be a finite number")
    return converted


def _safe_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    converted = float(value)
    if converted != converted or converted in {float("inf"), float("-inf")}:
        raise ValueError("value must be finite")
    return converted


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return _safe_float(value)
    return str(value)
