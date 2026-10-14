"""Focused tests for the revisioned single-QPU FIFO vertical slice."""

import unittest
from dataclasses import replace

import numpy as np

from core.attack_simulation import (
    AttackerMeasureOperation,
    FifoJobScheduler,
    JobCommand,
    JobStatus,
    QpuStateStore,
    QpuStateTransition,
    RevisionConflictError,
    SharedQpuRuntime,
    SharedQpuRuntimeConfig,
    VictimPrepareOperation,
    single_qubit_zero_snapshot,
)
from core.experimental_isolation_physics import (
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResetModelResult,
    ResetModelRegistry,
)


class QpuStateStoreTests(unittest.TestCase):
    def test_initial_snapshot_and_transition_detach_mutable_matrices(self) -> None:
        source = [[1.0, 0.0], [0.0, 0.0]]
        snapshot = replace(_state_store().read(), density_matrix=source)
        store = QpuStateStore(snapshot)
        source[0][0] = -1.0
        self.assertEqual(store.read().density_matrix[0][0], 1.0 + 0j)
        self.assertEqual(store.read().revision, 0)

        candidate = np.array([[0.0j, 0.0j], [0.0j, 1.0 + 0j]])
        transition = QpuStateTransition("immutable-event", "program_shot", "probe",
                                        candidate, 0.0, "attacker")
        candidate[1, 1] = -1.0  # Even pre-commit changes cannot alter the command.
        store.commit(0, transition)
        self.assertEqual(store.read().density_matrix[1][1], 1.0 + 0j)
        with self.assertRaises(TypeError):
            store.read().density_matrix[1][1] = -1.0
        self.assertEqual(store.read().revision, 1)
        self.assertEqual(len(store.events()), 1)

    def test_isolated_assignment_cannot_carry_previous_qubit_state(self) -> None:
        store = _state_store()
        with self.assertRaisesRegex(ValueError, "fresh zero state"):
            store.commit(0, QpuStateTransition(
                event_id="invalid-assignment",
                event_kind="isolated_resource_assignment",
                job_id="job-1",
                next_density_matrix=((0.0j, 0.0j), (0.0j, 1.0 + 0.0j)),
                duration_us=0.0,
                next_owner_tenant_id=None,
                next_physical_qubit_ids=(1,),
            ))
        self.assertEqual(store.read().physical_qubit_ids, (0,))
        self.assertEqual(store.events(), ())

    def test_commit_is_revision_checked_and_records_one_atomic_event(self) -> None:
        store = _state_store()
        transition = QpuStateTransition(
            event_id="event-1",
            event_kind="victim_preparation",
            job_id="job-1",
            next_density_matrix=((0.0j, 0.0j), (0.0j, 1.0 + 0.0j)),
            duration_us=2.5,
            next_owner_tenant_id="victim",
        )

        committed = store.commit(0, transition)

        self.assertEqual(committed.revision, 1)
        self.assertEqual(committed.device_time_us, 2.5)
        self.assertEqual(committed.owner_tenant_id, "victim")
        self.assertEqual(store.events()[0].revision_after, 1)
        with self.assertRaises(RevisionConflictError):
            store.commit(0, QpuStateTransition(
                event_id="event-2",
                event_kind="stale",
                job_id="job-2",
                next_density_matrix=committed.density_matrix,
                duration_us=1.0,
                next_owner_tenant_id="victim",
            ))
        self.assertEqual(store.read().revision, 1)


class FifoJobSchedulerTests(unittest.TestCase):
    def test_fifo_idempotency_and_queued_cancellation(self) -> None:
        scheduler = FifoJobScheduler()
        first_command = JobCommand(
            tenant_id="victim",
            operation=VictimPrepareOperation(1),
            idempotency_key="victim-1",
        )
        second_command = JobCommand(
            tenant_id="attacker",
            operation=AttackerMeasureOperation(1),
            idempotency_key="attacker-1",
        )

        first = scheduler.submit(first_command)
        duplicate = scheduler.submit(first_command)
        second = scheduler.submit(second_command)

        self.assertEqual(first.job_id, duplicate.job_id)
        self.assertEqual(
            scheduler.queued_job_ids(),
            (first.job_id, second.job_id),
        )
        cancelled = scheduler.cancel(second.job_id)
        self.assertEqual(cancelled.status, JobStatus.CANCELLED)
        self.assertEqual(scheduler.queued_job_ids(), (first.job_id,))

    def test_idempotency_keys_are_scoped_by_tenant(self) -> None:
        scheduler = FifoJobScheduler()

        victim = scheduler.submit(JobCommand(
            tenant_id="victim",
            operation=VictimPrepareOperation(0),
            idempotency_key="same-key",
        ))
        attacker = scheduler.submit(JobCommand(
            tenant_id="attacker",
            operation=AttackerMeasureOperation(1),
            idempotency_key="same-key",
        ))

        self.assertNotEqual(victim.job_id, attacker.job_id)


