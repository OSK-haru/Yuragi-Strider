"""Run-origin, rate, nondecreasing sampling and trusted receipt mapping checks."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import unittest

from core.attack_simulation.declared_world_clock import (
    CatchUpMonotonicSource, DeclaredWorldClock, WorldClockSettings, seconds_to_world_us,
)


class Host:
    def __init__(self, value=1_000_000_000):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


def ready(clock):
    clock.mark_ready(runtime_ready=True, ingress_ready=True,
                     workload_ready=True, manifest_frozen=True)


class DeclaredWorldClockTests(unittest.TestCase):
    def test_catchup_pause_excludes_only_the_paused_host_interval(self):
        host = Host(5_000_000_000)
        source = CatchUpMonotonicSource(host)
        self.assertEqual(source(), 5_000_000_000)
        host.value += 1_000_000
        self.assertEqual(source(), 5_001_000_000)
        source.pause()
        frozen = source()
        host.value += 8_000_000
        self.assertEqual(source(), frozen)
        source.resume()
        self.assertEqual(source(), frozen)
        host.value += 2_000_000
        self.assertEqual(source(), frozen + 2_000_000)

    def test_construction_and_failed_readiness_have_no_origin(self):
        host = Host()
        clock = DeclaredWorldClock(WorldClockSettings("world"), host_ns=host)
        self.assertEqual(host.calls, 0)
        with self.assertRaises(RuntimeError):
            clock.target()
        with self.assertRaises(RuntimeError):
            clock.start()
        for field in ("runtime_ready", "ingress_ready", "workload_ready", "manifest_frozen"):
            flags = dict(runtime_ready=True, ingress_ready=True,
                         workload_ready=True, manifest_frozen=True)
            flags[field] = False
            with self.assertRaises(ValueError):
                clock.mark_ready(**flags)
        ready(clock)
        host.value += 9_000_000_000  # Preparation time is outside the world.
        origin = clock.start()
        self.assertEqual(origin.time_us, 0)
        self.assertEqual(host.calls, 1)
        self.assertEqual(set(origin.__dict__), {"clock_domain", "time_us", "order"})
        with self.assertRaises(RuntimeError):
            ready(clock)
        with self.assertRaises(RuntimeError):
            clock.start()

    def test_rate_and_units_are_fixed_and_separate_from_device_duration(self):
        for rate in (0.5, 1, 2, 100):
            with self.subTest(rate=rate):
                host = Host()
                clock = DeclaredWorldClock(WorldClockSettings("world", rate), host_ns=host)
                ready(clock)
                clock.start()
                host.value += 1_250_000_000
                self.assertEqual(clock.target(order=7).time_us, 1_250_000 * rate)
                self.assertEqual(seconds_to_world_us(1.25), 1_250_000)
                with self.assertRaises(FrozenInstanceError):
                    clock.settings.rate = 9
        self.assertEqual(WorldClockSettings("world").rate, 1)
        for invalid in (True, 0, -1, float("nan"), float("inf"), "1", 10**1000):
            with self.subTest(invalid=str(invalid)[:20]), self.assertRaises(ValueError):
                WorldClockSettings("world", invalid)
        for invalid in (-1, True, float("nan"), float("inf"), 10**1000, "1"):
            with self.assertRaises(ValueError):
                seconds_to_world_us(invalid)

    def test_backward_and_invalid_samples_leave_last_good_position(self):
        host = Host()
        clock = DeclaredWorldClock(WorldClockSettings("world"), host_ns=host)
        ready(clock)
        clock.start()
        for invalid in (999_999_999, -1, True, 1.0,
                        float("nan"), float("inf"), 10**1000):
            host.value = invalid
            with self.assertRaises(ValueError):
                clock.target()
        host.value = 1_000_002_000
        with self.assertRaises(ValueError):
            clock.target(order=-1)
        self.assertEqual(clock.target().time_us, 2)

    def test_equal_serial_and_concurrent_samples_preserve_explicit_order(self):
        host = Host()
        clock = DeclaredWorldClock(WorldClockSettings("world"), host_ns=host)
        ready(clock)
        origin = clock.start()
        self.assertEqual(clock.target(), origin)
        self.assertEqual(clock.target(), origin)  # No automatic sequence invention.
        host.value += 2_000
        self.assertEqual(clock.target(order=7).key, (2, 7))
        self.assertEqual(clock.target(order=3).key, (2, 3))  # Caller owns tie order.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [future.result(timeout=5).key for future in
                       [pool.submit(clock.target, order=i) for i in range(8)]]
        self.assertEqual(results, [(2, i) for i in range(8)])
        self.assertEqual(clock.target().key, (2, 0))

    def test_receipts_are_internal_run_bounded_and_do_not_rewind_target(self):
        host = Host()
        clock = DeclaredWorldClock(WorldClockSettings("world"), host_ns=host)
        ready(clock)
        clock.start()
        host.value += 5_000
        self.assertEqual(clock.target().time_us, 5)
        self.assertEqual(clock._map_receipt_ns(1_000_004_000, order=2).time_us, 4)
        self.assertEqual(clock._map_receipt_ns(1_000_001_000, order=1).time_us, 1)
        for invalid in (999_999_999, 1_000_005_001, float("nan"), True):
            with self.assertRaises(ValueError):
                clock._map_receipt_ns(invalid, order=1)
        host.value += 1_000
        self.assertEqual(clock.target().time_us, 6)
        self.assertFalse(hasattr(clock, "map_receipt_ns"))
        self.assertFalse(hasattr(clock, "device_time_us"))

    def test_owned_lock_serializes_start_and_sampling(self):
        host = Host()
        clock = DeclaredWorldClock(WorldClockSettings("world"), host_ns=host)
        ready(clock)

        def start_once(_):
            try:
                return clock.start()
            except RuntimeError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [future.result(timeout=5) for future in
                       [pool.submit(start_once, i) for i in range(4)]]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(host.calls, 1)

        def incrementing_host():
            host.value += 1_000
            return host.value

        clock._host_ns = incrementing_host
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [future.result(timeout=5).time_us for future in
                       [pool.submit(clock.target) for _ in range(20)]]
        self.assertEqual(sorted(results), list(range(1, 21)))

    def test_replay_maps_equal_elapsed_inputs_and_close_never_restarts(self):
        traces = []
        for origin in (0, 10**18):
            host = Host(origin)
            clock = DeclaredWorldClock(WorldClockSettings("world", 2), host_ns=host)
            ready(clock)
            trace = [clock.start()]
            for elapsed in (1_000, 5_000, 2_000_000_000):
                host.value = origin + elapsed
                trace.append(clock.target())
            traces.append(trace)
            clock.close()
            for call in (clock.start, clock.target, lambda: ready(clock)):
                with self.assertRaises(RuntimeError):
                    call()
        self.assertEqual(traces[0], traces[1])


if __name__ == "__main__":
    unittest.main()
