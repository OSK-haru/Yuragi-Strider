"""Actual authenticated register jobs with explicit, finite trusted world traces."""

from dataclasses import replace
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import math
import unittest
from unittest.mock import patch

from core.attack_simulation.declared_execution import (
    DeclaredExecutionCapacityError, DeclaredQpuExecutor, JobExecutionWindow,
)
from core.attack_simulation.event_commit_boundary import EventPosition, UnresolvedIngressError
from core.attack_simulation.job_service import JobResultNotReadyError
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from tests.test_attack_workloads import fixture, plan


def pos(time, order=99):
    return EventPosition("world", time, order)


def setup(*, visits=1, max_events=16, idle=True):
    args, host, runner = fixture(plans=(plan(visits=visits),))
    model = IndependentIdleModel((IdleQubitSpec(math.inf, math.inf, 0),) * 2, 2) if idle else None
    executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=8,
        max_events_per_job=max_events, max_unresolved=8, idle_model=model)
    runner.start()
    host.value += 200_000
    dispatch = runner.dispatch_next()
    job = args[2].scheduler.get(dispatch.submission.job_id)
    arrival = runner.states()["plan-v"].events[0].arrival
    duration = executor._duration(job, args[1].read())
    window = JobExecutionWindow(arrival, pos(15, 2), pos(15 + duration, 3), pos(25 + duration, 4))
    executor.register_job(job.job_id, window)
    executor.frontier.set_target(pos(100))
    executor.frontier.seal_ingress(pos(100))  # Explicit finite-source assertion, NOT wall-clock inference.
    return args, host, runner, executor, job, window


