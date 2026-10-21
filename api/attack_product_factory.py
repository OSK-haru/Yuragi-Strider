"""Strict product assembly from an established, immutable scenario manifest.

This is an opt-in in-process factory. It does not mount routes or pick a
scenario, topology, initial state, authority, physics, timing, or host limits.
The caller prepares one free/challenge ScenarioEnvironment, then supplies the
trusted real-owner context and explicit host resource bounds here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import secrets
from typing import Any

from api.attack_live_world import LiveWorkloadWorld
from api.attack_host_clock_world import HostClockWorldController
from api.attack_live_driver import LiveWorldDriver, LiveDriverSettings
from api.attack_paused_world import PausedWorldController, VirtualHostClock
from api.attack_sessions import AttackSession, SessionHostLimits, WebOwnerContext
from api.attack_workloads import DeclaredWorkloadRunner
from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.declared_execution import DeclaredQpuExecutor
from core.attack_simulation.concurrency_policy import OVERLAP_DISJOINT_REGIONS, cross_region_transfers
from core.attack_simulation.concurrent_lanes import ConcurrentRegions
from core.attack_simulation.crosstalk_samples import SAMPLE_PULSE_SHAPE
from core.attack_simulation.declared_service_timing import DeclaredServiceTiming
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.job_service import InterleaveVictimSpec, JobApplicationService, PrincipalGrant
from core.attack_simulation.program_request import CircuitProgram
from core.attack_simulation.live_service_config import parse_live_service_config
from core.attack_simulation.qpu_state import (
    InactiveRegionState, QpuStateSnapshot, QpuStateStore, local_zero_snapshot,
)
from core.attack_simulation.qpu_topology import QpuTopology, RetainedRegionBinding, TopologyLimits
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.resource_binding import PhysicalQubitLayout, PhysicalQubitRef, ResetResourceBinding
from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import ScenarioMode
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.attack_simulation.virtual_credentials import VirtualCredentialAuthority
from core.attack_simulation.declared_world_clock import WorldClockSettings
from core.attack_simulation.declared_world_clock import CatchUpMonotonicSource
from core.attack_simulation.live_time_policy import EXPLICIT_WAIT, HOST_CLOCK, parse_live_time_policy
from core.attack_simulation.workload_config import parse_workload_plan
from core.device_profiles import GenericTransmonProfile
from core.experimental_isolation_physics.idle_evolution import (
    IdleQubitSpec, IndependentIdleModel,
)
from core.experimental_isolation_physics.imperfect_reset import ImperfectResetSpec
from core.experimental_isolation_physics.reset_model import (
    PerQubitPhenomenologicalResetModel, PhenomenologicalResetModel, ResetModelRegistry,
)
from core.physical_environment import (
    INPUT_MODE_PHYSICAL, UNIFIED_ENVIRONMENT_MODEL, PhysicalEnvironmentInputs,
    compute_environment_rates_per_qubit,
)
from core.results import EnvironmentConfig


D62_CALIBRATION_SOURCE = "validation_results/phase3b_runtime_calibration.json"
D62_CALIBRATION_BASIS = (
    "IBM Kingston physical qubit 150; calibration timestamp 2026-08-03T08:28:25+09:00; "
    "job d9nlh5ssfqic73ar6f30. Historical single-qubit calibration, not a current-device "
    "or device-wide representative value."
)
IDLE_INPUT_CALIBRATION = "calibration_values"
IDLE_INPUT_PHYSICAL = "physical_parameters"


def standard_calibration_initial_values() -> dict[str, object]:
    """Values for a configuration form only; never applied by runtime implicitly.

    The caller may seed an explicitly selected physical-qubit configuration
    from this catalog and must freeze its values and per-field provenance into
    the manifest.  No topology, retained region, initial state, victim action,
    service authority, or waiting model is selected here.
    """
    direct = f"Measured calibration value. {D62_CALIBRATION_BASIS}"
    derived_reset = (
        "Derived approximation: reset failure probability is the mean of the two readout "
        "assignment errors; reset success is one minus that value. Not directly measured. "
        + D62_CALIBRATION_BASIS
    )
    t1, t2 = 300.1614217896348, 315.2699283032744
    tphi = 1.0 / (1.0 / t2 - 1.0 / (2.0 * t1))
    p10, p01 = 0.006591796875, 0.007568359375
    readout_error = (p10 + p01) / 2.0
    entries = {
        "t1_us": (t1, direct),
        "t2_us_source": (t2, direct),
        "tphi_us": (tphi, f"Derived from measured T1/T2 using 1/Tphi=1/T2-1/(2*T1). {D62_CALIBRATION_BASIS}"),
        "readout_p1_given_0": (p10, direct),
        "readout_p0_given_1": (p01, direct),
        "readout_error": (readout_error,
                          f"Derived mean of the two measured readout assignment errors. {D62_CALIBRATION_BASIS}"),
        "measurement_duration_us": (2.180, direct),
        "reset_duration_us": (2.212, direct),
        "single_qubit_gate_duration_us": (0.032, direct),
        "thermal_equilibrium_excited_population": (0.0, "Editable zero-population assumption; no corresponding value in the cited calibration."),
        "reset_success_probability": (1.0 - readout_error, derived_reset),
    }
    return {
        "source": D62_CALIBRATION_SOURCE,
        "device": "ibm_kingston",
        "physical_qubit_id": 150,
        "calibrated_at": "2026-08-03T08:28:25+09:00",
        "job_id": "d9nlh5ssfqic73ar6f30",
        "scope_notice": "Historical values for one physical qubit only; not current or device-wide representative values.",
        "values": {key: {"value": value, "basis": basis} for key, (value, basis) in entries.items()},
    }


@dataclass(frozen=True)
class ProductAssemblyLimits:
    """Host-side bounds injected by the server, never user scenario settings."""

    session: SessionHostLimits
    topology: TopologyLimits
    max_runtime_jobs: int
    max_events_per_job: int
    max_unresolved_events: int
    max_workload_pending: int
    max_workload_history: int
    # Newest terminal job records kept per world; None keeps them all. The
    # runtime bound must leave room for queued jobs and jobs still named by
    # pending requests on top of this.
    retained_jobs: int | None = None

    def __post_init__(self) -> None:
        if type(self.session) is not SessionHostLimits or type(self.topology) is not TopologyLimits:
            raise TypeError("explicit session and topology host limits are required")
        for name in ("max_runtime_jobs", "max_events_per_job", "max_unresolved_events",
                     "max_workload_pending", "max_workload_history"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be an explicit positive host limit")
        if self.retained_jobs is not None and (
                type(self.retained_jobs) is not int
                or not 1 <= self.retained_jobs < self.max_runtime_jobs):
            raise ValueError("retained_jobs must leave room below max_runtime_jobs")


@dataclass
class ProductAttackEnvironment:
    """Supervisor-owned result; only the attacker virtual credential is exposed."""

    controller: object
    attacker_credential: str
    manifest_id: str
    mode: ScenarioMode
    driver: LiveWorldDriver | None = None
    # Hidden-case grader for challenge environments; never serialized to clients.
    evaluator: object | None = None

    def close(self) -> None:
        if self.driver is not None:
            self.driver.stop()
        self.controller.close()


class ProductEnvironmentFactory:
    """Assemble the common runtime from frozen manifest inputs."""

    def __init__(self, limits: ProductAssemblyLimits) -> None:
        if type(limits) is not ProductAssemblyLimits:
            raise TypeError("explicit product assembly limits are required")
        self._limits = limits

    def assemble(self, environment: ScenarioEnvironment, owner: WebOwnerContext) -> ProductAttackEnvironment:
        if type(environment) is not ScenarioEnvironment or type(owner) is not WebOwnerContext:
            raise TypeError("a prepared scenario environment and trusted owner are required")
        manifest = environment.establish()
        if type(manifest) is not ResolvedScenarioManifest:
            raise TypeError("only a frozen scenario manifest can be assembled")
        time_policy = manifest.experiment_config.get("live_time_policy")
        if not isinstance(time_policy, Mapping):
            raise ValueError("the product assembly path requires a frozen time policy")
        if not self._has_provenance(manifest):
            raise ValueError("physical and service timing values require manifest basis records")

        parsed = _parse_frozen_inputs(manifest, self._limits)
        authority = VirtualCredentialAuthority()
        credentials: dict[str, str] = {}
        session = runner = executor = world = controller = driver = None
        try:
            principals = parsed["principals"]
            for principal in principals:
                credentials[principal.principal_id] = authority.issue(principal.principal_id)

            runtime, service = build_service_stack(parsed, manifest)
            session = AttackSession(
                environment=environment, service=service, authority=authority,
                run_next=runtime.run_next, limits=self._limits.session,
            )
            session._bind_web_owner(owner)
            policy = parsed["experiment_policy"]
            clock = (CatchUpMonotonicSource() if policy.mode == HOST_CLOCK
                     else VirtualHostClock(start_ns=0))
            workload_credentials = {
                actor_id: credentials[actor_id]
                for actor_id in parsed["workload_actor_ids"]
            }
            runner = DeclaredWorkloadRunner(
                session=session, credentials=workload_credentials,
                run_id=f"run-{secrets.token_hex(16)}",
                max_pending=self._limits.max_workload_pending,
                max_history=self._limits.max_workload_history,
                host_ns=clock,
            )
            executor = DeclaredQpuExecutor(
                runtime=runtime,
                clock_domain=runner._clock.settings.clock_domain,
                max_jobs=self._limits.max_runtime_jobs,
                max_events_per_job=self._limits.max_events_per_job,
                max_unresolved=self._limits.max_unresolved_events,
                idle_model=parsed["idle_model"],
                concurrent_regions=concurrent_regions_for(parsed),
            )
            world = LiveWorkloadWorld(
                runner=runner, executor=executor,
                producer_id=f"live-{secrets.token_hex(12)}",
                retained_jobs=self._limits.retained_jobs,
            )
            world.start()
            if policy.mode == HOST_CLOCK:
                controller = HostClockWorldController(world)
                driver = LiveWorldDriver(world, LiveDriverSettings(0.01, 16384, 5.0))
                driver.start()
            else:
                controller = PausedWorldController.from_manifest(world, clock)
            attacker_id = next(
                item.principal_id for item in principals
                if item.tenant_id == parsed["attacker_tenant_id"]
            )
            evaluator = None
            if manifest.submission_enabled:
                from api.attack_evaluators import EvaluatorContext, build_evaluator, tenant_run_counts
                victim_tenant = parsed["victim_tenant_id"]
                evaluator = build_evaluator(EvaluatorContext(
                    manifest, self._limits,
                    # Read by the supervisor from the live world, never by the participant.
                    lambda: tenant_run_counts(runtime, victim_tenant),
                ))
            return ProductAttackEnvironment(
                controller=controller, attacker_credential=credentials[attacker_id],
                manifest_id=manifest.manifest_id, mode=manifest.mode,
                driver=driver, evaluator=evaluator,
            )
        except BaseException as assembly_error:
            # Close only unpublished objects made by this attempt, in reverse
            # ownership order. This is infrastructure cleanup, never a QPU reset.
            cleanup_errors = []
            if driver is not None:
                driver.stop()
            owned = controller or world or runner or session or executor
            if owned is not None:
                try:
                    owned.close()
                except BaseException as error:
                    cleanup_errors.append(error)
                    for fallback in (world, runner, session, executor):
                        if fallback is None or fallback is owned:
                            continue
                        try:
                            fallback.close()
                        except BaseException as fallback_error:
                            cleanup_errors.append(fallback_error)
            authority.close()
            if cleanup_errors:
                raise RuntimeError("assembly failed and at least one partial owner could not close") from cleanup_errors[0]
            raise

    @staticmethod
    def _has_provenance(manifest: ResolvedScenarioManifest) -> bool:
        required = {
            "qpu_service.reset_model.success_probability",
            "qpu_service.reset_model.target_populations",
            "qpu_service.reset_model.applies_to_physical_qubit_ids",
            "qpu_service.reset_model.observed_one_given_zero",
            "qpu_service.reset_model.observed_zero_given_one",
            "qpu_service.runtime_execution.reset_duration_us",
            "qpu_service.runtime_execution.victim_operation_duration_us",
            "qpu_service.runtime_execution.attacker_measurement_duration_us",
            "qpu_service.runtime_execution.program_measurement_duration_us",
            "qpu_service.runtime_execution.repetition_delay_us",
            "qpu_service.runtime_execution.entry_reuse_tolerance",
            "qpu_service.runtime_execution.concurrency_policy",
            "qpu_service.runtime_execution.drive_crosstalk_strength",
            "qpu_service.runtime_execution.drive_crosstalk_direction",
            "qpu_service.declared_service_timing.processing_delay_us",
            "qpu_service.declared_service_timing.publication_delay_us",
            "qpu_service.declared_service_timing.denial_delay_us",
            "qpu_service.live_ingress.outbound_delay_us",
            "qpu_service.control_timing.processing_delay_us",
            "qpu_service.control_timing.publication_delay_us",
            "qpu_service.topology",
            "experiment.world_clock.rate",
        }
        policy = manifest.experiment_config.get("live_time_policy")
        if isinstance(policy, Mapping) and policy.get("mode") == HOST_CLOCK:
            required.add("experiment.live_time_policy.pause_while_catching_up")
        idle = manifest.qpu_service_config.get("idle_dynamics")
        initial = manifest.qpu_service_config.get("initial_state")
        required.add("qpu_service.initial_state.kind" if isinstance(initial, Mapping)
                     and initial.get("kind") == "all_zero_register"
                     else "qpu_service.initial_state.density_matrix")
        reset = manifest.qpu_service_config.get("reset_model")
        if isinstance(reset, Mapping) and isinstance(reset.get("qubits"), Mapping):
            required.discard("qpu_service.reset_model.success_probability")
            required.discard("qpu_service.reset_model.observed_one_given_zero")
            required.discard("qpu_service.reset_model.observed_zero_given_one")
            for qubit_id in reset["qubits"]:
                for field in ("success_probability", "observed_one_given_zero", "observed_zero_given_one"):
                    required.add(f"qpu_service.reset_model.qubits.{qubit_id}.{field}")
        snapshot = manifest.qpu_service_config.get("device_snapshot")
        if isinstance(snapshot, Mapping):
            required.update({
                "qpu_service.device_snapshot",
                "qpu_service.runtime_execution.measure_input_state_before_transition",
                "qpu_service.runtime_execution.placement_policy",
            })
        if isinstance(idle, Mapping):
            required.add("qpu_service.idle_dynamics.input_mode")
            if idle.get("input_mode") == IDLE_INPUT_CALIBRATION and isinstance(idle.get("qubits"), Mapping):
                for qubit_id in idle["qubits"]:
                    for field in ("t1_us", "t2_us", "equilibrium_excited_population"):
                        required.add(f"qpu_service.idle_dynamics.qubits.{qubit_id}.{field}")
            elif idle.get("input_mode") == IDLE_INPUT_PHYSICAL and isinstance(idle.get("physical_inputs"), Mapping):
                for field in ("environment_model", "device_quality", "temperature_mk",
                              "flux_noise_phi0", "qubit_frequency_ghz", "t1_max_us", "tphi_max_us"):
                    required.add(f"qpu_service.idle_dynamics.physical_inputs.{field}")
                overrides = idle["physical_inputs"].get("per_qubit")
                if isinstance(overrides, Mapping):
                    for qubit_id, values in overrides.items():
                        if isinstance(values, Mapping):
                            for field in values:
                                required.add(
                                    f"qpu_service.idle_dynamics.physical_inputs.per_qubit.{qubit_id}.{field}"
                                )
        circuit = manifest.qpu_service_config.get("circuit_execution")
        if isinstance(circuit, Mapping) and "single_qubit_native_gates" in circuit:
            required.update({"qpu_service.circuit_execution.single_qubit_native_gates",
                             "qpu_service.circuit_execution.single_qubit_gate_decompositions"})
            if "backend_declaration" in circuit:
                required.add("qpu_service.circuit_execution.backend_declaration")
        if isinstance(circuit, Mapping) and isinstance(circuit.get("gate_durations_us"), Mapping):
            for gate in circuit["gate_durations_us"]:
                required.add(f"qpu_service.circuit_execution.gate_durations_us.{gate}")
        if isinstance(circuit, Mapping) and isinstance(circuit.get("cz_gate_calibrations"), Mapping):
            required.add("qpu_service.circuit_execution.cz_gate_calibrations")
            for edge, calibration in circuit["cz_gate_calibrations"].items():
                if isinstance(calibration, Mapping):
                    for field in calibration:
                        required.add(f"qpu_service.circuit_execution.cz_gate_calibrations.{edge}.{field}")
        if isinstance(circuit, Mapping) and isinstance(circuit.get("two_qubit_gate_calibrations"), Mapping):
            required.add("qpu_service.circuit_execution.two_qubit_gate_calibrations")
            for gate, edges in circuit["two_qubit_gate_calibrations"].items():
                if isinstance(edges, Mapping):
                    for edge, calibration in edges.items():
                        if isinstance(calibration, Mapping):
                            for field in calibration:
                                required.add(
                                    f"qpu_service.circuit_execution.two_qubit_gate_calibrations.{gate}.{edge}.{field}")
        if manifest.qpu_service_config.get("dedicated_region") is not None:
            required.update({
                "qpu_service.dedicated_region.physical_qubit_ids",
                "qpu_service.dedicated_region.reset_model",
                "qpu_service.dedicated_region.idle_dynamics",
                "qpu_service.dedicated_region.initial_state",
                "qpu_service.dedicated_region.reset_duration_us",
                "qpu_service.dedicated_region.program_measurement_duration_us",
                "qpu_service.dedicated_region.reset_model_configuration_id",
            })
        declared = {path for record in manifest.assumptions for path in record.target_paths}
        return required <= declared


def _parse_frozen_inputs(manifest: ResolvedScenarioManifest, limits: ProductAssemblyLimits) -> dict[str, Any]:
    qpu = manifest.qpu_service_config
    topology = QpuTopology.from_config(qpu.get("topology"), limits=limits.topology)
    runtime_raw = _object(qpu, "runtime_execution", {
        "schema_version", "reset_model_id", "reset_model_configuration_id", "reset_model_mode",
        "reset_count", "reset_duration_us", "victim_operation_duration_us",
        "attacker_measurement_duration_us", "queue_policy_id", "cancellation_policy_id",
        "measure_input_state_before_transition", "placement_policy", "isolated_assignment_duration_us",
        "program_measurement_duration_us", "repetition_delay_us", "entry_reuse_tolerance",
        "concurrency_policy", "drive_crosstalk_strength", "drive_crosstalk_direction", "resource_binding",
        "max_local_qubits", "max_execution_work",
    })
    if runtime_raw["schema_version"] != "shared_qpu_runtime_v3":
        raise ValueError("unsupported frozen QPU runtime schema")
    _validate_runtime_profile(runtime_raw)
    runtime_fields = dict(runtime_raw)
    runtime_fields.pop("schema_version")
    runtime_fields["reset_model_mode"] = str(runtime_fields["reset_model_mode"])
    runtime_fields["placement_policy"] = PlacementPolicy(runtime_fields["placement_policy"])
    binding_raw = runtime_fields["resource_binding"]
    if binding_raw is not None:
        if not isinstance(binding_raw, Mapping) or set(binding_raw) != {"victim", "attacker"}:
            raise ValueError("runtime resource_binding must explicitly name both actors")
        refs = {}
        for role in ("victim", "attacker"):
            ref = binding_raw[role]
            if not isinstance(ref, Mapping) or set(ref) != {"qpu_id", "physical_qubit_id"}:
                raise ValueError("invalid frozen physical resource reference")
            refs[role] = PhysicalQubitRef(**dict(ref))
        runtime_fields["resource_binding"] = ResetResourceBinding(**refs)
    runtime_config = SharedQpuRuntimeConfig(**runtime_fields)

    retained = _object(qpu, "retained_region", {"physical_qubit_ids"})["physical_qubit_ids"]
    if type(retained) not in (list, tuple):
        raise ValueError("retained_region.physical_qubit_ids must be an explicit list")
    region = RetainedRegionBinding(topology, tuple(retained), runtime_config.max_local_qubits)
    device_snapshot = qpu.get("device_snapshot")
    if device_snapshot is not None:
        execution_config = qpu.get("circuit_execution", {})
        declaration = execution_config.get("backend_declaration") if isinstance(execution_config, Mapping) else None
        _validate_device_snapshot_binding(device_snapshot, topology, region, declaration)
    reset_raw = _object(qpu, "reset_model", {
        "model_id", "target_populations", "applies_to_physical_qubit_ids",
    }, optional={
        "success_probability", "observed_one_given_zero", "observed_zero_given_one", "qubits",
    })
    if reset_raw["model_id"] != runtime_config.reset_model_id:
        raise ValueError("reset model ID differs from the frozen runtime profile")
    if (type(reset_raw["target_populations"]) not in (list, tuple)
            or any(type(value) not in (int, float) for value in reset_raw["target_populations"])):
        raise ValueError("reset target populations must be explicit real values")
    scope = reset_raw["applies_to_physical_qubit_ids"]
    if (type(scope) not in (list, tuple) or tuple(scope) != region.physical_qubit_ids
            or any(type(value) is not int for value in scope)):
        raise ValueError("reset/readout model scope must explicitly match the retained physical-qubit order")
    if "qubits" in reset_raw:
        if set(reset_raw) != {
            "model_id", "target_populations", "applies_to_physical_qubit_ids", "qubits",
        }:
            raise ValueError("per-qubit reset mode cannot contain scalar calibration values")
        model = _parse_per_qubit_reset(reset_raw, region.physical_qubit_ids)
    else:
        if set(reset_raw) != {
            "model_id", "target_populations", "applies_to_physical_qubit_ids",
            "success_probability", "observed_one_given_zero", "observed_zero_given_one",
        }:
            raise ValueError("legacy reset model requires all scalar calibration values")
        for name in ("success_probability", "observed_one_given_zero", "observed_zero_given_one"):
            value = reset_raw[name]
            if type(value) not in (int, float) or not math.isfinite(float(value)) or not 0.0 <= value <= 1.0:
                raise ValueError(f"reset_model.{name} must be an explicit probability")
        model = PhenomenologicalResetModel(
            ImperfectResetSpec(reset_raw["success_probability"], tuple(reset_raw["target_populations"])),
            reset_raw["observed_one_given_zero"], reset_raw["observed_zero_given_one"],
            model_id=reset_raw["model_id"],
        )
    if model.configuration_id != runtime_config.reset_model_configuration_id:
        raise ValueError("reset model parameters do not match the frozen configuration ID")
    reset_registry = ResetModelRegistry()
    reset_registry.register(model)

    idle_raw = _object(qpu, "idle_dynamics", {"model_id", "input_mode"},
                       optional={"qubits", "physical_inputs"})
    idle_model = _build_idle_model(
        idle_raw, region.physical_qubit_ids, runtime_config.max_local_qubits,
    )

    initial = _object(qpu, "initial_state", {"device_config_version"},
                      optional={"density_matrix", "kind"})
    if type(initial["device_config_version"]) is not str:
        raise ValueError("initial_state.device_config_version must be explicit text")
    if set(initial) == {"device_config_version", "kind"} and initial["kind"] == "all_zero_register":
        snapshot = local_zero_snapshot(
            qpu_id=topology.qpu_id, physical_qubit_ids=region.physical_qubit_ids,
            device_config_version=initial["device_config_version"],
        )
    elif set(initial) == {"device_config_version", "density_matrix"}:
        raw_matrix = initial["density_matrix"]
        if type(raw_matrix) not in (list, tuple):
            raise ValueError("initial_state.density_matrix must be explicit")
        matrix = tuple(tuple(_complex_pair(value) for value in row) for row in raw_matrix)
        snapshot = QpuStateSnapshot(
            schema_version="shared_qpu_state_v1", qpu_id=topology.qpu_id, revision=0,
            device_time_us=0.0, physical_qubit_ids=region.physical_qubit_ids,
            local_dimension=2, density_matrix=matrix,
            device_config_version=initial["device_config_version"],
        )
    else:
        raise ValueError("initial_state must select exactly one explicit representation")

    alternate_binding = alternate_snapshot = alternate_registry = alternate_idle = None
    alternate_durations = None
    alternate_raw = qpu.get("dedicated_region")
    if alternate_raw is not None:
        required = {"physical_qubit_ids", "reset_model", "idle_dynamics", "initial_state",
                    "reset_model_configuration_id", "reset_duration_us", "program_measurement_duration_us"}
        if not isinstance(alternate_raw, Mapping) or set(alternate_raw) != required:
            raise ValueError("alternate retained region must be a complete frozen profile")
        alternate_binding = RetainedRegionBinding(
            topology, tuple(alternate_raw["physical_qubit_ids"]), runtime_config.max_local_qubits,
        )
        alternate_ids = alternate_binding.physical_qubit_ids
        if (len(alternate_ids) != len(region.physical_qubit_ids)
                or set(alternate_ids) & set(region.physical_qubit_ids)):
            raise ValueError("retained regions must be disjoint and equally sized")
        if device_snapshot is not None and any(
                not device_snapshot["qubits"][str(qid)]["usable_for_placement"] for qid in alternate_ids):
            raise ValueError("alternate retained region includes ineligible qubits")
        alternate_initial = alternate_raw["initial_state"]
        if (not isinstance(alternate_initial, Mapping)
                or set(alternate_initial) != {"kind", "device_config_version"}
                or alternate_initial["kind"] != "all_zero_register"
                or alternate_initial["device_config_version"] != snapshot.device_config_version):
            raise ValueError("alternate initial state must be an explicit zero register")
        alternate_snapshot = local_zero_snapshot(
            qpu_id=topology.qpu_id, physical_qubit_ids=alternate_ids,
            device_config_version=alternate_initial["device_config_version"],
        )
        alternate_model = _parse_per_qubit_reset(alternate_raw["reset_model"], alternate_ids)
        if (alternate_model.model_id != runtime_config.reset_model_id
                or alternate_model.configuration_id != alternate_raw["reset_model_configuration_id"]):
            raise ValueError("alternate reset model differs from its frozen configuration")
        alternate_registry = ResetModelRegistry()
        alternate_registry.register(alternate_model)
        if idle_raw["input_mode"] == IDLE_INPUT_CALIBRATION:
            if alternate_raw["idle_dynamics"] is None:
                raise ValueError("alternate calibration values must be explicit")
            alternate_idle_raw = alternate_raw["idle_dynamics"]
        else:
            if alternate_raw["idle_dynamics"] is not None:
                raise ValueError("physical input mode must share its declared parameters")
            alternate_idle_raw = dict(idle_raw)
            physical_inputs = dict(alternate_idle_raw["physical_inputs"])
            physical_inputs["per_qubit"] = {
                qid: value for qid, value in physical_inputs["per_qubit"].items()
                if qid in {str(item) for item in alternate_ids}
            }
            alternate_idle_raw["physical_inputs"] = physical_inputs
        alternate_idle = _build_idle_model(alternate_idle_raw, alternate_ids, runtime_config.max_local_qubits)
        alternate_durations = (
            _finite_number(alternate_raw["reset_duration_us"], "alternate reset duration", minimum=0.0),
            _finite_number(alternate_raw["program_measurement_duration_us"],
                           "alternate measurement duration", minimum=0.0),
        )
    if (runtime_config.placement_policy is PlacementPolicy.TENANT_DEDICATED_REGION
            and alternate_binding is None):
        raise ValueError("dedicated placement requires a frozen alternate region")

    circuit = _object(qpu, "circuit_execution", {
        "compiler_id", "supported_gates", "max_program_qubits", "max_shots",
        "max_instruction_executions", "gate_durations_us",
    }, optional={"max_unitary_elements", "supported_initializations", "cz_gate_calibrations",
                 "two_qubit_gate_calibrations", "single_qubit_native_gates",
                 "single_qubit_gate_decompositions", "backend_declaration"})
    _basis_values(circuit["gate_durations_us"], "gate durations")
    if not isinstance(circuit["gate_durations_us"], Mapping):
        raise ValueError("all circuit gate durations must be explicit")
    compiler = LocalCircuitCompiler.from_manifest(manifest)
    if compiler.gate_durations_us is None:
        raise ValueError("product programs require frozen per-gate durations")

    authorization = JobAuthorizationPolicy.from_manifest(
        manifest, attacker_tenant_id=_actor_tenant(manifest, "attacker"),
        victim_tenant_id=_actor_tenant(manifest, "victim"),
    )
    principals = []
    for role, config in (("attacker", manifest.attacker_config), ("victim", manifest.victim_config)):
        principal_id, tenant_id = config.get("principal_id"), config.get("tenant_id")
        operations = config.get("operation_grants")
        if (type(principal_id) is not str or not principal_id.strip()
                or type(tenant_id) is not str or tenant_id != _actor_tenant(manifest, role)
                or type(operations) not in (list, tuple)):
            raise ValueError(f"manifest {role} principal, tenant, and operation grants are required")
        principals.append(PrincipalGrant(principal_id, tenant_id, frozenset(operations)))
    if len({item.principal_id for item in principals}) != len(principals):
        raise ValueError("attacker and victim principal IDs must be distinct")

    experiment_policy = parse_live_time_policy(manifest.experiment_config.get("live_time_policy"))
    if experiment_policy.mode not in {EXPLICIT_WAIT, HOST_CLOCK}:
        raise ValueError("unsupported product time policy")
    clock_settings = WorldClockSettings(**dict(_object(manifest.experiment_config, "world_clock", {"clock_domain", "rate"})))
    if experiment_policy.mode == HOST_CLOCK and clock_settings.rate != 1.0:
        raise ValueError("product host_clock requires the approved 1x world-time rate")
    workload_actor_ids = []
    for role, config in (("attacker", manifest.attacker_config), ("victim", manifest.victim_config)):
        workload = config.get("workload")
        if workload is not None:
            if not isinstance(workload, Mapping):
                raise ValueError(f"{role} workload must be an explicit object")
            plan = parse_workload_plan(workload)
            if (plan.actor_id != config.get("principal_id")
                    or plan.clock_domain != clock_settings.clock_domain):
                raise ValueError(f"{role} workload actor and clock domain must match the frozen manifest")
            workload_actor_ids.append(plan.actor_id)

    timing = _object(qpu, "declared_service_timing", {
        "processing_delay_us", "publication_delay_us", "denial_delay_us", "basis",
    })
    if type(timing["basis"]) is not str or not timing["basis"].strip():
        raise ValueError("declared service timing requires an explicit basis")
    declared_timing = DeclaredServiceTiming(
        clock_settings.clock_domain,
        timing["processing_delay_us"], timing["publication_delay_us"],
        timing["denial_delay_us"],
    )
    live_service = parse_live_service_config(
        manifest, max_pending=limits.max_workload_pending,
        max_history=limits.max_workload_history,
    )
    if live_service.ingress.clock_domain != declared_timing.clock_domain:
        raise ValueError("live ingress and declared service timing domains differ")
    return {
        "runtime_config": runtime_config, "region": region, "snapshot": snapshot,
        "reset_registry": reset_registry, "idle_model": idle_model,
        "alternate_region": alternate_binding, "alternate_snapshot": alternate_snapshot,
        "alternate_reset_registry": alternate_registry, "alternate_idle_model": alternate_idle,
        "alternate_durations": alternate_durations,
        "authorization": authorization, "principals": tuple(principals),
        "attacker_tenant_id": _actor_tenant(manifest, "attacker"),
        "victim_tenant_id": _actor_tenant(manifest, "victim"),
        "workload_actor_ids": tuple(workload_actor_ids), "live_service": live_service,
        "compiler": compiler, "experiment_policy": experiment_policy,
        "interleave_victims": _interleave_victims(manifest),
    }


def concurrent_regions_for(parsed: Mapping[str, Any]) -> ConcurrentRegions | None:
    """The frozen concurrent-sharing choice as the executor's input, or None for the exclusive FIFO."""
    config = parsed["runtime_config"]
    if config.concurrency_policy != OVERLAP_DISJOINT_REGIONS:
        return None
    transfers = cross_region_transfers(
        parsed["region"].topology.gate_edges, parsed["region"].physical_qubit_ids,
        parsed["alternate_region"].physical_qubit_ids,
        config.drive_crosstalk_strength, config.drive_crosstalk_direction)
    return ConcurrentRegions(transfers, SAMPLE_PULSE_SHAPE)


