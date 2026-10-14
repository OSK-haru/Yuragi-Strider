"""Actual common-session admission, explicit simulated release, no live QPU clock."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
import unittest
from unittest.mock import patch

from api.attack_sessions import SessionBusyError, SessionCapacityError, _LimitedClient
from api.attack_workloads import DeclaredWorkloadRunner, WorkloadInfrastructureError
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.program_request import CircuitProgram
from core.attack_simulation.victim_workload import (
    Branch, DeclaredDurations, Outcome, Phase, RegisteredProgram, TimedStart, WorkloadPlan, WorkloadStep,
)
from core.attack_simulation.workload_config import serialize_workload_plan
from tests.test_attack_sessions import OPERATIONS, session_fixture
from tests.test_local_program_runtime import program_json


class Host:
    def __init__(self):
        self.value = 1_000_000_000
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


def plan(actor="v", *, at=10, visits=1, mode="prepare"):
    program = RegisteredProgram("two", CircuitProgram.from_editor_json(program_json(2)))
    branches = (Branch(Outcome.SUCCEEDED, "step"),) if visits > 1 else ()
    step = WorkloadStep("step", "two", "qpu-1", 2, DeclaredDurations(2, 3, 5),
                        visits, branches, initialization=mode)
    return WorkloadPlan("plan-" + actor, actor, "world", (program,), (step,),
                        (TimedStart("step", at),), visits, 1)


def fixture(*, plans=None, max_jobs=8, attacker_operations=OPERATIONS, initializations=("prepare",)):
    plans = plans or (plan(),)
    by_actor = {p.actor_id: serialize_workload_plan(p) for p in plans}
    args = session_fixture(max_jobs=max_jobs, attacker_operations=attacker_operations,
        initializations=initializations, attacker_workload=by_actor.get("a"), victim_workload=by_actor.get("v"),
        world_clock={"clock_domain": "world", "rate": 1.0})
    host = Host()
    runner = DeclaredWorkloadRunner(session=args[5], credentials={p.actor_id: args[4][p.actor_id] for p in plans},
                                    run_id="run", max_pending=8, max_history=16, host_ns=host)
    return args, host, runner


def position(time):
    return EventPosition("world", time, 99)


class AttackWorkloadTests(unittest.TestCase):
    def test_denied_result_cannot_advance_after_discard_or_authentication_loss(self):
        definition = plan("a", visits=2)
        definition = replace(definition, steps=(replace(definition.steps[0],
                             branches=(Branch(Outcome.DENIED, "step"),)),))
        for discard in (True, False):
            with self.subTest(discard=discard):
                args, host, runner = fixture(plans=(definition,), attacker_operations=())
                runner.start()
                host.value += 100_000
                runner.dispatch_next()
                state, ingress = runner.states(), runner.ingress_snapshot()
                if discard:
                    args[6].discard("world")
                else:
                    args[3].close()
                with self.assertRaises(WorkloadInfrastructureError):
                    runner.complete_result("plan-a", 1, available=position(20))
                self.assertEqual(runner.states(), state)
                self.assertEqual(runner.ingress_snapshot(), ingress)
                self.assertEqual(args[1].read().revision, 0)

    def test_disposal_between_result_read_and_publish_fences_all_outcomes(self):
        from api.attack_workloads import advance_result
        for denied in (True, False):
            with self.subTest(denied=denied):
                definition = plan("a", visits=2)
                if denied:
                    definition = replace(definition, steps=(replace(definition.steps[0],
                        branches=(Branch(Outcome.DENIED, "step"),)),))
                args, host, runner = fixture(plans=(definition,),
                    attacker_operations=() if denied else OPERATIONS)
                runner.start()
                host.value += 100_000
                runner.dispatch_next()
                if not denied:
                    args[5].run_next(_workload_owner=runner._owner)
                state, ingress = runner.states(), runner.ingress_snapshot()
                entered, release = Event(), Event()
                def paused(*a, **kw):
                    entered.set()
                    if not release.wait(4):
                        raise RuntimeError("test barrier timed out")
                    return advance_result(*a, **kw)
                with patch("api.attack_workloads.advance_result", side_effect=paused), \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    advancing = pool.submit(runner.complete_result, "plan-a", 1, available=position(100))
                    try:
                        self.assertTrue(entered.wait(2))
                        args[6].discard("world")
                    finally:
                        release.set()
                    with self.assertRaises(WorkloadInfrastructureError):
                        advancing.result(timeout=5)
                self.assertEqual(runner.states(), state)
                self.assertEqual(runner.ingress_snapshot(), ingress)

    def test_preflight_compile_does_not_hold_session_disposal_lock(self):
        args = session_fixture(victim_workload=serialize_workload_plan(plan()),
                               world_clock={"clock_domain": "world", "rate": 1})
        host, entered, release, fenced = Host(), Event(), Event(), Event()
        compiler_type = type(args[5]._service._program_compiler)
        compile_program, revoke = compiler_type.compile, args[3].close
        def paused(compiler, *a, **kw):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("test barrier timed out")
            return compile_program(compiler, *a, **kw)
        def revoked():
            revoke()
            fenced.set()
        with patch.object(compiler_type, "compile", new=paused), \
                patch.object(args[3], "close", side_effect=revoked), ThreadPoolExecutor(max_workers=2) as pool:
            constructing = pool.submit(DeclaredWorkloadRunner, session=args[5],
                credentials={"v": args[4]["v"]}, run_id="run", max_pending=8, max_history=16, host_ns=host)
            try:
                self.assertTrue(entered.wait(2))
                closing = pool.submit(args[6].discard, "world")
                self.assertTrue(fenced.wait(2))
                closing.result(timeout=2)
            finally:
                release.set()
            with self.assertRaises(ValueError):
                constructing.result(timeout=5)
        self.assertEqual(host.calls, 0)
        self.assertIsNone(args[5]._workload_owner)
        self.assertEqual(args[1].read().revision, 0)
        self.assertEqual(args[2].scheduler.queued_job_ids(), ())

    def test_own_result_barrier_holds_other_actor_until_declared_successor_is_known(self):
        args, host, runner = fixture(plans=(plan("a", at=30), plan("v", at=10, visits=2)))
        runner.start()
        host.value += 500_000
        first = runner.dispatch_next()
        self.assertEqual(first.producer_id, "plan-v")
        self.assertIsNone(runner.dispatch_next())  # Unknown successor lower bound25 precedes35.
        args[5].run_next(_workload_owner=runner._owner)
        runner.complete_result("plan-v", 1, available=position(100))
        second, third = runner.dispatch_next(), runner.dispatch_next()
        self.assertEqual((second.producer_id, third.producer_id), ("plan-a", "plan-v"))
        self.assertEqual(args[2].scheduler.queued_job_ids(),
                         (second.submission.job_id, third.submission.job_id))
        self.assertEqual(runner.states()["plan-v"].events[1].arrival.time_us, 110)

    def test_expired_authentication_preserves_reservation_without_simulated_denial(self):
        args, host, runner = fixture()
        runner.start()
        host.value += 100_000
        args[3].close()
        with self.assertRaises(WorkloadInfrastructureError):
            runner.dispatch_next()
        event = runner.states()["plan-v"].events[0]
        self.assertIs(event.phase, Phase.DISPATCHING)
        self.assertIsNone(event.outcome)
        self.assertEqual(args[2].scheduler.queued_job_ids(), ())
        self.assertEqual(args[1].read().revision, 0)

    def test_inherit_uses_same_real_session_path_for_both_actors(self):
        from core.attack_simulation.program_request import STATE_INHERIT_OPERATION_ID
        args, host, runner = fixture(plans=(plan("a", mode="inherit"), plan("v", at=20, mode="inherit")),
            initializations=("prepare", "inherit"), attacker_operations=OPERATIONS + (STATE_INHERIT_OPERATION_ID,))
        runner.start()
        host.value += 100_000
        first, second = runner.dispatch_next(), runner.dispatch_next()
        self.assertEqual((first.producer_id, second.producer_id), ("plan-a", "plan-v"))
        for dispatch in (first, second):
            command = args[2].scheduler.get(dispatch.submission.job_id).command
            self.assertEqual(command.operation.initialization, "inherit")
            self.assertEqual([i.kind for i in command.operation.instructions], ["measure", "measure"])
        with patch.object(args[2].reset_models, "run_register", wraps=args[2].reset_models.run_register) as resets:
            args[5].run_next(_workload_owner=runner._owner)
            args[5].run_next(_workload_owner=runner._owner)
            self.assertEqual(resets.call_count, 1)  # Only tenant handover.
        self.assertEqual(args[1].read().revision, 5)

    def test_real_asgi_candidate_cannot_mix_unmapped_host_requests_into_declared_world(self):
        import asyncio
        from fastapi import FastAPI
        from api.attack_http import create_attack_router
        from tests.test_attack_http_adapter import _request
        args, host, runner = fixture()
        runner.start()
        app = FastAPI()
        app.include_router(create_attack_router(args[6]))
        response = asyncio.run(_request(app, "GET", "/api/attack/sessions/world/devices", credential=args[4]["v"]))
        self.assertEqual(response, (403, {"detail": {"code": "OPERATION_DENIED"}}))
        self.assertEqual(args[5].receipt_records(), ())
        self.assertEqual(args[1].read().revision, 0)

    def test_frozen_manifest_ready_start_and_no_untracked_http_mixing(self):
        args, host, runner = fixture()
        self.assertEqual(host.calls, 0)
        self.assertEqual(args[1].read().revision, 0)
        self.assertEqual(runner.states()["plan-v"].events[0].arrival.time_us, 15)
        with self.assertRaises(RuntimeError):
            runner.dispatch_next()
        host.value += 50_000_000  # Assembly time never becomes world elapsed.
        self.assertEqual(runner.start(), EventPosition("world", 0, 0))
        self.assertIsNone(runner.dispatch_next())
        for callback in (lambda: args[5].receive(args[4]["v"], "program.submit"),
                         lambda: args[5].request(args[4]["v"], lambda c, w: c.list_devices()),
                         args[5].run_next):
            with self.assertRaises(PermissionError):
                callback()
        with self.assertRaises(ValueError):
            args[5].claim_workload_runner(object())
        with self.assertRaises(RuntimeError):
            runner.start()

    def test_two_actors_are_admitted_by_declared_arrival_not_plan_or_callback_order(self):
        args, host, runner = fixture(plans=(plan("a", at=30), plan("v", at=10)))
        runner.start()
        host.value += 100_000
        first, second = runner.dispatch_next(), runner.dispatch_next()
        self.assertEqual((first.producer_id, second.producer_id), ("plan-v", "plan-a"))
        self.assertEqual(args[2].scheduler.queued_job_ids(),
                         (first.submission.job_id, second.submission.job_id))
        for _ in range(2):  # Explicit untimed supervisor smoke test, NOT world commit.
            self.assertEqual(args[5].run_next(_workload_owner=runner._owner).status.value, "succeeded")
        runner.complete_result("plan-v", 1, available=position(100))
        runner.complete_result("plan-a", 1, available=position(200))
        self.assertTrue(all(s.finished for s in runner.states().values()))
        self.assertEqual(args[1].read().revision, 5)  # Four shots plus one service handover.
        for record in args[5].receipt_records():
            self.assertEqual(record.receipt.clock_domain, "host_monotonic_ns")
        self.assertEqual(runner.states()["plan-v"].events[0].arrival.time_us, 15)

    def test_result_dependency_keeps_declared_timing_after_host_compile_delay(self):
        def trace(delayed):
            args, host, runner = fixture(plans=(plan(visits=2),))
            runner.start()
            host.value += 100_000
            compile_program = args[5]._service._prepare_program
            def compile_slow(*a, **kw):
                if delayed:
                    host.value += 9_000_000_000
                return compile_program(*a, **kw)
            with patch.object(args[5]._service, "_prepare_program", side_effect=compile_slow):
                runner.dispatch_next()
            self.assertIsNone(runner.dispatch_next())
            args[5].run_next(_workload_owner=runner._owner)
            runner.complete_result("plan-v", 1, available=position(100))
            host.value += 100_000
            later = runner.dispatch_next()
            args[5].run_next(_workload_owner=runner._owner)
            runner.complete_result("plan-v", 2, available=position(200))
            state = runner.states()["plan-v"]
            result = args[5].request(args[4]["v"], lambda c, w: c.get_result(later.submission.job_id),
                                     operation_id="job.result.read", _workload_owner=runner._owner)
            return [e.arrival.time_us for e in state.events], result.result, args[1].read().density_matrix, args[1].read().device_time_us
        self.assertEqual(trace(True), trace(False))
        self.assertEqual(trace(False)[0], [15, 110])

    def test_response_exception_retries_same_real_job_without_duplicate_admission(self):
        args, host, runner = fixture(max_jobs=1)
        runner.start()
        host.value += 100_000
        submit = _LimitedClient.submit_program
        def lose_response(client, request):
            submit(client, request)
            raise RuntimeError("response projection lost")
        with patch.object(_LimitedClient, "submit_program", new=lose_response):
            with self.assertRaises(RuntimeError):
                runner.dispatch_next()
        before = args[2].scheduler.queued_job_ids()
        self.assertEqual(len(before), 1)
        self.assertIs(runner.states()["plan-v"].events[0].phase, Phase.DISPATCHING)
        replay = runner.dispatch_next()
        self.assertEqual(before, (replay.submission.job_id,))
        self.assertEqual(args[2].scheduler.queued_job_ids(), before)
        self.assertIsNone(runner.dispatch_next())
        self.assertEqual(args[1].read().revision, 0)

    def test_grant_denial_and_host_capacity_are_different_outcomes(self):
        args, host, runner = fixture(plans=(plan("a"),), attacker_operations=())
        runner.start()
        host.value += 100_000
        dispatch = runner.dispatch_next()
        self.assertTrue(dispatch.submission.denied)
        runner.complete_result("plan-a", 1, available=position(20))
        self.assertIs(runner.states()["plan-a"].events[0].outcome, Outcome.DENIED)
        self.assertEqual(args[1].read().revision, 0)
        args, host, runner = fixture(plans=(plan(visits=2),), max_jobs=1)
        runner.start()
        host.value += 200_000
        runner.dispatch_next()
        args[5].run_next(_workload_owner=runner._owner)
        runner.complete_result("plan-v", 1, available=position(100))
        with self.assertRaises(SessionCapacityError):
            runner.dispatch_next()
        self.assertIs(runner.states()["plan-v"].events[1].phase, Phase.DISPATCHING)
        self.assertIsNone(runner.states()["plan-v"].events[1].outcome)

    def test_result_pending_cancellation_and_discard_do_not_reset_or_thaw(self):
        from core.attack_simulation.job_service import JobResultNotReadyError
        args, host, runner = fixture()
        runner.start()
        host.value += 100_000
        dispatch = runner.dispatch_next()
        saved = runner.states()["plan-v"]
        with self.assertRaises(JobResultNotReadyError):
            runner.complete_result("plan-v", 1, available=position(100))
        self.assertEqual(runner.states()["plan-v"], saved)
        args[5].request(args[4]["v"], lambda c, w: c.cancel(dispatch.submission.job_id),
                         operation_id="job.cancel", _workload_owner=runner._owner)
        runner.complete_result("plan-v", 1, available=position(100))
        self.assertIs(runner.states()["plan-v"].events[0].outcome, Outcome.CANCELLED)
        runner.close()
        self.assertTrue(args[0].established)
        self.assertEqual(args[1].read().revision, 0)
        with self.assertRaises((RuntimeError, WorkloadInfrastructureError)):
            runner.dispatch_next()

    def test_clock_progress_during_compile_and_close_fences_admission(self):
        args, host, runner = fixture()
        runner.start()
        host.value += 100_000
        entered, release, fenced = Event(), Event(), Event()
        compile_program = args[5]._service._prepare_program
        def paused(*a, **kw):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("test barrier timed out")
            return compile_program(*a, **kw)
        revoke = args[3].close
        def revoked():
            revoke()
            fenced.set()
        with patch.object(args[5]._service, "_prepare_program", side_effect=paused), \
                patch.object(args[3], "close", side_effect=revoked), ThreadPoolExecutor(max_workers=2) as pool:
            dispatch = pool.submit(runner.dispatch_next)
            try:
                self.assertTrue(entered.wait(2))
                host.value += 2_000_000_000
                self.assertEqual(runner.target().time_us, 2_000_100)
                with self.assertRaises(SessionBusyError):
                    runner.dispatch_next()
                ended = pool.submit(runner.close)
                self.assertTrue(fenced.wait(2))  # Observe actual disposal, no fabricated test state.
            finally:
                release.set()
            with self.assertRaises((WorkloadInfrastructureError, PermissionError)):
                dispatch.result(timeout=5)
            ended.result(timeout=5)
        self.assertEqual(args[2].scheduler.queued_job_ids(), ())
        self.assertEqual(args[1].read().revision, 0)
        self.assertTrue(runner.states()["plan-v"].closed)

    def test_invalid_identity_profile_clock_and_existing_session_fail_before_start(self):
        args = session_fixture()
        host = Host()
        with self.assertRaises(ValueError):
            DeclaredWorkloadRunner(session=args[5], credentials={"v": args[4]["v"]}, run_id="run",
                                   max_pending=8, max_history=16, host_ns=host)
        self.assertEqual(host.calls, 0)
        for definition in (plan("v", mode="inherit"), replace(plan(), actor_id="a")):
            args = session_fixture(victim_workload=serialize_workload_plan(definition),
                                   world_clock={"clock_domain": "world", "rate": 1})
            with self.assertRaises(ValueError):
                DeclaredWorkloadRunner(session=args[5], credentials={definition.actor_id: args[4][definition.actor_id]},
                                       run_id="run", max_pending=8, max_history=16, host_ns=host)
            self.assertEqual(args[1].read().revision, 0)
            self.assertIsNone(args[5]._workload_owner)
        args = session_fixture(victim_workload=serialize_workload_plan(plan()),
                               world_clock={"clock_domain": "world", "rate": 1})
        args[5].request(args[4]["v"], lambda c, w: c.list_devices(), operation_id="device.list")
        with self.assertRaisesRegex(ValueError, "fresh"):
            DeclaredWorkloadRunner(session=args[5], credentials={"v": args[4]["v"]}, run_id="run",
                                   max_pending=8, max_history=16, host_ns=host)
        self.assertEqual(host.calls, 0)
        self.assertIsNone(args[5]._workload_owner)


if __name__ == "__main__":
    unittest.main()