class SharedQpuRuntimeTests(unittest.TestCase):
    def test_runtime_rejects_a_different_reset_parameter_configuration(self) -> None:
        store = _state_store()
        registry = ResetModelRegistry()
        model = PhenomenologicalResetModel(
            ImperfectResetSpec(success_probability=0.8),
        )
        registry.register(model)

        with self.assertRaisesRegex(ValueError, "configuration"):
            SharedQpuRuntime(
                state_store=store,
                reset_models=registry,
                config=SharedQpuRuntimeConfig(
                    reset_model_id=model.model_id,
                    reset_model_configuration_id="sha256:not-the-model",
                ),
            )

    def test_victim_reset_attacker_flow_uses_one_state_authority(self) -> None:
        store = _state_store()
        runtime = _runtime(store)
        victim = runtime.submit(JobCommand(
            tenant_id="victim",
            operation=VictimPrepareOperation(1),
            idempotency_key="victim-1",
        ))
        attacker = runtime.submit(JobCommand(
            tenant_id="attacker",
            operation=AttackerMeasureOperation(1),
            idempotency_key="attacker-1",
        ))

        completed = runtime.run_until_idle()

        self.assertEqual([job.job_id for job in completed], [victim.job_id, attacker.job_id])
        self.assertTrue(all(job.status is JobStatus.SUCCEEDED for job in completed))
        self.assertEqual(
            [event.event_kind for event in store.events()],
            ["victim_preparation", "service_reset", "attacker_measurement"],
        )
        self.assertEqual([event.revision_after for event in store.events()], [1, 2, 3])
        self.assertEqual(store.read().owner_tenant_id, "attacker")
        self.assertAlmostEqual(store.read().device_time_us, 5.5)
        self.assertEqual(
            store.read().density_matrix,
            ((0.0j, 0.0j), (0.0j, 1.0 + 0.0j)),
        )
        self.assertEqual(completed[-1].result, {
            "shots": 1,
            "counts": {"0": 0, "1": 1},
        })
        self.assertNotIn("probability", completed[-1].result)
        self.assertNotIn("density_matrix", completed[-1].result)

    def test_cancelled_job_does_not_change_qpu_state(self) -> None:
        store = _state_store()
        runtime = _runtime(store)
        queued = runtime.submit(JobCommand(
            tenant_id="victim",
            operation=VictimPrepareOperation(1),
            idempotency_key="victim-cancel",
        ))

        cancelled = runtime.cancel(queued.job_id)

        self.assertEqual(cancelled.status, JobStatus.CANCELLED)
        self.assertEqual(store.read().revision, 0)
        self.assertEqual(store.events(), ())

    def test_reset_failure_does_not_commit_a_partial_reset_event(self) -> None:
        store = _state_store()
        registry = ResetModelRegistry()
        registry.register(_FailingResetModel())
        runtime = SharedQpuRuntime(
            state_store=store,
            reset_models=registry,
            config=SharedQpuRuntimeConfig(
                reset_model_id="failing_reset",
                reset_model_configuration_id="failing-reset-config-v1",
            ),
        )
        runtime.submit(JobCommand(
            tenant_id="victim",
            operation=VictimPrepareOperation(1),
            idempotency_key="victim-1",
        ))
        runtime.submit(JobCommand(
            tenant_id="attacker",
            operation=AttackerMeasureOperation(1),
            idempotency_key="attacker-1",
        ))

        victim, attacker = runtime.run_until_idle()

        self.assertEqual(victim.status, JobStatus.SUCCEEDED)
        self.assertEqual(attacker.status, JobStatus.INTERNAL_ERROR)
        self.assertEqual(store.read().revision, 1)
        self.assertEqual(store.read().owner_tenant_id, "victim")
        self.assertEqual(
            [event.event_kind for event in store.events()],
            ["victim_preparation"],
        )

    def test_missing_readout_fails_before_any_service_state_change(self) -> None:
        for placement in (
            "guaranteed_same_physical_qubit",
            "isolated_physical_resource",
        ):
            with self.subTest(placement=placement):
                store = _state_store()
                registry = ResetModelRegistry()
                registry.register(_MissingReadoutResetModel())
                runtime = SharedQpuRuntime(
                    state_store=store,
                    reset_models=registry,
                    config=SharedQpuRuntimeConfig(
                        reset_model_id="missing_readout",
                        reset_model_configuration_id="missing-readout-config-v1",
                        placement_policy=placement,
                    ),
                )
                runtime.submit(JobCommand(
                    tenant_id="victim",
                    operation=VictimPrepareOperation(1),
                    idempotency_key="victim-1",
                ))
                runtime.submit(JobCommand(
                    tenant_id="attacker",
                    operation=AttackerMeasureOperation(1),
                    idempotency_key="attacker-1",
                ))

                victim, attacker = runtime.run_until_idle()

                self.assertEqual(victim.status, JobStatus.SUCCEEDED)
                self.assertEqual(attacker.status, JobStatus.INTERNAL_ERROR)
                self.assertEqual(store.read().owner_tenant_id, "victim")
                self.assertEqual(store.read().revision, 1)
                self.assertEqual(
                    [event.event_kind for event in store.events()],
                    ["victim_preparation"],
                )
                retry = runtime.submit(JobCommand(
                    tenant_id="attacker",
                    operation=AttackerMeasureOperation(2),
                    idempotency_key="attacker-2",
                ))
                self.assertEqual(runtime.run_next().job_id, retry.job_id)
                self.assertEqual(store.read().revision, 1)

    def test_invalid_reset_state_is_rejected_before_isolated_assignment(self) -> None:
        store = _state_store()
        registry = ResetModelRegistry()
        registry.register(_InvalidPostStateResetModel())
        runtime = SharedQpuRuntime(
            state_store=store,
            reset_models=registry,
            config=SharedQpuRuntimeConfig(
                reset_model_id="invalid_post_state",
                reset_model_configuration_id="invalid-post-state-config-v1",
                placement_policy="isolated_physical_resource",
            ),
        )
        runtime.submit(JobCommand(
            tenant_id="victim",
            operation=VictimPrepareOperation(1),
            idempotency_key="victim-1",
        ))
        runtime.submit(JobCommand(
            tenant_id="attacker",
            operation=AttackerMeasureOperation(1),
            idempotency_key="attacker-1",
        ))

        _, attacker = runtime.run_until_idle()

        self.assertEqual(attacker.status, JobStatus.INTERNAL_ERROR)
        self.assertEqual(store.read().owner_tenant_id, "victim")
        self.assertEqual(store.read().revision, 1)

    def test_readout_misclassification_does_not_change_true_collapsed_state(self) -> None:
        store = _state_store()
        registry = ResetModelRegistry()
        reset_model = PhenomenologicalResetModel(
            ImperfectResetSpec(success_probability=0.0),
            observed_zero_given_one=1.0,
        )
        registry.register(reset_model)
        runtime = SharedQpuRuntime(
            state_store=store,
            reset_models=registry,
            config=SharedQpuRuntimeConfig(
                reset_model_id="phenomenological_imperfect_reset",
                reset_model_configuration_id=reset_model.configuration_id,
            ),
        )
        runtime.submit(JobCommand(
            tenant_id="victim",
            operation=VictimPrepareOperation(1),
            idempotency_key="victim-readout",
        ))
        runtime.submit(JobCommand(
            tenant_id="attacker",
            operation=AttackerMeasureOperation(1),
            idempotency_key="attacker-readout",
        ))

        _, attacker = runtime.run_until_idle()

        self.assertEqual(attacker.result["counts"], {"0": 1, "1": 0})
        self.assertEqual(
            store.read().density_matrix,
            ((0.0j, 0.0j), (0.0j, 1.0 + 0.0j)),
        )