def build_service_stack(parsed: Mapping[str, Any], manifest: ResolvedScenarioManifest,
                        *, interleave_victims=None) -> tuple[SharedQpuRuntime, JobApplicationService]:
    """Runtime and job service from frozen inputs, without sessions, clocks or credentials.

    Shared by live assembly and the synchronous hidden-case grader so both use
    exactly the same physics and service rules.
    """
    principals = parsed["principals"]
    dedicated = parsed["runtime_config"].placement_policy is PlacementPolicy.TENANT_DEDICATED_REGION
    primary_ids = parsed["region"].physical_qubit_ids
    alternate = parsed["alternate_region"]
    tenant_regions = ({
        parsed["victim_tenant_id"]: primary_ids,
        parsed["attacker_tenant_id"]: alternate.physical_qubit_ids,
    } if dedicated else None)
    store = QpuStateStore(
        parsed["snapshot"], region_binding=parsed["region"],
        alternate_region_binding=alternate if dedicated else None,
        inactive_region=(InactiveRegionState(
            alternate.physical_qubit_ids, parsed["alternate_snapshot"].density_matrix,
            None, 0.0,
        ) if dedicated else None),
    )
    runtime = SharedQpuRuntime(
        state_store=store, reset_models=parsed["reset_registry"],
        config=parsed["runtime_config"],
        tenant_regions=tenant_regions,
        regional_reset_models=(
            {primary_ids: parsed["reset_registry"],
             alternate.physical_qubit_ids: parsed["alternate_reset_registry"]}
            if dedicated else None
        ),
        regional_idle_models=(
            {primary_ids: parsed["idle_model"],
             alternate.physical_qubit_ids: parsed["alternate_idle_model"]}
            if dedicated else None
        ),
        regional_service_durations=(
            {primary_ids: (parsed["runtime_config"].reset_duration_us,
                           parsed["runtime_config"].program_measurement_duration_us),
             alternate.physical_qubit_ids: parsed["alternate_durations"]}
            if dedicated else None
        ),
        idle_model=None if dedicated else parsed["idle_model"],
    )
    runtime.configure_job_authorization(parsed["authorization"])
    service = JobApplicationService(
        runtime=runtime, principals=principals,
        program_compiler=parsed["compiler"],
        interleave_victims=(parsed["interleave_victims"] if interleave_victims is None else interleave_victims),
        observation_budget=manifest.observation_budget,
        scenario_manifest_id=manifest.manifest_id,
        program_layouts=({
            principal.principal_id: PhysicalQubitLayout(
                parsed["snapshot"].qpu_id, tenant_regions[principal.tenant_id],
            ) for principal in principals
        } if dedicated else None),
    )
    return runtime, service


