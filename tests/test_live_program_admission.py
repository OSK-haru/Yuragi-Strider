"""Trusted common-session live admission; driver ledger/order is external."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Event
import unittest
from unittest.mock import patch

from api.attack_sessions import SessionCapacityError
from core.attack_simulation.job_service import PrincipalGrant
from core.attack_simulation.jobs import JobCommand
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError, ProgramSubmitRequest
from core.attack_simulation.request_receipts import RequestReceipt
from tests.test_attack_sessions import circuit_json, session_fixture
from tests.test_attack_workloads import fixture


def request(key="live", *, shots=1):
    return ProgramSubmitRequest(CircuitProgram.from_editor_json(circuit_json()), "qpu-1", shots, key)


def receipt(sequence=1, principal="a", operation="program.submit"):
    # Represents a receipt already validated by the trusted driver's own ledger.
    return RequestReceipt(sequence, 1234, principal, operation)


class LiveProgramAdmissionTests(unittest.TestCase):
    def test_split_preparation_is_pure_and_admission_retains_original_receipt(self):
        args, _, runner = fixture()
        _, store, runtime, _, credentials, session, _ = args
        original = receipt()
        snapshot = store.read()
        command = session.prepare_live_program(runner._owner, credentials["a"], request(), original)
        self.assertIs(type(command), JobCommand)
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(session._accepted_requests, set())
        self.assertEqual(session._requests_in_flight, 0)
        view = session.admit_live_program(runner._owner, credentials["a"], command, original)
        job = runtime.scheduler.get(view.job_id)
        self.assertIs(job.ingress_receipt, original)
        self.assertEqual(job.acceptance_sequence, 1)
        self.assertEqual(store.read(), snapshot)
        self.assertEqual(store.events(), ())

    def test_host_cap_allows_exact_replay_but_rejects_changed_content(self):
        args, _, runner = fixture(max_jobs=1)
        runtime, credentials, session = args[2], args[4], args[5]
        original = receipt()
        first = session.submit_live_program(runner._owner, credentials["a"], request(), original)
        replay = session.submit_live_program(runner._owner, credentials["a"], request(), receipt(2))
        self.assertEqual(replay.job_id, first.job_id)
        with self.assertRaises(ProgramRequestError):
            session.submit_live_program(runner._owner, credentials["a"], request(shots=2), receipt(3))
        with self.assertRaises(SessionCapacityError):
            session.submit_live_program(runner._owner, credentials["a"], request("other"), receipt(4))
        # Capacity keys include principal even when the idempotency key matches.
        with self.assertRaises(SessionCapacityError):
            session.submit_live_program(runner._owner, credentials["v"], request(), receipt(5, "v"))
        self.assertEqual(session._accepted_requests, {("a", "live")})
        self.assertEqual(runtime.scheduler.queued_job_ids(), (first.job_id,))
        self.assertIs(runtime.scheduler.get(first.job_id).ingress_receipt, original)

    def test_split_admission_rechecks_capacity_after_another_job_wins(self):
        args, _, runner = fixture(max_jobs=1)
        runtime, credentials, session = args[2], args[4], args[5]
        pending = session.prepare_live_program(runner._owner, credentials["a"], request("pending"), receipt())
        winner = session.submit_live_program(runner._owner, credentials["a"], request("winner"), receipt(2))
        with self.assertRaises(SessionCapacityError):
            session.admit_live_program(runner._owner, credentials["a"], pending, receipt())
        self.assertEqual(runtime.scheduler.queued_job_ids(), (winner.job_id,))
        self.assertEqual(session._accepted_requests, {("a", "winner")})

    def test_owner_receipt_type_operation_and_principal_are_required_on_both_phases(self):
        args, _, runner = fixture()
        runtime, credentials, session = args[2], args[4], args[5]
        command = session.prepare_live_program(runner._owner, credentials["a"], request(), receipt())

        class ReceiptSubclass(RequestReceipt):
            pass

        invalid = (
            (None, credentials["a"], receipt(), PermissionError),
            (object(), credentials["a"], receipt(), PermissionError),
            (runner._owner, "invalid-virtual-credential", receipt(), PermissionError),
            (runner._owner, credentials["v"], receipt(), PermissionError),
            (runner._owner, credentials["a"], receipt(principal="v"), PermissionError),
            (runner._owner, credentials["a"], receipt(operation="application.request"), PermissionError),
            (runner._owner, credentials["a"], object(), TypeError),
            (runner._owner, credentials["a"], ReceiptSubclass(1, 1234, "a", "program.submit"), TypeError),
        )
        for owner, credential, ticket, error in invalid:
            with self.subTest(error=error, owner_matches=owner is runner._owner):
                with self.assertRaises(error):
                    session.prepare_live_program(owner, credential, request(), ticket)
                with self.assertRaises(error):
                    session.admit_live_program(owner, credential, command, ticket)
        with self.assertRaises(PermissionError):
            session.admit_live_program(runner._owner, credentials["v"], command, receipt(2, "v"))
        with self.assertRaises(TypeError):
            session.admit_live_program(runner._owner, credentials["a"], object(), receipt())
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(session._accepted_requests, set())
        self.assertEqual(session._requests_in_flight, 0)

    def test_unclaimed_session_cannot_use_live_submission(self):
        args = session_fixture()
        for owner in (None, object()):
            with self.subTest(owner=owner), self.assertRaises(PermissionError):
                args[5].submit_live_program(owner, args[4]["a"], request(), receipt())
        self.assertEqual(args[2].scheduler.queued_job_ids(), ())

    def test_admission_reauthorizes_grants_and_credentials_after_preparation(self):
        for revoked_credential in (False, True):
            with self.subTest(revoked_credential=revoked_credential):
                args, _, runner = fixture()
                runtime, authority, credentials, session = args[2], args[3], args[4], args[5]
                command = session.prepare_live_program(runner._owner, credentials["a"], request(), receipt())
                if revoked_credential:
                    authority.close()
                else:
                    grant = session._service._principals["a"]
                    session._service._principals["a"] = PrincipalGrant(
                        "a", grant.tenant_id, grant.operations - {"program.submit"},
                    )
                with self.assertRaises(PermissionError):
                    session.admit_live_program(runner._owner, credentials["a"], command, receipt())
                self.assertEqual(session._accepted_requests, set())
                self.assertEqual(runtime.scheduler.queued_job_ids(), ())

    def test_live_submission_never_uses_legacy_receive_dispatch_ledger_or_workspace(self):
        args, _, runner = fixture()
        credentials, session = args[4], args[5]
        with ExitStack() as patches:
            for name in ("receive", "request", "dispatch_ready", "_release_dispatch"):
                patches.enter_context(patch.object(session, name, side_effect=AssertionError("legacy ingress")))
            for name in ("reserve", "validate", "claim", "finish", "abandon"):
                patches.enter_context(patch.object(session._receipts, name, side_effect=AssertionError("legacy ledger")))
            first = session.submit_live_program(runner._owner, credentials["a"], request(), receipt())
            command = session.prepare_live_program(runner._owner, credentials["a"], request(), receipt(2))
            second = session.admit_live_program(runner._owner, credentials["a"], command, receipt(2))
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(session.receipt_records(), ())
        self.assertEqual(session._dispatch, {})
        self.assertEqual(session._workspaces, {})

    def test_close_fences_and_drains_both_split_and_combined_preparation_without_gates(self):
        for method in ("prepare_live_program", "submit_live_program"):
            with self.subTest(method=method):
                args, _, runner = fixture()
                store, runtime, credentials, session = args[1], args[2], args[4], args[5]
                service = session._service
                entered, release, fenced = Event(), Event(), Event()
                compile_program, fence_close = service._prepare_program, session._fence_close

                def paused(*a, **kw):
                    entered.set()
                    if not release.wait(4):
                        raise RuntimeError("test compilation barrier timed out")
                    return compile_program(*a, **kw)

                def fence():
                    fence_close()
                    fenced.set()

                with patch.object(service, "_prepare_program", side_effect=paused), \
                        patch.object(session, "_fence_close", side_effect=fence), \
                        ThreadPoolExecutor(max_workers=2) as pool:
                    compiling = pool.submit(getattr(session, method), runner._owner,
                                            credentials["a"], request(), receipt())
                    closing = None
                    try:
                        self.assertTrue(entered.wait(2))
                        # Main thread can acquire each gate while compilation pauses.
                        self.assertTrue(session._lock.acquire(timeout=1))
                        try:
                            self.assertEqual(session._requests_in_flight, 1)
                        finally:
                            session._lock.release()
                        self.assertTrue(session._execution_lock.acquire(timeout=1))
                        session._execution_lock.release()
                        self.assertTrue(session._scope_lock.acquire(timeout=1))
                        session._scope_lock.release()
                        closing = pool.submit(session.close)
                        self.assertTrue(fenced.wait(2))
                        self.assertFalse(closing.done())
                        self.assertIs(session._service, service)
                    finally:
                        release.set()
                    with self.assertRaises(PermissionError):
                        compiling.result(timeout=5)
                    closing.result(timeout=5)
                self.assertEqual(session._requests_in_flight, 0)
                self.assertIsNone(session._service)
                self.assertEqual(runtime.scheduler.queued_job_ids(), ())
                self.assertEqual(store.read().revision, 0)

    def test_compile_failure_releases_lifecycle_count_and_capacity(self):
        args, _, runner = fixture(max_jobs=1)
        credentials, session = args[4], args[5]
        for method in ("prepare_live_program", "submit_live_program"):
            with self.subTest(method=method), \
                    patch.object(session._service, "_prepare_program", side_effect=RuntimeError("compile failure")):
                with self.assertRaises(RuntimeError):
                    getattr(session, method)(runner._owner, credentials["a"], request(), receipt())
            self.assertEqual(session._requests_in_flight, 0)
            self.assertEqual(session._accepted_requests, set())
        session.submit_live_program(runner._owner, credentials["a"], request(), receipt(2))
        session.close()
        self.assertIsNone(session._service)


if __name__ == "__main__":
    unittest.main()
