"""Timed common cancellation and bounded unpublished transaction compensation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import math
from threading import Barrier
import unittest
from unittest.mock import patch

from api.attack_controls import DeclaredControlService
from api.attack_world import ClosedWorkloadWorld
from api.attack_workloads import DeclaredWorkloadRunner
from core.attack_simulation.declared_execution import DeclaredQpuExecutor, JobExecutionWindow
from core.attack_simulation.event_commit_boundary import EventOrderError, EventPosition
from core.attack_simulation.job_service import JobNotFoundError, JobResultNotReadyError
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.scenario_policy import ScenarioMode
from core.attack_simulation.victim_workload import (
    Branch, Outcome, Submission, begin_dispatch, bind_submission, start_workload,
)
from core.attack_simulation.workload_config import serialize_workload_plan
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from tests.test_attack_sessions import session_fixture
from tests.test_attack_workloads import Host, plan
from tests.test_live_service_config import LIVE, CONTROL


def pos(time_us, order=99):
    return EventPosition("world", time_us, order)


def assembly(*, mode=ScenarioMode.FREE, branches=True):
    definition = plan(visits=2 if branches else 1)
    if branches:
        definition = replace(definition, steps=(replace(definition.steps[0], branches=(
            Branch(Outcome.SUCCEEDED, "step"), Branch(Outcome.CANCELLED, "step"))),))
    args = session_fixture(max_jobs=8, victim_workload=serialize_workload_plan(definition),
        world_clock={"clock_domain": "world", "rate": 1}, scenario_mode=mode,
        service_timing={"processing_delay_us": 0, "publication_delay_us": 3,
                        "denial_delay_us": 2, "basis": "Test-only explicit durations."},
        live_ingress=LIVE, control_timing=CONTROL)
    host = Host()
    runner = DeclaredWorkloadRunner(session=args[5], credentials={"v": args[4]["v"]},
        run_id="run", max_pending=16, max_history=32, host_ns=host)
    executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=8,
        max_events_per_job=32, max_unresolved=16,
        idle_model=IndependentIdleModel((IdleQubitSpec(math.inf, math.inf, 0),) * 2, 2))
    controls = DeclaredControlService(runner=runner, executor=executor)
    return args, host, runner, executor, controls


def setup(**kwargs):
    args, host, runner, executor, controls = assembly(**kwargs)
    runner.start()
    host.value += 100_000
    submitted = runner.dispatch_next().submission
    job = args[2].scheduler.get(submitted.job_id)
    event = runner.states()["plan-v"].events[0]
    window = JobExecutionWindow(event.arrival, pos(40, 0),
        pos(40 + executor._duration(job, args[1].read()), 3), pos(200, 4))
    executor.register_job(job.job_id, window)
    executor.frontier.set_target(pos(1000))
    if kwargs.get("branches", True):
        # Deliberately obsolete success-only proof; no live driver may rely on
        # such a proof before it accounts for earlier cancellation requests.
        runner._book.refine_barrier("plan-v", 1, pos(210, 0))
    return args, host, runner, executor, controls, job, window


def cancel(fixture, **kwargs):
    args, _, _, _, controls, job, _ = fixture
    return controls.cancel(args[4]["v"], job.job_id, "cancel-one", arrival=pos(20, 0), **kwargs)


def evidence(fixture):
    args, _, runner, executor, _, job, _ = fixture
    return (args[2].scheduler.get(job.job_id), args[2].scheduler.queued_job_ids(),
            runner._book.snapshot(), dict(executor._windows), dict(executor._releases),
            dict(executor._cancellations), dict(executor._control_requests),
            args[1].read(), args[1].events(), executor.frontier.snapshot())


class DeclaredControlTests(unittest.TestCase):
    def test_cancel_replaces_success_release_and_uses_common_cancel_branch_in_both_modes(self):
        for mode in ScenarioMode:
            fixture = setup(mode=mode)
            args, _, runner, executor, _, job, window = fixture
            trace = cancel(fixture)
            self.assertEqual((trace.application, trace.release), (pos(22, 0), pos(25, 0)))
            self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.CANCELLED)
            self.assertNotIn(job.job_id, executor._windows)
            self.assertEqual(executor._releases[job.job_id], trace.release)
            self.assertEqual(runner._book.snapshot().barriers[0].earliest_next_arrival, pos(35, 0))
            runner.complete_result("plan-v", 1, available=trace.release)
            next_event = runner.states()["plan-v"].events[1]
            self.assertEqual(next_event.arrival.time_us, 35)
            self.assertGreaterEqual(next_event.arrival.key, pos(35, 0).key)
            self.assertLess(next_event.arrival.time_us, window.release.time_us)
            self.assertIs(runner.states()["plan-v"].events[0].outcome, Outcome.CANCELLED)
            self.assertEqual(args[1].events(), ())
            self.assertEqual(args[1].read().revision, 0)
            self.assertIsNone(executor.frontier.snapshot().confirmed)
            with self.assertRaises(RuntimeError):
                args[0].set_assumption(None)

    def test_historical_status_and_public_result_obey_cancel_application_and_release(self):
        fixture = setup()
        args, _, _, executor, _, job, window = fixture
        trace = cancel(fixture)
        service = args[5]._service
        for at in (window.arrival, pos(21), pos(22, 0)):
            expected = JobStatus.CANCELLED if at == trace.application else JobStatus.QUEUED
            self.assertIs(service._get_status_at("v", job.job_id, at).status, expected)
            with self.assertRaises(JobResultNotReadyError):
                service._get_result_at("v", job.job_id, at)
        result = service._get_result_at("v", job.job_id, trace.release)
        self.assertIs(result.status, JobStatus.CANCELLED)
        self.assertIsNone(result.result)
        with self.assertRaises(JobNotFoundError):
            service._get_status_at("v", job.job_id, pos(14))
        executor.frontier._target = trace.application  # Test a still-unreleased reply.
        with self.assertRaises(JobResultNotReadyError):
            service.get_result("v", job.job_id)

    def test_no_result_dependency_is_a_valid_common_operation(self):
        fixture = setup(branches=False)
        trace = cancel(fixture)
        self.assertEqual(fixture[2]._book.snapshot().barriers, ())
        self.assertEqual(trace.release, pos(25, 0))

    def test_exact_replay_after_result_delivery_does_not_rewrite_later_barriers(self):
        fixture = setup()
        args, _, runner, executor, _, job, _ = fixture
        trace = cancel(fixture)
        runner.complete_result("plan-v", 1, available=trace.release)
        runner.dispatch_next()
        before = evidence(fixture)
        self.assertIs(cancel(fixture), trace)
        self.assertEqual(evidence(fixture), before)
        with self.assertRaises(ValueError):
            fixture[4].cancel(args[4]["v"], job.job_id, "cancel-one", arrival=pos(21, 0))
        later_id = runner.states()["plan-v"].events[1].job_id
        with self.assertRaises(ValueError):
            fixture[4].cancel(args[4]["v"], later_id, "cancel-one", arrival=pos(20, 0))
        self.assertEqual(evidence(fixture), before)

    def test_actor_tenant_grants_and_credentials_are_not_bypassed(self):
        fixture = setup()
        args, _, _, executor, controls, job, _ = fixture
        before = evidence(fixture)
        with self.assertRaises(JobNotFoundError):
            controls.cancel(args[4]["a"], job.job_id, "foreign", arrival=pos(20))
        with self.assertRaises(PermissionError):
            controls.cancel("not-issued", job.job_id, "foreign", arrival=pos(20))
        with self.assertRaises(PermissionError):
            args[5]._service.cancel("v", job.job_id)
        with self.assertRaises(PermissionError):
            executor.apply_cancellation(owner=object(), principal_id="v", request_id="foreign",
                job_id=job.job_id, arrival=pos(20), application=pos(22), release=pos(25), book=fixture[2]._book)
        self.assertEqual(evidence(fixture), before)

    def test_start_has_priority_at_its_declared_position_even_if_host_has_not_started_it(self):
        fixture = setup()
        args, _, _, _, controls, job, window = fixture
        before = evidence(fixture)
        for arrival in (pos(38, 2), pos(40, 0), pos(41, 0)):
            with self.assertRaises(ValueError):
                controls.cancel(args[4]["v"], job.job_id, "late", arrival=arrival)
            self.assertEqual(evidence(fixture), before)
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.QUEUED)
        self.assertEqual(window.start.time_us, 40)

    def test_cutoff_grant_retirement_and_physical_seal_cannot_be_undone(self):
        for boundary in ("cutoff", "grant", "retired", "physical_seal"):
            fixture = setup()
            _, _, runner, executor, _, _, _ = fixture
            book = runner._book
            if boundary == "cutoff":
                book.confirm_admission_cutoff(pos(50))
            elif boundary in {"grant", "retired"}:
                token = book.reserve("z-source", 1, pos(50))
                self.assertIs(book.next_due(pos(50)), token)
                if boundary == "retired":
                    book.retire(token, decision="admitted")
            else:
                executor.frontier.seal_ingress(pos(50))
            before = evidence(fixture)
            with self.subTest(boundary=boundary), self.assertRaises(EventOrderError):
                cancel(fixture)
            self.assertEqual(evidence(fixture), before)

    def test_outcome_future_domain_backdate_and_close_reject_without_partial_effects(self):
        fixture = setup()
        args, _, _, executor, controls, job, _ = fixture
        before = evidence(fixture)
        for arrival in (pos(10), EventPosition("other", 20, 0), pos(1001)):
            with self.assertRaises((ValueError, JobNotFoundError, RuntimeError)):
                controls.cancel(args[4]["v"], job.job_id, "invalid", arrival=arrival)
            self.assertEqual(evidence(fixture), before)
        args[6].discard("world")
        with self.assertRaises(PermissionError):
            cancel(fixture)
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.QUEUED)
        self.assertEqual(args[1].events(), ())
        self.assertFalse(executor._cancellations)

    def test_transaction_compensates_only_unpublished_cancel_for_injected_owner_failures(self):
        for phase in ("transition", "cancel_return", "dependency", "window"):
            fixture = setup()
            args, _, runner, executor, _, _, _ = fixture
            before = evidence(fixture)
            if phase == "window":
                class FailOnce(dict):
                    def __delitem__(self, key):
                        super().__delitem__(key)
                        raise RuntimeError("test window write interruption")
                original = executor._windows
                executor._windows = FailOnce(original)
                with self.assertRaises(RuntimeError):
                    cancel(fixture)
                executor._windows = dict(executor._windows)
            else:
                owner = runner._book if phase == "dependency" else args[2].scheduler
                method = {"transition": "_transition", "cancel_return": "cancel",
                          "dependency": "commit_barrier_replacement"}[phase]
                original = getattr(owner, method)
                def interrupted(*args, **kwargs):
                    original(*args, **kwargs)
                    raise RuntimeError("test unpublished owner interruption")
                with patch.object(owner, method, side_effect=interrupted), self.assertRaises(RuntimeError):
                    cancel(fixture)
            self.assertEqual(evidence(fixture), before)
            self.assertIs(cancel(fixture).release, executor._releases[before[0].job_id])

    def test_start_then_cancel_leaves_confirmed_physics_and_reset_unchanged(self):
        fixture = setup(branches=False)
        args, _, runner, executor, _, job, window = fixture
        executor.frontier.seal_ingress(window.start)
        args[5].begin_declared_next(executor, _workload_owner=runner._owner)
        before = evidence(fixture)
        with self.assertRaises(EventOrderError):
            cancel(fixture)
        self.assertEqual(evidence(fixture), before)
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.RUNNING)

    def test_cancel_and_discard_race_has_no_partial_job_trace_or_dependency(self):
        fixture = setup()
        args, _, runner, executor, _, job, _ = fixture
        barrier = Barrier(2, timeout=5)
        def attempt():
            barrier.wait()
            try:
                return cancel(fixture)
            except PermissionError:
                return None
        def discard():
            barrier.wait()
            args[6].discard("world")
        with ThreadPoolExecutor(max_workers=2) as pool:
            result, disposal = pool.submit(attempt), pool.submit(discard)
            applied = result.result(timeout=5)
            disposal.result(timeout=5)
        actual = args[2].scheduler.get(job.job_id).status
        self.assertIs(actual, JobStatus.CANCELLED if applied is not None else JobStatus.QUEUED)
        self.assertEqual(job.job_id in executor._cancellations, applied is not None)
        expected = pos(35, 0) if applied is not None else pos(210, 0)
        self.assertEqual(runner._book.snapshot().barriers[0].earliest_next_arrival, expected)
        self.assertEqual(args[1].events(), ())

    def test_closed_submit_only_world_cannot_claim_control_capable_executor(self):
        _, _, runner, executor, _ = assembly()
        with self.assertRaises(ValueError):
            ClosedWorkloadWorld(runner=runner, executor=executor)
        self.assertIsNone(runner._coordinator_owner)

    def test_successor_timing_does_not_depend_on_late_host_cancellation_processing(self):
        outputs = []
        for target in (pos(30), pos(1e7)):
            fixture = setup()
            args, host, runner, executor, _, _, _ = fixture
            executor.frontier._target = target
            host.value += int(target.time_us * 1000)
            calls = host.calls
            trace = cancel(fixture)
            self.assertEqual(host.calls, calls)
            runner.complete_result("plan-v", 1, available=trace.release)
            outputs.append((trace.application, trace.release, runner.states()["plan-v"].events[1].arrival,
                            args[1].read(), args[1].events()))
        self.assertEqual(*outputs)

    def test_all_dependencies_of_a_shared_job_are_replaced_or_compensated_together(self):
        for fail_second in (False, True):
            fixture = setup()
            _, _, runner, executor, _, job, _ = fixture
            original = runner.states()["plan-v"]
            definition = replace(original.plan, plan_id="plan-v-shadow")
            state = start_workload(definition, run_id="shadow")
            book = runner._book
            book.publish(definition.plan_id, state)
            token = book.next_due(pos(100))
            state = begin_dispatch(state, token.event_id, through=pos(100))
            book.publish(definition.plan_id, state)
            state = bind_submission(state, token.event_id, Submission(job.job_id))
            book.retire(token, state)
            before = evidence(fixture)
            commit = book.commit_barrier_replacement
            calls = 0

            def interrupt_second(*args):
                nonlocal calls
                calls += 1
                result = commit(*args)
                if fail_second and calls == 2:
                    raise RuntimeError("test second dependency interruption")
                return result

            with patch.object(book, "commit_barrier_replacement", side_effect=interrupt_second):
                if fail_second:
                    with self.assertRaises(RuntimeError):
                        cancel(fixture)
                    self.assertEqual(evidence(fixture), before)
                else:
                    cancel(fixture)
                    self.assertEqual(calls, 2)
                    self.assertTrue(all(b.earliest_next_arrival == pos(35, 0) for b in book.snapshot().barriers))
            if fail_second:
                self.assertEqual(cancel(fixture).release, pos(25, 0))

    def test_missing_foreign_or_arbitrary_successor_replacement_rejects_before_cancel(self):
        fixture = setup()
        _, _, runner, executor, controls, job, _ = fixture
        book = runner._book
        barrier = book.snapshot().barriers[0]
        before = evidence(fixture)
        for replacements in ((), (book.prepare_barrier_replacement(barrier, pos(34)),),
                             (book.prepare_barrier_replacement(barrier, pos(36)),)):
            with self.assertRaises(ValueError):
                executor.apply_cancellation(owner=controls._owner, principal_id="v", request_id="missing",
                    job_id=job.job_id, arrival=pos(20, 0), application=pos(22, 0), release=pos(25, 0),
                    book=book, replacements=replacements)
            self.assertEqual(evidence(fixture), before)

    def test_prepared_dependency_capability_is_exact_cas_and_revalidates_after_cutoff(self):
        fixture = setup()
        _, _, runner, _, _, job, _ = fixture
        book = runner._book
        old = book.snapshot().barriers[0]
        token = book.prepare_barrier_replacement(old, book.cancellation_successor_bound(old, job.job_id, pos(25, 0)))
        with self.assertRaises(ValueError):
            book.commit_barrier_replacement(replace(token))
        book.refine_barrier(old.producer_id, old.producer_event_id, pos(211))
        with self.assertRaises(ValueError):
            book.commit_barrier_replacement(token)
        current = book.snapshot().barriers[0]
        token = book.prepare_barrier_replacement(current, pos(35))
        book.confirm_admission_cutoff(pos(50))
        with self.assertRaises(EventOrderError):
            book.commit_barrier_replacement(token)
        self.assertIs(book.snapshot().barriers[0], current)

    def test_final_cancel_checkpoint_cannot_undo_an_already_published_outcome(self):
        fixture = setup()
        cancel(fixture)
        before = evidence(fixture)
        with self.assertRaises(ValueError):
            fixture[0][2].scheduler._rollback_declared_cancel(fixture[5].job_id)
        self.assertEqual(evidence(fixture), before)

    def test_closed_only_refinement_and_sealing_are_fenced_for_control_worlds(self):
        fixture = setup()
        _, _, runner, executor, _, _, window = fixture
        before = evidence(fixture)
        with self.assertRaises(PermissionError):
            runner.refine_result_bound("plan-v", 1, available=window.release, _coordinator_owner=None)
        with self.assertRaises(PermissionError):
            runner.seal_physical_through(executor, pos(50), _coordinator_owner=None)
        self.assertEqual(evidence(fixture), before)

    def test_control_arrival_cannot_cross_boundaries_even_without_successors(self):
        for boundary in ("cutoff", "seal", "grant", "retired"):
            fixture = setup(branches=False)
            _, _, runner, executor, _, _, _ = fixture
            if boundary == "cutoff":
                runner._book.confirm_admission_cutoff(pos(50))
            elif boundary == "seal":
                executor.frontier.seal_ingress(pos(50))
            else:
                token = runner._book.reserve("z-source", 1, pos(50))
                runner._book.next_due(pos(50))
                if boundary == "retired":
                    runner._book.retire(token, decision="admitted")
            before = evidence(fixture)
            with self.subTest(boundary=boundary), self.assertRaises(EventOrderError):
                cancel(fixture)
            self.assertEqual(evidence(fixture), before)

    def test_timed_control_does_not_allow_release_only_evidence_or_setting_replacement(self):
        fixture = setup(branches=False)
        args, _, _, executor, controls, job, _ = fixture
        config = controls.config
        with self.assertRaises(AttributeError):
            controls.config = config
        args[2].scheduler.cancel(job.job_id)  # Deliberately bypass the common service internally.
        with self.assertRaises(PermissionError):
            executor.register_cancelled_release(job.job_id, pos(25))
        self.assertFalse(executor.result_is_available(job.job_id))
        with self.assertRaises(JobResultNotReadyError):
            args[5]._service.get_result("v", job.job_id)


if __name__ == "__main__":
    unittest.main()