def _interleave_victims(manifest: ResolvedScenarioManifest) -> tuple[InterleaveVictimSpec, ...]:
    """Victim programs an attacker may name; the secret bit never leaves the manifest."""
    run = manifest.victim_config.get("interleaved_run")
    if run is None:
        return ()
    if (not isinstance(run, Mapping)
            or set(run) not in ({"program", "secret_bit"}, {"program", "secret_message"})
            or run["program"] != "prepare_basis_state_then_measure"):
        raise ValueError("victim interleaved_run must name the supported program and one secret")
    if "secret_message" in run:
        bits = message_bits(run["secret_message"])
    elif type(run["secret_bit"]) is int and run["secret_bit"] in (0, 1):
        bits = (run["secret_bit"],)
    else:
        raise ValueError("victim interleaved_run secret_bit must be 0 or 1")
    principal_id = manifest.victim_config.get("principal_id")

    def program(bit: int) -> CircuitProgram:
        return CircuitProgram.from_editor_json(json.dumps({
            "logical_qubits": 1, "initial_states": [bit], "classical_bits": 1,
            "columns": [{"step": 0, "gates": [
                {"type": "MEASURE", "targets": [0], "classical_targets": [0]}]}],
        }))

    # A message victim runs one bit per accepted observation, in order.
    zero, one = program(0), program(1)
    sequence = tuple(one if bit else zero for bit in bits)
    victim = (InterleaveVictimSpec("victim", principal_id, sequence[0], sequence)
              if len(sequence) > 1 else InterleaveVictimSpec("victim", principal_id, sequence[0]))
    return (victim, InterleaveVictimSpec("training:0", principal_id, zero),
            InterleaveVictimSpec("training:1", principal_id, one))


