"""Single-owner assembly and rollback before a session is published."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch

from api.attack_sessions import AttackSession, SessionCapacityError
from core.attack_simulation.job_service import JobApplicationService
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.virtual_credentials import VirtualCredentialAuthority
from tests.test_attack_sessions import circuit_json, session_fixture


def unbound_fixture():
    # Capture the assembler input before any owner has been claimed. No test
    # edits owner flags or creates a second physics state authority.
    with patch("tests.test_attack_sessions.AttackSession") as construction, \
            patch("tests.test_attack_sessions.AttackSessionRegistry.register"):
        fixture = session_fixture(max_jobs=1)
    return fixture, construction.call_args.kwargs


class SessionOwnershipTests(unittest.TestCase):
    def test_simultaneous_session_construction_has_one_owner_and_one_capacity(self):
        fixture, kwargs = unbound_fixture()
        service, runtime = kwargs["service"], fixture[2]
        authorities = (VirtualCredentialAuthority(), VirtualCredentialAuthority())
        barrier = Barrier(2, timeout=2)
        validate = service.validate_session_binding

        def concurrent_validation(*args):
            validate(*args)
            barrier.wait()  # Both preflight checks pass before either claim.

        def construct(authority):
            try:
                return AttackSession(**{**kwargs, "authority": authority})
            except ValueError:
                return None

        with patch.object(service, "validate_session_binding", side_effect=concurrent_validation), \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(construct, authority) for authority in authorities]
            sessions = [future.result(timeout=4) for future in futures]
        winners = [session for session in sessions if session is not None]
        self.assertEqual(len(winners), 1)
        loser = authorities[sessions.index(None)]
        self.assertIsNone(loser._session_owner)
        self.assertIsNone(loser._allowed_principals)
        self.assertTrue(runtime.session_owned)
        session = winners[0]
        credential = session._authority.issue("a")
        program = CircuitProgram.from_editor_json(circuit_json())
        first = session.request(credential, lambda c, w:
            c.submit_program(ProgramSubmitRequest(program, "qpu-1", 1, "first")))
        with self.assertRaises(SessionCapacityError):
            session.request(credential, lambda c, w:
                c.submit_program(ProgramSubmitRequest(program, "qpu-1", 1, "second")))
        self.assertEqual(runtime.scheduler.queued_job_ids(), (first.job_id,))
        self.assertEqual(fixture[1].read().revision, 0)

    def test_rejected_authority_rolls_back_only_unpublished_service_and_runtime(self):
        fixture, kwargs = unbound_fixture()
        authority, service, runtime = kwargs["authority"], kwargs["service"], fixture[2]
        for failure in ("closed", "foreign_principal", "bound"):
            with self.subTest(failure=failure):
                rejected = VirtualCredentialAuthority()
                if failure == "closed":
                    rejected.close()
                elif failure == "foreign_principal":
                    rejected.issue("unknown")
                else:
                    foreign_owner = object()
                    rejected.bind_session(foreign_owner, frozenset({"a", "v"}))
                old_owner = rejected._session_owner
                with self.assertRaises(ValueError):
                    AttackSession(**{**kwargs, "authority": rejected})
                self.assertIsNone(service._session_owner)
                self.assertFalse(runtime.session_owned)
                self.assertEqual(rejected._session_owner, old_owner)
                self.assertEqual(runtime.scheduler.queued_job_ids(), ())
                self.assertEqual(fixture[1].read().revision, 0)
        session = AttackSession(**kwargs)
        self.assertEqual(authority._session_owner, id(session))
        session.close()
        self.assertTrue(runtime.session_owned)  # Disposal is not construction rollback.
        with self.assertRaises(ValueError):
            AttackSession(**{**kwargs, "authority": VirtualCredentialAuthority()})

    def test_distinct_services_cannot_claim_one_runtime_and_foreign_abort_is_denied(self):
        fixture, kwargs = unbound_fixture()
        first, runtime = kwargs["service"], fixture[2]
        second = JobApplicationService(runtime=runtime,
            principals=list(first._principals.values()), program_compiler=first._program_compiler,
            scenario_manifest_id=first._scenario_manifest_id)
        owners = (object(), object())
        barrier = Barrier(2, timeout=2)

        def claim(service, owner):
            barrier.wait()
            try:
                service.claim_session(owner)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim, service, owner) for service, owner in zip((first, second), owners)]
            results = [future.result(timeout=4) for future in futures]
        self.assertEqual(results.count(True), 1)
        winning_service = (first, second)[results.index(True)]
        winning_owner = owners[results.index(True)]
        with self.assertRaises(PermissionError):
            winning_service._abort_session_claim(object())
        with self.assertRaises(PermissionError):
            runtime._abort_session_claim(object())
        self.assertTrue(runtime.session_owned)
        winning_service._abort_session_claim(winning_owner)
        self.assertFalse(runtime.session_owned)

    def test_one_authority_cannot_bind_two_worlds_and_loser_can_retry(self):
        fixtures = (unbound_fixture(), unbound_fixture())
        authority = VirtualCredentialAuthority()
        barrier = Barrier(2, timeout=2)
        bind = authority.bind_session

        def concurrent_bind(*args):
            barrier.wait()
            return bind(*args)

        def construct(kwargs):
            try:
                return AttackSession(**{**kwargs, "authority": authority})
            except ValueError:
                return None

        with patch.object(authority, "bind_session", side_effect=concurrent_bind), \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(construct, kwargs) for _, kwargs in fixtures]
            sessions = [future.result(timeout=4) for future in futures]
        self.assertEqual(sum(session is not None for session in sessions), 1)
        loser = sessions.index(None)
        fixture, kwargs = fixtures[loser]
        self.assertFalse(fixture[2].session_owned)
        self.assertIsNone(kwargs["service"]._session_owner)
        retry = AttackSession(**{**kwargs, "authority": VirtualCredentialAuthority()})
        self.assertTrue(fixture[2].session_owned)
        self.assertEqual(authority._session_owner, id(sessions[1 - loser]))
        retry.close()


if __name__ == "__main__":
    unittest.main()