class _FailingResetModel:
    model_id = "failing_reset"
    model_version = "1"
    fidelity_level = "test_double"
    provenance = "test"
    model_mode = "state_transition_with_readout"
    configuration_id = "failing-reset-config-v1"

    def run(self, request):
        raise RuntimeError("simulated internal failure")


class _MissingReadoutResetModel:
    model_id = "missing_readout"
    model_version = "1"
    fidelity_level = "test_double"
    provenance = "test"
    model_mode = "state_transition_with_readout"
    configuration_id = "missing-readout-config-v1"

    def run(self, request):
        return ResetModelResult(
            post_reset_state=request.input_state,
            attacker_one_probability=1.0,
        )


class _InvalidPostStateResetModel(_MissingReadoutResetModel):
    model_id = "invalid_post_state"
    configuration_id = "invalid-post-state-config-v1"

    def run(self, request):
        return ResetModelResult(
            post_reset_state=((2.0 + 0.0j, 0.0j), (0.0j, 0.0j)),
            attacker_one_probability=1.0,
            observed_one_given_true_zero=0.0,
            observed_zero_given_true_one=0.0,
        )


def _state_store() -> QpuStateStore:
    return QpuStateStore(single_qubit_zero_snapshot(
        qpu_id="qpu-1",
        physical_qubit_id=0,
        device_config_version="device-v1",
    ))


def _runtime(store: QpuStateStore) -> SharedQpuRuntime:
    registry = ResetModelRegistry()
    reset_model = PhenomenologicalResetModel(
        ImperfectResetSpec(
            success_probability=0.8,
            target_populations=(1.0, 0.0),
        ),
    )
    registry.register(reset_model)
    return SharedQpuRuntime(
        state_store=store,
        reset_models=registry,
        config=SharedQpuRuntimeConfig(
            reset_model_id="phenomenological_imperfect_reset",
            reset_model_configuration_id=reset_model.configuration_id,
            reset_duration_us=2.0,
            victim_operation_duration_us=3.0,
            attacker_measurement_duration_us=0.5,
        ),
    )


if __name__ == "__main__":
    unittest.main()
