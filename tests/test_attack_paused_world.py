"""D60 explicit-wait world: frozen policy, paused time, exact deterministic waits."""

import math
import inspect
import time
import unittest
from threading import Event, Thread
from unittest.mock import patch

import numpy as np

from api.attack_live_world import LiveWorkloadWorld
from api.attack_paused_world import (
    PausedWorldController, VirtualHostClock, WaitOutcome,
)
from api.attack_workloads import DeclaredWorkloadRunner
from core.attack_simulation.declared_execution import DeclaredQpuExecutor
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.live_time_policy import LiveTimePolicy, parse_live_time_policy
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from core.attack_simulation.workload_config import serialize_workload_plan
from tests.test_attack_live_world import request
from tests.test_attack_sessions import session_fixture
from tests.test_attack_workloads import plan
from tests.test_attack_world import TIMING
from tests.test_live_service_config import CONTROL, LIVE

POLICY = {"mode": "explicit_wait", "max_wait_us": 1_000_000.0, "max_steps_per_wait": 10_000}


def paused(*, policy=POLICY, victim=True, visits=1):
    args = session_fixture(max_jobs=32, max_receipt_history=128,
        victim_workload=serialize_workload_plan(plan(visits=visits)) if victim else None,
        world_clock={"clock_domain": "world", "rate": 1}, service_timing=TIMING,
        live_ingress=LIVE, control_timing=CONTROL, live_time_policy=policy)
    clock = VirtualHostClock()
    runner = DeclaredWorkloadRunner(session=args[5], credentials={"v": args[4]["v"]} if victim else {},
        run_id="run", max_pending=32, max_history=64, host_ns=clock)
    width = len(args[1].read().physical_qubit_ids)
    executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=32,
        max_events_per_job=64, max_unresolved=32,
        idle_model=IndependentIdleModel((IdleQubitSpec(math.inf, math.inf, 0),) * width, width))
    world = LiveWorkloadWorld(runner=runner, executor=executor, producer_id="live")
    world.start()
    args[5]._service._sampling_salt = bytes(32)  # Deterministic test entropy only.
    return args, runner, executor, world, PausedWorldController.from_manifest(world, clock)


def trace(args, runner, executor):
    jobs = sorted(args[2].scheduler._jobs.values(), key=lambda j: j.acceptance_sequence)  # IDs are UUID4.
    return ([(j.command.tenant_id, j.status, j.result) for j in jobs],
            [(e.arrival, e.result_arrival, e.outcome) for s in runner.states().values() for e in s.events],
            [(e.event_kind, e.device_time_start_us, e.device_time_end_us, e.owner_after)
             for e in args[1].events()],
            args[1].read().density_matrix, executor.frontier.snapshot().confirmed)


class LiveTimePolicyTests(unittest.TestCase):
    def test_policy_must_be_explicit_and_match_its_mode(self):
        self.assertEqual(parse_live_time_policy(POLICY).max_wait_us, 1_000_000.0)
        host = parse_live_time_policy({"mode": "host_clock", "pause_while_catching_up": True})
        self.assertEqual(host.mode, "host_clock")
        self.assertTrue(host.pause_while_catching_up)
        self.assertFalse(parse_live_time_policy({"mode": "host_clock"}).pause_while_catching_up)
        for raw in (None, {}, {"mode": "explicit_wait"}, {**POLICY, "extra": 1},
                    {"mode": "host_clock", "max_wait_us": 1.0}, {**POLICY, "mode": "pause"},
                    {**POLICY, "max_wait_us": 0}, {**POLICY, "max_wait_us": math.inf},
                    {**POLICY, "max_steps_per_wait": 0}, {**POLICY, "max_steps_per_wait": True}):
            with self.assertRaises(ValueError):
                parse_live_time_policy(raw)

    def test_controller_requires_frozen_explicit_wait_policy_and_the_worlds_clock(self):
        args, runner, executor, world, controller = paused()
        with self.assertRaises(ValueError):
            PausedWorldController(world, VirtualHostClock(), LiveTimePolicy(**POLICY))
        with self.assertRaises(ValueError):
            PausedWorldController(world, controller._clock, LiveTimePolicy("host_clock"))
        with self.assertRaises(ValueError):
            paused(policy=None)  # No default: an unstated policy cannot be established.