class DeclaredExecutionTests(unittest.TestCase):
    def test_cancelled_before_window_can_publish_without_fabricated_qpu_execution(self):
        args, host, runner = fixture()
        executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=8,
            max_events_per_job=16, max_unresolved=8, idle_model=None)
        runner.start()
        host.value += 100_000
        job_id = runner.dispatch_next().submission.job_id
        args[5].request(args[4]["v"], lambda c, w: c.cancel(job_id),
            operation_id="job.cancel", _workload_owner=runner._owner)
        executor.frontier.set_target(pos(50))
        executor.register_cancelled_release(job_id, pos(60))
        with self.assertRaises(JobResultNotReadyError):
            runner.complete_result("plan-v", 1, available=pos(60))
        executor.frontier.set_target(pos(100))
        runner.complete_result("plan-v", 1, available=pos(60))
        self.assertTrue(runner.states()["plan-v"].finished)
        self.assertEqual(args[1].events(), ())
        self.assertIsNone(executor.frontier.snapshot().confirmed)
        with self.assertRaises(ValueError):
            executor.register_cancelled_release(job_id, pos(80))

    def test_nonzero_initial_device_origin_rejected_without_partial_binding(self):
        from core.attack_simulation.qpu_state import QpuStateStore, local_zero_snapshot
        from tests.test_attack_sessions import session_fixture
        import tests.test_attack_sessions as session_tests
        def nonzero(snapshot):
            return QpuStateStore(replace(snapshot, device_time_us=7))
        with patch.object(session_tests, "QpuStateStore", side_effect=nonzero):
            args = session_fixture()
        with self.assertRaises(ValueError):
            DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=8,
                max_events_per_job=16, max_unresolved=8, idle_model=None)
        self.assertFalse(args[1]._transaction_lock_bound)
        self.assertFalse(args[2].scheduler._transaction_lock_bound)
        self.assertIsNone(args[2]._declared_executor)

    def test_unexpected_start_owner_exception_restores_queued_head_and_idle(self):
        for phase in ("allocate", "running", "idle"):
            with self.subTest(phase=phase):
                args, host, runner, executor, job, window = setup()
                authority = args[1] if phase == "idle" else args[2].scheduler
                method = {"allocate": "allocate_next", "running": "mark_running", "idle": "commit_prepared"}[phase]
                original = getattr(authority, method)
                def interrupted(*a, **kw):
                    original(*a, **kw)
                    raise RuntimeError("host interruption during START")
                with patch.object(authority, method, side_effect=interrupted):
                    with self.assertRaises(RuntimeError):
                        executor.prepare_next()
                self.assertEqual(args[1].read().revision, 0)
                self.assertEqual(args[1].events(), ())
                self.assertEqual(args[2].scheduler.queued_job_ids(), (job.job_id,))
                self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.QUEUED)
                self.assertIsNone(executor.frontier.snapshot().confirmed)
                args[5].run_declared_next(executor, _workload_owner=runner._owner)
                self.assertEqual(executor.frontier.snapshot().confirmed, window.finish)

    def test_cancelled_only_history_rejects_assembly_without_partial_owner_claim(self):
        args, host, runner = fixture()
        runner.start()
        host.value += 100_000
        job = runner.dispatch_next().submission.job_id
        args[5].request(args[4]["v"], lambda c, w: c.cancel(job),
            operation_id="job.cancel", _workload_owner=runner._owner)
        with self.assertRaises(ValueError):
            DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=8,
                max_events_per_job=16, max_unresolved=8, idle_model=None)
        self.assertFalse(args[1]._transaction_lock_bound)
        self.assertFalse(args[2].scheduler._transaction_lock_bound)
        self.assertIsNone(args[2]._declared_executor)

    def test_queued_cancellation_has_no_physical_effect_and_keeps_declared_release(self):
        args, host, runner, executor, job, window = setup()
        args[5].request(args[4]["v"], lambda c, w: c.cancel(job.job_id),
            operation_id="job.cancel", _workload_owner=runner._owner)
        self.assertIsNone(args[5].run_declared_next(executor, _workload_owner=runner._owner))
        runner.complete_result("plan-v", 1, available=window.release)
        self.assertEqual(args[1].read().revision, 0)
        self.assertIsNone(executor.frontier.snapshot().confirmed)
        self.assertTrue(runner.states()["plan-v"].finished)

    def test_unexpected_owner_commit_exception_does_not_publish_half_finish(self):
        for phase in ("state", "job"):
            with self.subTest(phase=phase):
                args, host, runner, executor, job, window = setup()
                prepared = executor.prepare_next()
                baseline, events = args[1].read(), args[1].events()
                authority = args[1] if phase == "state" else args[2].scheduler
                method = "commit_prepared" if phase == "state" else "commit_prepared_success"
                commit = getattr(authority, method)
                def interrupted(token):
                    commit(token)
                    raise RuntimeError("host interruption after owner write")
                with patch.object(authority, method, side_effect=interrupted):
                    with self.assertRaises(RuntimeError):
                        executor.commit(prepared)
                self.assertIs(args[1].read(), baseline)
                self.assertEqual(args[1].events(), events)
                self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.RUNNING)
                self.assertEqual(executor.frontier.snapshot().confirmed, window.start)
                self.assertFalse(executor.result_is_available(job.job_id))
                executor.commit(prepared)
                self.assertEqual(executor.frontier.snapshot().confirmed, window.finish)

    def test_host_numeric_delay_preserves_physics_and_declared_trace(self):
        def trace(delayed):
            # Fixed synthetic simulator sampling salt, not a real credential.
            from core.attack_simulation.job_service import JobApplicationService
            initialize = JobApplicationService.__init__
            def fixed_sampling(service, *a, **kw):
                initialize(service, *a, **kw)
                service._sampling_salt = b"declared-trace-test" * 2
            with patch.object(JobApplicationService, "__init__", new=fixed_sampling):
                args, host, runner, executor, job, window = setup()
            original = SharedQpuRuntime._execute
            def execute(runtime, record):
                if delayed:
                    host.value += 9_000_000_000
                    self.assertGreater(runner.target().time_us, window.release.time_us)
                return original(runtime, record)
            with patch.object(SharedQpuRuntime, "_execute", new=execute):
                args[5].run_declared_next(executor, _workload_owner=runner._owner)
            result = args[5].request(args[4]["v"], lambda c, w: c.get_result(job.job_id),
                operation_id="job.result.read", _workload_owner=runner._owner)
            return (result.result, args[1].read().density_matrix, args[1].read().device_time_us,
                    [(e.event_kind, e.device_time_start_us, e.device_time_end_us) for e in args[1].events()],
                    executor.frontier.snapshot().confirmed, args[2].scheduler.get(job.job_id).status_history)
        self.assertEqual(trace(False), trace(True))

    def test_explicit_idle_channel_is_committed_with_start_not_just_a_timestamp(self):
        args, host, runner = fixture()
        executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=8,
            max_events_per_job=16, max_unresolved=8,
            idle_model=IndependentIdleModel((IdleQubitSpec(10, math.inf, 0.2),) * 2, 2))
        runner.start()
        host.value += 100_000
        job_id = runner.dispatch_next().submission.job_id
        job = args[2].scheduler.get(job_id)
        duration = executor._duration(job, args[1].read())
        window = JobExecutionWindow(pos(15, 1), pos(15, 2), pos(15 + duration, 3), pos(50))
        executor.register_job(job_id, window)
        executor.frontier.set_target(pos(100))
        executor.frontier.seal_ingress(pos(100))
        with patch.object(SharedQpuRuntime, "_execute", side_effect=RuntimeError("hold after START")):
            with self.assertRaises(RuntimeError):
                executor.prepare_next()
        population = 0.2 * (1 - math.exp(-15 / 10))
        self.assertAlmostEqual(args[1].read().density_matrix[3][3].real, population ** 2)
        self.assertEqual(args[1].read().device_time_us, 15)
        self.assertEqual(executor.frontier.snapshot().confirmed, window.start)

    def test_both_actor_inherit_jobs_keep_service_handover_reset(self):
        from tests.test_attack_sessions import OPERATIONS
        from core.attack_simulation.program_request import STATE_INHERIT_OPERATION_ID
        args, host, runner = fixture(plans=(plan("a", mode="inherit"), plan("v", at=30, mode="inherit")),
            initializations=("prepare", "inherit"), attacker_operations=OPERATIONS + (STATE_INHERIT_OPERATION_ID,))
        executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=8,
            max_events_per_job=16, max_unresolved=8,
            idle_model=IndependentIdleModel((IdleQubitSpec(math.inf, math.inf, 0),) * 2, 2))
        runner.start()
        host.value += 100_000
        dispatches = (runner.dispatch_next(), runner.dispatch_next())
        executor.frontier.set_target(pos(100))
        executor.frontier.seal_ingress(pos(100))
        for dispatch in dispatches:
            event = runner.states()[dispatch.producer_id].events[0]
            job = args[2].scheduler.get(dispatch.submission.job_id)
            duration = executor._duration(job, args[1].read())
            window = JobExecutionWindow(event.arrival, pos(event.arrival.time_us, 2),
                pos(event.arrival.time_us + duration, 3), pos(event.arrival.time_us + duration + 5, 4))
            executor.register_job(job.job_id, window)
            args[5].run_declared_next(executor, _workload_owner=runner._owner)
            runner.complete_result(dispatch.producer_id, 1, available=window.release)
        self.assertEqual([e.event_kind for e in args[1].events()],
            ["declared_idle", "program_shot", "program_shot", "declared_idle", "service_reset", "program_shot", "program_shot"])
        self.assertEqual(args[1].read().device_time_us, 38)
        self.assertTrue(all(s.finished for s in runner.states().values()))

    def test_common_session_atomic_finish_and_declared_result_release(self):
        args, host, runner, executor, job, window = setup(visits=2)
        executor.frontier.set_target(pos(100))
        # Nominal target cannot rewind; construct a second fixture for release gating.
        prepared = executor.prepare_next()
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.RUNNING)
        self.assertEqual(args[1].read().revision, 1)  # Declared idle is committed with START.
        self.assertEqual(executor.frontier.snapshot().confirmed, window.start)
        executor.commit(prepared)
        self.assertEqual(args[1].read().device_time_us, window.finish.time_us)
        self.assertEqual(executor.frontier.snapshot().confirmed, window.finish)
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.SUCCEEDED)
        before = runner.states()
        with self.assertRaisesRegex(ValueError, "committed service release"):
            runner.complete_result("plan-v", 1, available=pos(100))
        self.assertEqual(runner.states(), before)
        runner.complete_result("plan-v", 1, available=window.release)
        self.assertEqual(runner.states()["plan-v"].events[1].arrival.time_us, window.release.time_us + 10)
        with self.assertRaises(RuntimeError):
            args[5].run_next(_workload_owner=runner._owner)

    def test_result_not_published_merely_because_worker_completed(self):
        args, host, runner = fixture()
        executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=8,
            max_events_per_job=16, max_unresolved=8,
            idle_model=IndependentIdleModel((IdleQubitSpec(math.inf, math.inf, 0),) * 2, 2))
        runner.start()
        host.value += 100_000
        job_id = runner.dispatch_next().submission.job_id
        job = args[2].scheduler.get(job_id)
        duration = executor._duration(job, args[1].read())
        window = JobExecutionWindow(pos(15, 1), pos(15, 2), pos(15 + duration, 3), pos(100, 4))
        executor.register_job(job_id, window)
        executor.frontier.set_target(pos(50))
        executor.frontier.seal_ingress(pos(50))
        args[5].run_declared_next(executor, _workload_owner=runner._owner)
        with self.assertRaises(JobResultNotReadyError):
            runner.complete_result("plan-v", 1, available=window.release)
        self.assertIs(args[2].scheduler.get(job_id).status, JobStatus.SUCCEEDED)
        executor.frontier.set_target(pos(100, 4))
        runner.complete_result("plan-v", 1, available=window.release)
        self.assertTrue(runner.states()["plan-v"].finished)

    def test_declared_wait_without_model_and_event_capacity_fail_before_start(self):
        for idle, max_events, error in ((False, 16, ValueError), (True, 1, DeclaredExecutionCapacityError)):
            with self.subTest(idle=idle):
                args, host, runner, executor, job, window = setup(idle=idle, max_events=max_events)
                with self.assertRaises(error):
                    executor.prepare_next()
                self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.QUEUED)
                self.assertEqual(args[1].read().revision, 0)
                self.assertIsNone(executor.frontier.snapshot().confirmed)

    def test_unresolved_ingress_and_seal_block_before_physical_start(self):
        args, host, runner = fixture()
        executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=8,
            max_events_per_job=16, max_unresolved=8,
            idle_model=IndependentIdleModel((IdleQubitSpec(math.inf, math.inf, 0),) * 2, 2))
        runner.start()
        host.value += 100_000
        job = args[2].scheduler.get(runner.dispatch_next().submission.job_id)
        duration = executor._duration(job, args[1].read())
        window = JobExecutionWindow(pos(15, 1), pos(15, 2), pos(15 + duration, 3), pos(50))
        executor.register_job(job.job_id, window)
        executor.frontier.set_target(pos(100))
        ticket = executor.frontier.register_ingress(pos(10))
        with self.assertRaises(UnresolvedIngressError):
            executor.prepare_next()
        executor.frontier.seal_ingress(pos(100))
        with self.assertRaises(UnresolvedIngressError):
            executor.prepare_next()
        self.assertEqual(args[1].read().revision, 0)
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.QUEUED)
        executor.frontier.resolve_ingress(ticket)
        args[5].run_declared_next(executor, _workload_owner=runner._owner)
        self.assertEqual(executor.frontier.snapshot().confirmed, window.finish)

    def test_numeric_exception_is_pending_infrastructure_not_physical_failure(self):
        args, host, runner, executor, job, window = setup()
        with patch.object(SharedQpuRuntime, "_execute", side_effect=RuntimeError("host calculation interrupted")):
            with self.assertRaises(RuntimeError):
                args[5].run_declared_next(executor, _workload_owner=runner._owner)
        self.assertEqual(args[1].read().revision, 1)
        self.assertEqual([e.event_kind for e in args[1].events()], ["declared_idle"])
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.RUNNING)
        self.assertEqual(executor.frontier.snapshot().confirmed, window.start)
        with self.assertRaises(ValueError):
            args[2].cancel(job.job_id)
        args[5].run_declared_next(executor, _workload_owner=runner._owner)  # Explicit same-run retry.
        self.assertEqual(len(args[1].events()), 3)
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.SUCCEEDED)

    def test_disposal_during_numeric_work_fences_all_final_publication(self):
        args, host, runner, executor, job, window = setup()
        entered, release, fenced = Event(), Event(), Event()
        execute, revoke = SharedQpuRuntime._execute, args[3].close
        def paused(runtime, record):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("test barrier timed out")
            return execute(runtime, record)
        def revoked():
            revoke()
            fenced.set()
        with patch.object(SharedQpuRuntime, "_execute", new=paused), \
                patch.object(args[3], "close", side_effect=revoked), ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(args[5].run_declared_next, executor, _workload_owner=runner._owner)
            try:
                self.assertTrue(entered.wait(2))
                ended = pool.submit(args[6].discard, "world")
                self.assertTrue(fenced.wait(2))
            finally:
                release.set()
            with self.assertRaises((RuntimeError, PermissionError)):
                running.result(timeout=5)
            ended.result(timeout=5)
        self.assertEqual(args[1].read().revision, 1)
        self.assertEqual([e.event_kind for e in args[1].events()], ["declared_idle"])
        self.assertIs(args[2].scheduler.get(job.job_id).status, JobStatus.RUNNING)
        self.assertEqual(executor.frontier.snapshot().confirmed, window.start)
        self.assertTrue(executor.frontier.snapshot().closed)

    def test_forged_or_stale_job_result_cannot_partially_commit_qpu(self):
        args, host, runner, executor, job, window = setup()
        prepared = executor.prepare_next()
        with self.assertRaises(ValueError):
            executor.commit(replace(prepared))
        self.assertEqual(args[1].read().revision, 1)
        args[2].scheduler.mark_internal_error(job.job_id, "TEST_SUPERVISOR_INVALIDATION")
        with self.assertRaises((ValueError, RuntimeError)):
            executor.commit(prepared)
        self.assertEqual(args[1].read().revision, 1)
        self.assertEqual(executor.frontier.snapshot().confirmed, window.start)


if __name__ == "__main__":
    unittest.main()
