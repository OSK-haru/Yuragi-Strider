"""Trusted prepare/admit split and supervisor-only original job receipts."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict, replace
import unittest
from unittest.mock import patch

from core.attack_simulation.job_service import PrincipalGrant
from core.attack_simulation.jobs import AttackerMeasureOperation, JobCommand, JobStatus
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError, ProgramSubmitRequest
from core.attack_simulation.request_receipts import (
    ReceiptLimits, ReceiptOutcome, RequestReceipt, RequestReceiptLedger,
)
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from tests.test_common_command_pipeline import circuit_json, pipeline


def _fixture(**kwargs):
    store, runtime, _, terminals = pipeline(**kwargs)
    return store, runtime, terminals[0]._client._service


def _request(*gates, key="sample"):
    return ProgramSubmitRequest(
        CircuitProgram.from_editor_json(circuit_json(*gates)), "qpu-1", 1, key,
    )


def _receipt(sequence=1, principal="a"):
    return RequestReceipt(sequence, 1234, principal, "program.submit")


class ProgramAdmissionTests(unittest.TestCase):
    def test_prepare_is_immutable_and_has_no_job_or_state_side_effects(self):
        store, runtime, service = _fixture()
        snapshot = store.read()
        command = service._prepare_program("a", _request("X"))
        self.assertIs(type(command), JobCommand)
        with self.assertRaises(FrozenInstanceError):
            command.tenant_id = "other"
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read(), snapshot)
        self.assertEqual(store.events(), ())
        view = service._admit_program("a", command, receipt=_receipt())
        self.assertEqual(runtime.scheduler.get(view.job_id).acceptance_sequence, 1)
        self.assertEqual(store.read(), snapshot)

    def test_prepare_rejection_leaves_no_queue_or_physical_changes(self):
        store, runtime, service = _fixture()
        for principal, request, error in (
            ("unknown", _request(), PermissionError),
            ("a", replace(_request(), device_id="other"), ProgramRequestError),
            ("a", _request("Z"), ProgramRequestError),
        ):
            with self.subTest(principal=principal), self.assertRaises(error):
                service._prepare_program(principal, request)
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)
        self.assertEqual(store.events(), ())

    def test_admission_reauthorizes_after_prepare(self):
        store, runtime, service = _fixture()
        command = service._prepare_program("a", _request())
        original = service._principals["a"]
        service._principals["a"] = PrincipalGrant(
            "a", original.tenant_id, original.operations - {"program.submit"},
        )
        with self.assertRaises(PermissionError):
            service._admit_program("a", command, receipt=_receipt())
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)

    def test_admission_rejects_foreign_identity_manifest_allocation_and_legacy_operation(self):
        store, runtime, service = _fixture(physical_qubit_ids=(4, 9))
        command = service._prepare_program("a", _request())
        attempts = (
            ("v", command, PermissionError),
            ("a", replace(command, tenant_id="tenant-v"), PermissionError),
            ("a", replace(command, created_by_principal_id="v"), PermissionError),
            ("a", replace(command, scenario_manifest_id="another-world"), ProgramRequestError),
            ("a", replace(command, layout=PhysicalQubitLayout("qpu-1", (9,))), ProgramRequestError),
            ("a", replace(command, operation=AttackerMeasureOperation(1)), TypeError),
        )
        for principal, candidate, error in attempts:
            with self.subTest(candidate=candidate), self.assertRaises(error):
                service._admit_program(principal, candidate)
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)

    def test_receipt_exact_type_and_principal_checked_before_any_admission_or_replay(self):
        store, runtime, service = _fixture()
        command = service._prepare_program("a", _request())

        class ReceiptSubclass(RequestReceipt):
            pass

        wrong = ((object(), TypeError),
                 (ReceiptSubclass(1, 1234, "a", "program.submit"), TypeError),
                 (_receipt(principal="v"), PermissionError))
        for receipt, error in wrong:
            with self.subTest(receipt=receipt), self.assertRaises(error):
                service._admit_program("a", command, receipt=receipt)
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        accepted = service._admit_program("a", command, receipt=_receipt())
        with self.assertRaises(PermissionError):
            service._admit_program("a", command, receipt=_receipt(2, "v"))
        self.assertEqual(runtime.scheduler.queued_job_ids(), (accepted.job_id,))
        self.assertEqual(store.read().revision, 0)

    def test_replay_preserves_first_receipt_and_changed_content_rejected(self):
        _, runtime, service = _fixture()
        first_receipt = _receipt()
        command = service._prepare_program("a", _request())
        first = service._admit_program("a", command, receipt=first_receipt)
        second = service._admit_program("a", service._prepare_program("a", _request()), receipt=_receipt(2))
        self.assertEqual(first.job_id, second.job_id)
        self.assertIs(runtime.scheduler.get(first.job_id).ingress_receipt, first_receipt)
        with self.assertRaises(ProgramRequestError):
            service._admit_program("a", service._prepare_program("a", _request("X")), receipt=_receipt(3))
        self.assertEqual(runtime.scheduler.queued_job_ids(), (first.job_id,))
        self.assertIs(runtime.scheduler.get(first.job_id).ingress_receipt, first_receipt)

    def test_compatible_submit_has_no_receipt_and_later_replay_does_not_invent_one(self):
        _, runtime, service = _fixture()
        view = service.submit_program("a", _request())
        replay = service._admit_program("a", service._prepare_program("a", _request()), receipt=_receipt())
        self.assertEqual(view.job_id, replay.job_id)
        self.assertIsNone(runtime.scheduler.get(view.job_id).ingress_receipt)
        self.assertEqual(set(asdict(view)), {"job_id", "status", "error_code"})

    def test_direct_scheduler_enforces_receipt_scope_without_advancing_acceptance_sequence(self):
        _, runtime, service = _fixture()
        command = service._prepare_program("a", _request())
        for invalid, error in ((_receipt(principal="v"), PermissionError), (object(), TypeError)):
            with self.assertRaises(error):
                runtime.scheduler.submit(command, receipt=invalid)
        accepted = runtime.scheduler.submit(command, receipt=_receipt())
        self.assertEqual(accepted.acceptance_sequence, 1)
        self.assertEqual(runtime.scheduler.queued_job_ids(), (accepted.job_id,))

    def test_admission_rechecks_service_capability_and_runtime_work_limits(self):
        store, runtime, service = _fixture(max_execution_work=1)
        command = service._prepare_program("a", _request())
        with self.assertRaises(ProgramRequestError):
            service._admit_program("a", command, receipt=_receipt())
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)
        service._program_compiler = None
        with self.assertRaises(ProgramRequestError):
            service._admit_program("a", command, receipt=_receipt())

    def test_job_retains_receipt_after_ledger_eviction_and_all_status_transitions(self):
        for final in (JobStatus.SUCCEEDED, JobStatus.CANCELLED, JobStatus.INTERNAL_ERROR):
            with self.subTest(final=final):
                store, runtime, service = _fixture()
                ledger = RequestReceiptLedger(ReceiptLimits(1, 1), clock=lambda: 1234)
                receipt = ledger.reserve("a", "program.submit")
                view = service._admit_program("a", service._prepare_program("a", _request()), receipt=receipt)
                ledger.finish(receipt, outcome=ReceiptOutcome.COMPLETED, job_id=view.job_id)
                later = ledger.reserve("a", "device.list")
                ledger.finish(later, outcome=ReceiptOutcome.COMPLETED)
                self.assertNotIn(receipt, [record.receipt for record in ledger.records()])
                if final is JobStatus.CANCELLED:
                    service.cancel("a", view.job_id)
                    self.assertIsNone(runtime.run_next())
                    self.assertEqual(store.read().revision, 0)
                elif final is JobStatus.INTERNAL_ERROR:
                    with patch.object(runtime, "_execute", side_effect=RuntimeError("host error")):
                        runtime.run_next()
                    self.assertEqual(store.read().revision, 0)
                else:
                    runtime.run_next()
                job = runtime.scheduler.get(view.job_id)
                self.assertIs(job.status, final)
                self.assertIs(job.ingress_receipt, receipt)
                with self.assertRaises(FrozenInstanceError):
                    job.ingress_receipt.sequence = 99
                self.assertFalse(hasattr(service.get_result("a", view.job_id), "ingress_receipt"))

    def test_concurrent_replay_admits_once_and_retains_winning_receipt(self):
        store, runtime, service = _fixture()
        command = service._prepare_program("a", _request())
        receipts = tuple(_receipt(i) for i in range(1, 9))
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(service._admit_program, "a", command, receipt=r) for r in receipts]
            views = [future.result(timeout=5) for future in futures]
        self.assertEqual(len({view.job_id for view in views}), 1)
        self.assertEqual(runtime.scheduler.queued_job_ids(), (views[0].job_id,))
        self.assertIn(runtime.scheduler.get(views[0].job_id).ingress_receipt, receipts)
        self.assertEqual(store.read().revision, 0)


if __name__ == "__main__":
    unittest.main()