class PausedWorldTests(unittest.TestCase):
    def test_close_fences_wait_before_preparation_can_finish(self):
        args, runner, executor, world, controller = paused(visits=3)
        controller.wait(args[4]["a"], 100)
        committed_events = args[1].events()
        committed_position = executor.frontier.snapshot().confirmed
        self.assertTrue(committed_events)
        controller.submit(args[4]["a"], request("a"))
        entered, release, close_done = Event(), Event(), Event()
        original = executor.prepare_next
        results = []
        def slow_prepare():
            entered.set()
            if not release.wait(2):
                raise AssertionError("test preparation was not released")
            return original()
        def wait():
            try:
                results.append(controller.wait(args[4]["a"], 5000))
            except Exception as error:
                results.append(error)
        def close():
            controller.close()
            close_done.set()
        with patch.object(executor, "prepare_next", side_effect=slow_prepare):
            waiter = Thread(target=wait)
            waiter.start()
            try:
                self.assertTrue(entered.wait(2))
                closer = Thread(target=close)
                closer.start()
                self.assertTrue(controller._close_requested.wait(0.2))
                self.assertFalse(close_done.is_set())  # Drain waits for detached computation.
            finally:
                release.set()
                waiter.join(3)
                if "closer" in locals():
                    closer.join(3)
        self.assertFalse(waiter.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertTrue(close_done.is_set())
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], RuntimeError)
        self.assertEqual(args[1].events()[:len(committed_events)], committed_events)
        self.assertGreaterEqual(executor.frontier.snapshot().confirmed.key, committed_position.key)

    def test_controller_uses_public_live_world_queries_and_queries_are_read_only(self):
        args, runner, executor, world, controller = paused()
        source = inspect.getsource(PausedWorldController)
        for private_name in ("_actions", "_operations", "_reply_cache", "_waiting",
                             "_ingress", "_executor", "_runner", "_clock"):
            self.assertNotIn("self._world." + private_name, source)
        before = world.target()
        next_event = world.earliest_pending_event_time_us(15.0)
        self.assertEqual(next_event, 15.0)
        self.assertEqual(world.target(), before)
        receipt = controller.submit(args[4]["a"], request())
        self.assertFalse(world.is_reply_released(receipt))
        self.assertIs(controller.wait_reply(args[4]["a"], receipt).outcome, WaitOutcome.REACHED)
        self.assertTrue(world.is_reply_released(receipt))

    def test_world_stands_still_while_the_user_thinks(self):
        args, runner, executor, world, controller = paused()
        time.sleep(0.05)  # Real user think time.
        self.assertEqual(controller.now(), 0.0)
        world.pump(max_transitions=64)
        self.assertEqual(args[2].scheduler._jobs, {})  # The victim (due at 10 us) has not moved.
        receipt = controller.submit(args[4]["a"], request())
        self.assertEqual(world._operations[receipt.sequence].reservation.position.time_us,
                         LIVE["outbound_delay_us"])

    def test_duration_wait_stops_exactly_at_its_goal(self):
        args, runner, executor, world, controller = paused()
        report = controller.wait(args[4]["a"], 2000)
        self.assertIs(report.outcome, WaitOutcome.REACHED)
        self.assertEqual(report.world_time_us, 2000.0)
        self.assertEqual(controller.now(), 2000.0)
        job = runner.states()["plan-v"].events[0].job_id
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.SUCCEEDED)

    def test_wait_reply_stops_at_the_reply_release_and_releases_it(self):
        args, runner, executor, world, controller = paused()
        receipt = controller.submit(args[4]["a"], request())
        report = controller.wait_reply(args[4]["a"], receipt)
        self.assertIs(report.outcome, WaitOutcome.REACHED)
        release = world._operations[receipt.sequence].release.time_us
        self.assertGreaterEqual(report.world_time_us, release)
        self.assertLess(report.world_time_us - release, 0.01)  # Host-ns resolution only.
        reply = controller.reply(args[4]["a"], receipt)
        self.assertIsNone(reply.error_code)
        self.assertIs(reply.value.status, JobStatus.QUEUED)

    def test_operations_do_not_block_and_queued_cancel_applies_before_start(self):
        args, runner, executor, world, controller = paused(victim=False)
        # A long job occupies the FIFO; the short one waits behind it.
        long = controller.submit(args[4]["a"], request("long", shots=32))
        short = controller.submit(args[4]["a"], request("short"))
        cancelled = controller.submit(args[4]["a"], request("cancelled"))
        controller.wait_reply(args[4]["a"], cancelled)
        first_job = controller.reply(args[4]["a"], cancelled).value.job_id
        cancel = controller.cancel(args[4]["a"], first_job)  # Issued while all three are outstanding.
        self.assertIs(controller.wait_reply(args[4]["a"], cancel).outcome, WaitOutcome.REACHED)
        self.assertIsNone(controller.reply(args[4]["a"], cancel).error_code)
        controller.wait_reply(args[4]["a"], short)
        second_job = controller.reply(args[4]["a"], short).value.job_id
        controller.wait_reply(args[4]["a"], long)
        long_job = controller.reply(args[4]["a"], long).value.job_id
        result = controller.result(args[4]["a"], second_job)
        while controller.wait_reply(args[4]["a"], result).outcome is WaitOutcome.REACHED:
            reply = controller.reply(args[4]["a"], result)
            if reply.error_code != "RESULT_NOT_READY":
                break
            controller.wait(args[4]["a"], 100)
            result = controller.result(args[4]["a"], second_job)
        self.assertIs(args[2].scheduler.get(first_job).status, JobStatus.CANCELLED)
        self.assertIs(args[2].scheduler.get(second_job).status, JobStatus.SUCCEEDED)
        self.assertIs(args[2].scheduler.get(long_job).status, JobStatus.SUCCEEDED)
        self.assertEqual(sum(reply.value.result["counts"].values()), 2)
        self.assertNotIn(first_job, [e.job_id for e in args[1].events()])  # Never executed.

    def test_wait_any_returns_at_the_earliest_own_reply(self):
        args, runner, executor, world, controller = paused(victim=False)
        self.assertIs(controller.wait_any(args[4]["a"]).outcome, WaitOutcome.NOTHING_PENDING)
        self.assertEqual(controller.now(), 0.0)
        a = controller.submit(args[4]["a"], request("a"))
        controller.wait(args[4]["a"], 1)
        b = controller.submit(args[4]["a"], request("b"))
        report = controller.wait_any(args[4]["a"])
        self.assertIs(report.outcome, WaitOutcome.REACHED)
        self.assertIsNone(controller.reply(args[4]["a"], a).error_code)
        from core.attack_simulation.job_service import JobResultNotReadyError
        with self.assertRaises(JobResultNotReadyError):
            controller.reply(args[4]["a"], b)

    def test_same_operation_sequence_gives_the_same_world_on_slow_hosts(self):
        outputs = []
        for slow in (False, True):
            args, runner, executor, world, controller = paused(visits=3)
            original = executor.prepare_next
            def maybe_slow():
                if slow:
                    time.sleep(0.01)  # Real host compute delay never moves the virtual clock.
                return original()
            with patch.object(executor, "prepare_next", side_effect=maybe_slow):
                times = []
                receipt = controller.submit(args[4]["a"], request())
                times.append(controller.wait_reply(args[4]["a"], receipt).world_time_us)
                job = controller.reply(args[4]["a"], receipt).value.job_id
                times.append(controller.wait(args[4]["a"], 500).world_time_us)
                status = controller.status(args[4]["a"], job)
                times.append(controller.wait_reply(args[4]["a"], status).world_time_us)
                times.append(controller.wait(args[4]["a"], 5000).world_time_us)
                observed = controller.reply(args[4]["a"], status).value.status
            outputs.append((times, observed, trace(args, runner, executor)))
        (t0, s0, a), (t1, s1, b) = outputs
        self.assertEqual((t0, s0), (t1, s1))
        self.assertEqual(a[:3], b[:3])
        np.testing.assert_array_equal(a[3], b[3])
        self.assertEqual(a[4], b[4])

    def test_reply_wait_is_bounded_by_the_frozen_policy(self):
        args, runner, executor, world, controller = paused(
            policy={**POLICY, "max_wait_us": 0.5}, victim=False)
        receipt = controller.submit(args[4]["a"], request())
        report = controller.wait_reply(args[4]["a"], receipt)
        self.assertIs(report.outcome, WaitOutcome.LIMITED)
        self.assertEqual(report.world_time_us, 0.5)
        with self.assertRaises(ValueError):
            controller.wait(args[4]["a"], 0.6)

    def test_waits_are_scoped_to_the_callers_own_requests(self):
        args, runner, executor, world, controller = paused()
        receipt = controller.submit(args[4]["a"], request())
        with self.assertRaises(PermissionError):
            controller.wait_reply(args[4]["v"], receipt)
        with self.assertRaises(PermissionError):
            controller.wait(args[4]["a"][:-1] + "x", 10)
        for bad in (0, -1, math.nan, True):
            with self.assertRaises(ValueError):
                controller.wait(args[4]["a"], bad)
        self.assertEqual(controller.now(), 0.0)


if __name__ == "__main__":
    unittest.main()
