"""Bounded background pump: same declared trace, explicit stop/fault and retry."""

import time
from threading import Event
import unittest
from unittest.mock import patch

import numpy as np

from api.attack_live_driver import LiveDriverSettings, LiveDriverState, LiveWorldDriver
from api.attack_sessions import SessionBusyError
from core.attack_simulation.declared_world_clock import CatchUpMonotonicSource
from core.attack_simulation.live_time_policy import HOST_CLOCK
from core.attack_simulation.jobs import JobStatus
from tests.test_attack_live_world import request, setup
from tests.test_attack_world import at


def settings(**overrides):
    values = dict(idle_interval_s=0.005, max_transitions_per_tick=64, stop_timeout_s=5.0)
    values.update(overrides)
    return LiveDriverSettings(**values)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


def trace(args, runner, executor, world, receipt):
    live_job = world.reply(args[4]["a"], receipt).value.job_id
    jobs = [args[2].scheduler.get(live_job)] + [args[2].scheduler.get(e.job_id)
        for s in runner.states().values() for e in s.events]
    return ([(j.command.tenant_id, j.status, j.result) for j in jobs],
            [(e.arrival, e.result_arrival, e.outcome) for s in runner.states().values() for e in s.events],
            [(e.event_kind, e.device_time_start_us, e.device_time_end_us, e.owner_after)
             for e in args[1].events()],
            args[1].read().density_matrix, executor.frontier.snapshot().confirmed)


def settled(driver):
    report = driver.status().last_report
    return report is not None and report.finished


