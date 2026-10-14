"""Focused tests for full-sequence reset-leakage scenario orchestration."""

import unittest
from core.attack_simulation.resource_binding import (
    PhysicalQubitRef, ResetResourceBinding, reset_resource_binding_config,
)

from core.attack_simulation import (
    AssumptionBasis,
    AssumptionRecord,
    AttackerMeasureOperation,
    JobCommand,
    ObservationBudget,
    ObservationBudgetExceeded,
    ObservationBudgetLedger,
    ObservationQueryKind,
    PlacementPolicy,
    QpuStateStore,
    ResetLeakageExecutionError,
    ResetLeakageOrchestratorConfig,
    ResetLeakageRuntimePlan,
    ResetLeakageScenarioOrchestrator,
    ResetLeakageSessionState,
    ScenarioAccessDenied,
    ScenarioCompiler,
    ScenarioMode,
    ScenarioPolicy,
    ScenarioSession,
    ScenarioSessionClosed,
    ScenarioTemplate,
    SharedQpuRuntime,
    SharedQpuRuntimeConfig,
    VictimPrepareOperation,
    single_qubit_zero_snapshot,
)
from core.experimental_isolation_physics import (
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResetModelRegistry,
)


class ObservationBudgetLedgerTests(unittest.TestCase):
    def test_paired_query_ledger_does_not_treat_absent_budget_as_unlimited(self) -> None:
        with self.assertRaisesRegex(TypeError, "requires an explicit observation budget"):
            ObservationBudgetLedger(None)

    def test_retry_requires_original_query_and_has_an_independent_limit(self) -> None:
        ledger = ObservationBudgetLedger(_budget())

        with self.assertRaises(ObservationBudgetExceeded):
            ledger.reserve(
                kind=ObservationQueryKind.TRAINING,
                shots=1,
                retry=True,
            )

        ledger.reserve(kind=ObservationQueryKind.TRAINING, shots=3)
        usage = ledger.reserve(
            kind=ObservationQueryKind.TRAINING,
            shots=3,
            retry=True,
        )

        self.assertEqual(usage.training_queries, 1)
        self.assertEqual(usage.retries, 1)
        self.assertEqual(usage.consumed_shots, 6)
        with self.assertRaises(ObservationBudgetExceeded):
            ledger.reserve(
                kind=ObservationQueryKind.TRAINING,
                shots=3,
                retry=True,
            )


