"""Common Program runtime for a shared region or parked tenant regions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Mapping
from threading import Lock
import math
import random
from typing import Callable
from core.attack_simulation.concurrency_policy import (
    CONCURRENCY_POLICIES, CROSSTALK_DIRECTIONS, OVERLAP_DISJOINT_REGIONS, CrosstalkDeclaration,
)
import numpy as np

from core.attack_simulation.jobs import (
    AttackerMeasureOperation,
    FifoJobScheduler,
    JobCommand,
    JobRecord,
    VictimPrepareOperation,
    create_scheduler,
    scheduler_registered,
)
from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.interleaved_execution import derived_seed, interleaved_work
from core.attack_simulation.kick_application import EMPTY_PLAN, KickPlan, PlacedKick, kick_unitary
from core.attack_simulation.program_request import STATE_INHERIT_OPERATION_ID, interleave_operation_id
from core.attack_simulation.register_readout import register_assignment_errors
from core.attack_simulation.request_receipts import RequestReceipt
from core.attack_simulation.job_authorization import (
    ATTACKER_ROLE, VICTIM_ROLE, JobAuthorizationPolicy,
)
from core.attack_simulation.qpu_state import (
    MAX_DENSE_LOCAL_QUBITS,
    QpuStateStore,
    QpuStateTransition,
)
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.resource_binding import ResourceBinding
from core.experimental_isolation_physics.reset_model import (
    ResetModelMode,
    ResetModelResult,
    ResetModelRegistry,
    ResetModelRequest,
    RegisterResetResult,
)
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from core.gates import Matrix, density_from_ket
from core.register_operations import (
    apply_local_depolarizing_replacement, apply_operand_operator, freeze_register, sample_measurement,
)


@dataclass(frozen=True)
class SharedQpuRuntimeConfig:
    """Versioned service behavior for the shared-QPU execution runtime."""

    reset_model_id: str
    reset_model_configuration_id: str
    reset_model_mode: ResetModelMode = ResetModelMode.STATE_TRANSITION_WITH_READOUT
    reset_count: int = 1
    reset_duration_us: float = 1.0
    victim_operation_duration_us: float = 1.0
    attacker_measurement_duration_us: float = 1.0
    queue_policy_id: str = "on_demand_fifo_v1"
    cancellation_policy_id: str = "queued_only"
    measure_input_state_before_transition: bool = True
    placement_policy: PlacementPolicy = (
        PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT
    )
    isolated_assignment_duration_us: float = 0.0
    resource_binding: ResourceBinding | None = None
    program_measurement_duration_us: float = 1.0
    # Declared wait between consecutive shots of one Program; 0 means no gap.
    repetition_delay_us: float = 0.0
    # Simulation accuracy, not a service property: a shot may reuse the computation of an earlier
    # shot of the same job whose entry state is within this trace distance. 0 reuses only identical
    # entry states, which is exact. The total-variation error of the counts is at most shots * this.
    entry_reuse_tolerance: float = 0.0
    # How jobs of different tenants share the QPU in time. With overlap_disjoint_regions the jobs of the
    # two dedicated regions run side by side, coupled only by the declared drive crosstalk: one strength
    # for every coupling edge that joins the regions, in the chosen direction(s).
    concurrency_policy: str = "exclusive_fifo"
    crosstalk_declarations: tuple[CrosstalkDeclaration, ...] = ()
    # Compatibility fields for the current product scenario adapter. New scenarios
    # should declare directed region transfers in crosstalk_declarations.
    drive_crosstalk_strength: float = 0.0
    drive_crosstalk_direction: str = "both"
    max_local_qubits: int = 8
    max_execution_work: int = 536870912

    def to_manifest_config(self) -> dict[str, object]:
        """Return every behavior-changing runtime value for pre-start freezing."""
        binding = self.resource_binding
        config = {
            "schema_version": "shared_qpu_runtime_v3",
            "reset_model_id": self.reset_model_id,
            "reset_model_configuration_id": self.reset_model_configuration_id,
            "reset_model_mode": self.reset_model_mode.value,
            "reset_count": self.reset_count,
            "reset_duration_us": self.reset_duration_us,
            "victim_operation_duration_us": self.victim_operation_duration_us,
            "attacker_measurement_duration_us": self.attacker_measurement_duration_us,
            "queue_policy_id": self.queue_policy_id,
            "cancellation_policy_id": self.cancellation_policy_id,
            "measure_input_state_before_transition": self.measure_input_state_before_transition,
            "placement_policy": self.placement_policy.value,
            "isolated_assignment_duration_us": self.isolated_assignment_duration_us,
            "program_measurement_duration_us": self.program_measurement_duration_us,
            "repetition_delay_us": self.repetition_delay_us,
            "entry_reuse_tolerance": self.entry_reuse_tolerance,
            "concurrency_policy": self.concurrency_policy,
            "drive_crosstalk_strength": self.drive_crosstalk_strength,
            "drive_crosstalk_direction": self.drive_crosstalk_direction,
            "resource_binding": None if binding is None else dict(binding.to_manifest_config()),
            "max_local_qubits": self.max_local_qubits,
            "max_execution_work": self.max_execution_work,
        }
        if self.crosstalk_declarations:
            config["crosstalk_declarations"] = [
                {"src_region": item.src_region, "dst_region": item.dst_region,
                 "strength": item.strength, "detuning_rad_per_us": item.detuning_rad_per_us}
                for item in self.crosstalk_declarations
            ]
        return config

    def __post_init__(self) -> None:
        if type(self.max_local_qubits) is not int or not 1 <= self.max_local_qubits <= MAX_DENSE_LOCAL_QUBITS:
            raise ValueError("local-region cap exceeds the dense adapter's capacity")
        if type(self.max_execution_work) is not int or not 1 <= self.max_execution_work <= 2147483648:
            raise ValueError("invalid runtime matrix-work cap")
        _identifier(self.reset_model_id, "reset_model_id")
        _identifier(
            self.reset_model_configuration_id,
            "reset_model_configuration_id",
        )
        reset_model_mode = (
            self.reset_model_mode
            if isinstance(self.reset_model_mode, ResetModelMode)
            else ResetModelMode(self.reset_model_mode)
        )
        if reset_model_mode is not ResetModelMode.STATE_TRANSITION_WITH_READOUT:
            raise ValueError(
                "the stateful runtime requires state_transition_with_readout"
            )
        object.__setattr__(self, "reset_model_mode", reset_model_mode)
        count = int(self.reset_count)
        if count < 0:
            raise ValueError("reset_count must be non-negative")
        object.__setattr__(self, "reset_count", count)
        for name in (
            "reset_duration_us",
            "victim_operation_duration_us",
            "attacker_measurement_duration_us",
            "isolated_assignment_duration_us",
            "program_measurement_duration_us",
            "repetition_delay_us",
            "entry_reuse_tolerance",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, value)
        placement = (
            self.placement_policy
            if isinstance(self.placement_policy, PlacementPolicy)
            else PlacementPolicy(self.placement_policy)
        )
        if not scheduler_registered(self.queue_policy_id, self.cancellation_policy_id):
            raise ValueError("scheduler policy pair is not registered")
        scheduler_factory = create_scheduler(self.queue_policy_id, self.cancellation_policy_id)
        if placement.value not in scheduler_factory.supported_placement_policies:
            raise ValueError("registered scheduler does not support the selected placement policy")
        object.__setattr__(self, "placement_policy", placement)
        if self.concurrency_policy not in CONCURRENCY_POLICIES:
            raise ValueError("unknown concurrency policy")
        declarations = tuple(self.crosstalk_declarations)
        if any(type(item) is not CrosstalkDeclaration for item in declarations):
            raise TypeError("crosstalk_declarations must contain declared transfer specs")
        object.__setattr__(self, "crosstalk_declarations", declarations)
        if self.drive_crosstalk_direction not in CROSSTALK_DIRECTIONS:
            raise ValueError("unknown drive-crosstalk direction")
        strength = float(self.drive_crosstalk_strength)
        if not math.isfinite(strength) or not 0.0 <= strength <= 1.0:
            raise ValueError("drive_crosstalk_strength must be in [0, 1]")
        object.__setattr__(self, "drive_crosstalk_strength", strength)
        if (self.concurrency_policy == OVERLAP_DISJOINT_REGIONS
                and placement is not PlacementPolicy.TENANT_DEDICATED_REGION):
            raise ValueError("overlapping jobs need tenant-dedicated regions")
        if self.resource_binding is not None:
            if not callable(getattr(self.resource_binding, "resolve", None)):
                raise TypeError("resource_binding must implement ResourceBinding.resolve")


class SharedQpuRuntime:
    """Connect FIFO jobs, service resets, and the revisioned QPU state.

    This class is mode-independent.  Free and challenge sessions supply
    different policies around the same runtime rather than selecting separate
    physics or scheduling implementations.
    """

    def __init__(
        self,
        *,
        state_store: QpuStateStore,
        reset_models: ResetModelRegistry,
        config: SharedQpuRuntimeConfig,
        scheduler: FifoJobScheduler | None = None,
        tenant_regions: Mapping[str, tuple[int, ...]] | None = None,
        regional_reset_models: Mapping[tuple[int, ...], ResetModelRegistry] | None = None,
        regional_idle_models: Mapping[tuple[int, ...], IndependentIdleModel] | None = None,
        regional_service_durations: Mapping[tuple[int, ...], tuple[float, float]] | None = None,
        idle_model: IndependentIdleModel | None = None,
    ) -> None:
        snapshot = state_store.read()
        if snapshot.local_dimension != 2 or len(snapshot.physical_qubit_ids) > config.max_local_qubits:
            raise ValueError("local register exceeds the configured dense runtime capacity")
        if (config.placement_policy is PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE
                and len(snapshot.physical_qubit_ids) != 1):
            raise ValueError("isolated placement currently requires a one-qubit local region")
        if (state_store.region_binding is not None
                and config.placement_policy is PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE):
            raise ValueError("fixed retained region does not support isolated resource reassignment")
        dedicated = config.placement_policy is PlacementPolicy.TENANT_DEDICATED_REGION
        if dedicated:
            # SCAFFOLD: QpuStateStore currently represents one active and one
            # parked region. The policy declaration layer supports N named regions.
            regions = {tuple(ids) for ids in (tenant_regions or {}).values()}
            bindings = {binding.physical_qubit_ids for binding in state_store.regional_bindings}
            if (len(regions) != 2 or regions != bindings or state_store.inactive_region() is None
                    or set(regional_reset_models or {}) != regions
                    or set(regional_idle_models or {}) != regions
                    or set(regional_service_durations or {}) != regions
                    or any(len(model.qubits) != len(ids) for ids, model in (regional_idle_models or {}).items())
                    or any(type(pair) is not tuple or len(pair) != 2
                           or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                                  for value in pair)
                           for pair in (regional_service_durations or {}).values())
                    or config.resource_binding is not None):
                raise ValueError("dedicated placement requires two complete disjoint regional profiles")
        elif any(value is not None for value in (
                tenant_regions, regional_reset_models, regional_idle_models, regional_service_durations)):
            raise ValueError("regional profiles require dedicated placement")
        if idle_model is not None and (
                type(idle_model) is not IndependentIdleModel
                or len(idle_model.qubits) != len(snapshot.physical_qubit_ids)):
            raise ValueError("idle model must be a complete IndependentIdleModel for the local register")
        if config.repetition_delay_us > 0.0 and not dedicated and idle_model is None:
            raise ValueError("a positive repetition delay requires an explicit complete idle model")
        if config.resource_binding is not None:
            if not callable(getattr(config.resource_binding, "to_manifest_config", None)):
                raise TypeError("resource_binding must implement manifest serialization")
            bound = tuple(config.resource_binding.to_manifest_config().values())
            refs = tuple((str(item["qpu_id"]), int(item["physical_qubit_id"]))
                         for item in bound if isinstance(item, Mapping))
            if any(qpu_id != snapshot.qpu_id for qpu_id, _ in refs):
                raise ValueError("resource binding QPU ID does not match runtime state")
            if (len(snapshot.physical_qubit_ids) == 1 and refs
                    and snapshot.physical_qubit_ids[0] not in {qubit_id for _, qubit_id in refs}):
                raise ValueError("initial QPU state does not match its resource binding")
        self.state_store = state_store
        self.reset_models = reset_models
        self.config = config
        self.tenant_regions = dict(tenant_regions or {})
        self.regional_reset_models = dict(regional_reset_models or {})
        self.regional_idle_models = dict(regional_idle_models or {})
        self.regional_service_durations = dict(regional_service_durations or {})
        self.idle_model = idle_model
        self.scheduler = scheduler or create_scheduler(
            config.queue_policy_id, config.cancellation_policy_id,
        )
        if (self.scheduler.queue_policy_id, self.scheduler.cancellation_policy_id) != (
                config.queue_policy_id, config.cancellation_policy_id):
            raise ValueError("scheduler instance does not match the registered runtime profile")
        reset_model = self.reset_models.get(config.reset_model_id)
        if reset_model.configuration_id != config.reset_model_configuration_id:
            raise ValueError(
                "reset model configuration does not match runtime configuration"
            )
        if ResetModelMode(reset_model.model_mode) is not config.reset_model_mode:
            raise ValueError("reset model mode does not match runtime configuration")
        if not reset_models.supports_register(config.reset_model_id, len(snapshot.physical_qubit_ids)):
            raise ValueError("selected reset model does not support this register")
        if dedicated:
            for ids, registry in self.regional_reset_models.items():
                if not registry.supports_register(config.reset_model_id, len(ids)):
                    raise ValueError("regional reset model does not support its register")
        self._next_event_sequence = snapshot.revision + 1
        self._job_authorization: JobAuthorizationPolicy | None = None
        self._scenario_workload_job_ids: set[str] = set()
        self._execution_lock = Lock()
        self._session_owner: object | None = None
        self._session_owner_lock = Lock()
        self._declared_executor = None
        self._declared_event_limit: int | None = None

    def result_is_available(self, job_id: str) -> bool:
        """Declared publication gate is independent of numerical completion."""
        return self._declared_executor is None or self._declared_executor.result_is_available(job_id)

    @property
    def session_owned(self) -> bool:
        with self._session_owner_lock:
            return self._session_owner is not None

    def claim_session(self, owner: object) -> None:
        """Trusted assembly: do not silently reuse physical state across worlds."""
        with self._session_owner_lock:
            if self._session_owner is not None:
                raise ValueError("physical runtime is already owned by a session")
            self._session_owner = id(owner)

    def _abort_session_claim(self, owner: object) -> None:
        """Undo only this unpublished constructor's claim, not session disposal."""
        with self._session_owner_lock:
            if self._session_owner != id(owner):
                raise PermissionError("cannot release another session's runtime claim")
            self._session_owner = None

    def configure_job_authorization(self, policy: JobAuthorizationPolicy) -> None:
        if self._job_authorization is not None:
            raise RuntimeError("job authorization is already configured")
        if self.scheduler.queued_job_ids() or self.state_store.read().revision != 0:
            raise RuntimeError("job authorization must be configured before runtime use")
        self._job_authorization = policy

    @property
    def job_authorization(self) -> JobAuthorizationPolicy | None:
        return self._job_authorization

    def submit(
        self, command: JobCommand, *, receipt: RequestReceipt | None = None,
        before_accept: Callable[[], None] | None = None,
    ) -> JobRecord:
        if (self._job_authorization is not None
                and isinstance(command.operation, VictimPrepareOperation)):
            raise PermissionError("victim workload jobs are scenario controlled")
        self._authorize(command)
        self._validate_job_target(command)
        if before_accept is not None:
            return self.scheduler.submit(command, receipt=receipt, before_accept=before_accept)
        return self.scheduler.submit(command, receipt=receipt)

    def _submit_scenario_workload(self, command: JobCommand) -> JobRecord:
        if not isinstance(command.operation, VictimPrepareOperation):
            raise TypeError("scenario workload submission requires a victim operation")
        self._authorize(command)
        self._validate_job_target(command)
        submitted = self.scheduler.submit(command)
        self._scenario_workload_job_ids.add(submitted.job_id)
        return submitted

    def cancel(self, job_id: str) -> JobRecord:
        return self.scheduler.cancel(job_id)

    def run_next(self) -> JobRecord | None:
        if self._declared_executor is not None:
            raise RuntimeError("declared runtime requires its coordinated execution path")
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeError("a physical executor is already active")
        try:
            return self._run_next_exclusively()
        finally:
            self._execution_lock.release()

    def _run_next_exclusively(self) -> JobRecord | None:
        allocated = self.scheduler.allocate_next()
        if allocated is None:
            return None
        job_id = allocated.job_id
        self.scheduler.mark_running(job_id)
        try:
            result = self._execute(allocated)
        except Exception:
            return self.scheduler.mark_internal_error(job_id, "INTERNAL_EXECUTION_ERROR")
        finally:
            self._scenario_workload_job_ids.discard(job_id)
        return self.scheduler.mark_succeeded(job_id, result)

    def run_until_idle(self) -> tuple[JobRecord, ...]:
        completed: list[JobRecord] = []
        while True:
            result = self.run_next()
            if result is None:
                return tuple(completed)
            completed.append(result)

    def _execute(self, job: JobRecord, kicks: KickPlan = EMPTY_PLAN) -> dict[str, object]:
        if (self._job_authorization is not None
                and isinstance(job.command.operation, VictimPrepareOperation)
                and job.job_id not in self._scenario_workload_job_ids):
            raise PermissionError("victim workload jobs are scenario controlled")
        self._authorize(job.command)
        self._validate_job_target(job.command)
        operation = job.command.operation
        if isinstance(operation, CircuitExecutionOperation) and operation.interleave is not None:
            return self._execute_interleaved(job, operation)
        reset_outcome = self._prepare_job_context(job)
        snapshot = self.state_store.read()
        return self._dispatch(job, operation, snapshot, reset_outcome, kicks)

    def _prepare_job_context(self, job: JobRecord) -> ResetModelResult | RegisterResetResult | None:
        """Region/resource assignment and the tenant-handover service reset."""
        snapshot = self.state_store.read()
        if self.config.placement_policy is PlacementPolicy.TENANT_DEDICATED_REGION:
            target_region = self.tenant_regions[job.command.tenant_id]
            if snapshot.physical_qubit_ids != target_region:
                self._assign_tenant_region(job, target_region)
                snapshot = self.state_store.read()
        target_qubit_id = self._target_qubit_id(job)
        if (target_qubit_id is not None
                and snapshot.physical_qubit_ids != (target_qubit_id,)
                and snapshot.owner_tenant_id is None):
            if self.config.placement_policy is not PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE:
                raise ValueError("job resource does not match active QPU state")
            self._assign_isolated_resource(job, target_qubit_id)
            snapshot = self.state_store.read()
        reset_outcome: ResetModelResult | RegisterResetResult | None = None
        if (
            snapshot.owner_tenant_id is not None
            and snapshot.owner_tenant_id != job.command.tenant_id
        ):
            reset_outcome = self._service_reset(job)
        return reset_outcome

    def _execute_interleaved(self, job: JobRecord, operation: CircuitExecutionOperation) -> dict[str, object]:
        """Run [victim shot, handover reset, attacker shot] once per attacker shot.

        The victim's counts are never returned: the attacker only observes what
        its own measurement reveals through the carried-over physical state.
        """
        victim = operation.interleave
        counts: dict[str, int] = {}
        for index in range(operation.shots):
            victim_job = replace(job, command=replace(
                job.command, tenant_id=victim.tenant_id, layout=victim.layout,
                created_by_principal_id=None, target=None,
                operation=replace(victim.operation, sampling_seed=derived_seed(
                    victim.operation.sampling_seed, index, "victim")),
            ))
            self._prepare_job_context(victim_job)
            self._execute_circuit(victim_job, victim_job.command.operation)
            attacker_op = replace(operation, shots=1, interleave=None, sampling_seed=derived_seed(
                operation.sampling_seed, index, "attacker"))
            attacker_job = replace(job, command=replace(job.command, operation=attacker_op))
            self._prepare_job_context(attacker_job)
            shot = self._execute_circuit(attacker_job, attacker_op)
            for label, count in shot["counts"].items():
                counts[label] = counts.get(label, 0) + count
        return {"shots": operation.shots, "counts": counts}

    def _dispatch(self, job, operation, snapshot, reset_outcome, kicks: KickPlan = EMPTY_PLAN) -> dict[str, object]:
        if isinstance(operation, CircuitExecutionOperation):
            return self._execute_circuit(job, operation, kicks)
        if isinstance(operation, VictimPrepareOperation):
            next_state = density_from_ket(
                (1.0 + 0.0j, 0.0j)
                if operation.output_bit == 0
                else (0.0j, 1.0 + 0.0j)
            )
            self._commit_event(
                job=job,
                event_kind="victim_preparation",
                next_state=next_state,
                duration_us=self.config.victim_operation_duration_us,
                next_owner_tenant_id=job.command.tenant_id,
            )
            return {"prepared": True}

        if isinstance(operation, AttackerMeasureOperation):
            true_one_probability = _one_probability(snapshot.density_matrix)
            if reset_outcome is None:
                observed_one_given_true_zero = 0.0
                observed_zero_given_true_one = 0.0
            else:
                observed_one_given_true_zero = _required_probability(
                    reset_outcome.observed_one_given_true_zero,
                    "observed_one_given_true_zero",
                )
                observed_zero_given_true_one = _required_probability(
                    reset_outcome.observed_zero_given_true_one,
                    "observed_zero_given_true_one",
                )
            generator = random.Random(operation.sampling_seed)
            true_outcome = int(generator.random() < true_one_probability)
            if true_outcome == 0:
                observed = int(generator.random() < observed_one_given_true_zero)
            else:
                observed = int(not (
                    generator.random() < observed_zero_given_true_one
                ))
            collapsed_state = density_from_ket(
                (1.0 + 0.0j, 0.0j)
                if true_outcome == 0
                else (0.0j, 1.0 + 0.0j)
            )
            self._commit_event(
                job=job,
                event_kind="attacker_measurement",
                next_state=collapsed_state,
                duration_us=self.config.attacker_measurement_duration_us,
                next_owner_tenant_id=job.command.tenant_id,
            )
            return {
                "shots": 1,
                "counts": {
                    "0": 1 - observed,
                    "1": observed,
                },
            }

        raise TypeError("unsupported job operation")

    def _execute_circuit(
        self, job: JobRecord, operation: CircuitExecutionOperation, kicks: KickPlan = EMPTY_PLAN,
    ) -> dict[str, object]:
        generator = random.Random(operation.sampling_seed)
        counts = {"0": 0, "1": 0} if operation.classical_bits == 1 else {}
        snapshot = self.state_store.read()
        slots = tuple(snapshot.physical_qubit_ids.index(q) for q in job.command.layout.physical_qubit_ids)
        measurements = tuple(i for i in operation.instructions if i.kind == "measure")
        measured_qubits = tuple(q for i in measurements for q in i.qubits)
        classical_targets = tuple(c for i in measurements for c in i.classical_targets)
        measured_slots = tuple(slots[q] for q in measured_qubits)
        measurement_duration = self._active_service_durations()[1] * len({i.time_group for i in measurements})
        cache: list[tuple[bool, np.ndarray, tuple]] = []
        computed = reused = 0
        for shot_index in range(operation.shots):
            entry_state = np.asarray(self.state_store.read().density_matrix, dtype=complex)
            # Everything before the final measurement is a deterministic channel of the entry
            # state. Channels never increase trace distance, so a shot whose entry state is within
            # the tolerance of an earlier one reuses its result; the random draws are unchanged.
            # A shot with kicks depends on more than its entry state, so it neither reuses nor is reused.
            shot_kicks = kicks.of_shot(shot_index)
            cached = None if shot_kicks else _find_reusable(
                cache, shot_index > 0, entry_state, self.config.entry_reuse_tolerance)
            if cached is None:
                cached = self._run_before_measurement(operation, slots, entry_state, shot_index, shot_kicks)
                computed += 1
                if not shot_kicks:
                    if len(cache) >= _entry_cache_size(entry_state):
                        cache.pop(0)
                    cache.append((shot_index > 0, entry_state, cached))
            else:
                reused += 1
            state, assignment, duration = cached
            classical = [0] * operation.classical_bits
            # The validated final block has distinct, commuting targets and no later gates.
            # Sample its joint distribution once.
            state, bits = sample_measurement(state, measured_slots, generator)
            for q, bit, target in zip(measured_qubits, bits, classical_targets):
                p10, p01 = assignment[q]
                classical[target] = int(generator.random() < p10) if bit == 0 else int(generator.random() >= p01)
            duration += measurement_duration
            if shot_index == operation.shots - 1:
                for placed in kicks.trailing:
                    state = self._apply_kick(state, placed)
            self._commit_event(
                job=job, event_kind="program_shot", next_state=freeze_register(state),
                duration_us=duration, next_owner_tenant_id=job.command.tenant_id,
            )
            label = "".join(str(bit) for bit in classical)
            counts[label] = counts.get(label, 0) + 1
        self.last_entry_reuse = (computed, reused)
        return {"shots": operation.shots, "counts": counts}

    last_entry_reuse: tuple[int, int] = (0, 0)

    def _run_before_measurement(self, operation: CircuitExecutionOperation, slots: tuple[int, ...],
                                state: np.ndarray, shot_index: int, shot_kicks: tuple[PlacedKick, ...] = ()):
        """One shot up to, not including, the final measurement; returns (state, readout, time).

        ``shot_kicks`` are rotations from other jobs, applied before the instruction they are placed at.
        """
        snapshot = self.state_store.read()
        duration = 0.0
        if shot_index and self.config.repetition_delay_us > 0.0:
            # A leak in the wait acts at its recorded time. Evolve in segments so later
            # relaxation does not get moved ahead of that kick.
            elapsed = 0.0
            wait_kicks = sorted((kick for kick in shot_kicks if kick.wait_offset_us is not None),
                                key=lambda kick: kick.wait_offset_us)
            for placed in wait_kicks:
                offset = placed.wait_offset_us
                if offset > self.config.repetition_delay_us:
                    raise ValueError("kick wait offset exceeds the repetition delay")
                if offset > elapsed:
                    state = np.asarray(self._active_idle_model().evolve(state, elapsed_us=offset - elapsed),
                                       dtype=complex)
                    elapsed = offset
                state = self._apply_kick(state, placed)
            if elapsed < self.config.repetition_delay_us:
                state = np.asarray(self._active_idle_model().evolve(
                    state, elapsed_us=self.config.repetition_delay_us - elapsed), dtype=complex)
            duration += self.config.repetition_delay_us
        assignment: dict[int, tuple[float, float]] = {}
        if operation.initialization == "inherit":
            errors = register_assignment_errors(
                self._active_reset_models().get(self.config.reset_model_id),
                subsystem_count=len(snapshot.physical_qubit_ids), targets=slots,
            )
            assignment.update(enumerate(errors))
        pending = [kick for kick in shot_kicks if kick.wait_offset_us is None]
        for index, instruction in enumerate(operation.instructions):
            while pending and pending[0].position <= index:
                state = self._apply_kick(state, pending.pop(0))
            operands = tuple(slots[q] for q in instruction.qubits)
            if instruction.kind in {"initialize", "reset"}:
                readout = self._active_reset_models().run_register(self.config.reset_model_id, ResetModelRequest(
                    input_state=freeze_register(state), reset_count=1, victim_measurement=False,
                    subsystem_count=len(snapshot.physical_qubit_ids), targets=operands,
                ))
                self.state_store.validate_state(readout.post_reset_state)
                state = np.asarray(readout.post_reset_state, dtype=complex)
                assignment.update(zip(instruction.qubits, readout.assignment_errors))
                duration += self._active_service_durations()[0]
            elif instruction.kind == "unitary":
                if instruction.unitary is None:
                    raise ValueError("compiled unitary is missing")
                state = apply_operand_operator(state, instruction.unitary, operands)
                if instruction.depolarizing_probability:
                    state = apply_local_depolarizing_replacement(
                        state, operands, instruction.depolarizing_probability,
                    )
                duration += instruction.duration_us
            elif instruction.kind == "delay":
                if instruction.wait_us > 0.0:
                    state = self._idle_subset(state, operands, instruction.wait_us)
                duration += instruction.duration_us
            elif instruction.kind == "measure":
                continue  # the final measurement block is sampled by the caller
            else:
                raise ValueError("unknown compiled circuit instruction")
        for placed in pending:
            state = self._apply_kick(state, placed)
        state = np.array(state, dtype=complex)
        state.flags.writeable = False
        return state, assignment, duration

    def _apply_kick(self, state: np.ndarray, placed: PlacedKick) -> np.ndarray:
        """A leaked rotation from another job's drive; it takes no time of its own."""
        slot = self.state_store.read().physical_qubit_ids.index(placed.qubit)
        return apply_operand_operator(state, kick_unitary(placed), (slot,))

    def _idle_subset(self, state, slots: tuple[int, ...], elapsed_us: float):
        """Relax only the named register slots; the other qubits are left untouched."""
        model = self._active_idle_model()
        disabled = IdleQubitSpec(math.inf, math.inf, 0.0)
        subset = IndependentIdleModel(
            tuple(spec if index in slots else disabled for index, spec in enumerate(model.qubits)),
            model.max_local_qubits)
        return np.asarray(subset.evolve(state, elapsed_us=elapsed_us), dtype=complex)

    def _active_idle_model(self) -> IndependentIdleModel:
        if self.tenant_regions:
            return self.regional_idle_models[self.state_store.read().physical_qubit_ids]
        return self.idle_model

    def _service_reset(self, job: JobRecord) -> ResetModelResult | RegisterResetResult:
        snapshot = self.state_store.read()
        isolated = (
            self.config.placement_policy
            is PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE
        )
        input_state = (
            density_from_ket((1.0 + 0.0j, 0.0j))
            if isolated else snapshot.density_matrix
        )
        if isinstance(job.command.operation, CircuitExecutionOperation):
            # The FIFO locks this whole local region; reset only the new layout.
            targets = ((0,) if isolated else tuple(snapshot.physical_qubit_ids.index(q) for q in job.command.layout.physical_qubit_ids))
            outcome = self._active_reset_models().run_register(self.config.reset_model_id, ResetModelRequest(
                input_state=input_state, reset_count=self.config.reset_count,
                victim_measurement=self.config.measure_input_state_before_transition,
                subsystem_count=len(snapshot.physical_qubit_ids), targets=targets,
            ))
            self.state_store.validate_state(outcome.post_reset_state)
            if isolated:
                self._assign_isolated_resource(job, self._target_qubit_id(job))
            self._commit_event(job=job, event_kind="service_reset", next_state=outcome.post_reset_state,
                duration_us=self._active_service_durations()[0], next_owner_tenant_id=None)
            return outcome
        model = self._active_reset_models().get(self.config.reset_model_id)
        outcome = model.run(ResetModelRequest(
            input_state=input_state,
            reset_count=self.config.reset_count,
            victim_measurement=self.config.measure_input_state_before_transition,
        ))
        if outcome.post_reset_state is None:
            raise ValueError("the stateful runtime requires a reset post-state")
        self.state_store.validate_state(outcome.post_reset_state)
        # A malformed readout channel must not leave an ownerless committed state.
        _required_probability(
            outcome.observed_one_given_true_zero,
            "observed_one_given_true_zero",
        )
        _required_probability(
            outcome.observed_zero_given_true_one,
            "observed_zero_given_true_one",
        )
        if isolated:
            self._assign_isolated_resource(job, self._target_qubit_id(job))
        self._commit_event(
            job=job,
            event_kind="service_reset",
            next_state=outcome.post_reset_state,
            duration_us=self._active_service_durations()[0],
            next_owner_tenant_id=None,
        )
        return outcome

    def _validate_job_target(self, command: JobCommand) -> None:
        snapshot = self.state_store.read()
        physical_ids = (self.tenant_regions[command.tenant_id]
                        if self.config.placement_policy is PlacementPolicy.TENANT_DEDICATED_REGION
                        else snapshot.physical_qubit_ids)
        if isinstance(command.operation, CircuitExecutionOperation):
            layout = command.layout
            if layout is None or layout.qpu_id != snapshot.qpu_id or len(layout.physical_qubit_ids) != command.operation.logical_qubits:
                raise ValueError("compiled program requires a matching server-owned layout")
            if command.operation.interleave is not None:
                self._validate_interleaved(command)
            region = (next((binding for binding in self.state_store.regional_bindings
                            if binding.physical_qubit_ids == physical_ids), None)
                      if self.tenant_regions else self.state_store.region_binding)
            if region is not None:
                region.validate_layout(layout)
                for instruction in command.operation.instructions:
                    if instruction.kind == "unitary":
                        region.topology.validate_direct_operands(
                            tuple(layout.physical_qubit_ids[q] for q in instruction.qubits),
                        )
            binding = self.config.resource_binding
            if binding is None:
                if not set(layout.physical_qubit_ids) <= set(physical_ids):
                    raise ValueError("program layout is outside the retained local region")
            else:
                if self._job_authorization is None:
                    raise PermissionError("job authorization is not configured")
                role = self._job_authorization.tenant_roles[command.tenant_id]
                expected = binding.resolve(role)
                if layout.physical_qubit_ids != (expected.physical_qubit_id,):
                    raise ValueError("program layout does not match the scenario binding")
            if any(item.kind == "delay" and item.wait_us > 0.0 for item in command.operation.instructions):
                if (self.regional_idle_models.get(physical_ids) if self.tenant_regions else self.idle_model) is None:
                    raise ValueError("a delay needs an explicit complete idle model")
            if command.operation.initialization == "inherit":
                targets = (tuple(physical_ids.index(q) for q in layout.physical_qubit_ids)
                           if binding is None else (0,))
                register_assignment_errors(
                    (self.regional_reset_models[physical_ids] if self.tenant_regions else self.reset_models).get(
                        self.config.reset_model_id),
                    subsystem_count=len(snapshot.physical_qubit_ids), targets=targets,
                )
            cells = 4 ** len(snapshot.physical_qubit_ids)
            def _weight(instructions):
                return sum((12 * len(item.qubits) if item.kind in {"initialize", "reset", "delay"}
                            else 4 * len(item.qubits) if item.kind == "measure"
                            else 2 * (1 << len(item.qubits))) for item in instructions)
            weights = _weight(command.operation.instructions)
            if command.operation.interleave is not None:
                # Each round adds the victim program and two handover resets.
                weights += (_weight(command.operation.interleave.operation.instructions)
                            + 2 * 12 * len(layout.physical_qubit_ids))
            if command.operation.shots * cells * weights > self.config.max_execution_work:
                raise ValueError("program exceeds the local matrix-work limit")
            return
        if len(snapshot.physical_qubit_ids) != 1:
            raise ValueError("legacy Reset workload operations require the one-qubit preset")
        binding = self.config.resource_binding
        if binding is None:
            return
        if isinstance(command.operation, VictimPrepareOperation):
            expected = binding.resolve(VICTIM_ROLE)
        elif isinstance(command.operation, AttackerMeasureOperation):
            expected = binding.resolve(ATTACKER_ROLE)
        else:
            raise TypeError("unsupported job operation")
        if command.target != expected:
            raise ValueError("job target does not match the scenario resource binding")
        if self.state_store.read().qpu_id != expected.qpu_id:
            raise ValueError("job target QPU does not match the active state authority")

    def _validate_interleaved(self, command: JobCommand) -> None:
        """Interleaving is defined only where tenants share one retained region."""
        victim = command.operation.interleave
        if (self.config.placement_policy is not PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT
                or self.tenant_regions or self.config.resource_binding is not None):
            raise ValueError("interleaved runs require the shared same-region placement")
        if self.config.repetition_delay_us > 0.0:
            raise ValueError("interleaved runs do not define a repetition delay")
        roles = self._job_authorization.tenant_roles if self._job_authorization else {}
        from core.attack_simulation.job_authorization import ATTACKER_ROLE, VICTIM_ROLE
        if roles.get(victim.tenant_id) != VICTIM_ROLE or roles.get(command.tenant_id) != ATTACKER_ROLE:
            raise PermissionError("interleaving pairs the scenario victim with the requesting attacker")
        if self._declared_event_limit is not None:
            # Worst case adds the entry handover reset and one idle-gap event.
            events = interleaved_work(
                command.operation, command.tenant_id, command.tenant_id,
                self.config.reset_duration_us, self.config.program_measurement_duration_us,
            ).event_count + 1
            if events > self._declared_event_limit:
                raise ValueError(
                    f"interleaved run needs up to {events} physical events; this environment allows "
                    f"{self._declared_event_limit}")
        snapshot = self.state_store.read()
        if (victim.layout.qpu_id != snapshot.qpu_id
                or not set(victim.layout.physical_qubit_ids) <= set(snapshot.physical_qubit_ids)):
            raise ValueError("interleaved victim layout is outside the retained region")
        if not set(victim.layout.physical_qubit_ids) <= set(command.layout.physical_qubit_ids):
            raise ValueError("interleaving requires the victim's qubits to lie within the attacker's physical qubits")

    def _authorize(self, command: JobCommand) -> None:
        inherits = (isinstance(command.operation, CircuitExecutionOperation)
                    and command.operation.initialization == "inherit")
        if self._job_authorization is None:
            if self.config.resource_binding is not None or inherits:
                raise PermissionError("job authorization is not configured")
            return
        self._job_authorization.authorize(
            command.tenant_id,
            command.operation.catalog_operation_id,
        )
        if inherits:
            self._job_authorization.authorize(command.tenant_id, STATE_INHERIT_OPERATION_ID)
        if isinstance(command.operation, CircuitExecutionOperation) and command.operation.interleave is not None:
            self._job_authorization.authorize(
                command.tenant_id, interleave_operation_id(command.operation.interleave.reference))

    def _target_qubit_id(self, job: JobRecord) -> int | None:
        if self.config.resource_binding is None:
            return None
        if isinstance(job.command.operation, CircuitExecutionOperation):
            return job.command.layout.physical_qubit_ids[0]
        if job.command.target is None:
            raise ValueError("bound runtime requires a job target")
        if not isinstance(job.command.operation, (VictimPrepareOperation, AttackerMeasureOperation, CircuitExecutionOperation)):
            raise TypeError("unsupported job operation")
        return job.command.target.physical_qubit_id

    def _assign_isolated_resource(self, job: JobRecord, qubit_id: int | None) -> None:
        """Select a fresh zero-initialized resource before normal service reset.

        The service event preserves the same queue and measurement path while
        removing any state dependence on the previous tenant.  It is a
        controlled isolation baseline, not a claim about a specific provider.
        """

        self._commit_event(
            job=job,
            event_kind="isolated_resource_assignment",
            next_state=density_from_ket((1.0 + 0.0j, 0.0j)),
            duration_us=self.config.isolated_assignment_duration_us,
            next_owner_tenant_id=None,
            next_physical_qubit_ids=(qubit_id,) if qubit_id is not None else None,
        )

    def _active_reset_models(self) -> ResetModelRegistry:
        if self.tenant_regions:
            return self.regional_reset_models[self.state_store.read().physical_qubit_ids]
        return self.reset_models

    def _active_service_durations(self) -> tuple[float, float]:
        if self.tenant_regions:
            return self.regional_service_durations[self.state_store.read().physical_qubit_ids]
        return self.config.reset_duration_us, self.config.program_measurement_duration_us

    def _assign_tenant_region(self, job: JobRecord, target_region: tuple[int, ...]) -> None:
        """Restore the parked region after evolving only its declared idle interval."""
        parked = self.state_store.inactive_region()
        snapshot = self.state_store.read()
        if parked is None or parked.physical_qubit_ids != target_region:
            raise ValueError("dedicated job has no retained target region")
        elapsed = snapshot.device_time_us + self.config.isolated_assignment_duration_us - parked.updated_at_us
        if elapsed < 0:
            raise ValueError("regional physical time cannot run backwards")
        restored = self.regional_idle_models[target_region].evolve(
            np.asarray(parked.density_matrix, dtype=complex), elapsed_us=elapsed,
        )
        self._commit_event(
            job=job, event_kind="tenant_region_assignment", next_state=freeze_register(restored),
            duration_us=self.config.isolated_assignment_duration_us,
            next_owner_tenant_id=parked.owner_tenant_id,
            next_physical_qubit_ids=target_region,
        )

    def _commit_event(
        self,
        *,
        job: JobRecord,
        event_kind: str,
        next_state: Matrix,
        duration_us: float,
        next_owner_tenant_id: str | None,
        next_physical_qubit_ids: tuple[int, ...] | None = None,
    ) -> None:
        snapshot = self.state_store.read()
        event_id = f"event-{self._next_event_sequence:06d}"
        self.state_store.commit(
            snapshot.revision,
            QpuStateTransition(
                event_id=event_id,
                event_kind=event_kind,
                job_id=job.job_id,
                next_density_matrix=next_state,
                duration_us=duration_us,
                next_owner_tenant_id=next_owner_tenant_id,
                scenario_manifest_id=job.command.scenario_manifest_id,
                next_physical_qubit_ids=next_physical_qubit_ids,
            ),
        )
        self._next_event_sequence += 1



