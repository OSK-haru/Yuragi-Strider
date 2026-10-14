"""Scenario orchestration for the first incomplete-reset leakage exercise."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import random
from types import MappingProxyType

from core.attack_simulation.attack_path import (
    AttackPathDeclaration,
    RESET_LEAKAGE_MECHANISM,
)
from core.attack_simulation.execution_regime import ExecutionRegime
from core.attack_simulation.jobs import (
    AttackerMeasureOperation,
    JobCommand,
    JobStatus,
    VictimPrepareOperation,
)
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.job_service import (
    JobApplicationService,
    JobResultView,
    JobStatusView,
    PrincipalGrant,
)
from core.attack_simulation.credential_policy import CredentialDelivery, CredentialOrigin
from core.attack_simulation.virtual_credentials import (
    CredentialedJobConnection,
    VirtualCredentialAuthority,
)
from core.attack_simulation.observation_budget import (
    ObservationBudgetLedger,
    ObservationBudgetUsage,
    ObservationQueryKind,
)
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.resource_binding import ResetResourceBinding
from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest
from core.attack_simulation.scenario_policy import ObservationBudget, ScenarioMode
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime
from core.experimental_isolation_physics.reset_model import ResetModelMode


class ResetLeakageQueryPhase(str, Enum):
    TRAINING = "training"
    VICTIM_OBSERVATION = "victim_observation"
    HIDDEN_EVALUATION = "hidden_evaluation"


class ResetLeakageSessionState(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"


class ScenarioAccessDenied(PermissionError):
    """Raised before execution when the active scenario forbids an action."""


class ScenarioSessionClosed(RuntimeError):
    """Raised when a closed scenario session receives another operation."""


@dataclass(frozen=True)
class ResetLeakageAccessConfig:
    """Attacker-visible processes selected by the scenario configuration."""

    labeled_training_enabled: bool
    victim_observation_enabled: bool

    def __post_init__(self) -> None:
        for name in (
            "labeled_training_enabled",
            "victim_observation_enabled",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")

    @classmethod
    def from_manifest(
        cls,
        manifest: ResolvedScenarioManifest,
    ) -> "ResetLeakageAccessConfig":
        value = manifest.attacker_config.get("reset_leakage_access")
        if not isinstance(value, Mapping):
            raise ValueError(
                "attacker_config.reset_leakage_access must be configured"
            )
        return cls(
            labeled_training_enabled=_required_bool(
                value,
                "labeled_training_enabled",
            ),
            victim_observation_enabled=_required_bool(
                value,
                "victim_observation_enabled",
            ),
        )


@dataclass(frozen=True)
class ResetLeakageRuntimePlan:
    """Small immutable hot-path input compiled from a resolved manifest."""

    manifest_id: str
    mode: ScenarioMode
    execution_regime: ExecutionRegime
    observation_budget: ObservationBudget
    access_config: ResetLeakageAccessConfig
    placement_policy: PlacementPolicy
    attack_path: AttackPathDeclaration
    resource_binding: ResetResourceBinding
    job_authorization: JobAuthorizationPolicy
    reset_model_id: str
    reset_model_configuration_id: str
    reset_model_mode: ResetModelMode
    runtime_execution_config: Mapping[str, object] | None = None
    credential_delivery: CredentialDelivery = CredentialDelivery.PRECONFIGURED_PROFILE
    credential_origin: CredentialOrigin = CredentialOrigin.SIMULATOR_ISSUED

    @classmethod
    def from_manifest(
        cls,
        manifest: ResolvedScenarioManifest,
    ) -> "ResetLeakageRuntimePlan":
        if not isinstance(manifest.observation_budget, ObservationBudget):
            raise ValueError("reset-leakage runtime requires an explicit observation budget")
        runtime_execution_config = manifest.qpu_service_config.get("runtime_execution")
        if runtime_execution_config is not None and not isinstance(runtime_execution_config, Mapping):
            raise ValueError("qpu_service_config.runtime_execution must be an object")
        execution_regime_value = _required_string(
            manifest.qpu_service_config,
            "execution_regime",
            "qpu_service_config",
        )
        try:
            execution_regime = ExecutionRegime(execution_regime_value)
        except ValueError as error:
            raise ValueError(
                "qpu_service_config.execution_regime is not supported"
            ) from error
        if execution_regime is not ExecutionRegime.NISQ_PHYSICAL:
            raise ValueError(
                "the current reset-leakage runtime supports only the nisq_physical profile"
            )
        attack_path = AttackPathDeclaration.from_manifest(manifest)
        if (
            attack_path.attacker_entry != "job_submission"
            or attack_path.shared_resource != "same_physical_qubit"
            or attack_path.mechanism_id != RESET_LEAKAGE_MECHANISM
            or attack_path.victim_asset != "nonpublic_output_bit"
            or attack_path.attacker_observation != "own_measurement_counts"
        ):
            raise ValueError("attack path is not supported by the reset-leakage runtime")
        placement = manifest.relation_config.get("placement")
        if not isinstance(placement, Mapping):
            raise ValueError("relation_config.placement must be configured")
        try:
            placement_policy = PlacementPolicy(placement["policy"])
        except KeyError as error:
            raise ValueError("relation_config.placement.policy is required") from error
        if placement_policy is PlacementPolicy.SCHEDULER_DEPENDENT:
            raise ValueError(
                "the initial reset-leakage runtime does not support scheduler-dependent placement"
            )
        supported_placements = _required_string_sequence(
            manifest.qpu_service_config,
            "supported_placement_policies",
            "qpu_service_config",
        )
        if placement_policy.value not in supported_placements:
            raise ValueError(
                "relation placement is not supported by the QPU service configuration"
            )
        resource_binding = ResetResourceBinding.from_manifest(manifest, placement_policy)
        job_authorization = JobAuthorizationPolicy.from_manifest(
            manifest,
            attacker_tenant_id="attacker",
            victim_tenant_id="victim",
        )
        reset_model_id = _required_string(
            manifest.qpu_service_config,
            "reset_model_id",
            "qpu_service_config",
        )
        reset_model_configuration_id = _required_string(
            manifest.qpu_service_config,
            "reset_model_configuration_id",
            "qpu_service_config",
        )
        reset_model_mode_value = _required_string(
            manifest.qpu_service_config,
            "reset_model_mode",
            "qpu_service_config",
        )
        try:
            reset_model_mode = ResetModelMode(reset_model_mode_value)
        except ValueError as error:
            raise ValueError(
                "qpu_service_config.reset_model_mode is not supported"
            ) from error
        if reset_model_mode is not ResetModelMode.STATE_TRANSITION_WITH_READOUT:
            raise ValueError(
                "the current reset-leakage runtime requires state_transition_with_readout"
            )
        access_config = ResetLeakageAccessConfig.from_manifest(manifest)
        if access_config.labeled_training_enabled:
            training = manifest.experiment_config.get("training")
            if not isinstance(training, Mapping):
                raise ValueError(
                    "experiment_config.training is required for labeled training"
                )
            _required_string(training, "label_source", "experiment_config.training")
        return cls(
            manifest_id=manifest.manifest_id,
            mode=manifest.mode,
            execution_regime=execution_regime,
            observation_budget=manifest.observation_budget,
            access_config=access_config,
            placement_policy=placement_policy,
            attack_path=attack_path,
            resource_binding=resource_binding,
            job_authorization=job_authorization,
            reset_model_id=reset_model_id,
            reset_model_configuration_id=reset_model_configuration_id,
            reset_model_mode=reset_model_mode,
            runtime_execution_config=runtime_execution_config,
            credential_delivery=CredentialDelivery(manifest.attacker_config.get(
                "credential_delivery", CredentialDelivery.PRECONFIGURED_PROFILE.value,
            )),
            credential_origin=CredentialOrigin(manifest.attacker_config.get(
                "credential_origin", CredentialOrigin.SIMULATOR_ISSUED.value,
            )),
        )


@dataclass(frozen=True)
class ResetLeakageOrchestratorConfig:
    instance_id: str
    victim_tenant_id: str = "victim"
    attacker_tenant_id: str = "attacker"

    def __post_init__(self) -> None:
        for name in (
            "instance_id",
            "victim_tenant_id",
            "attacker_tenant_id",
        ):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, value)
        if self.victim_tenant_id == self.attacker_tenant_id:
            raise ValueError("victim and attacker must be different tenants")


@dataclass(frozen=True)
class ResetLeakageObservation:
    """Attacker-visible aggregate; hidden victim bits are never stored here."""

    query_id: str
    attempt: int
    phase: ResetLeakageQueryPhase
    shots: int
    counts: Mapping[str, int]
    budget_usage: ObservationBudgetUsage
    known_label: int | None = None

    def __post_init__(self) -> None:
        if not self.query_id.strip():
            raise ValueError("query_id must not be empty")
        if self.attempt < 1 or self.shots < 1:
            raise ValueError("attempt and shots must be positive")
        if set(self.counts) != {"0", "1"}:
            raise ValueError("counts must contain exactly zero and one outcomes")
        normalized = {
            "0": int(self.counts["0"]),
            "1": int(self.counts["1"]),
        }
        if any(value < 0 for value in normalized.values()):
            raise ValueError("counts must be non-negative")
        if sum(normalized.values()) != self.shots:
            raise ValueError("counts must sum to shots")
        object.__setattr__(self, "counts", MappingProxyType(normalized))
        if self.known_label is not None:
            object.__setattr__(self, "known_label", _bit(self.known_label))

    def to_attacker_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "query_id": self.query_id,
            "attempt": self.attempt,
            "phase": self.phase.value,
            "shots": self.shots,
            "counts": dict(self.counts),
            "budget_usage": self.budget_usage.to_dict(),
        }
        if self.known_label is not None:
            result["known_label"] = self.known_label
        return result


class ResetLeakageExecutionError(RuntimeError):
    """Execution failure that exposes no secret or partial sample results."""

    def __init__(self, query_id: str, attempt: int, error_code: str) -> None:
        super().__init__(
            f"query {query_id} attempt {attempt} failed: {error_code}"
        )
        self.query_id = query_id
        self.attempt = attempt
        self.error_code = error_code


@dataclass(frozen=True)
class _QuerySpec:
    phase: ResetLeakageQueryPhase
    shots: int
    victim_bit: int
    known_label: int | None
    measurement_seeds: tuple[int, ...] | None = None


class ResetLeakageScenarioOrchestrator:
    """Runs every observation as a full victim-reset-attacker sequence.

    This layer is scenario-specific, while scheduling, QPU state, reset
    physics, and policy accounting remain shared infrastructure. The initial
    implementation owns a synchronous runtime exclusively during each query.
    """

    def __init__(
        self,
        *,
        runtime: SharedQpuRuntime,
        plan: ResetLeakageRuntimePlan,
        config: ResetLeakageOrchestratorConfig,
        secret_seed: int,
        measurement_seed: int,
        budget_ledger: ObservationBudgetLedger | None = None,
    ) -> None:
        self.runtime = runtime
        self.plan = plan
        self.config = config
        self.access_config = plan.access_config
        self.observation_budget = plan.observation_budget
        if runtime.config.reset_model_id != plan.reset_model_id:
            raise ValueError("runtime reset model does not match compiled scenario plan")
        if (
            runtime.config.reset_model_configuration_id
            != plan.reset_model_configuration_id
        ):
            raise ValueError(
                "runtime reset model configuration does not match scenario plan"
            )
        if runtime.config.reset_model_mode is not plan.reset_model_mode:
            raise ValueError("runtime reset model mode does not match scenario plan")
        if runtime.config.placement_policy is not plan.placement_policy:
            raise ValueError("runtime placement does not match compiled scenario plan")
        if runtime.config.resource_binding != plan.resource_binding:
            raise ValueError("runtime resource binding does not match compiled scenario plan")
        if plan.runtime_execution_config is None:
            raise ValueError("runtime behavior must be frozen in the scenario manifest")
        if dict(plan.runtime_execution_config) != runtime.config.to_manifest_config():
            raise ValueError("runtime behavior differs from the frozen manifest")
        runtime.configure_job_authorization(plan.job_authorization.with_tenants(
            attacker_tenant_id=config.attacker_tenant_id,
            victim_tenant_id=config.victim_tenant_id,
        ))
        self._attacker_principal_id = f"{config.instance_id}:attacker"
        self._victim_principal_id = f"{config.instance_id}:victim"
        self._job_service = JobApplicationService(
            runtime=runtime,
            principals=(
                PrincipalGrant(
                    self._attacker_principal_id,
                    config.attacker_tenant_id,
                    plan.job_authorization.actor_grants["attacker"],
                ),
                PrincipalGrant(
                    self._victim_principal_id,
                    config.victim_tenant_id,
                    plan.job_authorization.actor_grants["victim"],
                ),
            ),
        )
        if plan.credential_origin is not CredentialOrigin.SIMULATOR_ISSUED:
            raise ValueError("the runtime supports only simulator-issued credentials")
        self._credential_authority = VirtualCredentialAuthority()
        attacker_credential = self._credential_authority.issue(self._attacker_principal_id)
        victim_credential = self._credential_authority.issue(self._victim_principal_id)
        self.attacker_connection = CredentialedJobConnection(
            authority=self._credential_authority,
            service=self._job_service,
            principal_id=self._attacker_principal_id,
            preconfigured_credential=(
                attacker_credential
                if plan.credential_delivery is CredentialDelivery.PRECONFIGURED_PROFILE
                else None
            ),
        )
        self._manual_attacker_credential = (
            attacker_credential
            if plan.credential_delivery is CredentialDelivery.MANUAL_SIMULATED_CREDENTIAL
            else None
        )
        self._victim_connection = CredentialedJobConnection(
            authority=self._credential_authority,
            service=self._job_service,
            principal_id=self._victim_principal_id,
            preconfigured_credential=victim_credential,
        )
        self.budget_ledger = budget_ledger or ObservationBudgetLedger(
            plan.observation_budget
        )
        if self.budget_ledger.budget != plan.observation_budget:
            raise ValueError("budget ledger does not match scenario policy")
        self._secret_rng = random.Random(_seed(secret_seed, "secret_seed"))
        self._measurement_rng = random.Random(
            _seed(measurement_seed, "measurement_seed")
        )
        self._next_query_sequence = 1
        self._queries: dict[str, _QuerySpec] = {}
        self._attempts: dict[str, int] = {}
        self._session_state = ResetLeakageSessionState.ACTIVE

    @property
    def session_state(self) -> ResetLeakageSessionState:
        return self._session_state

    def close(self) -> None:
        """Close the session without silently resetting QPU state or budget."""

        if self.runtime.scheduler.queued_job_ids():
            raise RuntimeError("cannot close a session with queued work")
        self._credential_authority.close()
        self._manual_attacker_credential = None
        self._session_state = ResetLeakageSessionState.CLOSED

    def take_manual_attacker_credential(self) -> str:
        """Reveal the simulator-issued attacker credential once, never the victim's."""

        self._require_active()
        credential = self._manual_attacker_credential
        if credential is None:
            raise RuntimeError("no manual attacker credential is available")
        self._manual_attacker_credential = None
        return credential

    def run_training_query(
        self,
        *,
        victim_bit: int,
        shots: int,
    ) -> ResetLeakageObservation:
        self._require_active()
        if not self.access_config.labeled_training_enabled:
            raise ScenarioAccessDenied(
                "labeled training is disabled by attacker configuration"
            )
        bit = _bit(victim_bit)
        return self._start_query(_QuerySpec(
            phase=ResetLeakageQueryPhase.TRAINING,
            shots=int(shots),
            victim_bit=bit,
            known_label=bit,
        ))

    def run_victim_observation(self, *, shots: int) -> ResetLeakageObservation:
        self._require_active()
        if not self.access_config.victim_observation_enabled:
            raise ScenarioAccessDenied(
                "victim observation is disabled by attacker configuration"
            )
        return self._start_query(_QuerySpec(
            phase=ResetLeakageQueryPhase.VICTIM_OBSERVATION,
            shots=int(shots),
            victim_bit=self._secret_rng.getrandbits(1),
            known_label=None,
        ))

    def _run_hidden_evaluation_case(
        self,
        *,
        victim_bit: int,
        shots: int,
        measurement_seeds: tuple[int, ...],
    ) -> ResetLeakageObservation:
        """Run one grader-owned case without consuming attacker query limits.

        This is a trusted application boundary and must never be routed through
        the simulated attacker API. Evaluation case count is controlled by the
        grader policy, separately from attacker observation budget.
        """

        self._require_active()
        self.attacker_connection.require_authenticated()

        requested_shots = int(shots)
        seeds = tuple(
            _seed(value, "measurement_seed")
            for value in measurement_seeds
        )
        if len(seeds) != requested_shots:
            raise ValueError("one measurement seed is required per shot")
        self.observation_budget.require_within(
            shots=requested_shots,
        )
        query_id = self._allocate_query_id()
        return self._execute(
            query_id,
            1,
            _QuerySpec(
                phase=ResetLeakageQueryPhase.HIDDEN_EVALUATION,
                shots=requested_shots,
                victim_bit=_bit(victim_bit),
                known_label=None,
                measurement_seeds=seeds,
            ),
            self.budget_ledger.read(),
        )

    def retry_query(self, query_id: str) -> ResetLeakageObservation:
        self._require_active()
        self.attacker_connection.require_authenticated()
        try:
            spec = self._queries[query_id]
        except KeyError as error:
            raise KeyError(f"unknown query: {query_id}") from error
        usage = self.budget_ledger.reserve(
            kind=_budget_kind(spec.phase),
            shots=spec.shots,
            retry=True,
        )
        attempt = self._attempts[query_id] + 1
        self._attempts[query_id] = attempt
        return self._execute(query_id, attempt, spec, usage)

    def _start_query(self, spec: _QuerySpec) -> ResetLeakageObservation:
        self.attacker_connection.require_authenticated()
        usage = self.budget_ledger.reserve(
            kind=_budget_kind(spec.phase),
            shots=spec.shots,
        )
        query_id = self._allocate_query_id()
        self._queries[query_id] = spec
        self._attempts[query_id] = 1
        return self._execute(query_id, 1, spec, usage)

    def _allocate_query_id(self) -> str:
        query_id = f"query-{self._next_query_sequence:06d}"
        self._next_query_sequence += 1
        return query_id

    def _require_active(self) -> None:
        if self._session_state is not ResetLeakageSessionState.ACTIVE:
            raise ScenarioSessionClosed("scenario session is closed")

    def _execute(
        self,
        query_id: str,
        attempt: int,
        spec: _QuerySpec,
        usage: ObservationBudgetUsage,
    ) -> ResetLeakageObservation:
        if self.runtime.scheduler.queued_job_ids():
            raise ResetLeakageExecutionError(
                query_id,
                attempt,
                "RUNTIME_NOT_EXCLUSIVE",
            )
        counts = {"0": 0, "1": 0}
        for sample_index in range(spec.shots):
            prefix = (
                f"{self.config.instance_id}:{query_id}:"
                f"attempt-{attempt}:sample-{sample_index}"
            )
            self._run_victim_job(JobCommand(
                tenant_id=self.config.victim_tenant_id,
                operation=VictimPrepareOperation(spec.victim_bit),
                idempotency_key=f"{prefix}:victim",
                scenario_manifest_id=self.plan.manifest_id,
                target=self.plan.resource_binding.victim,
            ), query_id, attempt)
            attacker = self._run_job(JobCommand(
                tenant_id=self.config.attacker_tenant_id,
                operation=AttackerMeasureOperation(
                    spec.measurement_seeds[sample_index]
                    if spec.measurement_seeds is not None
                    else self._measurement_rng.getrandbits(32)
                ),
                idempotency_key=f"{prefix}:attacker",
                scenario_manifest_id=self.plan.manifest_id,
                target=self.plan.resource_binding.attacker,
            ), query_id, attempt)
            observed = _single_observed_bit(attacker)
            counts[str(observed)] += 1
        return ResetLeakageObservation(
            query_id=query_id,
            attempt=attempt,
            phase=spec.phase,
            shots=spec.shots,
            counts=counts,
            budget_usage=usage,
            known_label=spec.known_label,
        )

    def _run_job(
        self,
        command: JobCommand,
        query_id: str,
        attempt: int,
    ) -> JobResultView:
        submitted = self.attacker_connection.submit(
            operation=command.operation,
            idempotency_key=command.idempotency_key,
            target=command.target,
            scenario_manifest_id=command.scenario_manifest_id,
        )
        completed = self.runtime.run_next()
        if completed is None or completed.job_id != submitted.job_id:
            raise ResetLeakageExecutionError(
                query_id,
                attempt,
                "SCHEDULER_ORDER_VIOLATION",
            )
        if completed.status is not JobStatus.SUCCEEDED:
            raise ResetLeakageExecutionError(
                query_id,
                attempt,
                completed.error_code or f"JOB_{completed.status.value.upper()}",
            )
        return self.attacker_connection.get_result(submitted.job_id)

    def _run_victim_job(
        self,
        command: JobCommand,
        query_id: str,
        attempt: int,
    ) -> JobStatusView:
        submitted = self._victim_connection.submit_scenario_workload(
            operation=command.operation,
            idempotency_key=command.idempotency_key,
            target=command.target,
            scenario_manifest_id=command.scenario_manifest_id,
        )
        completed = self.runtime.run_next()
        if completed is None or completed.job_id != submitted.job_id:
            raise ResetLeakageExecutionError(query_id, attempt, "SCHEDULER_ORDER_VIOLATION")
        if completed.status is not JobStatus.SUCCEEDED:
            raise ResetLeakageExecutionError(
                query_id, attempt, completed.error_code or f"JOB_{completed.status.value.upper()}"
            )
        return self._victim_connection.get_status(submitted.job_id)


