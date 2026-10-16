"""Server-owned product profiles backed by a verified device snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from api.attack_device_snapshot import DeviceSnapshot, deterministic_connected_region, load_ibm_kingston_snapshot
from api.attack_product_factory import IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL
from api.attack_settings_resolver import FreeScenarioDefinition, SettingField
from core.attack_simulation.qpu_topology import QpuTopology, TopologyLimits
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.program_request import (
    INTERLEAVE_TRAINING_OPERATION_ID, INTERLEAVE_VICTIM_OPERATION_ID, STATE_INHERIT_OPERATION_ID,
    VICTIM_ACTIVITY_OPERATION_ID,
)
from core.attack_simulation.scenario_assumptions import AssumptionBasis, AssumptionRecord
from core.attack_simulation.scenario_compiler import ScenarioOverride
from core.attack_simulation.scenario_policy import ObservationBudget, ScenarioTemplate
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntimeConfig
from core.device_profiles import GenericTransmonProfile
from core.experimental_isolation_physics.imperfect_reset import ImperfectResetSpec
from core.experimental_isolation_physics.reset_model import PerQubitPhenomenologicalResetModel
from core.physical_environment import UNIFIED_ENVIRONMENT_MODEL


OPERATIONS = (
    "device.info.read", "program.submit", "job.status.read", "job.result.read", "job.cancel",
    STATE_INHERIT_OPERATION_ID, INTERLEAVE_VICTIM_OPERATION_ID, INTERLEAVE_TRAINING_OPERATION_ID,
    VICTIM_ACTIVITY_OPERATION_ID,
)
# Operations that exist in a real service. The interleaved observation is an abstract command
# (no real service lets a tenant run another tenant's job before each shot), so it is a separate,
# opt-in setting that is off unless a scenario or the author turns it on.
INTERLEAVE_OPERATIONS = (INTERLEAVE_VICTIM_OPERATION_ID, INTERLEAVE_TRAINING_OPERATION_ID)
# Reading the victim's runs behind one's own job is also abstract: free mode only. A challenge
# preset never lists it (api/attack_challenge.py builds its operations from CHALLENGE_OPERATIONS).
ABSTRACT_OPERATIONS = (*INTERLEAVE_OPERATIONS, VICTIM_ACTIVITY_OPERATION_ID)
SERVICE_OPERATIONS = tuple(item for item in OPERATIONS if item not in ABSTRACT_OPERATIONS)
CHALLENGE_OPERATIONS = (*SERVICE_OPERATIONS, *INTERLEAVE_OPERATIONS)
ABSTRACT_INTERLEAVE = "qpu_service.abstract_commands.interleaved_observation"
ABSTRACT_VICTIM_ACTIVITY = "qpu_service.abstract_commands.victim_activity"
SUPPORTED_OPERATIONS_PATH = "qpu_service.supported_operations"
INTERLEAVED_RUN_SECRET_BIT = "victim.interleaved_run.secret_bit"
INTERLEAVED_MAX_ROUNDS = "attacker.interleaved_access.max_rounds_per_query"
RESET_CONFIGURATION_ID = "qpu_service.runtime_execution.reset_model_configuration_id"
# Host-protection bounds for the free-mode interleaved observation channel.
FREE_INTERLEAVED_QUERY_LIMIT = 1000
FREE_INTERLEAVED_DEFAULT_ROUNDS = 64
FREE_INTERLEAVED_ROUNDS_CEILING = 256
MAX_RETAINED_QUBITS = 6
_GATE_NAMES = ("H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ", "CNOT", "CZ")
_SINGLE_QUBIT_DECOMPOSITIONS = {
    "Z": ("rz",), "S": ("rz",), "T": ("rz",), "RZ": ("rz",),
    "X": ("x",), "H": ("rz", "sx", "rz"), "Y": ("x", "rz"),
    "RX": ("rz", "sx", "rz", "sx", "rz"),
    "RY": ("rz", "sx", "rz", "sx", "rz"),
}


def product_free_scenarios(snapshot: DeviceSnapshot | None = None) -> dict[str, FreeScenarioDefinition]:
    """Build both mutually-exclusive idle-noise profiles from verified data.

    The physical-parameter mode uses the existing generic idle-rate model, not
    snapshot T1/T2 values. Device topology, readout/reset calibration, and CZ
    service metadata remain the declared device profile in both modes.
    """
    snapshot = load_ibm_kingston_snapshot() if snapshot is None else snapshot
    region = tuple(sorted(deterministic_connected_region(snapshot, MAX_RETAINED_QUBITS)))
    alternate = tuple(sorted(deterministic_connected_region(
        snapshot, MAX_RETAINED_QUBITS, excluded_qubit_ids=frozenset(region),
    )))
    if len(region) != MAX_RETAINED_QUBITS or len(alternate) != MAX_RETAINED_QUBITS:
        raise ValueError("two disjoint six-qubit regions are required for the product placement choice")
    topology = _topology(snapshot)
    metadata = {
        "max_qubits": MAX_RETAINED_QUBITS,
        "eligible_qubit_ids": list(snapshot.usable_qubit_ids),
        "active_region_initial": list(region),
        "alternate_region_initial": list(alternate),
        "display_layout_notice": "表示用の配置であり、実機の物理的な位置ではありません。",
        "region_notice": "使用領域はCZ結合で連結している必要があります。作成時にサーバーが再検査します。",
        "qubit_noise_fields": {
            "primary": _qubit_noise_field_templates("qpu_service.idle_dynamics.qubits"),
            "dedicated": _qubit_noise_field_templates("qpu_service.dedicated_region.idle_dynamics.qubits"),
        },
    }

    def region_resolver(mode: str):
        def resolve(placement_policy: str, raw_regions: object) -> FreeScenarioDefinition:
            primary, secondary = _validate_region_selection(
                snapshot, placement_policy, raw_regions,
                default_primary=region, default_secondary=alternate,
            )
            return _profile(snapshot, topology, primary, mode, secondary, placement_policy)
        return resolve

    result = {}
    for mode in (IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL):
        definition = _profile(snapshot, topology, region, mode, alternate)
        mode_metadata = dict(metadata)
        if mode == IDLE_INPUT_PHYSICAL:
            mode_metadata.pop("qubit_noise_fields")
        result[mode] = replace(
            definition,
            region_metadata=mode_metadata,
            region_resolver=region_resolver(mode),
        )
    return result


def _validate_region_selection(snapshot: DeviceSnapshot, placement_policy: str, raw: object,
                                *, default_primary: tuple[int, ...],
                                default_secondary: tuple[int, ...]
                                ) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if raw is None:
        primary, secondary = default_primary, default_secondary
        if placement_policy not in {
            PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT.value,
            PlacementPolicy.TENANT_DEDICATED_REGION.value,
        }:
            raise ValueError("unsupported placement policy")
        return primary, secondary
    if type(raw) is not dict:
        raise ValueError("regions must be an explicit object")
    if placement_policy == PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT.value:
        if set(raw) != {"shared_physical_qubit_ids"}:
            raise ValueError("shared placement requires one selected region")
        primary = _validate_connected_region(snapshot, raw["shared_physical_qubit_ids"])
        secondary = tuple(sorted(deterministic_connected_region(
            snapshot, len(primary), excluded_qubit_ids=frozenset(primary),
        )))
        if len(secondary) != len(primary):
            raise ValueError("no disjoint fallback region is available")
        return primary, secondary
    if placement_policy == PlacementPolicy.TENANT_DEDICATED_REGION.value:
        if set(raw) != {"victim_physical_qubit_ids", "attacker_physical_qubit_ids"}:
            raise ValueError("tenant-dedicated placement requires both regions")
        victim = _validate_connected_region(snapshot, raw["victim_physical_qubit_ids"])
        attacker = _validate_connected_region(snapshot, raw["attacker_physical_qubit_ids"])
        if len(victim) != len(attacker):
            raise ValueError("tenant regions must have the same number of qubits")
        if set(victim) & set(attacker):
            raise ValueError("tenant regions must not overlap")
        return victim, attacker
    raise ValueError("unsupported placement policy")


def _validate_connected_region(snapshot: DeviceSnapshot, raw: object) -> tuple[int, ...]:
    if (type(raw) not in (list, tuple) or not 1 <= len(raw) <= MAX_RETAINED_QUBITS
            or any(type(qid) is not int for qid in raw) or len(set(raw)) != len(raw)):
        raise ValueError("region must contain one to six unique integer qubit IDs")
    selected = set(raw)
    if not selected <= set(snapshot.usable_qubit_ids):
        raise ValueError("region contains a qubit that is not eligible for placement")
    adjacency = {qid: set() for qid in selected}
    for left, right in snapshot.undirected_coupling_edges:
        if left in selected and right in selected:
            adjacency[left].add(right)
            adjacency[right].add(left)
    reached = set()
    stack = [next(iter(selected))]
    while stack:
        qid = stack.pop()
        if qid in reached:
            continue
        reached.add(qid)
        stack.extend(adjacency[qid] - reached)
    if reached != selected:
        raise ValueError("selected region is not connected by snapshot CZ edges")
    return tuple(sorted(selected))


def _qubit_noise_field_templates(prefix: str) -> list[dict[str, object]]:
    return [
        {"path": f"{prefix}.{{qubit_id}}.t1_us", "label": "Qubit {qubit_id} T1",
         "type": "number", "basis": "Per-qubit historical calibration; not current device-wide representative.",
         "description": "Historical per-qubit T1 calibration.", "unit": "us", "minimum": 0.001, "maximum": 1e6},
        {"path": f"{prefix}.{{qubit_id}}.t2_us", "label": "Qubit {qubit_id} T2",
         "type": "number", "basis": "Per-qubit historical calibration; not current device-wide representative.",
         "description": "Historical per-qubit T2 calibration.", "unit": "us", "minimum": 0.001, "maximum": 1e6},
        {"path": f"{prefix}.{{qubit_id}}.equilibrium_excited_population",
         "label": "Qubit {qubit_id} equilibrium excited population", "type": "number",
         "basis": "Editable zero-population assumption; not reported in snapshot.",
         "description": "Editable equilibrium excited-state population assumption.",
         "minimum": 0.0, "maximum": 1.0},
    ]


def standard_snapshot_initial_values(snapshot: DeviceSnapshot) -> dict[str, object]:
    """Public catalog values with explicit per-qubit provenance and ineligibility."""
    inventory = {}
    for qubit_id, calibration in snapshot.qubits.items():
        inventory[str(qubit_id)] = {
            "usable_for_placement": qubit_id in snapshot.usable_qubit_ids,
            "placement_status": ("usable" if qubit_id in snapshot.usable_qubit_ids else "unusable"),
            "unusable_reasons": list(snapshot.ineligible_qubit_reasons.get(qubit_id, ())),
            "T1_us": calibration.t1_us,
            "T2_us": calibration.t2_us,
            "readout_error": calibration.readout_error,
            "P(1|0)": calibration.p_one_given_zero,
            "P(0|1)": calibration.p_zero_given_one,
            "measurement_duration_us": calibration.measurement_duration_us,
            "reset_duration_us": calibration.reset_duration_us,
            "basis": "Per-qubit historical calibration from the hash-verified 2026-04-15 device snapshot; not current device guarantee.",
        }
    return {
        "source": "data/device_snapshots/ibm_kingston_2026-04-15/SOURCE.md",
        "device": snapshot.device_id,
        "snapshot_date": snapshot.snapshot_date,
        "props_sha256": snapshot.props_sha256,
        "conf_sha256": snapshot.conf_sha256,
        "qubit_count": snapshot.n_qubits,
        "directed_coupling_count": len(snapshot.directed_coupling_map),
        "undirected_edge_count": len(snapshot.undirected_coupling_edges),
        "active_region_limit": MAX_RETAINED_QUBITS,
        "active_region_initial": list(sorted(deterministic_connected_region(snapshot, MAX_RETAINED_QUBITS))),
        "alternate_region_initial": list(sorted(deterministic_connected_region(
            snapshot, MAX_RETAINED_QUBITS,
            excluded_qubit_ids=frozenset(deterministic_connected_region(snapshot, MAX_RETAINED_QUBITS)),
        ))),
        "eligibility_policy": dict(snapshot.eligibility_policy),
        "qubits": inventory,
        "cz_calibrations": {
            f"{left}:{right}": {"error": item.error, "duration_us": item.duration_us,
                                "basis": "Per-edge historical calibration from the hash-verified 2026-04-15 device snapshot."}
            for (left, right), item in snapshot.cz_calibrations.items()
        },
        "notice": (
            "Historical device snapshot. Per-qubit values are not current calibration guarantees; "
            "unusable qubits are listed without invented replacement values."
        ),
    }


def _topology(snapshot: DeviceSnapshot) -> QpuTopology:
    return QpuTopology(
        snapshot.device_id,
        f"ibm_kingston_snapshot_{snapshot.snapshot_date}",
        tuple(range(snapshot.n_qubits)),
        TopologyLimits(256, 2048, 4096),
        gate_edges=snapshot.undirected_coupling_edges,
    )


def _profile(snapshot: DeviceSnapshot, topology: QpuTopology, region: tuple[int, ...], mode: str,
             alternate_region: tuple[int, ...] | None = None,
             placement_policy: str = PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT.value):
    calibrations = {qubit_id: snapshot.qubits[qubit_id] for qubit_id in region}
    p10_p01 = tuple((item.p_one_given_zero, item.p_zero_given_one) for item in calibrations.values())
    reset_specs = tuple(
        ImperfectResetSpec(1.0 - (p10 + p01) / 2.0, (1.0, 0.0))
        for p10, p01 in p10_p01
    )
    reset = PerQubitPhenomenologicalResetModel(region, reset_specs, p10_p01)
    reset_durations = [item.reset_duration_us for item in calibrations.values()]
    measurement_durations = [item.measurement_duration_us for item in calibrations.values()]
    if any(value is None for value in (*reset_durations, *measurement_durations)):
        raise ValueError("eligible region is missing explicit measurement or reset duration")
    # SharedQpuRuntimeConfig currently exposes one duration per register-level
    # transition. Preserve the per-qubit source values in the manifest and use
    # the maximum as the conservative simultaneous-operation duration (D67 proposal).
    reset_duration = max(float(value) for value in reset_durations if value is not None)
    measurement_duration = max(float(value) for value in measurement_durations if value is not None)

    values = {}
    for qubit_id, item in calibrations.items():
        values[str(qubit_id)] = {
            "t1_us": item.t1_us,
            "t2_us": item.t2_us,
            "equilibrium_excited_population": 0.0,
        }
    if mode == IDLE_INPUT_CALIBRATION and any(
            item.t1_us is None or item.t2_us is None for item in calibrations.values()):
        raise ValueError("calibration mode region contains a qubit without T1/T2")

    physical = GenericTransmonProfile()
    physical_inputs = {
        "environment_model": UNIFIED_ENVIRONMENT_MODEL,
        "device_quality": 0.8,
        "temperature_mk": physical.default_temperature_mk,
        "flux_noise_phi0": physical.flux_noise_min_phi0,
        "qubit_frequency_ghz": physical.qubit_frequency_ghz,
        "t1_max_us": physical.t1_max_us,
        "tphi_max_us": physical.tphi_max_us,
        "per_qubit": {},
    }
    idle = ({"model_id": "independent_constant_rate_v1", "input_mode": mode,
             "qubits": values} if mode == IDLE_INPUT_CALIBRATION else
            {"model_id": "independent_constant_rate_v1", "input_mode": mode,
             "physical_inputs": physical_inputs})

    cz = {}
    for pair, item in snapshot.cz_calibrations.items():
        if pair[0] in region and pair[1] in region:
            cz[f"{pair[0]}:{pair[1]}"] = {
                "duration_us": item.duration_us,
                # Convert average infidelity r to the probability p of a
                # two-qubit replacement channel: r = p(d-1)/d, d=4.
                "depolarizing_probability": _cz_replacement_probability(item.error),
            }
    native_gates = {}
    for qid in region:
        values = {}
        for native in ("rz", "sx", "x"):
            item = snapshot.single_qubit_calibrations.get((native, qid))
            if item is None:
                raise ValueError("active qubit is missing native single-qubit calibration")
            if native != "rz" and item.error > 0.5:
                raise ValueError("single-qubit average infidelity cannot be represented by the replacement channel")
            values[native] = {"duration_us": item.duration_us, "gate_error": item.error}
        native_gates[str(qid)] = values
    # Two-qubit schema fallbacks exist only to satisfy the manifest contract.
    # Actual CZ and CNOT instructions are lowered against per-edge/per-qubit data.
    max_cz_duration = max((entry["duration_us"] for entry in cz.values()), default=0.0)
    max_h_duration = max(
        sum(native_gates[str(qid)][native]["duration_us"]
            for native in _SINGLE_QUBIT_DECOMPOSITIONS["H"])
        for qid in region
    )
    gate_durations = {
        "CZ": max_cz_duration,
        "CNOT": 2.0 * max_h_duration + max_cz_duration,
    }

    runtime = SharedQpuRuntimeConfig(
        reset_model_id=reset.model_id,
        reset_model_configuration_id=reset.configuration_id,
        reset_count=1,
        measure_input_state_before_transition=True,
        placement_policy=PlacementPolicy(placement_policy),
        reset_duration_us=reset_duration,
        victim_operation_duration_us=1.0,
        attacker_measurement_duration_us=measurement_duration,
        program_measurement_duration_us=measurement_duration,
        max_local_qubits=len(region),
        max_execution_work=2_147_483_648,
    )
    qpu = {
        "execution_regime": "nisq_physical",
        "supported_operations": list(SERVICE_OPERATIONS),
        "abstract_commands": {"interleaved_observation": False, "victim_activity": False},
        "runtime_execution": runtime.to_manifest_config(),
        "circuit_execution": {
            "compiler_id": "local_sampling_v1",
            "supported_gates": [*_GATE_NAMES, "MEASURE"],
            "max_program_qubits": len(region), "max_shots": 1024,
            "max_instruction_executions": 32768, "gate_durations_us": gate_durations,
            "supported_initializations": ["prepare", "inherit"],
            "cz_gate_calibrations": cz,
            "single_qubit_native_gates": native_gates,
            "single_qubit_gate_decompositions": dict(_SINGLE_QUBIT_DECOMPOSITIONS),
        },
        "topology": topology.to_config(),
        "retained_region": {"physical_qubit_ids": list(region)},
        "initial_state": {"device_config_version": f"{snapshot.device_id}-{snapshot.snapshot_date}",
                          "kind": "all_zero_register"},
        "device_snapshot": _manifest_snapshot(snapshot, region),
        "reset_model": {
            "model_id": reset.model_id, "target_populations": [1.0, 0.0],
            "applies_to_physical_qubit_ids": list(region),
            "qubits": {
                str(qubit_id): {
                    "success_probability": spec.success_probability,
                    "observed_one_given_zero": p10,
                    "observed_zero_given_one": p01,
                }
                for qubit_id, spec, (p10, p01) in zip(region, reset_specs, p10_p01, strict=True)
            },
        },
        "idle_dynamics": idle,
        "declared_service_timing": {
            "processing_delay_us": 0.0, "publication_delay_us": 0.0, "denial_delay_us": 0.0,
            "basis": "Explicit deterministic explicit_wait profile; host/network latency is separate.",
        },
        "live_ingress": {
            "receipt_origin": "authenticated_complete_envelope", "outbound_delay_us": 0.0,
            "basis": "Transport/host delay is not virtual QPU time.",
        },
        "control_timing": {
            "processing_delay_us": 0.0, "publication_delay_us": 0.0,
            "basis": "Explicit deterministic profile choice.", "cancellation_policy": "queued_only",
        },
    }
    if alternate_region is not None:
        alternate_qpu = _profile(snapshot, topology, alternate_region, mode).template.qpu_service_config
        alternate_runtime = alternate_qpu["runtime_execution"]
        qpu["dedicated_region"] = {
            "physical_qubit_ids": list(alternate_region),
            "reset_model": alternate_qpu["reset_model"],
            "idle_dynamics": alternate_qpu["idle_dynamics"] if mode == IDLE_INPUT_CALIBRATION else None,
            "initial_state": {"kind": "all_zero_register",
                              "device_config_version": alternate_qpu["initial_state"]["device_config_version"]},
            "reset_model_configuration_id": alternate_runtime["reset_model_configuration_id"],
            "reset_duration_us": alternate_runtime["reset_duration_us"],
            "program_measurement_duration_us": alternate_runtime["program_measurement_duration_us"],
        }
        qpu["circuit_execution"]["cz_gate_calibrations"].update(
            alternate_qpu["circuit_execution"]["cz_gate_calibrations"])
        qpu["circuit_execution"]["single_qubit_native_gates"].update(
            alternate_qpu["circuit_execution"]["single_qubit_native_gates"])
        qpu["circuit_execution"]["gate_durations_us"]["CZ"] = max(
            item["duration_us"] for item in qpu["circuit_execution"]["cz_gate_calibrations"].values())
        qpu["circuit_execution"]["gate_durations_us"]["CNOT"] = max(
            2.0 * sum(qpu["circuit_execution"]["single_qubit_native_gates"][qid][native]["duration_us"]
                     for native in _SINGLE_QUBIT_DECOMPOSITIONS["H"])
            + qpu["circuit_execution"]["gate_durations_us"]["CZ"]
            for qid in qpu["circuit_execution"]["single_qubit_native_gates"])
    template = _template(f"attack-free-kingston-{mode}", qpu, mode, region,
                         alternate_region if placement_policy == PlacementPolicy.TENANT_DEDICATED_REGION.value else None)
    assumptions = _assumptions(
        snapshot, region, mode, reset_duration, measurement_duration,
        qpu["circuit_execution"]["cz_gate_calibrations"],
    )
    if alternate_region is not None:
        evidence = "data/device_snapshots/ibm_kingston_2026-04-15/SOURCE.md"
        assumptions.extend((
            _record("qpu_service.dedicated_region.physical_qubit_ids", AssumptionBasis.SIMULATOR_CHOICE,
                    "Deterministic disjoint connected six-qubit region for tenant separation."),
            _record("qpu_service.dedicated_region.reset_model", AssumptionBasis.SIMULATOR_CHOICE,
                    "Per-qubit reset/readout approximation derived from the historical snapshot.", evidence=evidence),
            _record("qpu_service.dedicated_region.idle_dynamics", AssumptionBasis.SIMULATOR_CHOICE,
                    "Historical per-qubit calibration, or the same editable physical input mode as the primary region.",
                    evidence=evidence),
            _record("qpu_service.dedicated_region.initial_state", AssumptionBasis.SIMULATOR_CHOICE,
                    "Explicit all-|0> initial state for the alternate retained region."),
            _record("qpu_service.dedicated_region.reset_duration_us", AssumptionBasis.SIMULATOR_CHOICE,
                    "Maximum simultaneous reset duration in the alternate region.", evidence=evidence),
            _record("qpu_service.dedicated_region.program_measurement_duration_us", AssumptionBasis.SIMULATOR_CHOICE,
                    "Maximum simultaneous measurement duration in the alternate region.", evidence=evidence),
            _record("qpu_service.dedicated_region.reset_model_configuration_id", AssumptionBasis.SIMULATOR_CHOICE,
                    "Digest of the frozen alternate per-qubit reset model.", evidence=evidence),
        ))
    fields = _fields(region, mode, calibrations, physical_inputs, reset_duration, measurement_duration)
    if mode == IDLE_INPUT_CALIBRATION and placement_policy == PlacementPolicy.TENANT_DEDICATED_REGION.value:
        alternate_calibrations = {qid: snapshot.qubits[qid] for qid in alternate_region or ()}
        fields += _qubit_noise_fields(
            alternate_region or (), alternate_calibrations,
            "qpu_service.dedicated_region.idle_dynamics.qubits",
        )
    assumptions.extend(_interleaving_assumptions(region))

    def derive_overrides(supplied: Mapping[str, object]) -> tuple[ScenarioOverride, ...]:
        prefix = "qpu_service.reset_model.qubits."
        derived: list[ScenarioOverride] = []
        interleave_on = supplied.get(ABSTRACT_INTERLEAVE) is True
        activity_on = supplied.get(ABSTRACT_VICTIM_ACTIVITY) is True
        if interleave_on or activity_on:
            # Each abstract command adds only its own operations to the real service's list.
            derived.append(ScenarioOverride(
                SUPPORTED_OPERATIONS_PATH,
                [*SERVICE_OPERATIONS, *(INTERLEAVE_OPERATIONS if interleave_on else ()),
                 *((VICTIM_ACTIVITY_OPERATION_ID,) if activity_on else ())],
                "Server-derived: the author enabled an abstract command."))
        if not any(path.startswith(prefix) for path in supplied):
            return tuple(derived)
        edited = tuple(
            ImperfectResetSpec(supplied.get(f"{prefix}{qid}.success_probability", spec.success_probability), (1.0, 0.0))
            for qid, spec in zip(region, reset_specs, strict=True)
        )
        model = PerQubitPhenomenologicalResetModel(region, edited, p10_p01)
        return (*derived, ScenarioOverride(
            RESET_CONFIGURATION_ID, model.configuration_id,
            "Digest recomputed by the server from the browser-edited per-qubit reset success probabilities.",
        ))

    def budget_resolver(supplied: Mapping[str, object]) -> ObservationBudget:
        return ObservationBudget(
            shots_per_query=supplied.get(INTERLEAVED_MAX_ROUNDS, FREE_INTERLEAVED_DEFAULT_ROUNDS),
            training_query_limit=FREE_INTERLEAVED_QUERY_LIMIT,
            victim_observation_limit=FREE_INTERLEAVED_QUERY_LIMIT,
        )

    return FreeScenarioDefinition(template, tuple(assumptions), fields, OPERATIONS,
                                  derive_overrides=derive_overrides, budget_resolver=budget_resolver)


def _manifest_snapshot(snapshot: DeviceSnapshot, region: tuple[int, ...]) -> dict[str, object]:
    return {
        "snapshot_id": f"{snapshot.device_id}-{snapshot.snapshot_date}",
        "source": "data/device_snapshots/ibm_kingston_2026-04-15/SOURCE.md",
        "props_sha256": snapshot.props_sha256, "conf_sha256": snapshot.conf_sha256,
        "qubit_count": snapshot.n_qubits,
        "directed_coupling_count": len(snapshot.directed_coupling_map),
        "active_region_physical_qubit_ids": list(region),
        "eligibility_policy": dict(snapshot.eligibility_policy),
        "qubit_calibration_basis": "Per-qubit historical calibration from the hash-verified 2026-04-15 device snapshot; not current device guarantee.",
        "cz_calibration_basis": "Per-edge historical calibration from the hash-verified 2026-04-15 device snapshot.",
        "qubits": {
            str(qid): {
                "usable_for_placement": qid in snapshot.usable_qubit_ids,
                "placement_status": ("usable" if qid in snapshot.usable_qubit_ids else "unusable"),
                "unusable_reasons": list(snapshot.ineligible_qubit_reasons.get(qid, ())),
                "T1_us": item.t1_us, "T2_us": item.t2_us,
                "readout_error": item.readout_error,
                "P(1|0)": item.p_one_given_zero, "P(0|1)": item.p_zero_given_one,
                "measurement_duration_us": item.measurement_duration_us,
                "reset_duration_us": item.reset_duration_us,
            }
            for qid, item in snapshot.qubits.items()
        },
        "cz_calibrations": {
            f"{left}:{right}": [item.error, item.duration_us]
            for (left, right), item in snapshot.cz_calibrations.items()
        },
    }


def _cz_replacement_probability(gate_error: float) -> float:
    """Map two-qubit average infidelity to complete-mixing replacement weight."""
    probability = gate_error * 4.0 / 3.0
    if not 0.0 <= probability <= 1.0:
        raise ValueError("CZ average infidelity cannot be represented by the replacement channel")
    return probability


def _template(template_id: str, qpu: Mapping[str, object], mode: str,
              region: tuple[int, ...], dedicated_region: tuple[int, ...] | None = None) -> ScenarioTemplate:
    editable = [
        "qpu_service.runtime_execution.placement_policy",
        "qpu_service.runtime_execution.reset_count",
        "qpu_service.runtime_execution.victim_operation_duration_us",
        "qpu_service.runtime_execution.repetition_delay_us",
        "qpu_service.declared_service_timing.processing_delay_us",
        "qpu_service.declared_service_timing.publication_delay_us",
        "qpu_service.declared_service_timing.denial_delay_us",
        "qpu_service.live_ingress.outbound_delay_us",
        "qpu_service.control_timing.processing_delay_us",
        "qpu_service.control_timing.publication_delay_us",
        "experiment.live_time_policy.pause_while_catching_up",
    ]
    if mode == IDLE_INPUT_CALIBRATION:
        editable.extend(_qubit_noise_paths(region, "qpu_service.idle_dynamics.qubits"))
        if dedicated_region is not None:
            editable.extend(_qubit_noise_paths(
                dedicated_region, "qpu_service.dedicated_region.idle_dynamics.qubits",
            ))
    else:
        editable.extend(
            f"qpu_service.idle_dynamics.physical_inputs.{field}"
            for field in ("device_quality", "temperature_mk", "flux_noise_phi0",
                          "qubit_frequency_ghz", "t1_max_us", "tphi_max_us")
        )
    editable.extend((INTERLEAVED_RUN_SECRET_BIT, INTERLEAVED_MAX_ROUNDS, RESET_CONFIGURATION_ID,
                     ABSTRACT_INTERLEAVE, ABSTRACT_VICTIM_ACTIVITY, SUPPORTED_OPERATIONS_PATH))
    editable.extend(f"qpu_service.reset_model.qubits.{qid}.success_probability" for qid in region)
    return ScenarioTemplate(
        template_id, "1",
        {"principal_id": "attacker", "tenant_id": "tenant-attacker", "operation_grants": list(OPERATIONS),
         "interleaved_access": {"max_rounds_per_query": FREE_INTERLEAVED_DEFAULT_ROUNDS}},
        {"principal_id": "victim", "tenant_id": "tenant-victim", "operation_grants": list(OPERATIONS),
         "interleaved_run": {"program": "prepare_basis_state_then_measure", "secret_bit": 1}},
        dict(qpu), {"actor_tenants": {"attacker": "tenant-attacker", "victim": "tenant-victim"}},
        {"world_clock": {"clock_domain": "scenario-world", "rate": 1.0},
         "live_time_policy": {"mode": "host_clock", "pause_while_catching_up": False}},
        free_override_fields=tuple(editable),
    )


def _qubit_noise_fields(region: tuple[int, ...], calibrations: Mapping[int, object],
                        prefix: str) -> tuple[SettingField, ...]:
    fields = []
    for qid in region:
        calibration = calibrations[qid]
        t1 = getattr(calibration, "t1_us", None)
        t2 = getattr(calibration, "t2_us", None)
        fields.extend((
            _number(f"{prefix}.{qid}.t1_us", f"qubit {qid} T1（エネルギー緩和時間）", 0.001, 1e6,
                    "us", f"スナップショット（2026-04-15）の値 {t1} µs。現在の実機の代表値ではありません。"),
            _number(f"{prefix}.{qid}.t2_us", f"qubit {qid} T2（位相緩和時間）", 0.001, 1e6,
                    "us", f"スナップショット（2026-04-15）の値 {t2} µs。現在の実機の代表値ではありません。"),
            _number(f"{prefix}.{qid}.equilibrium_excited_population",
                    f"qubit {qid} 熱励起の割合", 0.0, 1.0, None,
                    "スナップショットに値がないため、変更できる仮定として0を置いています。"),
        ))
    return tuple(fields)


def _reset_success_fields(region: tuple[int, ...], calibrations: Mapping[int, object]) -> tuple[SettingField, ...]:
    fields = []
    for qid in region:
        item = calibrations[qid]
        derived = 1.0 - (item.p_one_given_zero + item.p_zero_given_one) / 2.0
        fields.append(_number(
            f"qpu_service.reset_model.qubits.{qid}.success_probability",
            f"qubit {qid} のReset成功確率", 0.0, 1.0, None,
            f"サービスが交代時に行うResetが |0> に戻す確率です。スナップショットの読み出し誤差から導いた近似値"
            f"（{derived:.6g}）が初期値で、実測値ではありません。値を下げるほど前の利用者の状態が残ります。"))
    return tuple(fields)


def _qubit_noise_paths(region: tuple[int, ...], prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}.{qid}.{field}" for qid in region
                 for field in ("t1_us", "t2_us", "equilibrium_excited_population"))


def _assumptions(snapshot: DeviceSnapshot, region: tuple[int, ...], mode: str,
                 reset_duration: float, measurement_duration: float,
                 cz: Mapping[str, Mapping[str, float]]) -> list[AssumptionRecord]:
    evidence = "data/device_snapshots/ibm_kingston_2026-04-15/SOURCE.md"
    assumptions = [
        _record("qpu_service.idle_dynamics.input_mode", AssumptionBasis.SIMULATOR_CHOICE,
                f"Mutually exclusive idle input mode selected for this profile: {mode}."),
        _record("qpu_service.device_snapshot", AssumptionBasis.MEASURED,
                "Hash-verified historical IBM Kingston calibration snapshot; raw qubit and gate values are frozen in manifest.",
                evidence=evidence),
        _record("qpu_service.topology", AssumptionBasis.MEASURED,
                "156 physical qubits and the snapshot coupling map; topology is not synthesized.", evidence=evidence),
        _record("qpu_service.retained_region.physical_qubit_ids", AssumptionBasis.SIMULATOR_CHOICE,
                "Deterministic connected usable region, capped at six qubits for dense-state memory.",
                applicability={"active_physical_qubit_ids": ",".join(map(str, region))}),
        _record("qpu_service.circuit_execution.cz_gate_calibrations", AssumptionBasis.MEASURED,
                "CZ durations and reported gate_error values are copied from the verified snapshot; channel interpretation is provisional.",
                evidence=evidence),
        _record("qpu_service.circuit_execution.single_qubit_native_gates", AssumptionBasis.MEASURED,
                "Active-qubit rz/sx/x durations and average gate infidelities from the verified historical snapshot.",
                evidence=evidence),
        _record("qpu_service.circuit_execution.single_qubit_gate_decompositions", AssumptionBasis.SIMULATOR_CHOICE,
                "D69 approved rz/sx/x decomposition. Each sx/x applies p=2r (d=2); rz has no error channel. Repeated local replacement channels are composed exactly."),
        _record("qpu_service.runtime_execution.reset_duration_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Register-level shared duration is the maximum of per-qubit snapshot reset durations; per-qubit values remain frozen in device_snapshot.", evidence=evidence),
        _record("qpu_service.runtime_execution.program_measurement_duration_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Register-level shared duration is the maximum of per-qubit snapshot measurement durations; per-qubit values remain frozen in device_snapshot.", evidence=evidence),
        _record("qpu_service.runtime_execution.repetition_delay_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Declared wait between consecutive shots of one Program, imitating the repetition delay of cloud QPUs. The initial 0 means no gap (the previous behavior); the public documentation gives no value that could be adopted. A positive value relaxes the inherited state with the idle model between shots."),
        _record("qpu_service.runtime_execution.attacker_measurement_duration_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Uses the same conservative register-level maximum of per-qubit snapshot measurement durations.", evidence=evidence),
        _record("qpu_service.runtime_execution.victim_operation_duration_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Explicit 1 us victim-operation model duration; not reported by this snapshot."),
        _record("qpu_service.reset_model.target_populations", AssumptionBasis.SIMULATOR_CHOICE,
                "Reset target is computational ground state |0> for each active qubit."),
        _record("qpu_service.reset_model.applies_to_physical_qubit_ids", AssumptionBasis.SIMULATOR_CHOICE,
                "Reset/readout approximation is explicitly scoped to the active region."),
        _record("qpu_service.initial_state.kind", AssumptionBasis.SIMULATOR_CHOICE,
                "Explicit all-|0> initial state for the retained region; expanded only in runtime memory."),
        _record("qpu_service.declared_service_timing.processing_delay_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Explicit zero virtual service delay; host execution latency remains separate."),
        _record("qpu_service.declared_service_timing.publication_delay_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Explicit zero virtual result-publication delay."),
        _record("qpu_service.declared_service_timing.denial_delay_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Explicit zero virtual denial delay."),
        _record("qpu_service.live_ingress.outbound_delay_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Explicit zero simulated transport delay; host/network latency is separate."),
        _record("qpu_service.control_timing.processing_delay_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Explicit zero virtual control processing delay."),
        _record("qpu_service.control_timing.publication_delay_us", AssumptionBasis.SIMULATOR_CHOICE,
                "Explicit zero virtual control publication delay."),
        _record("qpu_service.runtime_execution.reset_count", AssumptionBasis.SIMULATOR_CHOICE,
                "Number of reset attempts at one service transition; it is not a per-shot reset claim. An interleaved observation run applies the same reset at every victim/attacker handover, so a higher count suppresses the leaked bit."),
        _record("qpu_service.runtime_execution.measure_input_state_before_transition",
                AssumptionBasis.SIMULATOR_CHOICE,
                "At a user transition, measure the input qubits before conditional-X reset; this models a measurement-based reset mechanism, not a guaranteed policy of the referenced service."),
        _record("qpu_service.runtime_execution.placement_policy", AssumptionBasis.SIMULATOR_CHOICE,
                "Retain the same physical-qubit region across the user transition so state carryover can be studied; this is a declared scenario condition, not an inferred cloud placement guarantee."),
        _record("qpu_service.circuit_execution.max_program_qubits", AssumptionBasis.SIMULATOR_CHOICE,
                "Program width is bounded by the six-qubit dense-state memory limit."),
        _record("experiment.world_clock.rate", AssumptionBasis.SIMULATOR_CHOICE,
                "Product host-clock multiplier is fixed to 1.0; not a QPU physical timing value."),
        _record("experiment.live_time_policy.mode", AssumptionBasis.SIMULATOR_CHOICE,
                "Product world time follows monotonic host elapsed time at the frozen 1x rate; explicit_wait remains for internal test scenarios only."),
        _record("experiment.live_time_policy.pause_while_catching_up", AssumptionBasis.SIMULATOR_CHOICE,
                "Initial value false. When enabled, host-derived world time pauses only during active numerical catch-up, never as a user-controlled replay clock."),
    ]
    if mode == IDLE_INPUT_PHYSICAL:
        assumptions.append(_record(
            "qpu_service.idle_dynamics.physical_inputs.environment_model",
            AssumptionBasis.SIMULATOR_CHOICE,
            f"Existing {UNIFIED_ENVIRONMENT_MODEL}; it derives idle rates and does not read snapshot T1/T2."))
        for name, value in _physical_input_values().items():
            if name == "environment_model":
                continue
            assumptions.append(_record(
                f"qpu_service.idle_dynamics.physical_inputs.{name}", AssumptionBasis.SIMULATOR_CHOICE,
                f"Explicit generic physical-model input ({value}); not a device calibration."))
    for qid in region:
        item = snapshot.qubits[qid]
        if mode == IDLE_INPUT_CALIBRATION:
            for name, value in (("t1_us", item.t1_us), ("t2_us", item.t2_us)):
                assumptions.append(_record(
                    f"qpu_service.idle_dynamics.qubits.{qid}.{name}", AssumptionBasis.MEASURED,
                    f"Per-qubit value from {snapshot.snapshot_date} calibration; historical, not a current representative value.",
                    evidence=evidence))
            assumptions.append(_record(
                f"qpu_service.idle_dynamics.qubits.{qid}.equilibrium_excited_population",
                AssumptionBasis.SIMULATOR_CHOICE,
                "Editable zero-population assumption; the snapshot does not provide this value."))
        else:
            # Snapshot is deliberately not used for idle T1/T2 in this mode.
            pass
        p10, p01 = item.p_one_given_zero, item.p_zero_given_one
        assumptions.extend((
            _record(f"qpu_service.reset_model.qubits.{qid}.observed_one_given_zero",
                    AssumptionBasis.MEASURED, "Per-qubit readout assignment error from verified snapshot.", evidence=evidence),
            _record(f"qpu_service.reset_model.qubits.{qid}.observed_zero_given_one",
                    AssumptionBasis.MEASURED, "Per-qubit readout assignment error from verified snapshot.", evidence=evidence),
            _record(f"qpu_service.reset_model.qubits.{qid}.success_probability",
                    AssumptionBasis.SIMULATOR_CHOICE,
                    f"Derived approximation 1-(P(1|0)+P(0|1))/2 = {1-(p10+p01)/2:.12g}; not directly measured.",
                    evidence=evidence),
        ))
    for edge, item in cz.items():
        assumptions.extend((
            _record(f"qpu_service.circuit_execution.cz_gate_calibrations.{edge}.duration_us",
                    AssumptionBasis.MEASURED, "Per-edge CZ duration from verified snapshot.", evidence=evidence),
            _record(f"qpu_service.circuit_execution.cz_gate_calibrations.{edge}.depolarizing_probability",
                    AssumptionBasis.SIMULATOR_CHOICE,
                    "Maps two-qubit average infidelity r to replacement-channel probability p=4r/3 (d=4); selected as the simulator channel model, not uniquely implied by calibration data.",
                    evidence=evidence),
        ))
    assumptions.append(_record(
        "qpu_service.circuit_execution.gate_durations_us.CZ", AssumptionBasis.SIMULATOR_CHOICE,
        "Schema fallback only; compiled CZ operations use the pair-specific snapshot duration."))
    assumptions.extend((
        _record("qpu_service.circuit_execution.gate_durations_us.CNOT", AssumptionBasis.SIMULATOR_CHOICE,
                "Schema fallback only, derived as 2×maximum calibrated H duration plus maximum CZ duration. CNOT execution is lowered to calibrated H-CZ-H primitives, not this aggregate."),
        _record("qpu_service.circuit_execution.supported_gates", AssumptionBasis.SIMULATOR_CHOICE,
                "Logical CNOT is implemented by calibrated H-CZ-H decomposition; CZ calibration is selected by the unordered physical edge."),
    ))
    return assumptions


def _interleaving_assumptions(region: tuple[int, ...]) -> list[AssumptionRecord]:
    paper = "Mi et al., ACM CCS 2022: cross-tenant leakage through imperfect qubit reset"
    return [
        _record("qpu_service.circuit_execution.supported_initializations", AssumptionBasis.SIMULATOR_CHOICE,
                "The service accepts a Program that omits its own initial reset (inherit). An attacker circuit that "
                "starts straight at its gates and measurement is an ordinary job; this is what exposes a state left by "
                "an imperfect reset."),
        _record(ABSTRACT_INTERLEAVE, AssumptionBasis.SIMULATOR_CHOICE,
                "The interleaved observation does not exist in real services. It is off by default and available "
                "only when the author (free mode) or the preset (challenge) enables it; when off the service does "
                "not list the interleave operations at all."),
        _record(ABSTRACT_VICTIM_ACTIVITY, AssumptionBasis.SIMULATOR_CHOICE,
                "Reading who ran when behind one's own job (victim run, service reset, attacker measurement) does "
                "not exist in real services. It is off by default, available only when the author enables it in "
                "free mode, and never in a challenge preset. It returns only the recorded steps and world times, "
                "never a state, a measured value or a tenant identity."),
        _record("attacker.operation_grants", AssumptionBasis.SIMULATOR_CHOICE,
                "The attacker may submit inherit Programs and interleaved observation runs. The server fixes this "
                "grant; the browser cannot widen it."),
        _record("attacker.interleaved_access.max_rounds_per_query", AssumptionBasis.SIMULATOR_CHOICE,
                "Assumes the attacker's job is scheduled directly after every victim job on the same qubit, one "
                "attacker shot per victim run (the co-scheduling that the cited attack needs). It is a scenario "
                "condition, not a guarantee of any real queue; the cap is also a host-protection limit."),
        _record("victim.interleaved_run.secret_bit", AssumptionBasis.PAPER_ASSUMPTION,
                "The victim repeatedly prepares one classical bit on the shared qubit and measures it, which leaves "
                "that bit as the state the service must reset.", evidence=paper),
        _record("victim.interleaved_run.program", AssumptionBasis.SIMULATOR_CHOICE,
                "The victim runs: initialize, apply X when the bit is 1, measure. Known-label training runs use the "
                "same program with the bit fixed to 0 or 1."),
        _record(RESET_CONFIGURATION_ID, AssumptionBasis.SIMULATOR_CHOICE,
                "Digest of the per-qubit reset model; recomputed by the server when reset success probabilities are "
                "edited, never accepted from the browser."),
    ]


def _physical_input_values() -> dict[str, object]:
    profile = GenericTransmonProfile()
    return {
        "device_quality": 0.8,
        "temperature_mk": profile.default_temperature_mk,
        "flux_noise_phi0": profile.flux_noise_min_phi0,
        "qubit_frequency_ghz": profile.qubit_frequency_ghz,
        "t1_max_us": profile.t1_max_us,
        "tphi_max_us": profile.tphi_max_us,
    }


def _fields(region: tuple[int, ...], mode: str, calibrations: Mapping[int, object],
            physical_inputs: Mapping[str, object], reset_duration: float,
            measurement_duration: float) -> tuple[SettingField, ...]:
    fields = [
        SettingField(ABSTRACT_INTERLEAVE, "抽象コマンド: 交互観測を有効にする", "boolean",
                     "現実のサービスには無い操作です。初期値はオフです。オンにすると、攻撃者のジョブに"
                     "submit --interleave を付けて、被害者の実行を各shotの直前に挟めます。",
                     "実在のクラウドでは、他の利用者のジョブが自分の測定の直前に実行されるよう指定する手段はありません。"
                     "この設定は、論文の攻撃条件（被害者の直後に攻撃者が測る）を、待ち行列の偶然に頼らず再現するための"
                     "シミュレーター上の便宜です。オフのときは、通常のジョブ送信だけが使えます。"),
        SettingField(ABSTRACT_VICTIM_ACTIVITY, "抽象コマンド: 被害者の動きの表示を有効にする", "boolean",
                     "現実のサービスには無い操作です。初期値はオフです。オンにすると、交互観測のジョブのあとに "
                     "activity（または画面の「被害者の動き」）で、shotごとの「被害者の実行 → サービスのReset → "
                     "攻撃者の測定」の順番と世界時刻を読めます。自由モード専用で、課題モードでは使えません。",
                     "実在のクラウドでは、自分の測定の合間に他の利用者が何を実行したかを知る手段はありません。"
                     "この設定は、論文の攻撃条件が成り立つ仕組み（被害者の直後にサービスのResetを挟んで攻撃者が"
                     "測る）を、画面で確かめて学ぶためのシミュレーター上の便宜です。読み出せるのは記録された"
                     "手順と時刻だけで、状態や被害者の測定値は含みません。"),
        SettingField(INTERLEAVED_RUN_SECRET_BIT, "被害者の秘密bit", "integer",
                     "被害者が毎回準備して測定する1 qubitの値です。自由モードでは見て変更できます。課題モードでは非公開になります。",
                     "共有qubitの交代を「被害者の実行 → サービスのReset → 攻撃者の測定」の繰り返しとして観測する実験で、"
                     "被害者が流す値です。実在のサービスの利用者の値ではありません。",
                     0, 1),
        _integer(INTERLEAVED_MAX_ROUNDS, "1回の観測で攻撃者が繰り返せる回数", 1, FREE_INTERLEAVED_ROUNDS_CEILING,
                 "攻撃者が1回の観測要求で、被害者の実行の直後に自分の測定を挟める回数の上限です。"
                 "ホストの計算量を守るための上限でもあります。"),
        SettingField(
            "qpu_service.runtime_execution.placement_policy", "配置の方針", "choice",
            "シナリオが決める配置の条件です。現実のクラウドの配置保証ではありません。",
            "同じ領域: 両利用者が同じ6 qubitを順番に使用し、交代時のResetをモデル化。"
            "別領域: 各利用者に重ならない6 qubitを割り当て、未使用側の状態も保持。"
            "ジョブはどちらも共通FIFOで処理し、同時実行はしません。",
            choices=(PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT.value,
                     PlacementPolicy.TENANT_DEDICATED_REGION.value),
        ),
        _integer("qpu_service.runtime_execution.reset_count", "交代時のReset回数", 0, 8,
                 "利用者が交代するときにサービスが行うResetの回数です。shotごとのResetではありません。"),
        _number("qpu_service.runtime_execution.victim_operation_duration_us", "被害者の操作時間",
                0.001, 1000.0, "us", "シミュレーターが仮定した時間です。スナップショットの較正値ではありません。"),
        _number("qpu_service.runtime_execution.repetition_delay_us", "shot間の待機", 0.0, 1e6, "us",
                "回路を繰り返すとき、前のshotと次のshotの間に置く待機です。初期値は0（待機なし）で、実サービスの値ではありません。"),
        _number("qpu_service.declared_service_timing.processing_delay_us", "サービスの処理遅延", 0.0, 1e6,
                "us", "仮想環境の中の遅延です。PCの処理時間とは別です。"),
        _number("qpu_service.declared_service_timing.publication_delay_us", "結果公開の遅延", 0.0, 1e6,
                "us", "仮想環境の中の遅延です。PCの処理時間とは別です。"),
        _number("qpu_service.declared_service_timing.denial_delay_us", "拒否応答の遅延", 0.0, 1e6,
                "us", "仮想環境の中の遅延です。PCの処理時間とは別です。"),
        _number("qpu_service.live_ingress.outbound_delay_us", "受付までの通信遅延", 0.0, 1e6,
                "us", "仮想環境の中の遅延です。PCの処理時間や実際の通信時間とは別です。"),
        _number("qpu_service.control_timing.processing_delay_us", "制御操作の処理遅延", 0.0, 1e6,
                "us", "仮想環境の中の遅延です。"),
        _number("qpu_service.control_timing.publication_delay_us", "制御操作の応答遅延", 0.0, 1e6,
                "us", "仮想環境の中の遅延です。"),
        SettingField("experiment.live_time_policy.pause_while_catching_up", "計算が追いつくまで時計を止める", "boolean",
                     "PCの計算の遅れを補う任意の設定です。初期値はオフです。",
                     "数値計算が追いつくまでだけ世界時刻を止める。利用者の操作待ちや通信待ちでは止めない。"),
    ]
    fields.extend(_reset_success_fields(region, calibrations))
    if mode == IDLE_INPUT_CALIBRATION:
        fields.extend(_qubit_noise_fields(region, calibrations, "qpu_service.idle_dynamics.qubits"))
    else:
        defaults = GenericTransmonProfile()
        for name, label, lower, upper, unit in (
            ("device_quality", "デバイス品質", 0.0, 1.0, None),
            ("temperature_mk", "温度", 0.0, 100.0, "mK"),
            ("flux_noise_phi0", "磁束ノイズ", 0.0, 1e-3, "Phi0"),
            ("qubit_frequency_ghz", "qubit周波数", 0.1, 20.0, "GHz"),
            ("t1_max_us", "T1の上限", 0.001, 1e6, "us"),
            ("tphi_max_us", "Tφの上限", 0.001, 1e6, "us"),
        ):
            fields.append(_number(f"qpu_service.idle_dynamics.physical_inputs.{name}", label,
                                  lower, upper, unit, "本体シミュレーターと同じ物理モデルへの入力です。スナップショットの較正値ではありません。",
                                  required=name == "qubit_frequency_ghz"))
    return tuple(fields)


def _record(path: str, basis: AssumptionBasis, rationale: str, *, evidence: str | None = None,
            applicability: Mapping[str, str] | None = None) -> AssumptionRecord:
    return AssumptionRecord(path.replace(".", "_").replace(":", "_"), (path,), basis, rationale,
                            evidence_ref=evidence,
                            applicability={} if applicability is None else dict(applicability))


def _number(path: str, label: str, minimum: float, maximum: float, unit: str | None,
            description: str, *, required: bool = False) -> SettingField:
    return SettingField(path, label, "number", description, description, minimum, maximum,
                        unit=unit, required=required)


def _integer(path: str, label: str, minimum: int, maximum: int, description: str) -> SettingField:
    return SettingField(path, label, "integer", description, description, minimum, maximum)
