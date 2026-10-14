"""Focused tests for aggregate-only hidden reset-leakage grading."""

from dataclasses import dataclass
import unittest
from core.attack_simulation.resource_binding import (
    PhysicalQubitRef, ResetResourceBinding, reset_resource_binding_config,
)

from core.attack_simulation import (
    AssumptionBasis,
    AssumptionRecord,
    EstimatorExecutionError,
    FrequencyThresholdEstimator,
    LabeledBinaryObservation,
    HiddenEvaluationPolicy,
    ObservationBudget,
    QpuStateStore,
    ResetLeakageHiddenGrader,
    ResetLeakageOrchestratorConfig,
    ResetLeakageRuntimePlan,
    ResetLeakageScenarioOrchestrator,
    ScenarioCompiler,
    ScenarioMode,
    ScenarioPolicy,
    ScenarioSession,
    ScenarioTemplate,
    SharedQpuRuntime,
    SharedQpuRuntimeConfig,
    fit_frequency_threshold_estimator,
    single_qubit_zero_snapshot,
)
from core.experimental_isolation_physics import (
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResetModelRegistry,
)


class ResetLeakageHiddenGraderTests(unittest.TestCase):
    def test_threshold_estimator_passes_and_result_is_aggregate_only(self) -> None:
        store, orchestrator = _orchestrator()
        grader = ResetLeakageHiddenGrader(
            orchestrator=orchestrator,
            policy=_policy(required_correct=4),
        )

        result = grader.grade(FrequencyThresholdEstimator(threshold=0.5))
        public = result.to_public_dict()

        self.assertTrue(result.passed)
        self.assertEqual(result.correct_count, 4)
        self.assertEqual(result.accuracy, 1.0)
        self.assertNotIn("counts", public)
        self.assertNotIn("victim_bits", public)
        self.assertNotIn("case_order_seed", public)
        self.assertNotIn("cases", public)
        self.assertEqual(
            [event.event_kind for event in store.events()].count(
                "attacker_measurement"
            ),
            12,
        )

    def test_constant_prediction_fails_balanced_hidden_cases(self) -> None:
        _, orchestrator = _orchestrator()
        grader = ResetLeakageHiddenGrader(
            orchestrator=orchestrator,
            policy=_policy(required_correct=3),
        )

        result = grader.grade(_ConstantEstimator(0))

        self.assertEqual(result.correct_count, 2)
        self.assertFalse(result.passed)

    def test_estimator_trained_only_from_public_results_passes(self) -> None:
        _, orchestrator = _orchestrator()
        public_training = (
            orchestrator.run_training_query(
                victim_bit=0,
                shots=3,
            ).to_attacker_dict(),
            orchestrator.run_training_query(
                victim_bit=1,
                shots=3,
            ).to_attacker_dict(),
        )
        training = fit_frequency_threshold_estimator(
            LabeledBinaryObservation.from_attacker_result(result)
            for result in public_training
        )

        result = ResetLeakageHiddenGrader(
            orchestrator=orchestrator,
            policy=_policy(required_correct=4),
        ).grade(training.estimator)

        self.assertTrue(result.passed)
        self.assertEqual(
            training.to_public_dict()["provenance"],
            "attacker_visible_labelled_observations",
        )

    def test_hidden_evaluation_does_not_consume_attacker_observation_budget(self) -> None:
        _, orchestrator = _orchestrator()
        before = orchestrator.budget_ledger.read()

        ResetLeakageHiddenGrader(
            orchestrator=orchestrator,
            policy=_policy(required_correct=4),
        ).grade(FrequencyThresholdEstimator(threshold=0.5))

        self.assertEqual(orchestrator.budget_ledger.read(), before)

    def test_hidden_measurement_seed_is_independent_from_prior_attacker_queries(self) -> None:
        _, first = _orchestrator(success_probability=0.5)
        _, second = _orchestrator(success_probability=0.5)
        first.run_training_query(victim_bit=1, shots=3)
        policy = _policy(required_correct=3)

        first_result = ResetLeakageHiddenGrader(
            orchestrator=first,
            policy=policy,
        ).grade(FrequencyThresholdEstimator(threshold=0.5))
        second_result = ResetLeakageHiddenGrader(
            orchestrator=second,
            policy=policy,
        ).grade(FrequencyThresholdEstimator(threshold=0.5))

        self.assertEqual(
            first_result.to_public_dict(),
            second_result.to_public_dict(),
        )

    def test_hidden_policy_requires_both_secret_values(self) -> None:
        with self.assertRaises(ValueError):
            HiddenEvaluationPolicy(
                policy_id="test-policy",
                policy_version="1",
                case_count=4,
                zero_case_count=4,
                shots_per_case=3,
                required_correct=3,
                case_order_seed=7,
                measurement_seed=13,
            )

    def test_estimator_failure_does_not_echo_hidden_case_data(self) -> None:
        _, orchestrator = _orchestrator()
        grader = ResetLeakageHiddenGrader(
            orchestrator=orchestrator,
            policy=_policy(required_correct=3),
        )

        with self.assertRaises(EstimatorExecutionError) as raised:
            grader.grade(_FailingEstimator())

        message = str(raised.exception)
        self.assertNotIn("counts", message)
        self.assertNotIn("victim", message)
        self.assertNotIn("seed", message)


