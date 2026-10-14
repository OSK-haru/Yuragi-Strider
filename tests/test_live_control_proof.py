"""Exact live arrival proofs and trusted queued-window replanning."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from api.attack_controls import DeclaredControlService
from core.attack_simulation.declared_execution import JobExecutionWindow, QueuedCancellationRejectedError
from core.attack_simulation.declared_ingress import DeclaredIngressBook, IngressReservation
from core.attack_simulation.event_commit_boundary import EventOrderError, EventPosition
from core.attack_simulation.job_service import JobNotFoundError
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.live_ingress import LiveIngress
from tests.test_declared_controls import assembly, evidence, pos, setup


def live_fixture(*, branches=False, retire=True, producer_id="driver-live"):
    # Reuse the existing owners fixture, exercising the real constructor with
    # the driver's explicit namespace rather than changing existing tests.
    with patch("tests.test_declared_controls.DeclaredControlService",
               side_effect=lambda **owners: DeclaredControlService(**owners, ingress_producer_id=producer_id)):
        args, host, runner, executor, controls = assembly(branches=branches)
    live = LiveIngress(controls.config.ingress, clock=runner._clock,
        book=runner._book, gate=executor._gate, producer_id=producer_id)
    runner.start()
    host.value += 12_500
    receipt = live.capture("v", "job.cancel")
    token = live.reservation(receipt)
    assert token.position == pos(20, 0)
    host.value += 12_500
    job = args[2].scheduler.get(runner.dispatch_next().submission.job_id)
    arrival = runner.states()["plan-v"].events[0].arrival
    window = JobExecutionWindow(arrival, pos(40, 0),
        pos(40 + executor._duration(job, args[1].read()), 3), pos(200, 4))
    executor.register_job(job.job_id, window)
    executor.frontier.set_target(pos(1000))
    if branches:
        runner._book.refine_barrier("plan-v", 1, pos(210, 0))
    if retire:
        assert live.next_due(token.position) is token
        live.retire(token, decision="admitted")
    return (args, host, runner, executor, controls, job, window), live, token


def cancel_with(fixture, token, *, arrival=None, credential=None, request_id="live-cancel"):
    args, _, _, _, controls, job, _ = fixture
    return controls.cancel(args[4]["v"] if credential is None else credential,
        job.job_id, request_id, arrival=token.position if arrival is None else arrival,
        ingress_reservation=token)


def later_admission(fixture, at=30, *, retire=True):
    book = fixture[2]._book
    token = book.reserve("unrelated", 1, pos(at, 0))
    assert book.next_due(token.position) is token
    if retire:
        book.retire(token, decision="admitted")
    return token


class LiveControlProofTests(unittest.TestCase):
    def test_constructor_rejects_bad_namespace_before_claiming_or_binding_owners(self):
        for namespace in ("", "has space", "plan-v"):
            captured = {}

            def construct(**owners):
                captured.update(owners)
                return DeclaredControlService(**owners, ingress_producer_id=namespace)

            with patch("tests.test_declared_controls.DeclaredControlService", side_effect=construct):
                with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                    assembly(branches=False)
            executor, runner = captured["executor"], captured["runner"]
            self.assertIsNone(executor._control_owner)
            self.assertIsNone(executor._receipt_namespace)
            self.assertFalse(runner._book._transaction_lock_bound)

    def test_namespace_accepts_arbitrary_valid_driver_name_and_is_frozen_before_start(self):
        for producer_id in ("live", "web-receipts:common", "z-driver"):
            fixture, _, token = live_fixture(producer_id=producer_id)
            self.assertIs(cancel_with(fixture, token).ingress_reservation, token)
        _, _, runner, executor, controls = assembly(branches=False)
        for invalid in ("", "has space", "\u975eASCII", "x" * 129, None):
            with self.assertRaises(ValueError):
                controls.bind_receipt_namespace(invalid)
            self.assertIsNone(executor._receipt_namespace)
        with self.assertRaises(ValueError):
            controls.bind_receipt_namespace("plan-v")
        with self.assertRaises(PermissionError):
            executor.bind_receipt_namespace("receipts", owner=object())
        controls.bind_receipt_namespace("receipts")
        for changed in ("receipts", "other-receipts"):
            with self.assertRaises(ValueError):
                controls.bind_receipt_namespace(changed)
        self.assertEqual(executor._receipt_namespace, "receipts")
        runner.start()
        with self.assertRaises(ValueError):
            controls.bind_receipt_namespace("receipts")

    def test_unbound_namespace_or_workload_token_never_exempts_historical_arrival(self):
        fixture = setup(branches=False)
        book = fixture[2]._book
        token = book.reserve("live", 1, pos(20, 0))
        book.next_due(token.position)
        book.retire(token, decision="admitted")
        before = evidence(fixture)
        with self.assertRaises(ValueError):
            cancel_with(fixture, token)
        self.assertEqual(evidence(fixture), before)
        fixture, _, _ = live_fixture()
        workload_token = next(record.reservation for record in fixture[2]._book.snapshot().history
                              if record.reservation.producer_id == "plan-v")
        before = evidence(fixture)
        with self.assertRaises(ValueError):
            cancel_with(fixture, workload_token)
        self.assertEqual(evidence(fixture), before)

    def test_retired_receipt_allows_application_before_later_admission_cutoff_and_start(self):
        fixture, live, token = live_fixture()
        args, host, runner, executor, _, job, window = fixture
        later = later_admission(fixture)
        runner._book.confirm_admission_cutoff(later.position)
        executor.frontier.seal_ingress(pos(21, 0))
        executor.frontier.confirm(pos(21, 0))
        physical = args[1].read(), args[1].events(), executor.frontier.snapshot()
        calls = host.calls
        trace = cancel_with(fixture, token)
        self.assertIs(trace.ingress_reservation, token)
        self.assertEqual((trace.arrival, trace.application, trace.release),
                         (pos(20, 0), pos(22, 0), pos(25, 0)))
        self.assertLess(trace.application.key, later.position.key)
        self.assertLess(trace.application.key, window.start.key)
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.CANCELLED)
        self.assertEqual((args[1].read(), args[1].events(), executor.frontier.snapshot()), physical)
        self.assertEqual(host.calls, calls)
        self.assertEqual(live.snapshot().history[-1].reservation, later)
        self.assertEqual(runner._book.decision(token), "admitted")
        public = args[5]._service._get_status_at("v", job.job_id, trace.application)
        self.assertEqual(set(vars(public)), {"job_id", "status", "error_code"})

    def test_exact_current_grant_is_valid_without_retiring_or_stealing_the_grant(self):
        fixture, live, token = live_fixture(retire=False)
        self.assertIs(live.next_due(token.position), token)
        trace = cancel_with(fixture, token)
        self.assertEqual(trace.application, pos(22, 0))
        self.assertIs(live.snapshot().granted, token)
        self.assertIsNone(fixture[2]._book.decision(token))
        live.retire(token, decision="admitted")
        self.assertIs(cancel_with(fixture, token), trace)

    def test_recorded_arrival_can_replace_dependencies_after_safe_later_admission(self):
        fixture, _, token = live_fixture(branches=True)
        later_admission(fixture)
        fixture[2]._book.confirm_admission_cutoff(pos(30, 0))
        fixture[3].frontier.seal_ingress(pos(21, 0))
        fixture[3].frontier.confirm(pos(21, 0))
        trace = cancel_with(fixture, token)
        self.assertEqual(trace.application, pos(22, 0))
        self.assertEqual(fixture[2]._book.snapshot().barriers[0].earliest_next_arrival, pos(35, 0))

    def test_retired_proof_remains_valid_during_unrelated_later_grant(self):
        fixture, _, token = live_fixture()
        later = later_admission(fixture, retire=False)
        cancel_with(fixture, token)
        self.assertIs(fixture[2]._book.snapshot().granted, later)

    def test_proof_is_checked_even_without_dependencies_and_on_exact_replay(self):
        fixture, _, token = live_fixture()
        foreign = DeclaredIngressBook("world", 4, 8).reserve(token.producer_id, token.event_id, token.position)
        forged = IngressReservation(token.producer_id, token.event_id + 100, token.position)
        for replay in (False, True):
            if replay:
                trace = cancel_with(fixture, token)
            before = evidence(fixture)
            for invalid in (replace(token), foreign, forged, object()):
                with self.subTest(replay=replay, proof=type(invalid).__name__):
                    with self.assertRaises(ValueError) as rejected:
                        cancel_with(fixture, invalid, arrival=token.position)
                    self.assertNotIsInstance(rejected.exception, QueuedCancellationRejectedError)
                    self.assertEqual(evidence(fixture), before)
            if replay:
                self.assertIs(cancel_with(fixture, token), trace)

    def test_exact_proof_cannot_change_arrival_or_use_another_producer(self):
        fixture, _, token = live_fixture()
        other = later_admission(fixture)
        before = evidence(fixture)
        for proof, arrival in ((token, pos(19, 0)), (token, pos(21, 0)),
                               (token, EventPosition("other", 20, 0)), (other, other.position)):
            with self.subTest(arrival=arrival), self.assertRaises(ValueError):
                cancel_with(fixture, proof, arrival=arrival)
            self.assertEqual(evidence(fixture), before)

    def test_known_but_ungranted_receipt_is_not_ordered_admission(self):
        fixture, _, token = live_fixture(retire=False)
        before = evidence(fixture)
        with self.assertRaises(EventOrderError):
            cancel_with(fixture, token)
        self.assertEqual(evidence(fixture), before)

    def test_denied_retirement_cannot_prove_successful_control(self):
        fixture, live, token = live_fixture(retire=False)
        live.next_due(token.position)
        live.retire(token, decision="denied")
        before = evidence(fixture)
        with self.assertRaises(ValueError) as rejected:
            cancel_with(fixture, token)
        self.assertNotIsInstance(rejected.exception, QueuedCancellationRejectedError)
        self.assertEqual(evidence(fixture), before)

    def test_proof_never_bypasses_common_actor_authorization(self):
        fixture, _, token = live_fixture()
        before = evidence(fixture)
        for credential, error in ((fixture[0][4]["a"], JobNotFoundError),
                                  ("not-issued", PermissionError)):
            with self.assertRaises(error):
                cancel_with(fixture, token, credential=credential)
            self.assertEqual(evidence(fixture), before)
        cancel_with(fixture, token)
        with patch.object(fixture[0][5]._service, "_authorize", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                cancel_with(fixture, token)

    def test_application_must_be_strictly_after_confirmed_physics_and_before_start(self):
        for confirmed in (pos(22, 0), pos(23, 0), pos(40, 0)):
            fixture, _, token = live_fixture()
            executor = fixture[3]
            executor.frontier.seal_ingress(confirmed)
            executor.frontier.confirm(confirmed)
            before = evidence(fixture)
            with self.subTest(confirmed=confirmed), self.assertRaises(EventOrderError):
                cancel_with(fixture, token)
            self.assertEqual(evidence(fixture), before)
        fixture, _, token = live_fixture()
        executor, controls, job, window = fixture[3:]
        before = evidence(fixture)
        with self.assertRaises(QueuedCancellationRejectedError):
            executor.apply_cancellation(owner=controls._owner, principal_id="v", request_id="late",
                job_id=job.job_id, arrival=token.position, application=window.start,
                release=pos(50, 0), book=fixture[2]._book, ingress_reservation=token)
        self.assertEqual(evidence(fixture), before)

    def test_only_nonqueued_service_state_uses_typed_control_rejection(self):
        fixture, _, token = live_fixture()
        fixture[0][2].scheduler._start_declared(fixture[5].job_id)
        before = evidence(fixture)
        with self.assertRaises(QueuedCancellationRejectedError):
            cancel_with(fixture, token)
        self.assertEqual(evidence(fixture), before)

    def test_proof_cannot_apply_before_the_nominal_target_is_due(self):
        fixture, _, token = live_fixture()
        # Trusted test target: receipt is due but its processing delay is not.
        fixture[3].frontier._target = token.position
        before = evidence(fixture)
        with self.assertRaises(RuntimeError):
            cancel_with(fixture, token)
        self.assertEqual(evidence(fixture), before)

    def test_proof_retains_successor_cutoff_grant_retirement_and_seal_guards(self):
        for boundary in ("cutoff", "grant", "retired", "seal"):
            fixture, _, token = live_fixture(branches=True)
            runner, executor = fixture[2:4]
            if boundary == "cutoff":
                runner._book.confirm_admission_cutoff(pos(50, 0))
            elif boundary == "seal":
                executor.frontier.seal_ingress(pos(50, 0))
            else:
                later_admission(fixture, 50, retire=boundary == "retired")
            before = evidence(fixture)
            with self.subTest(boundary=boundary), self.assertRaises(EventOrderError):
                cancel_with(fixture, token)
            self.assertEqual(evidence(fixture), before)

    def test_exact_replay_after_successor_delivery_avoids_repreparing_dependencies(self):
        fixture, _, token = live_fixture(branches=True)
        trace = cancel_with(fixture, token)
        runner = fixture[2]
        runner.complete_result("plan-v", 1, available=trace.release)
        runner.dispatch_next()
        before = evidence(fixture)
        with patch.object(runner._book, "prepare_barrier_replacement", side_effect=AssertionError):
            self.assertIs(cancel_with(fixture, token), trace)
        self.assertEqual(evidence(fixture), before)


def second_queued(fixture):
    args, _, _, executor, _, first, first_window = fixture
    # Trusted queue fixture: retain the same common actor command/compiled program.
    second = args[2].scheduler.submit(replace(first.command, idempotency_key="second"))
    start = first_window.finish.time_us + 1
    duration = executor._duration(second, args[1].read())
    window = JobExecutionWindow(pos(18, 0), pos(start, 0),
        pos(start + duration, 3), pos(start + duration + 3, 0))
    executor.register_job(second.job_id, window)
    return second, window


class QueuedReplanTests(unittest.TestCase):
    def test_replan_during_detached_idle_preparation_prevents_stale_start_publication(self):
        fixture = setup(branches=False)
        args, _, runner, executor, controls, job, old = fixture
        executor.frontier.seal_ingress(old.finish)
        new = JobExecutionWindow(old.arrival, pos(30, 0),
            pos(30 + executor._duration(job, args[1].read()), 3), pos(100, 0))
        physical = args[1].read(), args[1].events(), executor.frontier.snapshot()
        evolve = executor._idle_model.evolve

        def replan_while_computing(*args, **kwargs):
            executor.replan_queued_job(job.job_id, new, owner=controls._owner)
            return evolve(*args, **kwargs)

        with patch.object(type(executor._idle_model), "evolve", side_effect=replan_while_computing):
            with self.assertRaisesRegex(RuntimeError, "start changed"):
                args[5].begin_declared_next(executor, _workload_owner=runner._owner)
        self.assertEqual((args[1].read(), args[1].events(), executor.frontier.snapshot()), physical)
        self.assertIsNone(executor._active)
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.QUEUED)
        self.assertIs(executor._windows[job.job_id], new)
        args[5].begin_declared_next(executor, _workload_owner=runner._owner)
        self.assertEqual(executor.frontier.snapshot().confirmed, new.start)

    def test_preceding_cancellation_allows_earlier_known_queued_start_below_ingress_seal(self):
        fixture = setup(branches=False)
        args, _, runner, executor, controls, first, _ = fixture
        second, old = second_queued(fixture)
        controls.cancel(args[4]["v"], first.job_id, "first-cancel", arrival=pos(20, 0))
        executor.frontier.seal_ingress(old.start)
        executor.frontier.confirm(pos(21, 0))
        new = JobExecutionWindow(old.arrival, pos(23, 0),
            pos(23 + executor._duration(second, args[1].read()), 3), pos(100, 0))
        before = args[1].read(), args[1].events(), executor.frontier.snapshot()
        history_size = len(executor._releases)
        executor.replan_queued_job(second.job_id, new, owner=controls._owner)
        self.assertLess(new.start.key, old.start.key)
        self.assertIs(executor._windows[second.job_id], new)
        self.assertIs(executor._releases[second.job_id], new.release)
        self.assertEqual(len(executor._releases), history_size)
        self.assertEqual((args[1].read(), args[1].events(), executor.frontier.snapshot()), before)
        executor.register_job(second.job_id, new)
        with self.assertRaises(ValueError):
            executor.register_job(second.job_id, old)
        args[5].run_declared_next(executor, _workload_owner=runner._owner)
        self.assertIs(args[2].scheduler.get(second.job_id).status, JobStatus.SUCCEEDED)

    def test_nonhead_queued_job_can_be_cancelled_without_changing_head_window(self):
        fixture = setup(branches=False)
        args, _, _, executor, controls, first, first_window = fixture
        second, _ = second_queued(fixture)
        trace = controls.cancel(args[4]["v"], second.job_id, "second-cancel", arrival=pos(20, 0))
        self.assertEqual(trace.job_id, second.job_id)
        self.assertEqual(args[2].scheduler.queued_job_ids(), (first.job_id,))
        self.assertIs(executor._windows[first.job_id], first_window)

    def test_replan_rejects_wrong_owner_arrival_domain_unknown_job_and_confirmed_start(self):
        fixture = setup(branches=False)
        executor, controls, job, old = fixture[3:]
        executor.frontier.seal_ingress(pos(25, 0))
        executor.frontier.confirm(pos(25, 0))
        wrong_domain = JobExecutionWindow(*(EventPosition("other", p.time_us, p.order)
            for p in (old.arrival, old.start, old.finish, old.release)))
        before = evidence(fixture)
        for job_id, window, owner, error in (
            (job.job_id, old, object(), PermissionError),
            (job.job_id, old, None, PermissionError),
            (job.job_id, replace(old, arrival=pos(16, 0)), controls._owner, ValueError),
            (job.job_id, wrong_domain, controls._owner, ValueError),
            (job.job_id, object(), controls._owner, ValueError),
            ("missing", old, controls._owner, ValueError),
            (job.job_id, replace(old, start=pos(25, 0)), controls._owner, EventOrderError),
            (job.job_id, replace(old, start=pos(24, 0)), controls._owner, EventOrderError),
        ):
            with self.subTest(job_id=job_id, window=window), self.assertRaises(error):
                executor.replan_queued_job(job_id, window, owner=owner)
            self.assertEqual(evidence(fixture), before)

    def test_replan_rejects_running_finished_cancelled_and_closed_owners(self):
        for phase in ("running", "finished", "cancelled", "closed"):
            fixture = setup(branches=False)
            args, _, runner, executor, controls, job, window = fixture
            if phase in ("running", "finished"):
                executor.frontier.seal_ingress(window.finish)
                if phase == "running":
                    args[5].begin_declared_next(executor, _workload_owner=runner._owner)
                else:
                    args[5].run_declared_next(executor, _workload_owner=runner._owner)
            elif phase == "cancelled":
                controls.cancel(args[4]["v"], job.job_id, "cancel", arrival=pos(20, 0))
            else:
                executor.close()
            before = evidence(fixture)
            with self.subTest(phase=phase), self.assertRaises((ValueError, RuntimeError)):
                executor.replan_queued_job(job.job_id, window, owner=controls._owner)
            self.assertEqual(evidence(fixture), before)


if __name__ == "__main__":
    unittest.main()