MAX_SECRET_MESSAGE_LENGTH = 64


def message_bits(message: object) -> tuple[int, ...]:
    """Printable ASCII, 8 bits per character, most significant bit first."""
    if (type(message) is not str or not 1 <= len(message) <= MAX_SECRET_MESSAGE_LENGTH
            or not message.isascii() or not message.isprintable()):
        raise ValueError("secret_message must be 1-64 printable ASCII characters")
    return tuple((ord(char) >> shift) & 1 for char in message for shift in range(7, -1, -1))


def _parse_per_qubit_reset(raw: Mapping[str, Any], physical_ids: tuple[int, ...]
                           ) -> PerQubitPhenomenologicalResetModel:
    if (set(raw) != {"model_id", "target_populations", "applies_to_physical_qubit_ids", "qubits"}
            or tuple(raw["applies_to_physical_qubit_ids"]) != physical_ids):
        raise ValueError("per-qubit reset model scope must match the retained region")
    populations = raw["target_populations"]
    if (type(populations) not in (list, tuple) or len(populations) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in populations)):
        raise ValueError("per-qubit reset target populations must be explicit")
    per_qubit = raw["qubits"]
    if not isinstance(per_qubit, Mapping) or set(per_qubit) != {str(qid) for qid in physical_ids}:
        raise ValueError("per-qubit reset model requires exactly the retained physical IDs")
    specs, assignment_errors = [], []
    for qubit_id in physical_ids:
        entry = per_qubit[str(qubit_id)]
        expected = {"success_probability", "observed_one_given_zero", "observed_zero_given_one"}
        if not isinstance(entry, Mapping) or set(entry) != expected:
            raise ValueError("per-qubit reset calibration is incomplete")
        values = [entry[name] for name in expected]
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not 0.0 <= value <= 1.0 for value in values):
            raise ValueError("per-qubit reset calibration must contain probabilities")
        specs.append(ImperfectResetSpec(entry["success_probability"], tuple(populations)))
        assignment_errors.append((entry["observed_one_given_zero"], entry["observed_zero_given_one"]))
    return PerQubitPhenomenologicalResetModel(
        physical_ids, tuple(specs), tuple(assignment_errors), model_id=raw["model_id"],
    )