def _budget_kind(phase: ResetLeakageQueryPhase) -> ObservationQueryKind:
    if phase is ResetLeakageQueryPhase.TRAINING:
        return ObservationQueryKind.TRAINING
    return ObservationQueryKind.VICTIM


def _single_observed_bit(job: JobResultView) -> int:
    result = job.result
    if result is None or result.get("shots") != 1:
        raise ValueError("attacker measurement must return exactly one shot")
    counts = result.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("attacker measurement result has no counts")
    zero = int(counts.get("0", 0))
    one = int(counts.get("1", 0))
    if zero + one != 1 or zero not in (0, 1) or one not in (0, 1):
        raise ValueError("attacker measurement counts are not one-hot")
    return one


def _bit(value: int) -> int:
    converted = int(value)
    if converted not in (0, 1):
        raise ValueError("victim_bit must be zero or one")
    return converted


def _seed(value: int, name: str) -> int:
    converted = int(value)
    if converted < 0 or converted > 2 ** 32 - 1:
        raise ValueError(f"{name} must be between zero and 2**32 - 1")
    return converted


def _required_bool(value: Mapping[str, object], name: str) -> bool:
    try:
        selected = value[name]
    except KeyError as error:
        raise ValueError(f"reset_leakage_access.{name} is required") from error
    if type(selected) is not bool:
        raise TypeError(f"reset_leakage_access.{name} must be a boolean")
    return selected


def _required_string(
    value: Mapping[str, object],
    name: str,
    prefix: str,
) -> str:
    try:
        selected = value[name]
    except KeyError as error:
        raise ValueError(f"{prefix}.{name} is required") from error
    if not isinstance(selected, str):
        raise TypeError(f"{prefix}.{name} must be a string")
    converted = selected.strip()
    if not converted:
        raise ValueError(f"{prefix}.{name} must not be empty")
    return converted


def _required_string_sequence(
    value: Mapping[str, object],
    name: str,
    prefix: str,
) -> tuple[str, ...]:
    try:
        selected = value[name]
    except KeyError as error:
        raise ValueError(f"{prefix}.{name} is required") from error
    if not isinstance(selected, tuple):
        raise TypeError(f"{prefix}.{name} must be a sequence")
    converted = tuple(
        _required_string({"item": item}, "item", f"{prefix}.{name}")
        for item in selected
    )
    if not converted or len(set(converted)) != len(converted):
        raise ValueError(f"{prefix}.{name} must be non-empty and unique")
    return converted
