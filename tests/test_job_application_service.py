"""Authorization and projection checks for the in-process job boundary."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.job_service import (
    JobApplicationService,
    JobNotFoundError,
    JobResultNotReadyError,
    PrincipalGrant,
)
from core.attack_simulation.jobs import AttackerMeasureOperation, JobStatus, VictimPrepareOperation
from core.attack_simulation.qpu_state import QpuStateStore, single_qubit_zero_snapshot
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.attack_simulation.virtual_credentials import (
    CredentialedJobConnection,
    VirtualCredentialAuthority,
)
from core.experimental_isolation_physics import (
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResetModelRegistry,
)


OPERATIONS = frozenset({
    "program.submit", "job.status.read", "job.result.read", "job.cancel",
})


def _service(
    *,
    creator_only_operations=frozenset(),
    service_operations=OPERATIONS,
    actor_operations=OPERATIONS,
    scenario_operations=OPERATIONS,
):
    store = QpuStateStore(single_qubit_zero_snapshot(
        qpu_id="qpu-1", physical_qubit_id=0, device_config_version="device-v1",
    ))
    registry = ResetModelRegistry()
    model = PhenomenologicalResetModel(ImperfectResetSpec(success_probability=0.8))
    registry.register(model)
    runtime = SharedQpuRuntime(
        state_store=store,
        reset_models=registry,
        config=SharedQpuRuntimeConfig(
            reset_model_id=model.model_id,
            reset_model_configuration_id=model.configuration_id,
        ),
    )
    runtime.configure_job_authorization(JobAuthorizationPolicy(
        service_operations=service_operations,
        scenario_operations=scenario_operations,
        actor_grants={"attacker": actor_operations, "victim": OPERATIONS},
        tenant_roles={"attacker-tenant": "attacker", "victim-tenant": "victim"},
    ))
    service = JobApplicationService(
        runtime=runtime,
        principals=(
            PrincipalGrant("creator", "attacker-tenant", OPERATIONS),
            PrincipalGrant("coauthor", "attacker-tenant", frozenset({"program.submit"})),
            PrincipalGrant("reader", "attacker-tenant", frozenset({
                "job.status.read", "job.result.read",
            })),
            PrincipalGrant("operator", "attacker-tenant", frozenset({"job.cancel"})),
            PrincipalGrant("victim", "victim-tenant", OPERATIONS),
        ),
        creator_only_operations=creator_only_operations,
    )
    return store, runtime, service


class JobApplicationServiceTests(unittest.TestCase):
    def test_virtual_credentials_resolve_principal_before_job_authorization(self):
        store, runtime, service = _service()
        authority = VirtualCredentialAuthority()
        creator_token = authority.issue("creator")
        reader_token = authority.issue("reader")
        victim_token = authority.issue("victim")
        creator = CredentialedJobConnection(
            authority=authority, service=service, principal_id="creator",
        )
        with self.assertRaises(PermissionError):
            creator.submit(operation=AttackerMeasureOperation(1), idempotency_key="x")
        with self.assertRaises(PermissionError):
            creator.configure(victim_token)
        with self.assertRaises(PermissionError):
            creator.configure("arbitrary-user-value")
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)
        creator.configure(creator_token)
        job = creator.submit(operation=AttackerMeasureOperation(1), idempotency_key="x")
        reader = CredentialedJobConnection(
            authority=authority, service=service,
            principal_id="reader", preconfigured_credential=reader_token,
        )
        with self.assertRaises(PermissionError):
            reader.submit(operation=AttackerMeasureOperation(1), idempotency_key="y")
        runtime.run_next()
        self.assertEqual(reader.get_result(job.job_id).job_id, job.job_id)
        self.assertFalse(hasattr(reader.get_result(job.job_id), "credential"))
        other_world = VirtualCredentialAuthority()
        with self.assertRaises(PermissionError):
            other_world.resolve(creator_token)
        authority.close()
        with self.assertRaises(PermissionError):
            creator.get_result(job.job_id)

    def test_same_tenant_reader_can_get_status_and_result_without_internal_command(self):
        _, runtime, service = _service()
        submitted = service.submit(
            "creator", operation=AttackerMeasureOperation(1), idempotency_key="sample",
        )
        with self.assertRaises(JobResultNotReadyError):
            service.get_result("reader", submitted.job_id)
        runtime.run_next()

        status = service.get_status("reader", submitted.job_id)
        result = service.get_result("reader", submitted.job_id)
        self.assertEqual(status.status, JobStatus.SUCCEEDED)
        self.assertEqual(sum(result.result["counts"].values()), 1)
        self.assertFalse(hasattr(status, "command"))
        self.assertFalse(hasattr(result, "command"))
        result.result["counts"]["1"] = 99
        self.assertNotEqual(service.get_result("reader", submitted.job_id).result["counts"]["1"], 99)

    def test_result_projection_drops_unexpected_internal_fields(self):
        _, runtime, service = _service()
        submitted = service.submit(
            "creator", operation=AttackerMeasureOperation(1), idempotency_key="sample",
        )
        runtime.run_next()
        internal = runtime.scheduler.get(submitted.job_id)
        unexpected = replace(internal, result={
            **internal.result, "victim_bit": 1, "density_matrix": "private",
        })
        with patch.object(runtime.scheduler, "get", return_value=unexpected):
            visible = service.get_result("reader", submitted.job_id)
        self.assertEqual(set(visible.result), {"shots", "counts"})

    def test_foreign_and_missing_job_are_indistinguishable_without_state_change(self):
        store, runtime, service = _service()
        submitted = service.submit(
            "creator", operation=AttackerMeasureOperation(1), idempotency_key="sample",
        )
        before_queue = runtime.scheduler.queued_job_ids()
        for operation in (service.get_status, service.get_result, service.cancel):
            for job_id in (submitted.job_id, "job-missing"):
                with self.assertRaisesRegex(JobNotFoundError, "job not found"):
                    operation("victim", job_id)
        self.assertEqual(runtime.scheduler.queued_job_ids(), before_queue)
        self.assertEqual(store.read().revision, 0)

    def test_grants_control_operations_and_shared_cancel_is_queued_only(self):
        store, runtime, service = _service()
        with self.assertRaises(PermissionError):
            service.submit("reader", operation=AttackerMeasureOperation(1), idempotency_key="x")
        submitted = service.submit(
            "creator", operation=AttackerMeasureOperation(1), idempotency_key="sample",
        )
        with self.assertRaises(PermissionError):
            service.cancel("reader", submitted.job_id)
        with self.assertRaises(PermissionError):
            service.get_status("operator", submitted.job_id)
        self.assertEqual(runtime.scheduler.queued_job_ids(), (submitted.job_id,))
        self.assertEqual(service.cancel("operator", submitted.job_id).status, JobStatus.CANCELLED)
        self.assertEqual(store.read().revision, 0)
        with self.assertRaises(ValueError):
            service.cancel("operator", submitted.job_id)

    def test_creator_only_profile_can_restrict_a_same_tenant_reader(self):
        _, _, service = _service(creator_only_operations=frozenset({"job.result.read"}))
        submitted = service.submit(
            "creator", operation=AttackerMeasureOperation(1), idempotency_key="sample",
        )
        with self.assertRaises(JobNotFoundError):
            service.get_result("reader", submitted.job_id)

    def test_each_policy_layer_can_deny_cancel_without_changing_the_queue(self):
        for layer in ("service_operations", "actor_operations", "scenario_operations"):
            with self.subTest(layer=layer):
                store, runtime, service = _service(**{
                    layer: OPERATIONS - {"job.cancel"},
                })
                submitted = service.submit(
                    "creator", operation=AttackerMeasureOperation(1),
                    idempotency_key="sample",
                )
                with self.assertRaises(PermissionError):
                    service.cancel("creator", submitted.job_id)
                self.assertEqual(runtime.scheduler.queued_job_ids(), (submitted.job_id,))
                self.assertEqual(store.read().revision, 0)

    def test_idempotency_is_scoped_to_principal_within_tenant(self):
        _, runtime, service = _service()
        first = service.submit(
            "creator", operation=AttackerMeasureOperation(1), idempotency_key="same",
        )
        repeat = service.submit(
            "creator", operation=AttackerMeasureOperation(1), idempotency_key="same",
        )
        self.assertEqual(first.job_id, repeat.job_id)
        other = service.submit(
            "coauthor", operation=AttackerMeasureOperation(1), idempotency_key="same",
        )
        self.assertNotEqual(first.job_id, other.job_id)
        self.assertEqual(len(runtime.scheduler.queued_job_ids()), 2)

    def test_scenario_workload_is_limited_to_the_victim_principal(self):
        _, runtime, service = _service()
        with self.assertRaises(PermissionError):
            service._submit_scenario_workload(
                "creator", operation=VictimPrepareOperation(1), idempotency_key="x",
            )
        submitted = service._submit_scenario_workload(
            "victim", operation=VictimPrepareOperation(1), idempotency_key="victim-x",
        )
        self.assertEqual(submitted.status, JobStatus.QUEUED)
        self.assertEqual(runtime.run_next().status, JobStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
