"""World metadata checks without choosing a live clock or victim conditions."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier
import unittest
from unittest.mock import patch

from core.attack_simulation.event_commit_boundary import (
    EventOrderError, EventPosition, UnresolvedIngressError,
)
from core.attack_simulation.request_receipts import RequestReceipt
from core.attack_simulation.world_event_frontier import (
    DeclaredWorldFrontier, WorldFrontierCapacityError,
)


def position(time_us, order=0, domain="world"):
    return EventPosition(domain, time_us, order)


class WorldEventFrontierTests(unittest.TestCase):
    def frontier(self, limit=3):
        return DeclaredWorldFrontier(clock_domain="world", max_unresolved=limit)

    def test_target_advances_while_confirmation_waits_and_then_catches_up(self):
        frontier = self.frontier()
        ticket = frontier.register_ingress(position(10))
        frontier.set_target(position(11))
        frontier.seal_ingress(position(11))
        frontier.confirm(position(9))
        frontier.set_target(position(20))
        with self.assertRaises(UnresolvedIngressError):
            frontier.confirm(position(11))
        waiting = frontier.snapshot()
        self.assertEqual((waiting.target, waiting.confirmed), (position(20), position(9)))
        later = frontier.register_ingress(position(12))
        frontier.resolve_ingress(ticket)
        frontier.confirm(position(11))
        self.assertEqual(frontier.snapshot().unresolved, (later,))
        frontier.seal_ingress(position(20))
        frontier.resolve_ingress(later)
        frontier.confirm(position(20))
        self.assertEqual(frontier.snapshot().confirmed, position(20))

    def test_missing_watermark_future_arrivals_and_explicit_same_time_order(self):
        frontier = self.frontier()
        frontier.set_target(position(10, 4))
        before = frontier.snapshot()
        with self.assertRaises(UnresolvedIngressError):
            frontier.confirm(position(10, 1))
        self.assertEqual(frontier.snapshot(), before)
        ticket = frontier.register_ingress(position(10, 2))
        frontier.seal_ingress(position(10, 3))
        frontier.confirm(position(10, 1))
        for order in (2, 3):
            with self.assertRaises(EventOrderError):
                frontier.register_ingress(position(10, order))
        with self.assertRaises(UnresolvedIngressError):
            frontier.confirm(position(10, 2))
        frontier.resolve_ingress(ticket)
        frontier.confirm(position(10, 2))
        frontier.register_ingress(position(10, 4))

    def test_capacity_ticket_identity_and_close_preserve_evidence(self):
        frontier, other = self.frontier(1), self.frontier(1)
        ticket = frontier.register_ingress(position(10))
        copied, foreign = replace(ticket), other.register_ingress(position(10))
        before = frontier.snapshot()
        with self.assertRaises(WorldFrontierCapacityError):
            frontier.register_ingress(position(11))
        for invalid in (copied, foreign, object()):
            with self.assertRaises(ValueError):
                frontier.resolve_ingress(invalid)
        self.assertEqual(frontier.snapshot(), before)
        frontier.resolve_ingress(ticket)
        with self.assertRaises(ValueError):
            frontier.resolve_ingress(ticket)
        frontier.set_target(position(20))
        frontier.seal_ingress(position(20))
        frontier.confirm(position(19))
        later = frontier.register_ingress(position(21))
        before = frontier.snapshot()
        frontier.close()
        frontier.close()
        self.assertEqual(frontier.snapshot(), replace(before, closed=True))
        for action in (lambda: frontier.set_target(position(22)),
                       lambda: frontier.seal_ingress(position(20)),
                       lambda: frontier.register_ingress(position(22)),
                       lambda: frontier.resolve_ingress(later),
                       lambda: frontier.confirm(position(20))):
            with self.assertRaises(RuntimeError):
                action()

    def test_invalid_inputs_do_not_mutate_and_no_host_clock_is_read(self):
        frontier = self.frontier()
        with patch("time.monotonic_ns", side_effect=AssertionError("not world time")):
            frontier.set_target(position(20))
            frontier.seal_ingress(position(10))
            frontier.confirm(position(9))
        before = frontier.snapshot()
        for candidate in (RequestReceipt(1, 1, "a", "program.submit"), position(11, domain="other")):
            for action in (frontier.set_target, frontier.register_ingress, frontier.seal_ingress, frontier.confirm):
                with self.assertRaises((TypeError, ValueError)):
                    action(candidate)
        for action in (lambda: frontier.set_target(position(19)),
                       lambda: frontier.seal_ingress(position(9)),
                       lambda: frontier.seal_ingress(position(21)),
                       lambda: frontier.confirm(position(9)),
                       lambda: frontier.confirm(position(21))):
            with self.assertRaises((EventOrderError, ValueError)):
                action()
        self.assertEqual(frontier.snapshot(), before)
        with self.assertRaises(FrozenInstanceError):
            before.confirmed = None
        for limit in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.frontier(limit)

    def test_ingress_seal_race_keeps_registered_request_or_rejects_late_input(self):
        # Either ordering is valid; no successful registration can be omitted
        # from the snapshot used by confirmation after both operations finish.
        for _ in range(12):
            frontier = self.frontier()
            frontier.set_target(position(11))
            barrier = Barrier(2, timeout=2)

            def register():
                barrier.wait()
                try:
                    return frontier.register_ingress(position(10))
                except EventOrderError:
                    return None

            def seal():
                barrier.wait()
                frontier.seal_ingress(position(11))

            with ThreadPoolExecutor(max_workers=2) as pool:
                registration, sealing = pool.submit(register), pool.submit(seal)
                ticket = registration.result(timeout=3)
                sealing.result(timeout=3)
            if ticket is not None:
                with self.assertRaises(UnresolvedIngressError):
                    frontier.confirm(position(11))
                frontier.resolve_ingress(ticket)
            frontier.confirm(position(11))
            self.assertEqual(frontier.snapshot().confirmed, position(11))


if __name__ == "__main__":
    unittest.main()
