"""Tenant-scoped job operations for a trusted application principal resolver.

The caller must obtain principal IDs from an authenticated context.  This
in-process service is not itself an authentication mechanism or an HTTP API.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import hashlib
import secrets
from types import MappingProxyType
from threading import Lock

from core.attack_simulation.circuit_program import (
    CircuitExecutionOperation, InterleavedVictim, LocalCircuitCompiler,
)
from core.attack_simulation.observation_budget import (
    ObservationBudgetExceeded, ObservationBudgetLedger, ObservationQueryKind,
)
from core.attack_simulation.program_request import (
    CircuitProgram, ProgramRequestError, ProgramSubmitRequest, STATE_INHERIT_OPERATION_ID,
    interleave_operation_id,
    VICTIM_ACTIVITY_OPERATION_ID,
)
from core.attack_simulation.scenario_policy import ObservationBudget
from core.attack_simulation.request_receipts import RequestReceipt

from core.attack_simulation.jobs import (
    JobCommand,
    JobOperation,
    JobRecord,
    JobRetentionExpiredError,
    JobStatus,
    TERMINAL_JOB_STATUSES,
    AttackerMeasureOperation,
    VictimPrepareOperation,
)
from core.attack_simulation.resource_binding import PhysicalQubitLayout, PhysicalQubitRef
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime
from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest
from core.attack_simulation.job_authorization import JobAuthorizationPolicy


@dataclass(frozen=True)
class PrincipalGrant:
    principal_id: str
    tenant_id: str
    operations: frozenset[str]

    def __post_init__(self) -> None:
        if not self.principal_id or not self.principal_id.strip():
            raise ValueError("principal_id must be nonempty")
        if not self.tenant_id or not self.tenant_id.strip():
            raise ValueError("tenant_id must be nonempty")
        if isinstance(self.operations, str):
            raise TypeError("principal operations must be a collection of operation IDs")
        object.__setattr__(self, "operations", frozenset(self.operations))
        if any(not isinstance(value, str) or not value.strip()
               for value in self.operations):
            raise ValueError("principal operations must be nonempty identifiers")


@dataclass(frozen=True)
class JobStatusView:
    job_id: str
    status: JobStatus
    error_code: str | None = None


@dataclass(frozen=True)
class JobResultView:
    job_id: str
    status: JobStatus
    result: dict[str, object] | None
    error_code: str | None = None


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str
    max_qubits: int
    supported_gates: tuple[str, ...]
    max_shots: int
    max_instruction_executions: int
    queue_policy: str
    cancellation_policy: str
    initialization_policy: str = "imperfect_reset_then_declared_preparation"
    gate_model: str = "ideal_unitaries_with_reset_and_readout"
    max_local_qubits: int = 8
    max_execution_work: int = 536870912
    classical_bit_order: str = "c0_leftmost"


@dataclass(frozen=True)
class InterleaveVictimSpec:
    """Scenario-owned victim program an attacker may name but never read or edit."""

    reference: str
    principal_id: str
    program: CircuitProgram
    # Programs the victim runs in turn: the k-th accepted run uses
    # sequence[k mod len]. Empty means every run uses ``program``.
    sequence: tuple[CircuitProgram, ...] = ()

    def __post_init__(self) -> None:
        interleave_operation_id(self.reference)
        if not isinstance(self.principal_id, str) or not self.principal_id.strip():
            raise ValueError("interleaved victim principal is required")
        if not isinstance(self.program, CircuitProgram):
            raise TypeError("interleaved victim program must be a CircuitProgram")
        if (type(self.sequence) is not tuple
                or any(not isinstance(item, CircuitProgram) for item in self.sequence)):
            raise TypeError("interleaved victim sequence must be a tuple of CircuitPrograms")
        if self.sequence and self.program is not self.sequence[0]:
            raise ValueError("a sequenced victim's program is its first entry")


# The participant view shows the first shots only; the pattern repeats and the full
# trace of a long job would be thousands of steps.
MAX_ACTIVITY_SHOTS = 16


class JobNotFoundError(LookupError):
    """The job is absent or outside the principal's visible resource scope."""


class JobExpiredError(JobNotFoundError):
    """The principal's own job record was discarded by the retention bound."""


class JobResultNotReadyError(RuntimeError):
    """The job has not reached a terminal state."""


