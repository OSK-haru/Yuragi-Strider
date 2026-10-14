"""Tests for attacker-visible reset-leakage estimator training."""

import unittest
from core.attack_simulation.resource_binding import (
    PhysicalQubitRef, ResetResourceBinding, reset_resource_binding_config,
)

from core.attack_simulation import (
    AssumptionBasis,
    AssumptionRecord,
    BinaryObservation,
    ObservationBudget,
    QpuStateStore,
    RESET_LEAKAGE_V0_SETTINGS,
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
    LabeledBinaryObservation,
    fit_frequency_threshold_estimator,
    single_qubit_zero_snapshot,
)
from core.experimental_isolation_physics import ResetModelRegistry


class ResetLeakageAnalysisTests(unittest.TestCase):
    def test_fits_threshold_from_public_training_results(self) -> None:
        training = fit_frequency_threshold_estimator((
            LabeledBinaryObservation.from_attacker_result({
                "phase": "training",
                "known_label": 0,
                "shots": 10,
                "counts": {"0": 9, "1": 1},
                "internal_value_that_must_not_be_read": "ignored",
            }),
            LabeledBinaryObservation.from_attacker_result({
                "phase": "training",
                "known_label": 1,
                "shots": 10,
                "counts": {"0": 2, "1": 8},
            }),
        ))

        self.assertAlmostEqual(training.estimator.threshold, 0.45)
        self.assertTrue(training.estimator.predict_one_when_at_or_above)
        self.assertEqual(
            training.estimator.predict(BinaryObservation(
                shots=10,
                counts={"0": 8, "1": 2},
            )),
            0,
        )
        self.assertEqual(
            training.estimator.predict(BinaryObservation(
                shots=10,
                counts={"0": 1, "1": 9},
            )),
            1,
        )
        self.assertEqual(
            training.to_public_dict()["provenance"],
            "attacker_visible_labelled_observations",
        )

    def test_supports_reversed_observation_direction(self) -> None:
        training = fit_frequency_threshold_estimator((
            LabeledBinaryObservation(0, 10, {"0": 2, "1": 8}),
            LabeledBinaryObservation(1, 10, {"0": 9, "1": 1}),
        ))

        self.assertFalse(training.estimator.predict_one_when_at_or_above)
        self.assertEqual(
            training.estimator.predict(BinaryObservation(
                shots=10,
                counts={"0": 8, "1": 2},
            )),
            1,
        )

    def test_requires_both_labels(self) -> None:
        with self.assertRaisesRegex(ValueError, "both labels"):
            fit_frequency_threshold_estimator((
                LabeledBinaryObservation(0, 4, {"0": 4, "1": 0}),
            ))

    def test_rejects_unlabelled_or_non_training_public_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "public training"):
            LabeledBinaryObservation.from_attacker_result({
                "phase": "victim_observation",
                "shots": 4,
                "counts": {"0": 2, "1": 2},
            })

    def test_rejects_training_without_observed_separation(self) -> None:
        with self.assertRaisesRegex(ValueError, "no class separation"):
            fit_frequency_threshold_estimator((
                LabeledBinaryObservation(0, 4, {"0": 2, "1": 2}),
                LabeledBinaryObservation(1, 4, {"0": 2, "1": 2}),
            ))

    def test_approved_v0_can_train_and_grade_from_public_results(self) -> None:
        orchestrator = _approved_v0_orchestrator()
        training = fit_frequency_threshold_estimator(
            LabeledBinaryObservation.from_attacker_result(result)
            for victim_bit in (0, 1)
            for result in (
                orchestrator.run_training_query(
                    victim_bit=victim_bit,
                    shots=RESET_LEAKAGE_V0_SETTINGS.shots_per_case,
                ).to_attacker_dict()
                for _ in range(
                    RESET_LEAKAGE_V0_SETTINGS.training_queries_per_label
                )
            )
        )

        result = ResetLeakageHiddenGrader(
            orchestrator=orchestrator,
            policy=RESET_LEAKAGE_V0_SETTINGS.hidden_evaluation_policy(
                case_order_seed=7,
                measurement_seed=13,
            ),
        ).grade(training.estimator)

        self.assertTrue(result.passed)
        self.assertGreaterEqual(
            result.correct_count,
            RESET_LEAKAGE_V0_SETTINGS.hidden_required_correct,
        )


def _approved_v0_orchestrator() -> ResetLeakageScenarioOrchestrator:
    settings = RESET_LEAKAGE_V0_SETTINGS
    reset_model = settings.build_reset_model()
    registry = ResetModelRegistry()
    registry.register(reset_model)
    store = QpuStateStore(single_qubit_zero_snapshot(
        qpu_id="qpu-reset-v0",
        physical_qubit_id=0,
        device_config_version="device-reset-v0",
    ))
    runtime_config = SharedQpuRuntimeConfig(
        reset_model_id=reset_model.model_id,
        reset_model_configuration_id=reset_model.configuration_id,
        resource_binding=ResetResourceBinding(
            PhysicalQubitRef("qpu-reset-v0", 0), PhysicalQubitRef("qpu-reset-v0", 0),
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
            template_version=settings.profile_version,
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
                "resource_inventory": {"qpu_id": "qpu-reset-v0", "physical_qubit_ids": (0, 1)},
                "reset_model_id": reset_model.model_id,
                "reset_model_configuration_id": reset_model.configuration_id,
                "reset_model_mode": "state_transition_with_readout",
                "runtime_execution": runtime_config.to_manifest_config(),
                "supported_placement_policies": (
                    "guaranteed_same_physical_qubit",
                ),
                "supported_attack_mechanisms": ("incomplete_reset_state_carryover",),
            },
            relation_config={
                "resource_binding": reset_resource_binding_config("qpu-reset-v0"),
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
                shots_per_query=settings.shots_per_case,
                training_query_limit=settings.training_query_limit,
                victim_observation_limit=1,
                retry_limit=0,
            ),
            evaluator_id="binary_victim_output_v1",
            submission_enabled=True,
        ),
    )
    plan = ResetLeakageRuntimePlan.from_manifest(
        ScenarioCompiler().compile(session, assumptions=_permission_assumptions())
    )
    return ResetLeakageScenarioOrchestrator(
        runtime=runtime,
        plan=plan,
        config=ResetLeakageOrchestratorConfig(instance_id="reset-v0-test"),
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