class ResetLeakageScenarioOrchestratorTests(unittest.TestCase):
    def test_manual_virtual_credential_is_required_before_any_query_effect(self) -> None:
        store, orchestrator = _orchestrator(
            mode=ScenarioMode.FREE,
            credential_delivery="manual_simulated_credential",
        )
        with self.assertRaises(PermissionError):
            orchestrator.run_training_query(victim_bit=1, shots=1)
        self.assertEqual(store.events(), ())
        self.assertEqual(orchestrator.budget_ledger.read().consumed_shots, 0)
        credential = orchestrator.take_manual_attacker_credential()
        with self.assertRaises(RuntimeError):
            orchestrator.take_manual_attacker_credential()
        with self.assertRaises(PermissionError):
            orchestrator.attacker_connection.configure("user-chosen-string")
        orchestrator.attacker_connection.configure(credential)
        observation = orchestrator.run_training_query(victim_bit=1, shots=1)
        self.assertEqual(sum(observation.counts.values()), 1)
        orchestrator.close()
        with self.assertRaises(PermissionError):
            orchestrator.attacker_connection.require_authenticated()

    def test_preconfigured_profile_works_without_revealing_attacker_credential(self) -> None:
        _, orchestrator = _orchestrator()
        with self.assertRaises(RuntimeError):
            orchestrator.take_manual_attacker_credential()
        self.assertEqual(
            sum(orchestrator.run_training_query(victim_bit=0, shots=1).counts.values()),
            1,
        )

    def test_each_shot_runs_the_complete_physical_sequence(self) -> None:
        store, orchestrator = _orchestrator()

        result = orchestrator.run_training_query(victim_bit=1, shots=3)

        self.assertEqual(result.counts, {"0": 0, "1": 3})
        self.assertEqual(result.known_label, 1)
        event_kinds = [event.event_kind for event in store.events()]
        self.assertEqual(event_kinds.count("victim_preparation"), 3)
        self.assertEqual(event_kinds.count("attacker_measurement"), 3)
        attacker_indexes = [
            index
            for index, kind in enumerate(event_kinds)
            if kind == "attacker_measurement"
        ]
        for index in attacker_indexes:
            self.assertEqual(
                event_kinds[index - 2:index + 1],
                [
                    "victim_preparation",
                    "service_reset",
                    "attacker_measurement",
                ],
            )
        self.assertTrue(all(
            event.scenario_manifest_id == orchestrator.plan.manifest_id
            for event in store.events()
        ))
        self.assertEqual(store.read().qpu_id, "qpu-1")
        self.assertEqual(store.read().physical_qubit_ids, (0,))
        self.assertTrue(all(
            event.physical_qubit_ids_before == (0,)
            and event.physical_qubit_ids_after == (0,)
            for event in store.events()
        ))

    def test_runtime_rejects_state_from_a_different_qpu_before_jobs(self) -> None:
        _, orchestrator = _orchestrator()
        wrong_store = QpuStateStore(single_qubit_zero_snapshot(
            qpu_id="qpu-2", physical_qubit_id=0, device_config_version="device-v1",
        ))
        with self.assertRaisesRegex(ValueError, "does not match the victim resource binding"):
            SharedQpuRuntime(
                state_store=wrong_store,
                reset_models=orchestrator.runtime.reset_models,
                config=orchestrator.runtime.config,
            )
        self.assertEqual(wrong_store.read().revision, 0)
        self.assertEqual(wrong_store.events(), ())

    def test_bound_runtime_rejects_wrong_job_destinations_before_queueing(self) -> None:
        store, orchestrator = _orchestrator()
        runtime = orchestrator.runtime
        for target in (
            None,
            PhysicalQubitRef("qpu-2", 0),
            PhysicalQubitRef("qpu-1", 1),
        ):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "job target"):
                runtime.submit(JobCommand(
                    tenant_id="attacker",
                    operation=AttackerMeasureOperation(1),
                    idempotency_key=f"wrong-{target}",
                    target=target,
                ))
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)
        self.assertEqual(store.events(), ())

    def test_hidden_victim_bit_is_not_in_attacker_result(self) -> None:
        _, orchestrator = _orchestrator()

        result = orchestrator.run_victim_observation(shots=2)
        attacker_view = result.to_attacker_dict()

        self.assertEqual(sum(attacker_view["counts"].values()), 2)
        self.assertNotIn("known_label", attacker_view)
        self.assertNotIn("victim_bit", attacker_view)
        self.assertNotIn("seed", attacker_view)

    def test_budget_is_rejected_before_any_job_or_event_is_created(self) -> None:
        store, orchestrator = _orchestrator()

        with self.assertRaises(ObservationBudgetExceeded):
            orchestrator.run_training_query(victim_bit=0, shots=4)

        self.assertEqual(store.events(), ())
        self.assertEqual(orchestrator.runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(
            orchestrator.budget_ledger.read().consumed_shots,
            0,
        )

    def test_logical_query_and_retry_limits_are_enforced(self) -> None:
        _, orchestrator = _orchestrator()
        first = orchestrator.run_training_query(victim_bit=0, shots=1)

        with self.assertRaises(ObservationBudgetExceeded):
            orchestrator.run_training_query(victim_bit=1, shots=1)

        retry = orchestrator.retry_query(first.query_id)
        self.assertEqual(retry.attempt, 2)
        with self.assertRaises(ObservationBudgetExceeded):
            orchestrator.retry_query(first.query_id)

    def test_same_seeds_and_actions_reproduce_attacker_counts(self) -> None:
        _, first = _orchestrator(secret_seed=41, measurement_seed=73)
        _, second = _orchestrator(secret_seed=41, measurement_seed=73)

        first_result = first.run_victim_observation(shots=3)
        second_result = second.run_victim_observation(shots=3)

        self.assertEqual(
            first_result.to_attacker_dict(),
            second_result.to_attacker_dict(),
        )

    def test_orchestrator_refuses_a_runtime_with_preexisting_work(self) -> None:
        _, orchestrator = _orchestrator()
        orchestrator.runtime.submit(JobCommand(
            tenant_id="attacker",
            operation=AttackerMeasureOperation(0),
            idempotency_key="preexisting",
            target=orchestrator.plan.resource_binding.attacker,
        ))

        with self.assertRaises(ResetLeakageExecutionError) as raised:
            orchestrator.run_training_query(victim_bit=0, shots=1)

        self.assertEqual(raised.exception.error_code, "RUNTIME_NOT_EXCLUSIVE")

    def test_free_mode_can_disable_assumed_attacker_processes(self) -> None:
        store, orchestrator = _orchestrator(
            mode=ScenarioMode.FREE,
            labeled_training_enabled=False,
            victim_observation_enabled=False,
        )

        with self.assertRaises(ScenarioAccessDenied):
            orchestrator.run_training_query(victim_bit=0, shots=1)
        with self.assertRaises(ScenarioAccessDenied):
            orchestrator.run_victim_observation(shots=1)

        self.assertEqual(store.events(), ())
        self.assertEqual(
            orchestrator.budget_ledger.read().consumed_shots,
            0,
        )

    def test_isolated_resource_control_removes_victim_state_dependence(self) -> None:
        zero_store, zero = _orchestrator(
            placement_policy=PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE,
        )
        one_store, one = _orchestrator(
            placement_policy=PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE,
        )

        zero_result = zero.run_training_query(victim_bit=0, shots=3)
        one_result = one.run_training_query(victim_bit=1, shots=3)

        self.assertEqual(zero_result.counts, one_result.counts)
        self.assertEqual(one_result.counts, {"0": 3, "1": 0})
        self.assertIn(
            "isolated_resource_assignment",
            [event.event_kind for event in zero_store.events()],
        )
        self.assertIn(
            "isolated_resource_assignment",
            [event.event_kind for event in one_store.events()],
        )
        self.assertEqual(zero_store.read().physical_qubit_ids, (1,))
        self.assertEqual(one_store.read().physical_qubit_ids, (1,))
        assignments = [event for event in one_store.events()
                       if event.event_kind == "isolated_resource_assignment"]
        self.assertTrue(any(
            event.physical_qubit_ids_before == (0,)
            and event.physical_qubit_ids_after == (1,)
            for event in assignments
        ))
        self.assertTrue(any(
            event.physical_qubit_ids_before == (1,)
            and event.physical_qubit_ids_after == (0,)
            for event in assignments
        ))

    def test_close_preserves_state_and_budget_but_rejects_new_operations(self) -> None:
        store, orchestrator = _orchestrator()
        orchestrator.run_training_query(victim_bit=1, shots=1)
        snapshot_before = store.read()
        usage_before = orchestrator.budget_ledger.read()

        orchestrator.close()

        self.assertIs(
            orchestrator.session_state,
            ResetLeakageSessionState.CLOSED,
        )
        self.assertEqual(store.read(), snapshot_before)
        self.assertEqual(orchestrator.budget_ledger.read(), usage_before)
        with self.assertRaises(ScenarioSessionClosed):
            orchestrator.run_victim_observation(shots=1)


def _budget() -> ObservationBudget:
    return ObservationBudget(
        shots_per_query=3,
        training_query_limit=1,
        victim_observation_limit=1,
        retry_limit=1,
    )


def _orchestrator(
    *,
    secret_seed: int = 11,
    measurement_seed: int = 29,
    mode: ScenarioMode = ScenarioMode.CHALLENGE,
    labeled_training_enabled: bool = True,
    victim_observation_enabled: bool = True,
    credential_delivery: str = "preconfigured_profile",
    placement_policy: PlacementPolicy = (
        PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT
    ),
) -> tuple[QpuStateStore, ResetLeakageScenarioOrchestrator]:
    store = QpuStateStore(single_qubit_zero_snapshot(
        qpu_id="qpu-1",
        physical_qubit_id=0,
        device_config_version="device-v1",
    ))
    registry = ResetModelRegistry()
    reset_model = PhenomenologicalResetModel(
        ImperfectResetSpec(success_probability=0.0),
    )
    registry.register(reset_model)
    runtime_config = SharedQpuRuntimeConfig(
        reset_model_id="phenomenological_imperfect_reset",
        reset_model_configuration_id=reset_model.configuration_id,
        placement_policy=placement_policy,
        resource_binding=ResetResourceBinding(
            PhysicalQubitRef("qpu-1", 0),
            PhysicalQubitRef(
                "qpu-1", 1 if placement_policy is PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE else 0,
            ),
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
                "credential_delivery": credential_delivery,
                "credential_origin": "simulator_issued",
                "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
                "reset_leakage_access": {
                    "labeled_training_enabled": labeled_training_enabled,
                    "victim_observation_enabled": victim_observation_enabled,
                },
            },
            victim_config={"workload": "binary_output", "operation_grants": ["program.submit", "job.status.read", "job.result.read", "job.cancel"]},
            qpu_service_config={
                "execution_regime": "nisq_physical",
                "supported_credential_deliveries": [
                    "preconfigured_profile", "manual_simulated_credential",
                ],
                "supported_operations": ["program.submit", "job.status.read", "job.result.read", "job.cancel"],
                "resource_inventory": {"qpu_id": "qpu-1", "physical_qubit_ids": [0, 1]},
                "reset_model_id": "phenomenological_imperfect_reset",
                "reset_model_configuration_id": reset_model.configuration_id,
                "reset_model_mode": "state_transition_with_readout",
                "runtime_execution": runtime_config.to_manifest_config(),
                "supported_placement_policies": [
                    "guaranteed_same_physical_qubit",
                    "isolated_physical_resource",
                ],
                "supported_attack_mechanisms": ["incomplete_reset_state_carryover"],
            },
            relation_config={
                "resource_binding": reset_resource_binding_config(
                    "qpu-1", isolated=placement_policy is PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE,
                ),
                "attack_path": {
                    "attacker_entry": "job_submission",
                    "shared_resource": "same_physical_qubit",
                    "mechanism_id": "incomplete_reset_state_carryover",
                    "victim_asset": "nonpublic_output_bit",
                    "attacker_observation": "own_measurement_counts",
                },
                "placement": {
                    "policy": placement_policy.value,
                },
            },
            experiment_config={
                "training": {
                    "label_source": "controlled_reference",
                },
            },
        ),
        policy=(
            ScenarioPolicy(
                mode=ScenarioMode.CHALLENGE,
                observation_budget=_budget(),
                allowed_operations=("program.submit", "job.status.read", "job.result.read", "job.cancel"),
                locked_fields=(
                    "attacker.reset_leakage_access",
                    "attacker.operation_grants",
                    "attacker.credential_delivery",
                    "attacker.credential_origin",
                    "victim.operation_grants",
                    "qpu_service.supported_operations",
                ),
                evaluator_id="binary_victim_output_v1",
                submission_enabled=True,
            )
            if mode is ScenarioMode.CHALLENGE
            else ScenarioPolicy(
                mode=ScenarioMode.FREE,
                observation_budget=_budget(),
                allowed_operations=("program.submit", "job.status.read", "job.result.read", "job.cancel"),
            )
        ),
    )
    plan = ResetLeakageRuntimePlan.from_manifest(
        ScenarioCompiler().compile(session, assumptions=_permission_assumptions())
    )
    orchestrator = ResetLeakageScenarioOrchestrator(
        runtime=runtime,
        plan=plan,
        config=ResetLeakageOrchestratorConfig(instance_id="instance-1"),
        secret_seed=secret_seed,
        measurement_seed=measurement_seed,
    )
    return store, orchestrator


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