def _entry_cache_size(state: np.ndarray) -> int:
    """How many earlier shots to remember: at most 64, and at most 32 MB of states."""
    return max(4, min(64, (32 << 20) // state.nbytes))


def _find_reusable(cache, repeats: bool, entry_state: np.ndarray, tolerance: float):
    """A cached shot result whose entry state is within ``tolerance`` in trace distance, or None.

    Cheap bounds first: trace distance >= ||D||_F / 2 and <= sqrt(d) * ||D||_F / 2.
    """
    dimension = entry_state.shape[0]
    for cached_repeats, cached_entry, result in reversed(cache):
        if cached_repeats != repeats:
            continue
        delta = entry_state - cached_entry
        norm = float(np.linalg.norm(delta))
        if norm == 0.0:
            return result
        if tolerance <= 0.0 or 0.5 * norm > tolerance:
            continue
        if 0.5 * math.sqrt(dimension) * norm <= tolerance:
            return result
        eigenvalues = np.linalg.eigvalsh((delta + delta.conj().T) / 2.0)
        if 0.5 * float(np.abs(eigenvalues).sum()) <= tolerance:
            return result
    return None

def _one_probability(state: Matrix) -> float:
    value = float(state[1][1].real)
    if not math.isfinite(value) or value < -1e-12 or value > 1.0 + 1e-12:
        raise ValueError("state has an invalid one probability")
    return min(1.0, max(0.0, value))


def _required_probability(value: float | None, name: str) -> float:
    if value is None:
        raise ValueError(f"the stateful runtime requires {name}")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0 or converted > 1.0:
        raise ValueError(f"{name} must be finite and between zero and one")
    return converted


def _identifier(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    return converted
