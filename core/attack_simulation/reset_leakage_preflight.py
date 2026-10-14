"""Private challenge-instance preflight for the reset-leakage exercise."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import random

from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.qpu_state import QpuStateStore, single_qubit_zero_snapshot
from core.attack_simulation.attack_path import (
    RESET_LEAKAGE_MECHANISM,
    reset_leakage_attack_path_config,
)
from core.attack_simulation.resource_binding import (
    PhysicalQubitRef,
    ResetResourceBinding,
    reset_resource_binding_config,
)
from core.attack_simulation.reset_leakage_analysis import (
    FrequencyThresholdTrainingError,
    FrequencyThresholdTrainingResult,
    LabeledBinaryObservation,
    fit_frequency_threshold_estimator,
)
from core.attack_simulation.reset_leakage_grader import (
    HiddenEvaluationResult,
    ResetLeakageHiddenGrader,
)
from core.attack_simulation.reset_leakage_scenario import (
    ResetLeakageOrchestratorConfig,
    ResetLeakageRuntimePlan,
    ResetLeakageScenarioOrchestrator,
)
from core.attack_simulation.reset_leakage_settings import (
    RESET_LEAKAGE_V0_SETTINGS,
    ResetLeakageV0Settings,
)
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_assumptions import (
    AssumptionBasis,
    AssumptionRecord,
)
from core.attack_simulation.scenario_policy import (
    ScenarioMode,
    ScenarioPolicy,
    ScenarioPreset,
    ScenarioTemplate,
)
from core.attack_simulation.shared_qpu_runtime import (
    SharedQpuRuntime,
    SharedQpuRuntimeConfig,
)
from core.experimental_isolation_physics import (
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResetModelRegistry,
)


class ResetLeakagePreflightCondition(str, Enum):
    CANDIDATE = "candidate"
    PERFECT_RESET_CONTROL = "perfect_reset_control"
    ISOLATED_RESOURCE_CONTROL = "isolated_resource_control"


@dataclass(frozen=True)
class ResetLeakageSeedBundle:
    """Manager-private random inputs for one challenge instance."""

    training_measurement_seed: int
    hidden_case_order_seed: int
    hidden_measurement_seed: int
    victim_secret_seed: int

    def __post_init__(self) -> None:
        for name in (
            "training_measurement_seed",
            "hidden_case_order_seed",
            "hidden_measurement_seed",
            "victim_secret_seed",
        ):
            object.__setattr__(self, name, _seed(getattr(self, name), name))

    def to_private_dict(self) -> dict[str, int]:
        return {
            "training_measurement_seed": self.training_measurement_seed,
            "hidden_case_order_seed": self.hidden_case_order_seed,
            "hidden_measurement_seed": self.hidden_measurement_seed,
            "victim_secret_seed": self.victim_secret_seed,
        }


@dataclass(frozen=True)
class ResetLeakagePreflightPolicy:
    selection_seed: int
    max_attempts: int = 32

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selection_seed",
            _seed(self.selection_seed, "selection_seed"),
        )
        attempts = int(self.max_attempts)
        if attempts < 1:
            raise ValueError("max_attempts must be positive")
        object.__setattr__(self, "max_attempts", attempts)


@dataclass(frozen=True)
class ResetLeakagePreflightResult:
    """Private qualification record; never project this through tenant APIs."""

    profile_id: str
    profile_version: str
    selection_seed: int
    max_attempts: int
    attempts_used: int
    seed_bundle: ResetLeakageSeedBundle
    training: FrequencyThresholdTrainingResult
    candidate: HiddenEvaluationResult
    perfect_reset_control: HiddenEvaluationResult
    isolated_resource_control: HiddenEvaluationResult
    constant_zero_correct: int
    constant_one_correct: int
    rejected_training: int
    rejected_candidate: int
    rejected_controls: int

    def to_private_dict(self) -> dict[str, object]:
        return {
            "contract": "reset_leakage_instance_preflight_v1",
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "selection_seed": self.selection_seed,
            "max_attempts": self.max_attempts,
            "attempts_used": self.attempts_used,
            "seed_bundle": self.seed_bundle.to_private_dict(),
            "training": self.training.to_public_dict(),
            "candidate": self.candidate.to_public_dict(),
            "perfect_reset_control": (
                self.perfect_reset_control.to_public_dict()
            ),
            "isolated_resource_control": (
                self.isolated_resource_control.to_public_dict()
            ),
            "constant_zero_correct": self.constant_zero_correct,
            "constant_one_correct": self.constant_one_correct,
            "rejections_before_selection": {
                "training": self.rejected_training,
                "candidate": self.rejected_candidate,
                "controls": self.rejected_controls,
            },
            "visibility": "scenario_manager_private",
        }


class ResetLeakagePreflightError(RuntimeError):
    """Raised after the bounded candidate search finds no valid instance."""

    def __init__(
        self,
        *,
        max_attempts: int,
        rejected_training: int,
        rejected_candidate: int,
        rejected_controls: int,
    ) -> None:
        super().__init__(
            "no reset-leakage seed bundle passed preflight within "
            f"{max_attempts} attempts"
        )
        self.max_attempts = max_attempts
        self.rejected_training = rejected_training
        self.rejected_candidate = rejected_candidate
        self.rejected_controls = rejected_controls


ResetLeakageOrchestratorFactory = Callable[
    [ResetLeakagePreflightCondition, ResetLeakageSeedBundle, int],
    ResetLeakageScenarioOrchestrator,
]


def preflight_reset_leakage_instance(
    *,
    policy: ResetLeakagePreflightPolicy,
    settings: ResetLeakageV0Settings = RESET_LEAKAGE_V0_SETTINGS,
    orchestrator_factory: ResetLeakageOrchestratorFactory | None = None,
) -> ResetLeakagePreflightResult:
    """Select one bounded, manager-private challenge seed bundle."""

    factory = orchestrator_factory or (
        lambda condition, bundle, attempt: build_reset_leakage_v0_orchestrator(
            condition=condition,
            seed_bundle=bundle,
            attempt=attempt,
            settings=settings,
        )
    )
    generator = random.Random(policy.selection_seed)
    rejected_training = 0
    rejected_candidate = 0
    rejected_controls = 0
    hidden_policy_factory = settings.hidden_evaluation_policy
    constant_zero_correct = settings.hidden_zero_case_count
    constant_one_correct = (
        settings.hidden_case_count - settings.hidden_zero_case_count
    )
    if max(constant_zero_correct, constant_one_correct) >= (
        settings.hidden_required_correct
    ):
        raise ValueError("constant estimator can pass the hidden policy")

    for attempt in range(1, policy.max_attempts + 1):
        bundle = ResetLeakageSeedBundle(
            training_measurement_seed=generator.getrandbits(32),
            hidden_case_order_seed=generator.getrandbits(32),
            hidden_measurement_seed=generator.getrandbits(32),
            victim_secret_seed=generator.getrandbits(32),
        )
        candidate_orchestrator = factory(
            ResetLeakagePreflightCondition.CANDIDATE,
            bundle,
            attempt,
        )
        try:
            training = _train_canonical_estimator(
                candidate_orchestrator,
                settings,
            )
        except FrequencyThresholdTrainingError:
            rejected_training += 1
            continue
        hidden_policy = hidden_policy_factory(
            case_order_seed=bundle.hidden_case_order_seed,
            measurement_seed=bundle.hidden_measurement_seed,
        )
        candidate = ResetLeakageHiddenGrader(
            orchestrator=candidate_orchestrator,
            policy=hidden_policy,
        ).grade(training.estimator)
        if not candidate.passed:
            rejected_candidate += 1
            continue

        perfect = ResetLeakageHiddenGrader(
            orchestrator=factory(
                ResetLeakagePreflightCondition.PERFECT_RESET_CONTROL,
                bundle,
                attempt,
            ),
            policy=hidden_policy,
        ).grade(training.estimator)
        isolated = ResetLeakageHiddenGrader(
            orchestrator=factory(
                ResetLeakagePreflightCondition.ISOLATED_RESOURCE_CONTROL,
                bundle,
                attempt,
            ),
            policy=hidden_policy,
        ).grade(training.estimator)
        if perfect.passed or isolated.passed:
            rejected_controls += 1
            continue

        return ResetLeakagePreflightResult(
            profile_id=settings.profile_id,
            profile_version=settings.profile_version,
            selection_seed=policy.selection_seed,
            max_attempts=policy.max_attempts,
            attempts_used=attempt,
            seed_bundle=bundle,
            training=training,
            candidate=candidate,
            perfect_reset_control=perfect,
            isolated_resource_control=isolated,
            constant_zero_correct=constant_zero_correct,
            constant_one_correct=constant_one_correct,
            rejected_training=rejected_training,
            rejected_candidate=rejected_candidate,
            rejected_controls=rejected_controls,
        )

    raise ResetLeakagePreflightError(
        max_attempts=policy.max_attempts,
        rejected_training=rejected_training,
        rejected_candidate=rejected_candidate,
        rejected_controls=rejected_controls,
    )


def build_reset_leakage_v0_orchestrator(
    *,
    condition: ResetLeakagePreflightCondition,
    seed_bundle: ResetLeakageSeedBundle,
    attempt: int,
    settings: ResetLeakageV0Settings = RESET_LEAKAGE_V0_SETTINGS,
) -> ResetLeakageScenarioOrchestrator:
    """Build one fresh internal runtime for a preflight condition."""

    if not isinstance(condition, ResetLeakagePreflightCondition):
        condition = ResetLeakagePreflightCondition(condition)
    if condition is ResetLeakagePreflightCondition.PERFECT_RESET_CONTROL:
        excited = float(settings.target_excited_population)
        reset_model = PhenomenologicalResetModel(
            spec=ImperfectResetSpec(
                success_probability=1.0,
                target_populations=(1.0 - excited, excited),
            ),
            observed_one_given_zero=settings.observed_one_given_zero,
            observed_zero_given_one=settings.observed_zero_given_one,
            model_id="perfect_reset_control",
        )
    else:
        reset_model = settings.build_reset_model()

    placement = (
        PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE
        if condition is ResetLeakagePreflightCondition.ISOLATED_RESOURCE_CONTROL
        else PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT
    )
    registry = ResetModelRegistry()
    registry.register(reset_model)
    qpu_id = f"reset-v0-{condition.value}-{attempt}"
    binding_config = reset_resource_binding_config(
        qpu_id,
        isolated=placement is PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE,
    )
    runtime_config = SharedQpuRuntimeConfig(
        reset_model_id=reset_model.model_id,
        reset_model_configuration_id=reset_model.configuration_id,
        reset_count=settings.reset_count,
        reset_duration_us=1.0,
        victim_operation_duration_us=1.0,
        attacker_measurement_duration_us=1.0,
        measure_input_state_before_transition=True,
        placement_policy=placement,
        isolated_assignment_duration_us=0.0,
        resource_binding=ResetResourceBinding(
            victim=PhysicalQubitRef(qpu_id, 0),
            attacker=PhysicalQubitRef(
                qpu_id,
                1 if placement is PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE else 0,
            ),
        ),
        program_measurement_duration_us=1.0,
        max_local_qubits=8,
        max_execution_work=536870912,
    )
    policy = ScenarioPolicy(
        mode=ScenarioMode.CHALLENGE,
        allowed_operations=("program.submit", "job.status.read", "job.result.read", "job.cancel"),
        locked_fields=(
            "attacker.operation_grants",
            "attacker.credential_delivery",
            "attacker.credential_origin",
            "victim.operation_grants",
            "qpu_service.supported_operations",
            "qpu_service.runtime_execution",
            "attacker.reset_leakage_access",
        ),
        observation_budget=settings.observation_budget(
            victim_observation_limit=1,
        ),
        evaluator_id="binary_victim_output_v1",
        submission_enabled=True,
    )
    preset = ScenarioPreset(
        template=ScenarioTemplate(
            template_id=settings.profile_id,
            template_version=settings.profile_version,
            attacker_config={
                "workspace": "terminal",
                "credential_delivery": "preconfigured_profile",
                "credential_origin": "simulator_issued",
                "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
                "reset_leakage_access": {
                    "labeled_training_enabled": True,
                    "victim_observation_enabled": True,
                },
            },
            victim_config={"workload": "binary_output", "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"]},
            qpu_service_config={
                "execution_regime": "nisq_physical",
                "supported_credential_deliveries": ["preconfigured_profile", "manual_simulated_credential"],
                "supported_operations": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
                "resource_inventory": {"qpu_id": qpu_id, "physical_qubit_ids": (0, 1)},
                "reset_model_id": reset_model.model_id,
                "reset_model_configuration_id": reset_model.configuration_id,
                "reset_model_mode": "state_transition_with_readout",
                "runtime_execution": runtime_config.to_manifest_config(),
                "supported_placement_policies": (placement.value,),
                "supported_attack_mechanisms": (RESET_LEAKAGE_MECHANISM,),
            },
            relation_config={
                "attack_path": reset_leakage_attack_path_config(),
                "resource_binding": binding_config,
                "placement": {"policy": placement.value},
                "target_binding": {"policy": "scenario_bound_target"},
            },
            experiment_config={
                "training": {"label_source": "controlled_reference"},
            },
            free_override_fields=(
                "attacker.credential_delivery",
                "attacker.reset_leakage_access",
                "qpu_service.supported_operations",
                "qpu_service.runtime_execution",
            ),
        ),
        challenge_policy=policy,
        assumptions=tuple(
            AssumptionRecord(
                assumption_id=f"reset_v0.fixed.{path.replace('.', '_')}",
                target_paths=(path,),
                basis=AssumptionBasis.SIMULATOR_CHOICE,
                rationale="Fixed by the Reset v0 challenge profile before scenario start.",
                applicability={
                    "profile_id": settings.profile_id,
                    "profile_version": settings.profile_version,
                },
            )
            for path in policy.locked_fields
        ),
    )
    session = preset.materialize(ScenarioMode.CHALLENGE)
    plan = ResetLeakageRuntimePlan.from_manifest(
        ScenarioEnvironment(session).establish()
    )
    state_store = QpuStateStore(single_qubit_zero_snapshot(
        qpu_id=qpu_id,
        physical_qubit_id=0,
        device_config_version=f"{settings.profile_id}:{settings.profile_version}",
    ))
    runtime = SharedQpuRuntime(state_store=state_store, reset_models=registry,
                               config=runtime_config)
    return ResetLeakageScenarioOrchestrator(
        runtime=runtime,
        plan=plan,
        config=ResetLeakageOrchestratorConfig(
            instance_id=f"preflight-{attempt}-{condition.value}",
        ),
        secret_seed=seed_bundle.victim_secret_seed,
        measurement_seed=seed_bundle.training_measurement_seed,
    )


def _train_canonical_estimator(
    orchestrator: ResetLeakageScenarioOrchestrator,
    settings: ResetLeakageV0Settings,
) -> FrequencyThresholdTrainingResult:
    return fit_frequency_threshold_estimator(
        LabeledBinaryObservation.from_attacker_result(result)
        for victim_bit in (0, 1)
        for result in (
            orchestrator.run_training_query(
                victim_bit=victim_bit,
                shots=settings.shots_per_case,
            ).to_attacker_dict()
            for _ in range(settings.training_queries_per_label)
        )
    )


def _seed(value: int, name: str) -> int:
    converted = int(value)
    if converted < 0 or converted > 2 ** 32 - 1:
        raise ValueError(f"{name} must be between zero and 2**32 - 1")
    return converted
