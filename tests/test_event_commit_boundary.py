"""Declared trace causality without choosing live mapping or victim physics."""

from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch

from core.attack_simulation.event_commit_boundary import (
    EventOrderError, EventPosition, UnresolvedIngressError, validate_event_commit,
)
from core.attack_simulation.request_receipts import RequestReceipt


def position(time_us, order=0, domain="declared-world"):
    return EventPosition(domain, time_us, order)


class EventCommitBoundaryTests(unittest.TestCase):
    def check(self, candidate, *, committed=None, unresolved=(), limit=4):
        return validate_event_commit(committed=committed, candidate=candidate,
                                     unresolved=unresolved, max_unresolved=limit)

    def test_unresolved_earlier_request_blocks_later_event_but_not_an_earlier_frontier(self):
        receipt = position(10)
        self.check(position(9), unresolved=(receipt,))
        with self.assertRaises(UnresolvedIngressError):
            self.check(position(11), committed=position(9), unresolved=(receipt,))
        # Admission/rejection/abandonment all resolve this barrier; their job
        # effects remain the scheduler's responsibility, not this checker's.
        self.check(position(11), committed=position(9))

    def test_explicit_same_time_order_and_exact_pending_position(self):
        unresolved = (position(10, 2),)
        self.check(position(10, 1), committed=position(10, 0), unresolved=unresolved)
        for candidate in (position(10, 2), position(10, 3)):
            with self.subTest(candidate=candidate), self.assertRaises(UnresolvedIngressError):
                self.check(candidate, committed=position(10, 0), unresolved=unresolved)
        self.check(position(10, 2), committed=position(10, 1))

    def test_no_rewind_duplicate_commit_or_late_insertion(self):
        for candidate in (position(9), position(10, 0), position(10, 1)):
            with self.subTest(candidate=candidate), self.assertRaises(EventOrderError):
                self.check(candidate, committed=position(10, 1))
        for late in (position(9), position(10, 1)):
            with self.subTest(late=late), self.assertRaises(EventOrderError):
                self.check(position(11), committed=position(10, 1), unresolved=(late,))

    def test_explicit_trace_is_independent_of_host_clock_and_unresolved_enumeration_order(self):
        receipts = (position(12), position(10, 2), position(10, 1))
        with patch("time.monotonic_ns", side_effect=AssertionError("host clock is outside this boundary")):
            for snapshot in (receipts, tuple(reversed(receipts))):
                with self.assertRaises(UnresolvedIngressError):
                    self.check(position(11), committed=position(9), unresolved=snapshot)
                self.check(position(10, 0), committed=position(9), unresolved=snapshot)
            self.check(position(13), committed=position(9))

    def test_host_receipt_and_foreign_world_cannot_be_silently_mapped(self):
        host = RequestReceipt(1, 123, "a", "program.submit")
        with self.assertRaises(TypeError):
            self.check(host)
        with self.assertRaises(TypeError):
            self.check(position(11), unresolved=(host,))
        for committed, unresolved in ((position(9, domain="other-world"), ()),
                                      (None, (position(10, domain="other-world"),))):
            with self.assertRaises(ValueError):
                self.check(position(11), committed=committed, unresolved=unresolved)

    def test_snapshot_bound_and_immutable_positions(self):
        pending = position(10)
        with self.assertRaises(FrozenInstanceError):
            pending.time_us = 0
        for snapshot, limit, error in (([pending], 4, TypeError), ((pending,) * 5, 4, ValueError),
                                       ((), True, ValueError), ((), 0, ValueError)):
            with self.subTest(limit=limit), self.assertRaises(error):
                self.check(position(9), unresolved=snapshot, limit=limit)
        for changes in ({"time_us": float("nan")}, {"time_us": float("inf")},
                        {"time_us": -1}, {"time_us": True}, {"time_us": 1 << 2048}, {"order": True},
                        {"order": -1}, {"clock_domain": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(pending, **changes)


if __name__ == "__main__":
    unittest.main()