@dataclass(frozen=True)
class _ConstantEstimator:
    value: int
    estimator_id: str = "constant"
    estimator_version: str = "1"

    def predict(self, observation) -> int:
        return self.value


@dataclass(frozen=True)
class _FailingEstimator:
    estimator_id: str = "failing"
    estimator_version: str = "1"

    def predict(self, observation) -> int:
        raise RuntimeError(f"do not expose {observation.counts}")


def _policy(*, required_correct: int) -> HiddenEvaluationPolicy:
    return HiddenEvaluationPolicy(
        policy_id="test-policy",
        policy_version="1",
        case_count=4,
        zero_case_count=2,
        shots_per_case=3,
        required_correct=required_correct,
        case_order_seed=7,
        measurement_seed=13,
    )


def _orchestrator(
    *,
    success_probability: float = 0.0,
) -> tuple[QpuStateStore, ResetLeakageScenarioOrchestrator]:
    store = QpuStateStore(single_qubit_zero_snapshot(
        qpu_id="qpu-1",
        physical_qubit_id=0,
        device_config_version="device-v1",
    ))
    registry = ResetModelRegistry()
    reset_model = PhenomenologicalResetModel(
        ImperfectResetSpec(success_probability=success_probability),
    )
    registry.register(reset_model)
    runtime_config = SharedQpuRuntimeConfig(
        reset_model_id="phenomenological_imperfect_reset",
        reset_model_configuration_id=reset_model.configuration_id,
        resource_binding=ResetResourceBinding(
            PhysicalQubitRef("qpu-1", 0), PhysicalQubitRef("qpu-1", 0),
        ),
    )
    runtime = SharedQpuRuntime(
        state_store=store,
        reset_models=registry,
        config=runtime_config,
    )
    session = ScenarioSession(
        template=ScenarioTemplate(
            template_id="reset_leakage_v0",
            template_version="1",
            attacker_config={
                "workspace": "terminal",
                "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
                "reset_leakage_access": {
                    "labeled_training_enabled": True,
                    "victim_observation_enabled": True,
                },
            },
            victim_config={"workload": "binary_output", "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"]},
            qpu_service_config={
                "execution_regime": "nisq_physical",
                "supported_operations": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
                "resource_inventory": {"qpu_id": "qpu-1", "physical_qubit_ids": [0, 1]},
                "reset_model_id": "phenomenological_imperfect_reset",
                "reset_model_configuration_id": reset_model.configuration_id,
                "reset_model_mode": "state_transition_with_readout",
                "runtime_execution": runtime_config.to_manifest_config(),
                "supported_placement_policies": [
                    "guaranteed_same_physical_qubit",
                ],
                "supported_attack_mechanisms": ["incomplete_reset_state_carryover"],
            },
            relation_config={
                "resource_binding": reset_resource_binding_config("qpu-1"),
                "attack_path": {
                    "attacker_entry": "job_submission",
                    "shared_resource": "same_physical_qubit",
                    "mechanism_id": "incomplete_reset_state_carryover",
                    "victim_asset": "nonpublic_output_bit",
                    "attacker_observation": "own_measurement_counts",
                },
                "placement": {
                    "policy": "guaranteed_same_physical_qubit",
                },
            },
            experiment_config={
                "training": {
                    "label_source": "controlled_reference",
                },
            },
        ),
        policy=ScenarioPolicy(
            mode=ScenarioMode.CHALLENGE,
            allowed_operations=("program.submit", "job.status.read", "job.result.read", "job.cancel"),
            locked_fields=(
                "attacker.operation_grants",
                "victim.operation_grants",
                "qpu_service.supported_operations",
            ),
            observation_budget=ObservationBudget(
                shots_per_query=3,
                training_query_limit=2,
                victim_observation_limit=1,
                retry_limit=1,
            ),
            evaluator_id="binary_victim_output_v1",
            submission_enabled=True,
        ),
    )
    plan = ResetLeakageRuntimePlan.from_manifest(
        ScenarioCompiler().compile(session, assumptions=_permission_assumptions())
    )
    return store, ResetLeakageScenarioOrchestrator(
        runtime=runtime,
        plan=plan,
        config=ResetLeakageOrchestratorConfig(instance_id="instance-1"),
        secret_seed=11,
        measurement_seed=29,
    )


def _permission_assumptions() -> tuple[AssumptionRecord, ...]:
    return tuple(
        AssumptionRecord(
            assumption_id=f"test.fixed.{path.replace('.', '_')}",
            target_paths=(path,),
            basis=AssumptionBasis.SIMULATOR_CHOICE,
            rationale="Test scenario fixes this permission field.",
        )
        for path in (
            "attacker.operation_grants",
            "victim.operation_grants",
            "qpu_service.supported_operations",
        )
    )


if __name__ == "__main__":
    unittest.main()