def _build_idle_model(
    raw: Mapping[str, Any], physical_qubit_ids: tuple[int, ...], max_local_qubits: int,
) -> IndependentIdleModel:
    if raw.get("model_id") != "independent_constant_rate_v1":
        raise ValueError("waiting physics requires the supported independent constant-rate model")
    mode = raw.get("input_mode")
    qubit_names = {str(qubit_id) for qubit_id in physical_qubit_ids}

    if mode == IDLE_INPUT_CALIBRATION:
        if set(raw) != {"model_id", "input_mode", "qubits"}:
            raise ValueError("calibration mode accepts only calibration qubit values")
        qubits = raw["qubits"]
        if not isinstance(qubits, Mapping) or set(qubits) != qubit_names:
            raise ValueError("calibration mode requires one explicit T1/T2/population set per retained qubit")
        specs = []
        for qubit_id in physical_qubit_ids:
            item = qubits[str(qubit_id)]
            required = {"t1_us", "t2_us", "equilibrium_excited_population"}
            if not isinstance(item, Mapping) or set(item) != required:
                raise ValueError("calibration entries require only T1, T2, and equilibrium population")
            t1 = _finite_number(item["t1_us"], f"calibration T1 for qubit {qubit_id}", minimum=0.0,
                                strict_minimum=True)
            t2 = _finite_number(item["t2_us"], f"calibration T2 for qubit {qubit_id}", minimum=0.0,
                                strict_minimum=True)
            population = _finite_number(item["equilibrium_excited_population"],
                                        f"equilibrium population for qubit {qubit_id}",
                                        minimum=0.0, maximum=1.0)
            gamma_phi = 1.0 / t2 - 1.0 / (2.0 * t1)
            if gamma_phi < 0.0:
                raise ValueError(f"calibration T2 exceeds the pure-dephasing physical bound for qubit {qubit_id}")
            tphi = math.inf if gamma_phi == 0.0 else 1.0 / gamma_phi
            specs.append(IdleQubitSpec(t1, tphi, population))
        return IndependentIdleModel(tuple(specs), max_local_qubits)

    if mode == IDLE_INPUT_PHYSICAL:
        if set(raw) != {"model_id", "input_mode", "physical_inputs"}:
            raise ValueError("physical-parameter mode accepts only physical inputs")
        values = raw["physical_inputs"]
        fields = {
            "environment_model", "device_quality", "temperature_mk", "flux_noise_phi0",
            "qubit_frequency_ghz", "t1_max_us", "tphi_max_us", "per_qubit",
        }
        if not isinstance(values, Mapping) or set(values) != fields:
            raise ValueError("physical mode requires explicit environment parameters and per-qubit overrides")
        if values["environment_model"] != UNIFIED_ENVIRONMENT_MODEL:
            raise ValueError("unsupported physical environment model")
        device_quality = _finite_number(values["device_quality"], "device_quality", minimum=0.0, maximum=1.0)
        temperature = _finite_number(values["temperature_mk"], "temperature_mk", minimum=0.0)
        flux_noise = _finite_number(values["flux_noise_phi0"], "flux_noise_phi0", minimum=0.0)
        frequency = _finite_number(values["qubit_frequency_ghz"], "qubit_frequency_ghz",
                                   minimum=0.0, strict_minimum=True)
        t1_max = _finite_number(values["t1_max_us"], "t1_max_us", minimum=0.0, strict_minimum=True)
        tphi_max = _finite_number(values["tphi_max_us"], "tphi_max_us", minimum=0.0, strict_minimum=True)
        per_qubit = values["per_qubit"]
        if not isinstance(per_qubit, Mapping) or set(per_qubit) - qubit_names:
            raise ValueError("per-qubit physical overrides must use retained physical qubit IDs")
        normalized_overrides: list[dict[str, float]] = []
        has_overrides = bool(per_qubit)
        for qubit_id in physical_qubit_ids:
            entry = per_qubit.get(str(qubit_id), {})
            if not isinstance(entry, Mapping) or set(entry) - {"t1_max_us", "tphi_max_us"}:
                raise ValueError("per-qubit overrides may contain only T1/Tphi maxima")
            normalized_overrides.append({
                key: _finite_number(entry[key], f"qubit {qubit_id} {key}", minimum=0.0,
                                    strict_minimum=True)
                for key in entry
            })
        physical = PhysicalEnvironmentInputs(
            device_quality=device_quality, temperature_mk=temperature,
            flux_noise_phi0=flux_noise, qubit_frequency_ghz=frequency,
            ideal_reference=False,
        )
        # EnvironmentConfig is the same validated input envelope used by the
        # simulator; the rate helper resolves it through PhysicalEnvironmentInputs.
        rates_input = EnvironmentConfig(
            input_mode=INPUT_MODE_PHYSICAL,
            model=UNIFIED_ENVIRONMENT_MODEL,
            device_quality=physical.device_quality,
            temperature_mk=physical.temperature_mk,
            flux_noise_phi0=physical.flux_noise_phi0,
            qubit_frequency_ghz=physical.qubit_frequency_ghz,
            ideal_reference=physical.ideal_reference,
            t1_max_us=t1_max, tphi_max_us=tphi_max,
            per_qubit=normalized_overrides if has_overrides else [],
        )
        rates = compute_environment_rates_per_qubit(
            rates_input, len(physical_qubit_ids), GenericTransmonProfile(),
        )
        specs = []
        for qubit_id, value in zip(physical_qubit_ids, rates, strict=True):
            population_rate = value.gamma_down_per_us + value.gamma_up_per_us
            if not math.isfinite(population_rate) or population_rate <= 0.0:
                raise ValueError(f"physical rates do not define finite T1 for qubit {qubit_id}")
            if (not math.isfinite(value.gamma_phi_per_us) or value.gamma_phi_per_us < 0.0
                    or not math.isfinite(value.gamma_up_per_us) or value.gamma_up_per_us < 0.0):
                raise ValueError(f"physical rates are invalid for qubit {qubit_id}")
            excited_population = value.gamma_up_per_us / population_rate
            tphi = math.inf if value.gamma_phi_per_us == 0.0 else 1.0 / value.gamma_phi_per_us
            specs.append(IdleQubitSpec(1.0 / population_rate, tphi, excited_population))
        return IndependentIdleModel(tuple(specs), max_local_qubits)

    raise ValueError("idle_dynamics.input_mode must select one supported input method")