class LiveWorldDriverTests(unittest.TestCase):
    def test_background_driver_is_rejected_for_explicit_wait_world(self):
        from tests.test_attack_paused_world import paused
        args, runner, executor, world, controller = paused()
        with self.assertRaisesRegex(ValueError, "only for host_clock"):
            LiveWorldDriver(world, settings())
        world.close()

    def test_background_pump_matches_manual_pump_trace(self):
        outputs = []
        for background, per_tick in ((False, 64), (True, 64), (True, 1)):
            args, host, runner, executor, world = setup(visits=3)
            args[5]._service._sampling_salt = bytes(32)  # Deterministic test entropy only.
            at(host, 2)
            receipt = world.submit(args[4]["a"], request())
            at(host, 1000)
            if background:
                driver = LiveWorldDriver(world, settings(max_transitions_per_tick=per_tick))
                driver.start()
                self.assertTrue(wait_for(lambda: settled(driver)))
                self.assertTrue(driver.stop())
                self.assertIs(driver.status().state, LiveDriverState.STOPPED)
            else:
                for _ in range(32):
                    if world.pump(max_transitions=per_tick).finished:
                        break
            outputs.append(trace(args, runner, executor, world, receipt))
        for other in outputs[1:]:
            self.assertEqual(outputs[0][:3], other[:3])
            np.testing.assert_array_equal(outputs[0][3], other[3])
            self.assertEqual(outputs[0][4], other[4])

    def test_catch_up_pause_excludes_only_the_numerical_pump_interval(self):
        host = type("FakeMonotonic", (), {"__init__": lambda self: setattr(self, "value", 1_000_000_000),
            "__call__": lambda self: self.value})()
        source = CatchUpMonotonicSource(host)
        args, _, runner, executor, world = setup(
            victim_workload=False,
            live_time_policy={"mode": HOST_CLOCK, "pause_while_catching_up": True},
            host_clock_source=source,
        )
        entered = Event()
        observed = []
        original = world.pump

        def slow_pump(**kwargs):
            before = world.target().time_us
            entered.set()
            host.value += 500_000_000
            observed.append((before, world.target().time_us))
            return original(**kwargs)

        with patch.object(world, "pump", side_effect=slow_pump):
            driver = LiveWorldDriver(world, settings())
            driver.start()
            self.assertTrue(entered.wait(2.0))
            self.assertTrue(driver.stop())
        self.assertTrue(observed)
        self.assertEqual(observed[0][0], observed[0][1])
        self.assertEqual(world.target().time_us, observed[0][0])
        world.close()

    def test_wake_picks_up_later_ingress_without_waiting_for_idle_interval(self):
        args, host, runner, executor, world = setup(victim_workload=False)
        driver = LiveWorldDriver(world, settings(idle_interval_s=60.0))
        driver.start()
        try:
            self.assertTrue(wait_for(lambda: driver.status().ticks >= 1))
            at(host, 2)
            receipt = world.submit(args[4]["a"], request())
            at(host, 1000)
            driver.wake()
            self.assertTrue(wait_for(lambda: settled(driver) and driver.status().transitions > 0))
            self.assertIsNone(world.reply(args[4]["a"], receipt).error_code)
        finally:
            self.assertTrue(driver.stop())

    def test_close_stops_driver_then_discards_world(self):
        args, host, runner, executor, world = setup()
        driver = LiveWorldDriver(world, settings())
        driver.start()
        self.assertTrue(wait_for(lambda: driver.status().ticks >= 1))
        self.assertTrue(driver.close())
        self.assertTrue(world._closed)
        self.assertIs(driver.status().state, LiveDriverState.STOPPED)
        self.assertFalse(driver._thread.is_alive())

    def test_external_discard_ends_driver_as_world_closed_not_fault(self):
        args, host, runner, executor, world = setup()
        driver = LiveWorldDriver(world, settings())
        driver.start()
        self.assertTrue(wait_for(lambda: driver.status().ticks >= 1))
        world.close()
        self.assertTrue(wait_for(lambda: driver.status().state is LiveDriverState.WORLD_CLOSED))
        self.assertIsNone(driver.status().last_error)
        self.assertTrue(driver.stop())

    def test_first_failure_faults_without_automatic_retry(self):
        args, host, runner, executor, world = setup()
        calls = []
        def broken(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("secret detail must not be recorded")
        with patch.object(world, "pump", side_effect=broken):
            driver = LiveWorldDriver(world, settings())
            driver.start()
            self.assertTrue(wait_for(lambda: driver.status().state is LiveDriverState.FAULTED))
            self.assertTrue(driver.stop())
            time.sleep(0.03)
        self.assertEqual(len(calls), 1)
        self.assertEqual(driver.status().last_error, "RuntimeError")
        self.assertIs(driver.status().state, LiveDriverState.FAULTED)
        world.close()

    def test_numeric_interruption_faults_keeps_running_job_until_explicit_retry(self):
        args, host, runner, executor, world = setup()
        at(host, 1000)
        with patch.object(executor, "prepare_next", side_effect=RuntimeError("test numeric failure")):
            driver = LiveWorldDriver(world, settings())
            driver.start()
            self.assertTrue(wait_for(lambda: driver.status().state is LiveDriverState.FAULTED))
            self.assertTrue(driver.stop())
        job = runner.states()["plan-v"].events[0].job_id
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.RUNNING)
        before = args[1].events()
        time.sleep(0.03)  # A faulted driver must not resume the job by itself.
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.RUNNING)
        self.assertEqual(args[1].events(), before)
        self.assertTrue(world.pump(max_transitions=64).finished)  # Explicit supervisor retry.
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.SUCCEEDED)
        self.assertEqual(args[1].events()[:len(before)], before)
        world.close()

    def test_concurrent_manual_pump_is_skipped_not_a_fault(self):
        args, host, runner, executor, world = setup()
        calls = []
        def busy(**kwargs):
            calls.append(kwargs)
            raise SessionBusyError("manual pump active")
        with patch.object(world, "pump", side_effect=busy):
            driver = LiveWorldDriver(world, settings())
            driver.start()
            self.assertTrue(wait_for(lambda: len(calls) >= 5))
            self.assertTrue(driver.stop())
        self.assertIsNone(driver.status().last_error)
        self.assertIs(driver.status().state, LiveDriverState.STOPPED)
        world.close()

    def test_settings_are_bounded_and_driver_starts_once(self):
        for bad in (dict(idle_interval_s=0), dict(idle_interval_s=61.0),
                    dict(max_transitions_per_tick=0), dict(max_transitions_per_tick=16385),
                    dict(stop_timeout_s=0)):
            with self.assertRaises(ValueError):
                settings(**bad)
        args, host, runner, executor, world = setup()
        driver = LiveWorldDriver(world, settings())
        driver.start()
        with self.assertRaises(RuntimeError):
            driver.start()
        self.assertTrue(driver.close())
        with self.assertRaises(TypeError):
            LiveWorldDriver(object(), settings())


if __name__ == "__main__":
    unittest.main()
