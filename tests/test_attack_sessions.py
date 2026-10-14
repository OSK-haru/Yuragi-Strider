"""Real process-local session binding; no new dependency or network server."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from api.attack_sessions import AttackSession, AttackSessionRegistry, SessionBusyError, SessionCapacityError, SessionHostLimits
from api.attack_terminal import TerminalSession
from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.job_service import JobApplicationService, PrincipalGrant
from core.attack_simulation.program_request import STATE_INHERIT_OPERATION_ID
from core.attack_simulation.qpu_state import QpuStateStore, local_zero_snapshot
from core.attack_simulation.request_receipts import ReceiptLimits, ReceiptOutcome, RequestReceiptLedger
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPolicy, ScenarioSession, ScenarioTemplate
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.attack_simulation.virtual_credentials import VirtualCredentialAuthority
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRegistry


OPERATIONS = ("device.info.read", "program.submit", "job.status.read", "job.result.read", "job.cancel")


def circuit_json():
    return json.dumps({"logical_qubits": 2, "classical_bits": 2, "initial_states": [0, 0], "columns": [
        {"step": 0, "gates": [{"type": "H", "targets": [0]}]},
        {"step": 1, "gates": [{"type": "CNOT", "targets": [1], "controls": [0]}]},
        {"step": 2, "gates": [
            {"type": "MEASURE", "targets": [0], "classical_targets": [0]},
            {"type": "MEASURE", "targets": [1], "classical_targets": [1]},
        ]},
    ]})


def session_fixture(*, max_jobs=4, max_workspaces=2, max_pending_receipts=32,
                    max_receipt_history=64, attacker_operations=OPERATIONS,
                    initializations=("prepare",), attacker_workload=None, victim_workload=None,
                    world_clock=None, service_timing=None, scenario_mode=ScenarioMode.FREE,
                    topology_binding=None, live_ingress=None, control_timing=None,
                    live_time_policy=None):
    operations = OPERATIONS + ((STATE_INHERIT_OPERATION_ID,) if "inherit" in initializations else ())
    model = PhenomenologicalResetModel(ImperfectResetSpec(success_probability=1.0))
    runtime_config = SharedQpuRuntimeConfig(
        reset_model_id=model.model_id,
        reset_model_configuration_id=model.configuration_id,
    )
    scenario = ScenarioSession(
        template=ScenarioTemplate("http_fixture", "1",
            attacker_config={"operation_grants": list(attacker_operations),
                             **({"workload": attacker_workload} if attacker_workload is not None else {})},
            victim_config={"operation_grants": list(operations),
                           **({"workload": victim_workload} if victim_workload is not None else {})},
            qpu_service_config={"execution_regime": "nisq_physical", "supported_operations": list(operations),
                "runtime_execution": runtime_config.to_manifest_config(),
                **({"declared_service_timing": service_timing} if service_timing is not None else {}),
                **({"live_ingress": live_ingress} if live_ingress is not None else {}),
                **({"control_timing": control_timing} if control_timing is not None else {}),
                **({"topology": topology_binding.topology.to_config()} if topology_binding is not None else {}),
                "circuit_execution": {"compiler_id": "local_sampling_v1", "max_program_qubits": 8,
                    "supported_gates": ["H", "CNOT", "MEASURE"], "max_shots": 32,
                    "max_instruction_executions": 256,
                    **({"supported_initializations": list(initializations)}
                       if initializations != ("prepare",) else {})}},
            experiment_config={**({"world_clock": world_clock} if world_clock is not None else {}),
                               **({"live_time_policy": live_time_policy} if live_time_policy is not None else {})},
        ),
        policy=ScenarioPolicy(scenario_mode, allowed_operations=operations,
            locked_fields=("attacker.operation_grants", "victim.operation_grants",
                           "qpu_service.supported_operations", "qpu_service.circuit_execution")
            if scenario_mode is ScenarioMode.CHALLENGE else ()),
    )
    environment = ScenarioEnvironment(scenario)
    manifest = environment.establish()
    models = ResetModelRegistry()
    models.register(model)
    store = QpuStateStore(local_zero_snapshot(qpu_id="qpu-1",
        physical_qubit_ids=(0, 1) if topology_binding is None else topology_binding.physical_qubit_ids,
        device_config_version="http-test"),
        **({"region_binding": topology_binding} if topology_binding is not None else {}))
    runtime = SharedQpuRuntime(state_store=store, reset_models=models,
        config=runtime_config)
    runtime.configure_job_authorization(JobAuthorizationPolicy.from_manifest(manifest, attacker_tenant_id="ta", victim_tenant_id="tv"))
    service = JobApplicationService(runtime=runtime, program_compiler=LocalCircuitCompiler.from_manifest(manifest),
        scenario_manifest_id=manifest.manifest_id,
        principals=[PrincipalGrant("a", "ta", frozenset(operations)), PrincipalGrant("v", "tv", frozenset(operations))])
    authority = VirtualCredentialAuthority()
    credentials = {principal: authority.issue(principal) for principal in ("a", "v")}
    session = AttackSession(environment=environment, service=service, authority=authority,
        run_next=runtime.run_next, limits=SessionHostLimits(
            max_jobs, max_workspaces, ReceiptLimits(max_pending_receipts, max_receipt_history)))
    registry = AttackSessionRegistry(max_sessions=1)
    registry.register("world", session)
    return environment, store, runtime, authority, credentials, session, registry


class AttackSessionTests(unittest.TestCase):
    def test_receipt_initialization_failure_does_not_claim_service_or_authority(self):
        with patch("api.attack_sessions.RequestReceiptLedger", side_effect=TypeError("invalid receipt limits")), \
                patch.object(JobApplicationService, "claim_session") as claim, \
                patch.object(VirtualCredentialAuthority, "bind_session") as bind:
            with self.assertRaises(TypeError):
                session_fixture()
            claim.assert_not_called()
            bind.assert_not_called()

    def test_receipt_is_host_ingress_not_job_admission_or_device_time(self):
        with patch("api.attack_sessions.RequestReceiptLedger", side_effect=lambda limits:
                   RequestReceiptLedger(limits, clock=lambda: 42)):
            _, store, runtime, _, credentials, session, _ = session_fixture()
        first = session.receive(credentials["a"], "program.submit")
        second = session.receive(credentials["v"], "program.submit")
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual((first.received_at_ns, second.received_at_ns), (42, 42))
        self.assertEqual(first.clock_domain, "host_monotonic_ns")
        self.assertTrue(all(record.outcome is None for record in session.receipt_records()))
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().device_time_us, 0)
        session.abandon_receipt(first)
        session.abandon_receipt(second)

    def test_receipt_owner_capacity_failure_and_reuse_are_separate_from_jobs(self):
        _, store, runtime, _, credentials, session, _ = session_fixture(max_pending_receipts=1)
        first = session.receive(credentials["a"], "device.list")
        with self.assertRaises(SessionCapacityError):
            session.request(credentials["v"], lambda c, w: c.list_devices())
        with self.assertRaises((PermissionError, ValueError)):
            session.request(credentials["v"], lambda c, w: c.list_devices(), receipt=first)
        self.assertIsNone(session.receipt_records()[0].outcome)
        session.request(credentials["a"], lambda c, w: c.list_devices(), receipt=first)
        with self.assertRaises((PermissionError, ValueError)):
            session.request(credentials["a"], lambda c, w: c.list_devices(), receipt=first)
        with self.assertRaises(FileNotFoundError):
            session.request(credentials["a"], lambda c, w: w.read_circuit("absent.json"))
        self.assertEqual([record.outcome for record in session.receipt_records()],
                         [ReceiptOutcome.COMPLETED, ReceiptOutcome.REJECTED])
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)

    def test_close_abandons_pending_receipts_without_unfreezing_environment(self):
        environment, store, runtime, _, credentials, session, _ = session_fixture()
        pending = session.receive(credentials["a"], "device.list")
        session.close()
        session.abandon_receipt(pending)  # Dispatch cleanup is idempotent.
        self.assertEqual(session.receipt_records()[0].outcome, ReceiptOutcome.ABANDONED)
        with self.assertRaises(PermissionError):
            session.request(credentials["a"], lambda c, w: c.list_devices(), receipt=pending)
        self.assertTrue(environment.established)
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)

    def test_manifest_executor_and_authority_mismatch_are_rejected_before_binding(self):
        a = session_fixture()
        other = session_fixture(attacker_operations=("device.info.read",))
        limits = SessionHostLimits(4, 2, ReceiptLimits(8, 16))
        with self.assertRaisesRegex(ValueError, "different scenario manifest"):
            AttackSession(environment=other[0], service=a[5]._service,
                          authority=VirtualCredentialAuthority(), run_next=a[2].run_next, limits=limits)
        # A fresh service is needed; a service/authority may not back two worlds.
        with self.assertRaisesRegex(ValueError, "another runtime"):
            AttackSession(environment=a[0], service=a[5]._service,
                          authority=a[3], run_next=other[2].run_next, limits=limits)
        with self.assertRaises(ValueError):
            a[3].bind_session(object(), frozenset({"a", "v"}))
        with self.assertRaises(ValueError):
            a[3].issue("unknown")
        self.assertEqual(a[2].scheduler.queued_job_ids(), ())
        self.assertEqual(a[1].read().revision, 0)

    def test_authorization_and_compiler_caches_are_checked_not_only_manifest_id(self):
        fixture = session_fixture()
        original = fixture[5]._service
        common = dict(runtime=fixture[2], scenario_manifest_id=fixture[0].establish().manifest_id,
                      principals=list(original._principals.values()))
        wrong_compiler = JobApplicationService(**common,
            program_compiler=LocalCircuitCompiler(max_shots=1))
        with self.assertRaisesRegex(ValueError, "compiler differs"):
            wrong_compiler.validate_session_binding(fixture[0].establish(), fixture[2].run_next)
        policy = fixture[2].job_authorization
        fixture[2]._job_authorization = JobAuthorizationPolicy(
            policy.service_operations, policy.scenario_operations,
            {"attacker": frozenset(), "victim": policy.actor_grants["victim"]}, policy.tenant_roles)
        with self.assertRaisesRegex(ValueError, "authorization differs"):
            original.validate_session_binding(fixture[0].establish(), fixture[2].run_next)

    def test_compute_delay_does_not_block_admission_change_counts_or_device_duration(self):
        baseline, delayed = session_fixture(), session_fixture()
        started, release = Event(), Event()
        for fixture in (baseline, delayed):
            fixture[5]._service._sampling_salt = bytes(32)  # Private deterministic test entropy.
            fixture[5].request(fixture[4]["a"], lambda c, w: w.put_circuit("bell.json", circuit_json()))

        def submit(fixture, key):
            return fixture[5].request(fixture[4]["a"], lambda c, w: TerminalSession(c, w).execute(
                f"program submit bell.json --device qpu-1 --shots 4 --request-id {key}"))

        first_baseline = submit(baseline, "first")
        baseline[5].run_next()
        second_baseline = submit(baseline, "second")
        baseline[5].run_next()
        first_delayed = submit(delayed, "first")
        execute = delayed[2]._execute

        def paused(job):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test compute barrier timed out")
            return execute(job)

        with patch.object(delayed[2], "_execute", side_effect=paused), ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(delayed[5].run_next)
            try:
                self.assertTrue(started.wait(2))
                observation = pool.submit(delayed[5].request, delayed[4]["a"],
                                          lambda c, w: c.get_status(first_delayed.job_id))
                self.assertEqual(observation.result(timeout=2).status.value, "running")
                admission = pool.submit(submit, delayed, "second")
                second_delayed = admission.result(timeout=2)
                self.assertEqual(second_delayed.status.value, "queued")
                self.assertEqual(delayed[1].read().device_time_us, 0)
                with self.assertRaises(RuntimeError):
                    delayed[2].run_next()  # No second physical executor, even via trusted bypass.
                with self.assertRaises(SessionBusyError):
                    delayed[5].run_next()
            finally:
                release.set()
            self.assertEqual(running.result(timeout=2).status.value, "succeeded")
        delayed[5].run_next()
        for expected, actual in ((first_baseline, first_delayed), (second_baseline, second_delayed)):
            left = baseline[5].request(baseline[4]["a"], lambda c, w: c.get_result(expected.job_id))
            right = delayed[5].request(delayed[4]["a"], lambda c, w: c.get_result(actual.job_id))
            self.assertEqual(left.result, right.result)
        self.assertEqual(baseline[1].read().device_time_us, delayed[1].read().device_time_us)
        self.assertEqual(baseline[1].read().density_matrix, delayed[1].read().density_matrix)
        self.assertEqual(delayed[2].scheduler.get(first_delayed.job_id).acceptance_sequence, 1)
        self.assertEqual(delayed[2].scheduler.get(second_delayed.job_id).acceptance_sequence, 2)

    def test_queued_cancel_and_close_remain_available_while_numeric_work_is_paused(self):
        fixture = session_fixture()
        session, credentials, runtime = fixture[5], fixture[4], fixture[2]
        session.request(credentials["a"], lambda c, w: w.put_circuit("bell.json", circuit_json()))
        def submit(key):
            return session.request(credentials["a"], lambda c, w: TerminalSession(c, w).execute(
                f"program submit bell.json --device qpu-1 --shots 1 --request-id {key}"))
        first, second = submit("first"), submit("second")
        started, release, closing = Event(), Event(), Event()
        execute = runtime._execute
        close_authority = fixture[3].close
        def paused(job):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test barrier timed out")
            return execute(job)
        def revoke():
            close_authority()
            closing.set()
        with patch.object(runtime, "_execute", side_effect=paused), \
                patch.object(fixture[3], "close", side_effect=revoke), ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(session.run_next)
            try:
                self.assertTrue(started.wait(2))
                cancelled = pool.submit(session.request, credentials["a"], lambda c, w: c.cancel(second.job_id))
                self.assertEqual(cancelled.result(timeout=2).status.value, "cancelled")
                discarded = pool.submit(session.close)
                self.assertTrue(closing.wait(2))
                self.assertFalse(discarded.done())
                with self.assertRaises(PermissionError):
                    session.request(credentials["a"], lambda c, w: c.get_status(first.job_id))
            finally:
                release.set()
            self.assertEqual(running.result(timeout=2).status.value, "succeeded")
            discarded.result(timeout=2)
        self.assertEqual(runtime.scheduler.get(second.job_id).status.value, "cancelled")
        self.assertEqual(fixture[1].read().revision, 1)  # Completed shot is not undone on close.
        self.assertEqual(session._workspaces, {})

    def test_terminal_namespace_is_actor_private_and_close_revokes(self):
        environment, store, runtime, authority, tokens, session, registry = session_fixture()
        session.request(tokens["a"], lambda client, workspace: workspace.put_circuit("bell.json", circuit_json()))
        with self.assertRaises(FileNotFoundError):
            session.request(tokens["v"], lambda client, workspace: workspace.read_circuit("bell.json"))
        job = session.request(tokens["a"], lambda client, workspace: TerminalSession(client, workspace).execute(
            "program submit bell.json --device qpu-1 --shots 4 --request-id request"))
        self.assertEqual(job.status.value, "queued")
        self.assertEqual(store.read().revision, 0)
        session.run_next()
        result = session.request(tokens["a"], lambda client, workspace: client.get_result(job.job_id))
        self.assertEqual(sum(result.result["counts"].values()), 4)
        registry.discard("world")
        registry.discard("world")
        self.assertEqual(session._workspaces, {})
        self.assertIsNone(session._service)
        with self.assertRaises(PermissionError):
            authority.resolve(tokens["a"])
        with self.assertRaises(PermissionError):
            registry.authenticated("world", tokens["v"])
        with self.assertRaises(RuntimeError):
            session.run_next()
        self.assertTrue(environment.established)  # Disposal is not a mutable manifest reset.

    def test_host_capacity_limits_are_explicit_and_atomic(self):
        _, store, runtime, _, tokens, session, registry = session_fixture(max_jobs=1, max_workspaces=1)
        session.request(tokens["a"], lambda client, workspace: workspace.put_circuit("bell.json", circuit_json()))
        execute = lambda key: session.request(tokens["a"], lambda client, workspace: TerminalSession(client, workspace).execute(
            f"program submit bell.json --device qpu-1 --shots 1 --request-id {key}"))
        first = execute("same")
        self.assertEqual(execute("same").job_id, first.job_id)
        with self.assertRaises(SessionCapacityError):
            execute("new")
        with self.assertRaises(SessionCapacityError):
            session.request(tokens["v"], lambda client, workspace: client.list_devices())
        with self.assertRaises(SessionCapacityError):
            registry.register("other", session)
        self.assertEqual(runtime.scheduler.queued_job_ids(), (first.job_id,))
        self.assertEqual(store.read().revision, 0)
        with self.assertRaises(ValueError):
            SessionHostLimits(True, 2, ReceiptLimits(8, 16))
        with self.assertRaises(ValueError):
            AttackSessionRegistry(max_sessions=0)

    def test_unestablished_environment_is_not_bound_and_assumptions_remain_frozen(self):
        from core.attack_simulation.scenario_compiler import ScenarioOverride
        environment, _, _, _, _, session, _ = session_fixture()
        with self.assertRaises(RuntimeError):
            environment.set_assumption(ScenarioOverride("attacker.operation_grants", [], "test"))
        unestablished = ScenarioEnvironment(environment.session)
        with self.assertRaises(ValueError):
            AttackSession(environment=unestablished, service=session._service, authority=session._authority,
                run_next=lambda: None, limits=SessionHostLimits(1, 1, ReceiptLimits(8, 16)))


if __name__ == "__main__":
    unittest.main()