def _validate_device_snapshot_binding(snapshot: object, topology: QpuTopology,
                                      region: RetainedRegionBinding, declaration: object = None) -> None:
    required = {
        "snapshot_id", "source", "props_sha256", "conf_sha256", "qubit_count",
        "directed_coupling_count", "active_region_physical_qubit_ids", "eligibility_policy",
        "qubits", "cz_calibrations", "qubit_calibration_basis", "cz_calibration_basis",
    }
    custom = declaration is not None
    if custom:
        required |= {"directed_coupling_map", "two_qubit_calibrations"}
    if not isinstance(snapshot, Mapping) or set(snapshot) != required:
        raise ValueError("device snapshot provenance record is incomplete")
    ids = tuple(topology.physical_qubit_ids)
    if (type(snapshot["qubit_count"]) is not int or snapshot["qubit_count"] != len(ids)
            or type(snapshot["directed_coupling_count"]) is not int
            or (not custom and snapshot["directed_coupling_count"] != 2 * len(topology.gate_edges))
            or tuple(snapshot["active_region_physical_qubit_ids"]) != region.physical_qubit_ids):
        raise ValueError("device snapshot dimensions do not match the frozen QPU topology")
    if custom:
        raw_map = snapshot["directed_coupling_map"]
        if (type(raw_map) not in (list, tuple) or len(raw_map) != snapshot["directed_coupling_count"]
                or any(type(edge) not in (list, tuple) or len(edge) != 2
                       or any(type(qid) is not int for qid in edge) for edge in raw_map)):
            raise ValueError("directed coupling map does not match its declared count")
        undirected = {tuple(sorted(edge)) for edge in raw_map}
        if undirected != set(topology.gate_edges):
            raise ValueError("directed coupling map differs from frozen topology")
    for field in ("props_sha256", "conf_sha256"):
        digest = snapshot[field]
        if type(digest) is not str or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("device snapshot source digest is invalid")
    qubits = snapshot["qubits"]
    if not isinstance(qubits, Mapping) or set(qubits) != {str(qid) for qid in ids}:
        raise ValueError("device snapshot must inventory every physical qubit")
    for qid in ids:
        entry = qubits[str(qid)]
        if (not isinstance(entry, Mapping) or type(entry.get("usable_for_placement")) is not bool
                or type(entry.get("unusable_reasons")) not in (list, tuple)):
            raise ValueError("device snapshot qubit eligibility record is invalid")
        if qid in region.physical_qubit_ids and not entry["usable_for_placement"]:
            raise ValueError("retained region includes a qubit marked unusable")
    cz = snapshot["cz_calibrations"]
    expected_edges = {f"{left}:{right}" for left, right in topology.gate_edges}
    declared_two = declaration.get("two_qubit_gates") if isinstance(declaration, Mapping) else {"cz": False}
    cz_expected_edges = expected_edges if "cz" in declared_two else set()
    if not isinstance(cz, Mapping) or set(cz) != cz_expected_edges:
        raise ValueError("device snapshot CZ inventory differs from topology edges")
    for edge, entry in cz.items():
        if (type(entry) not in (list, tuple) or len(entry) != 2
                or any(type(value) not in (int, float) or not math.isfinite(float(value)) or value < 0
                       for value in entry)):
            raise ValueError("device snapshot CZ calibration record is invalid")
    if custom:
        generic = snapshot["two_qubit_calibrations"]
        raw_directions = {tuple(edge) for edge in snapshot["directed_coupling_map"]}
        if not isinstance(generic, Mapping) or set(generic) != set(declared_two):
            raise ValueError("device snapshot two-qubit calibration inventory differs from declaration")
        for gate, entries in generic.items():
            directed = declared_two[gate]
            if type(directed) is not bool or not isinstance(entries, Mapping) or not entries:
                raise ValueError("device snapshot two-qubit declaration is invalid")
            parsed = set()
            for edge, entry in entries.items():
                try:
                    pair = tuple(int(part) for part in edge.split(":"))
                except (AttributeError, ValueError):
                    raise ValueError("two-qubit calibration edge has invalid directed form") from None
                if (len(pair) != 2 or pair[0] == pair[1] or pair not in raw_directions
                        or not directed and pair[0] > pair[1]):
                    raise ValueError("two-qubit calibration direction is outside backend coupling map")
                parsed.add(pair)
                if (type(entry) not in (list, tuple) or len(entry) != 2
                        or any(type(value) not in (int, float) or not math.isfinite(float(value))
                               or value < 0 for value in entry)):
                    raise ValueError("two-qubit snapshot calibration record is invalid")
            if not directed:
                expected_symmetric = {tuple(sorted(edge)) for edge in raw_directions}
                if parsed != expected_symmetric:
                    raise ValueError("symmetric gate calibration does not cover backend coupling edges")
            elif parsed != raw_directions:
                raise ValueError("directed gate calibration does not cover backend coupling directions")
    for field in ("qubit_calibration_basis", "cz_calibration_basis"):
        if type(snapshot[field]) is not str or not snapshot[field].strip():
            raise ValueError("device snapshot calibration basis is missing")
    policy = snapshot["eligibility_policy"]
    if not isinstance(policy, Mapping) or not {
        "readout_error_exclude_above", "cz_error_exclude_at_or_above",
        "exclude_missing_t1_or_t2", "exclude_missing_measure_or_reset_duration",
    } <= set(policy):
        raise ValueError("device snapshot eligibility policy is missing its recorded thresholds")
    readout_limit = policy["readout_error_exclude_above"]
    cz_limit = policy["cz_error_exclude_at_or_above"]
    if any(type(value) not in (int, float) or not math.isfinite(float(value)) or not 0.0 <= value <= 1.0
           for value in (readout_limit, cz_limit)):
        raise ValueError("device snapshot eligibility thresholds are invalid")
    if type(policy["exclude_missing_t1_or_t2"]) is not bool or type(
            policy["exclude_missing_measure_or_reset_duration"]) is not bool:
        raise ValueError("device snapshot missing-calibration policy is invalid")
    high_error_edges = set()
    for edge, entry in cz.items():
        left, right = (int(part) for part in edge.split(":"))
        if entry[0] >= cz_limit:
            high_error_edges.update((left, right))
    for qid in ids:
        entry = qubits[str(qid)]
        readout_error = entry.get("readout_error")
        if type(readout_error) not in (int, float) or not math.isfinite(float(readout_error)) or not 0 <= readout_error <= 1:
            raise ValueError("device snapshot readout calibration is invalid")
        missing_t = entry.get("T1_us") is None or entry.get("T2_us") is None
        missing_service_time = (entry.get("measurement_duration_us") is None
                                or entry.get("reset_duration_us") is None)
        expected_usable = not (
            readout_error > readout_limit
            or policy["exclude_missing_t1_or_t2"] and missing_t
            or policy["exclude_missing_measure_or_reset_duration"] and missing_service_time
            or qid in high_error_edges
        )
        if entry["usable_for_placement"] is not expected_usable:
            raise ValueError("device snapshot eligibility does not follow its recorded policy")
        expected_status = "usable" if expected_usable else "unusable"
        if entry.get("placement_status") != expected_status:
            raise ValueError("device snapshot placement label differs from eligibility")
        if qid in region.physical_qubit_ids and not expected_usable:
            raise ValueError("retained region contains a qubit excluded by snapshot policy")