class JobApplicationService:
    """Apply operation grants and tenant scope before touching the scheduler."""

    def __init__(
        self,
        *,
        runtime: SharedQpuRuntime,
        principals: Iterable[PrincipalGrant],
        creator_only_operations: frozenset[str] = frozenset(),
        program_compiler: LocalCircuitCompiler | None = None,
        scenario_manifest_id: str | None = None,
        program_layouts: Mapping[str, PhysicalQubitLayout] | None = None,
        interleave_victims: Iterable[InterleaveVictimSpec] = (),
        observation_budget: ObservationBudget | None = None,
    ) -> None:
        policy = runtime.job_authorization
        if policy is None:
            raise ValueError("job authorization must be configured before the job service")
        indexed: dict[str, PrincipalGrant] = {}
        for principal in principals:
            if principal.principal_id in indexed:
                raise ValueError("principal IDs must be unique")
            if principal.tenant_id not in policy.tenant_roles:
                raise ValueError("principal tenant has no scenario actor role")
            indexed[principal.principal_id] = principal
        if not indexed:
            raise ValueError("at least one principal is required")
        self._runtime = runtime
        self._principals = indexed
        self._creator_only_operations = frozenset(creator_only_operations)
        self._program_compiler = program_compiler
        layouts = dict(program_layouts or {})
        if layouts and runtime.config.resource_binding is not None:
            raise ValueError("explicit program layouts cannot override the Reset scenario binding")
        snapshot = runtime.state_store.read()
        for principal_id, layout in layouts.items():
            if principal_id not in indexed or not isinstance(layout, PhysicalQubitLayout):
                raise ValueError("program layout must belong to a registered principal")
            allowed_region = runtime.tenant_regions.get(indexed[principal_id].tenant_id,
                                                         snapshot.physical_qubit_ids)
            if layout.qpu_id != snapshot.qpu_id or not set(layout.physical_qubit_ids) <= set(allowed_region):
                raise ValueError("program layout is outside the retained local region")
        self._program_layouts = MappingProxyType(layouts)
        victims: dict[str, InterleaveVictimSpec] = {}
        for spec in interleave_victims:
            if type(spec) is not InterleaveVictimSpec or spec.reference in victims:
                raise ValueError("interleaved victim references must be unique specs")
            if (spec.principal_id not in indexed
                    or policy.tenant_roles[indexed[spec.principal_id].tenant_id] != "victim"):
                raise ValueError("interleaved victim program must belong to the scenario victim")
            victims[spec.reference] = spec
        self._interleave_victims = MappingProxyType(victims)
        # Compile declared sequence alternatives during assembly, outside any
        # admission/physical commit lock. Repeated bits share one immutable IR.
        self._victim_sequences: dict[str, tuple[InterleavedVictim, ...]] = {}
        for reference, spec in victims.items():
            if spec.sequence:
                if program_compiler is None:
                    raise ValueError("interleaved sequences require a program compiler")
                compiled = {
                    program: self._compile_victim(program_compiler, spec, program,
                                                   snapshot.qpu_id, snapshot.qpu_id, 0)
                    for program in dict.fromkeys(spec.sequence)
                }
                self._victim_sequences[reference] = tuple(compiled[program] for program in spec.sequence)
        self._budget_ledger = None if observation_budget is None else ObservationBudgetLedger(observation_budget)
        # Sequenced victims: run key -> position, fixed once at first acceptance.
        self._victim_positions: dict[tuple[str, str | None, str], int] = {}
        self._victim_next: dict[str, int] = {}
        self._scenario_manifest_id = scenario_manifest_id
        self._sampling_salt = secrets.token_bytes(32)
        self._session_owner: object | None = None
        self._session_owner_lock = Lock()

    def validate_session_binding(self, manifest: ResolvedScenarioManifest, executor: object) -> None:
        """Supervisor-only consistency check; never exposes runtime to a client.

        This checks the current adapter's declared compiler/authorization and
        executor, not undeclared physics or a future generic RuntimePlan.
        """
        if self._scenario_manifest_id != manifest.manifest_id:
            raise ValueError("job service belongs to a different scenario manifest")
        runtime_profile = manifest.qpu_service_config.get("runtime_execution")
        if (not isinstance(runtime_profile, Mapping)
                or dict(runtime_profile) != self._runtime.config.to_manifest_config()):
            raise ValueError("QPU runtime behavior differs from the frozen manifest")
        binding = self._runtime.state_store.region_binding
        if "topology" in manifest.qpu_service_config:
            if binding is None:
                raise ValueError("declared topology requires a retained region binding")
            from core.attack_simulation.qpu_topology import QpuTopology
            topology = QpuTopology.from_config(
                manifest.qpu_service_config["topology"], limits=binding.topology.limits,
            )
            if topology != binding.topology:
                raise ValueError("QPU topology differs from the established manifest")
        elif binding is not None:
            raise ValueError("retained region binding requires topology in the manifest")
        if self._runtime.config.placement_policy.value == "tenant_dedicated_region":
            primary = manifest.qpu_service_config.get("retained_region")
            alternate = manifest.qpu_service_config.get("dedicated_region")
            if not isinstance(primary, Mapping) or not isinstance(alternate, Mapping):
                raise ValueError("dedicated placement requires both frozen regions")
            expected_regions = {
                manifest.victim_config.get("tenant_id"): tuple(primary.get("physical_qubit_ids", ())),
                manifest.attacker_config.get("tenant_id"): tuple(alternate.get("physical_qubit_ids", ())),
            }
            if (self._runtime.tenant_regions != expected_regions
                    or {item.physical_qubit_ids for item in self._runtime.state_store.regional_bindings}
                    != set(expected_regions.values())
                    or any(self._program_layouts.get(principal_id) != PhysicalQubitLayout(
                        binding.topology.qpu_id, expected_regions[principal.tenant_id],
                    ) for principal_id, principal in self._principals.items())):
                raise ValueError("regional runtime or principal allocation differs from the manifest")
        policy = self._runtime.job_authorization
        if policy is None:
            raise ValueError("job service authorization is missing")
        by_role = {role: tenant for tenant, role in policy.tenant_roles.items()}
        if set(by_role) != {"attacker", "victim"}:
            raise ValueError("unsupported actor tenant binding")
        expected_policy = JobAuthorizationPolicy.from_manifest(
            manifest, attacker_tenant_id=by_role["attacker"], victim_tenant_id=by_role["victim"],
        )
        if (policy.service_operations != expected_policy.service_operations
                or policy.scenario_operations != expected_policy.scenario_operations
                or policy.actor_grants != expected_policy.actor_grants):
            raise ValueError("job authorization differs from the established manifest")
        expected_compiler = (LocalCircuitCompiler.from_manifest(manifest)
                             if "circuit_execution" in manifest.qpu_service_config else None)
        if self._program_compiler != expected_compiler:
            raise ValueError("circuit compiler differs from the established manifest")
        expected_executor = self._runtime.run_next
        if (getattr(executor, "__self__", None) is not expected_executor.__self__
                or getattr(executor, "__func__", None) is not expected_executor.__func__):
            raise ValueError("session executor belongs to another runtime")
        with self._session_owner_lock:
            if self._session_owner is not None or self._runtime.session_owned:
                raise ValueError("job service is already owned by a session")

    def claim_session(self, owner: object) -> None:
        """Trusted assembly only; a discarded service cannot enter a new world."""
        with self._session_owner_lock:
            if self._session_owner is not None:
                raise ValueError("job service is already owned by a session")
            self._runtime.claim_session(owner)
            self._session_owner = id(owner)  # Do not retain the session graph.

    def _abort_session_claim(self, owner: object) -> None:
        """Construction rollback only; successful/closed worlds remain claimed."""
        with self._session_owner_lock:
            if self._session_owner != id(owner):
                raise PermissionError("cannot release another session's service claim")
            self._runtime._abort_session_claim(owner)
            self._session_owner = None

    @property
    def session_principal_ids(self) -> frozenset[str]:
        """Trusted assembly metadata, not a participant device/job projection."""
        return frozenset(self._principals)

    def validate_workload_binding(self, plan: object, *, role: str) -> None:
        """Pure supervisor preflight, not an authorization grant or submission.

        Zero-grant actors remain valid: common admission checks their permissions
        when an operation is attempted. Configuration/capability errors fail
        before the declared clock starts instead of becoming physics failures.
        """
        from core.attack_simulation.victim_workload import WorkloadPlan
        if type(plan) is not WorkloadPlan or role not in ("attacker", "victim"):
            raise ValueError("a validated plan and declared actor role are required")
        principal = self._principals.get(plan.actor_id)
        policy = self._runtime.job_authorization
        if (principal is None or policy is None
                or policy.tenant_roles.get(principal.tenant_id) != role):
            raise ValueError("workload actor does not match its declared role")
        if self._program_compiler is None:
            raise ValueError("workload service requires the common Program compiler")
        snapshot = self._runtime.state_store.read()
        programs = {item.program_id: item.program for item in plan.programs}
        for step in plan.steps:
            request = ProgramSubmitRequest(programs[step.program_ref], step.device_id, step.shots,
                                           "workload-preflight", step.initialization)
            if request.device_id != snapshot.qpu_id:
                raise ValueError("workload device is not available")
            available = self._program_layouts.get(plan.actor_id)
            ids = available.physical_qubit_ids if available else snapshot.physical_qubit_ids
            target = None
            if self._runtime.config.resource_binding is not None:
                binding = self._runtime.config.resource_binding
                target = binding.attacker if role == "attacker" else binding.victim
                ids = (target.physical_qubit_id,)
            operation = self._program_compiler.compile(
                request, sampling_seed=0, physical_qubit_ids=tuple(ids),
            )
            if operation.logical_qubits > len(ids):
                raise ValueError("workload exceeds its configured local allocation")
            command = JobCommand(principal.tenant_id, operation, "workload-preflight",
                                 self._scenario_manifest_id, target, principal.principal_id,
                                 PhysicalQubitLayout(snapshot.qpu_id, tuple(ids[:operation.logical_qubits])))
            self._runtime._validate_job_target(command)

    def list_devices(self, principal_id: str) -> tuple[DeviceInfo, ...]:
        self._authorize(principal_id, "device.info.read")
        compiler = self._program_compiler
        if compiler is None:
            return ()
        snapshot = self._runtime.state_store.read()
        layout = self._program_layouts.get(principal_id)
        capacity = len(layout.physical_qubit_ids) if layout is not None else len(snapshot.physical_qubit_ids)
        if self._runtime.config.resource_binding is not None:
            capacity = 1  # Explicit one-qubit Reset scenario binding, not a platform restriction.
        return (DeviceInfo(
            device_id=snapshot.qpu_id,
            max_qubits=min(compiler.max_program_qubits, capacity),
            supported_gates=compiler.supported_gates,
            max_shots=compiler.max_shots,
            max_instruction_executions=compiler.max_instruction_executions,
            queue_policy=self._runtime.config.queue_policy_id,
            cancellation_policy=self._runtime.config.cancellation_policy_id,
            max_local_qubits=self._runtime.config.max_local_qubits,
            max_execution_work=self._runtime.config.max_execution_work,
            initialization_policy=(
                "imperfect_reset_then_declared_preparation"
                if "inherit" not in compiler.supported_initializations
                else "inherited_physical_register"
                if "prepare" not in compiler.supported_initializations
                else "explicit_preparation_or_inherited_physical_register"
            ),
        ),)

    def get_device(self, principal_id: str, device_id: str) -> DeviceInfo:
        for device in self.list_devices(principal_id):
            if device.device_id == device_id:
                return device
        raise LookupError("device is not available")

    def submit_program(self, principal_id: str, request: ProgramSubmitRequest) -> JobStatusView:
        return self._admit_program(principal_id, self._prepare_program(principal_id, request))

    def _prepare_program(self, principal_id: str, request: ProgramSubmitRequest) -> JobCommand:
        """Trusted assembly only: compile without queue or QPU-state mutation."""
        principal = self._authorize(principal_id, "program.submit")
        compiler = self._program_compiler
        if compiler is None:
            raise ProgramRequestError("this service has no circuit program compiler")
        if not isinstance(request, ProgramSubmitRequest):
            raise TypeError("request must be a ProgramSubmitRequest")
        if request.initialization == "inherit":
            self._authorize(principal_id, STATE_INHERIT_OPERATION_ID)
        snapshot = self._runtime.state_store.read()
        if request.device_id != snapshot.qpu_id:
            raise ProgramRequestError("device is not available")
        digest = hashlib.sha256(
            self._sampling_salt + principal.principal_id.encode("utf-8")
            + b"\x00" + request.idempotency_key.encode("utf-8")
        ).digest()
        target = None
        allowed_layout = self._program_layouts.get(principal_id)
        available = allowed_layout.physical_qubit_ids if allowed_layout else snapshot.physical_qubit_ids
        binding = self._runtime.config.resource_binding
        if binding is not None:
            role = self._runtime.job_authorization.tenant_roles[principal.tenant_id]
            target = binding.victim if role == "victim" else binding.attacker
            available = (target.physical_qubit_id,)
        if request.interleave is not None:
            self._authorize(principal_id, interleave_operation_id(request.interleave))
        operation = compiler.compile(
            request, sampling_seed=int.from_bytes(digest[:4], "big"),
            physical_qubit_ids=tuple(available),
        )
        if operation.logical_qubits > len(available):
            raise ProgramRequestError("program exceeds this principal's local allocation")
        layout = PhysicalQubitLayout(snapshot.qpu_id, tuple(available[:operation.logical_qubits]))
        if request.interleave is not None:
            operation = replace(operation, interleave=self._interleaved_victim(
                request, compiler, digest, snapshot.qpu_id, layout))
        return JobCommand(
            tenant_id=principal.tenant_id, operation=operation,
            idempotency_key=request.idempotency_key, target=target,
            scenario_manifest_id=self._scenario_manifest_id,
            created_by_principal_id=principal.principal_id,
            layout=layout,
        )

    def _interleaved_victim(self, request: ProgramSubmitRequest, compiler: LocalCircuitCompiler,
                            digest: bytes, qpu_id: str, attacker_layout: PhysicalQubitLayout) -> InterleavedVictim:
        spec = self._interleave_victims.get(request.interleave)
        if spec is None:
            raise ProgramRequestError("this scenario does not provide that interleaved victim")
        victim = self._compile_victim(
            compiler, spec, spec.program, request.device_id, qpu_id,
            int.from_bytes(hashlib.sha256(digest + b"\x00victim").digest()[:4], "big"))
        if not set(victim.layout.physical_qubit_ids) <= set(attacker_layout.physical_qubit_ids):
            raise ProgramRequestError("interleaved runs require the shared same-region placement (victim and attacker on the same qubits)")
        return victim

    def _compile_victim(self, compiler: LocalCircuitCompiler, spec: InterleaveVictimSpec,
                        program: CircuitProgram, device_id: str, qpu_id: str,
                        sampling_seed: int) -> InterleavedVictim:
        configured = self._program_layouts.get(spec.principal_id)
        available = (configured.physical_qubit_ids if configured
                     else self._runtime.state_store.read().physical_qubit_ids)
        operation = compiler.compile(
            ProgramSubmitRequest(program, device_id, 1, f"interleave-{spec.reference}", "prepare"),
            sampling_seed=sampling_seed, physical_qubit_ids=tuple(available),
        )
        layout = PhysicalQubitLayout(qpu_id, tuple(available[:operation.logical_qubits]))
        return InterleavedVictim(spec.reference, self._principals[spec.principal_id].tenant_id,
                                 operation, layout)

    def _sequenced_victim(self, command: JobCommand) -> tuple[JobCommand, int | None]:
        """Pick the victim program by acceptance order, not by preparation.

        Preparation compiled the first program as a placeholder. The position is
        fixed at the first acceptance of a run key, so a replay rebuilds the
        same command; rejected submissions never take a position.
        """
        victim = command.operation.interleave
        spec = self._interleave_victims[victim.reference]
        if not spec.sequence:
            return command, None
        run = (command.tenant_id, command.created_by_principal_id, command.idempotency_key)
        position = self._victim_positions.get(run, self._victim_next.get(spec.reference, 0))
        template = self._victim_sequences[victim.reference][position % len(spec.sequence)]
        chosen = replace(template, operation=replace(
            template.operation, sampling_seed=victim.operation.sampling_seed))
        if chosen.layout != victim.layout:
            raise ProgramRequestError("interleaved victim layout changed after preparation")
        operation = replace(command.operation, interleave=chosen)
        return replace(command, operation=operation), position

    def observation_budget_view(self, principal_id: str) -> dict[str, object] | None:
        """Public limits and usage for the interleaved observation channel."""
        self._authorize(principal_id, "device.info.read")
        if self._budget_ledger is None:
            return None
        budget, usage = self._budget_ledger.budget, self._budget_ledger.read()
        return {
            "shots_per_query": budget.shots_per_query,
            "training_query_limit": budget.training_query_limit,
            "victim_observation_limit": budget.victim_observation_limit,
            "training_queries": usage.training_queries,
            "victim_observations": usage.victim_observations,
        }

    def _charge_observation(self, command: JobCommand) -> None:
        """Reserve one query at the scheduler's first-acceptance boundary."""
        operation = command.operation
        if self._budget_ledger is None:
            return
        kind = (ObservationQueryKind.VICTIM if operation.interleave.reference == "victim"
                else ObservationQueryKind.TRAINING)
        try:
            self._budget_ledger.reserve(kind=kind, shots=operation.shots)
        except ObservationBudgetExceeded as error:
            raise ProgramRequestError(f"observation budget: {error}") from error

    def _admit_program(
        self, principal_id: str, command: JobCommand,
        *, receipt: RequestReceipt | None = None,
    ) -> JobStatusView:
        """Trusted prepared-command boundary, not a participant submit API.

        Authorization and server allocation are rechecked after preparation.
        The session owns receipt authenticity and ordering; the scheduler
        retains only the original accepted job's immutable arrival metadata.
        """
        principal = self._authorize(principal_id, "program.submit")
        if type(command) is not JobCommand or type(command.operation) is not CircuitExecutionOperation:
            raise TypeError("admission requires a prepared circuit JobCommand")
        if (command.created_by_principal_id != principal.principal_id
                or command.tenant_id != principal.tenant_id):
            raise PermissionError("prepared command belongs to a different principal")
        if command.scenario_manifest_id != self._scenario_manifest_id:
            raise ProgramRequestError("prepared command belongs to a different scenario manifest")
        if self._program_compiler is None:
            raise ProgramRequestError("this service has no circuit program compiler")
        if command.operation.initialization == "inherit":
            self._authorize(principal_id, STATE_INHERIT_OPERATION_ID)
        if command.operation.initialization not in self._program_compiler.supported_initializations:
            raise ProgramRequestError("initialization is not supported by this service")
        snapshot = self._runtime.state_store.read()
        configured_layout = self._program_layouts.get(principal_id)
        available = (configured_layout.physical_qubit_ids if configured_layout
                     else snapshot.physical_qubit_ids)
        binding = self._runtime.config.resource_binding
        target = None
        if binding is not None:
            role = self._runtime.job_authorization.tenant_roles[principal.tenant_id]
            target = binding.victim if role == "victim" else binding.attacker
            available = (target.physical_qubit_id,)
        width = command.operation.logical_qubits
        if (width > len(available) or command.target != target
                or command.layout != PhysicalQubitLayout(snapshot.qpu_id, tuple(available[:width]))):
            raise ProgramRequestError("prepared program does not match the server allocation")
        try:
            if command.operation.interleave is not None:
                self._authorize(principal_id, interleave_operation_id(command.operation.interleave.reference))
                # Reject invalid commands before they consume observation budget.
                self._runtime._authorize(command)
                self._runtime._validate_job_target(command)
                with self._runtime.scheduler.admission_transaction():
                    command, position = self._sequenced_victim(command)
                    submitted = self._runtime.submit(
                        command, receipt=receipt, before_accept=lambda: self._charge_observation(command),
                    )
                    run = (command.tenant_id, command.created_by_principal_id, command.idempotency_key)
                    if position is not None and run not in self._victim_positions:
                        self._victim_positions[run] = position
                        self._victim_next[command.operation.interleave.reference] = position + 1
                return _status(submitted)
            return _status(self._runtime.submit(command, receipt=receipt))
        except JobRetentionExpiredError:
            # A replay of a discarded job: the key is the submitter's own.
            raise JobExpiredError("job record is past its retention period") from None
        except ValueError as error:
            raise ProgramRequestError(str(error)) from error

    def submit(
        self,
        principal_id: str,
        *,
        operation: JobOperation,
        idempotency_key: str,
        target: PhysicalQubitRef | None = None,
        scenario_manifest_id: str | None = None,
    ) -> JobStatusView:
        principal = self._authorize(principal_id, "program.submit")
        if isinstance(operation, CircuitExecutionOperation):
            raise PermissionError("compiled circuits must enter through submit_program")
        if operation.catalog_operation_id != "program.submit":
            raise PermissionError("operation is not a program submission")
        command = JobCommand(
            tenant_id=principal.tenant_id,
            operation=operation,
            idempotency_key=idempotency_key,
            scenario_manifest_id=scenario_manifest_id,
            target=target,
            created_by_principal_id=principal.principal_id,
        )
        return _status(self._runtime.submit(command))

    def _submit_scenario_workload(
        self,
        principal_id: str,
        *,
        operation: VictimPrepareOperation,
        idempotency_key: str,
        target: PhysicalQubitRef | None = None,
        scenario_manifest_id: str | None = None,
    ) -> JobStatusView:
        principal = self._authorize(principal_id, "program.submit")
        policy = self._runtime.job_authorization
        if policy is None or policy.tenant_roles[principal.tenant_id] != "victim":
            raise PermissionError("scenario workload requires the victim actor")
        command = JobCommand(
            tenant_id=principal.tenant_id,
            operation=operation,
            idempotency_key=idempotency_key,
            scenario_manifest_id=scenario_manifest_id,
            target=target,
            created_by_principal_id=principal.principal_id,
        )
        return _status(self._runtime._submit_scenario_workload(command))

    def get_status(self, principal_id: str, job_id: str) -> JobStatusView:
        return _status(self._visible_job(principal_id, job_id, "job.status.read"))

    def get_result(self, principal_id: str, job_id: str) -> JobResultView:
        job = self._visible_job(principal_id, job_id, "job.result.read")
        if job.status not in TERMINAL_JOB_STATUSES or not self._runtime.result_is_available(job_id):
            raise JobResultNotReadyError("job result is not ready")
        return JobResultView(
            job_id=job.job_id,
            status=job.status,
            result=_project_result(job),
            error_code=job.error_code,
        )

    def _observe_declared_job(self, principal_id: str, job_id: str, operation_id: str, position):
        """Trusted coordinator boundary, not a public timestamp parameter.

        Authorization/tenant scope is the same as ordinary job operations.
        Only the registered executor can prove the requested historical state.
        Receipt capture and live scheduling remain the caller's responsibility.
        """
        from core.attack_simulation.declared_execution import (
            DeclaredObservationNotReadyError, JobNotAtPositionError,
        )
        self._visible_job(principal_id, job_id, operation_id)
        executor = self._runtime._declared_executor
        if executor is None:
            raise ValueError("timed observation requires a declared executor")
        try:
            return executor.observation_at(job_id, position)
        except JobNotAtPositionError:
            raise JobNotFoundError("job not found") from None
        except DeclaredObservationNotReadyError:
            raise JobResultNotReadyError("declared observation is waiting for confirmed effects") from None

    def _get_status_at(self, principal_id: str, job_id: str, position) -> JobStatusView:
        status, _ = self._observe_declared_job(principal_id, job_id, "job.status.read", position)
        return JobStatusView(job_id, status)

    def _get_result_at(self, principal_id: str, job_id: str, position) -> JobResultView:
        status, ready = self._observe_declared_job(principal_id, job_id, "job.result.read", position)
        if not ready:
            raise JobResultNotReadyError("job result is not ready at this position")
        # A result copy captured before FINISH must not be reused after the
        # timed proof. Reauthorize and obtain the confirmed terminal record.
        job = self._visible_job(principal_id, job_id, "job.result.read")
        if job.status is not status:
            raise JobResultNotReadyError("job changed after the timed observation proof")
        return JobResultView(job_id, status, _project_result(job), job.error_code)

    def get_victim_activity(self, principal_id: str, job_id: str) -> dict[str, object]:
        """Abstract command: the runs behind the principal's own finished job.

        Free-mode scenarios enable it explicitly; challenge presets never do. Only
        the job's recorded steps are returned (who ran when), never a state or a
        measured value, and only once the job's result is available.
        """
        job = self._visible_job(principal_id, job_id, VICTIM_ACTIVITY_OPERATION_ID)
        if job.status not in TERMINAL_JOB_STATUSES or not self._runtime.result_is_available(job_id):
            raise JobResultNotReadyError("job activity is not ready")
        return self._project_victim_activity(job)

    def _get_victim_activity_at(self, principal_id: str, job_id: str, position) -> dict[str, object]:
        status, ready = self._observe_declared_job(principal_id, job_id, VICTIM_ACTIVITY_OPERATION_ID, position)
        if not ready:
            raise JobResultNotReadyError("job activity is not ready at this position")
        job = self._visible_job(principal_id, job_id, VICTIM_ACTIVITY_OPERATION_ID)
        if job.status is not status:
            raise JobResultNotReadyError("job changed after the timed observation proof")
        return self._project_victim_activity(job)

    def _project_victim_activity(self, job: JobRecord) -> dict[str, object]:
        """Group the job's recorded events into per-shot steps for the participant."""
        roles = self._runtime.job_authorization.tenant_roles
        operation = job.command.operation
        interleave = getattr(operation, "interleave", None)
        reference = None if interleave is None else interleave.reference
        prepared_bit = None
        if reference is not None:
            spec = self._interleave_victims.get(reference)
            if spec is not None:
                prepared_bit = int(spec.program.to_circuit_dict()["initial_states"][0])
        shots: list[list[dict[str, object]]] = [[]]
        total = 0
        for event in self._runtime.state_store.events():
            if event.job_id != job.job_id:
                continue
            if event.event_kind == "program_shot":
                actor = roles.get(event.owner_after, "unknown")
                step = {"actor": actor, "kind": "victim_run" if actor == "victim" else "attacker_measure"}
            elif event.event_kind == "service_reset":
                step = {"actor": "service", "kind": "service_reset", "after": roles.get(event.owner_before)}
            elif event.event_kind == "declared_idle":
                step = {"actor": "service", "kind": "idle"}
            else:
                step = {"actor": "service", "kind": str(event.event_kind)}
            step["start_us"] = float(event.device_time_start_us)
            step["end_us"] = float(event.device_time_end_us)
            shots[-1].append(step)
            if step["kind"] == "attacker_measure":
                total += 1
                shots.append([])
        if not shots[-1]:
            shots.pop()  # A job that ends right after an attacker shot leaves an empty tail.
        shown = shots[:MAX_ACTIVITY_SHOTS]
        return {
            "job_id": job.job_id, "reference": reference, "victim_prepared_bit": prepared_bit,
            "shots_total": total, "shots_shown": len(shown),
            "shots": [{"index": index, "steps": steps} for index, steps in enumerate(shown)],
        }

    def cancel(self, principal_id: str, job_id: str) -> JobStatusView:
        self._visible_job(principal_id, job_id, "job.cancel")
        executor = self._runtime._declared_executor
        if executor is not None and executor._control_owner is not None:
            raise PermissionError("this world requires coordinated timed control")
        return _status(self._runtime.cancel(job_id))

    def _cancel_at(self, principal_id: str, job_id: str, **evidence):
        """Trusted timed operation, preserving common grant/tenant/creator scope."""
        executor = self._runtime._declared_executor
        if executor is None:
            raise ValueError("timed control requires a declared executor")
        from core.attack_simulation.declared_execution import JobNotAtPositionError
        with executor._gate:
            self._visible_job(principal_id, job_id, "job.cancel")
            try:
                return executor.apply_cancellation(principal_id=principal_id, job_id=job_id, **evidence)
            except JobNotAtPositionError:
                raise JobNotFoundError("job not found") from None

    def _visible_job(
        self, principal_id: str, job_id: str, operation_id: str,
    ) -> JobRecord:
        principal = self._authorize(principal_id, operation_id)
        try:
            job = self._runtime.scheduler.get(job_id)
        except JobRetentionExpiredError as error:
            visible = error.tenant_id == principal.tenant_id and (
                operation_id not in self._creator_only_operations
                or error.principal_id == principal.principal_id)
            if visible:
                raise JobExpiredError("job record is past its retention period") from None
            raise JobNotFoundError("job not found") from None
        except KeyError as error:
            raise JobNotFoundError("job not found") from error
        if job.command.tenant_id != principal.tenant_id:
            raise JobNotFoundError("job not found")
        if (operation_id in self._creator_only_operations
                and job.command.created_by_principal_id != principal.principal_id):
            raise JobNotFoundError("job not found")
        return job

    def _authorize(self, principal_id: str, operation_id: str) -> PrincipalGrant:
        try:
            principal = self._principals[principal_id]
        except KeyError as error:
            raise PermissionError("principal is not registered") from error
        policy = self._runtime.job_authorization
        if policy is None:
            raise PermissionError("job authorization is not configured")
        policy.authorize(principal.tenant_id, operation_id)
        if operation_id not in principal.operations:
            raise PermissionError("principal is not granted this operation")
        return principal


def _status(job: JobRecord) -> JobStatusView:
    return JobStatusView(job_id=job.job_id, status=job.status, error_code=job.error_code)


def _project_result(job: JobRecord) -> dict[str, object] | None:
    if job.result is None:
        return None
    if isinstance(job.command.operation, (AttackerMeasureOperation, CircuitExecutionOperation)):
        counts = job.result.get("counts")
        shots = job.result.get("shots")
        if not isinstance(counts, dict) or type(shots) is not int:
            raise ValueError("measurement result has an invalid public shape")
        width = job.command.operation.classical_bits if isinstance(job.command.operation, CircuitExecutionOperation) else 1
        if not counts or any(not isinstance(label, str) or len(label) != width or set(label) - {"0", "1"} for label in counts) or any(
            type(value) is not int or value < 0 for value in counts.values()
        ) or shots != sum(counts.values()):
            raise ValueError("measurement counts have an invalid public shape")
        return {"shots": shots, "counts": dict(counts)}
    if isinstance(job.command.operation, VictimPrepareOperation):
        prepared = job.result.get("prepared")
        if not isinstance(prepared, bool):
            raise ValueError("preparation result has an invalid public shape")
        return {"prepared": prepared}
    raise TypeError("result projection is not defined for this operation")