def _finite_number(value: object, name: str, *, minimum: float,
                   maximum: float = math.inf, strict_minimum: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be an explicit finite number")
    number = float(value)
    if number < minimum or strict_minimum and number == minimum or number > maximum:
        bracket = "<=" if strict_minimum else "<"
        raise ValueError(f"{name} must be in {bracket} range [{minimum}, {maximum}]")
    return number


def _object(parent: Mapping[str, Any], key: str, required: set[str], optional: set[str] = frozenset()) -> Mapping:
    value = parent.get(key)
    if not isinstance(value, Mapping) or not required <= set(value) or set(value) - required - optional:
        raise ValueError(f"{key} requires explicit fields: {sorted(required)}")
    return value


def _validate_runtime_profile(raw: Mapping[str, Any]) -> None:
    for name in ("reset_model_id", "reset_model_configuration_id", "reset_model_mode",
                 "queue_policy_id", "cancellation_policy_id", "placement_policy",
                 "concurrency_policy", "drive_crosstalk_direction"):
        if type(raw[name]) is not str or not raw[name].strip():
            raise ValueError(f"runtime_execution.{name} must be explicit text")
    if type(raw["reset_count"]) is not int or raw["reset_count"] < 0:
        raise ValueError("runtime_execution.reset_count must be an explicit nonnegative integer")
    for name in ("max_local_qubits", "max_execution_work"):
        if type(raw[name]) is not int or raw[name] < 1:
            raise ValueError(f"runtime_execution.{name} must be an explicit positive integer")
    if type(raw["measure_input_state_before_transition"]) is not bool:
        raise ValueError("runtime_execution.measure_input_state_before_transition must be explicit boolean")
    for name in ("reset_duration_us", "victim_operation_duration_us",
                 "attacker_measurement_duration_us", "isolated_assignment_duration_us",
                 "program_measurement_duration_us", "repetition_delay_us", "entry_reuse_tolerance",
                 "drive_crosstalk_strength"):
        value = raw[name]
        if (type(value) not in (int, float) or not math.isfinite(float(value)) or value < 0):
            raise ValueError(f"runtime_execution.{name} must be finite and nonnegative")


def _basis_values(value: object, name: str) -> None:
    if not isinstance(value, Mapping) or any(type(item) not in (int, float) or not math.isfinite(float(item)) or item < 0
                                             for item in value.values()):
        raise ValueError(f"{name} must contain explicit finite nonnegative values")


def _complex_pair(value: object) -> complex:
    if type(value) not in (list, tuple) or len(value) != 2 or any(type(part) not in (int, float) for part in value):
        raise ValueError("density matrix values must be [real, imaginary] pairs")
    result = complex(value[0], value[1])
    if not math.isfinite(result.real) or not math.isfinite(result.imag):
        raise ValueError("density matrix values must be finite")
    return result


def _actor_tenant(manifest: ResolvedScenarioManifest, role: str) -> str:
    value = manifest.relation_config.get("actor_tenants")
    if not isinstance(value, Mapping) or set(value) != {"attacker", "victim"}:
        raise ValueError("relation.actor_tenants must explicitly bind attacker and victim tenants")
    tenant = value[role]
    if type(tenant) is not str or not tenant.strip():
        raise ValueError(f"relation.actor_tenants.{role} must be explicit")
    return tenant
