"""Host receipt identity, bounded retention and concurrency contract."""

from copy import copy
from dataclasses import FrozenInstanceError, replace
from queue import Queue
from threading import Event, Thread
import unittest
from unittest.mock import Mock

from core.attack_simulation.request_receipts import (
    ReceiptCapacityError,
    ReceiptLimits,
    ReceiptOutcome,
    ReceiptRecord,
    RequestReceipt,
    RequestReceiptLedger,
)


class RequestReceiptTests(unittest.TestCase):
    def test_limits_are_explicit_positive_exact_integers(self) -> None:
        class IntSubclass(int):
            pass

        for invalid in (True, False, 0, -1, 1.0, "1", None, IntSubclass(1)):
            for position in (0, 1):
                with self.subTest(invalid=invalid, position=position):
                    values = [1, 1]
                    values[position] = invalid
                    with self.assertRaises(ValueError):
                        ReceiptLimits(*values)
        with self.assertRaises(TypeError):
            ReceiptLimits()
        with self.assertRaises(TypeError):
            RequestReceiptLedger(None)
        with self.assertRaises(TypeError):
            RequestReceiptLedger(ReceiptLimits(1, 1), clock=7)
        with self.assertRaises(FrozenInstanceError):
            ReceiptLimits(1, 1).max_pending = 2

    def test_same_timestamp_has_distinct_sequence_and_no_admission_outcome(self) -> None:
        ledger = RequestReceiptLedger(ReceiptLimits(2, 2), clock=lambda: 0)
        first = ledger.reserve("actor", "program.submit")
        second = ledger.reserve("actor", "job.status.read")
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual((first.received_at_ns, second.received_at_ns), (0, 0))
        self.assertEqual(first.clock_domain, "host_monotonic_ns")
        self.assertEqual(ledger.records(), (ReceiptRecord(first, None), ReceiptRecord(second, None)))
        ledger.validate(first, "actor")
        ledger.validate(second, "actor")

    def test_clock_rejection_and_exception_leave_state_and_sequence_unchanged(self) -> None:
        class IntSubclass(int):
            pass

        for invalid in (9, -1, True, 10.0, "10", None, IntSubclass(10), RuntimeError("clock failure")):
            with self.subTest(invalid=invalid):
                clock = Mock(side_effect=[10, invalid, 10])
                ledger = RequestReceiptLedger(ReceiptLimits(2, 2), clock=clock)
                first = ledger.reserve("actor", "submit")
                before = ledger.records()
                with self.assertRaises((ValueError, RuntimeError)):
                    ledger.reserve("actor", "submit")
                self.assertEqual(ledger.records(), before)
                ledger.validate(first, "actor")
                second = ledger.reserve("actor", "submit")
                self.assertEqual((second.sequence, second.received_at_ns), (2, 10))

    def test_identifier_validation_preserves_registered_unicode_principals(self) -> None:
        class StringSubclass(str):
            pass

        clock = Mock(return_value=1)
        ledger = RequestReceiptLedger(ReceiptLimits(2, 2), clock=clock)
        for invalid in ("", [], {}, 1, None):
            with self.subTest(principal=invalid):
                with self.assertRaises(ValueError):
                    ledger.reserve(invalid, "submit")
                self.assertEqual(ledger.records(), ())
        for invalid in ("", "a" * 129, "操作", [], {}, 1, None, StringSubclass("submit")):
            with self.subTest(operation=invalid):
                with self.assertRaises(ValueError):
                    ledger.reserve("actor", invalid)
                self.assertEqual(ledger.records(), ())
        clock.assert_not_called()
        principal = StringSubclass("登録済み主体" * 40)
        principal.extra = []
        receipt = ledger.reserve(principal, "b" * 128)
        self.assertEqual(receipt.sequence, 1)
        self.assertEqual(receipt.principal_id, principal)
        self.assertIs(type(receipt.principal_id), str)
        self.assertIsNot(receipt.principal_id, principal)
        principal.extra.append("mutated")
        ledger.validate(receipt, str(principal))
        self.assertGreater(len(receipt.principal_id), 128)

    def test_capacity_is_pending_only_and_failure_does_not_consume_clock_or_sequence(self) -> None:
        clock = Mock(side_effect=[5, 6])
        ledger = RequestReceiptLedger(ReceiptLimits(1, 1), clock=clock)
        first = ledger.reserve("actor", "submit")
        with self.assertRaises(ReceiptCapacityError):
            ledger.reserve("actor", "submit")
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(ledger.records(), (ReceiptRecord(first, None),))
        self.assertTrue(ledger.finish(first, outcome=ReceiptOutcome.COMPLETED, job_id="job-1"))
        second = ledger.reserve("actor", "submit")
        self.assertEqual((second.sequence, second.received_at_ns), (2, 6))
        self.assertEqual(len(ledger.records()), 2)

    def test_foreign_copied_and_finished_receipts_cannot_be_used(self) -> None:
        ledger = RequestReceiptLedger(ReceiptLimits(1, 1), clock=lambda: 7)
        other = RequestReceiptLedger(ReceiptLimits(1, 1), clock=lambda: 7)
        receipt = ledger.reserve("actor", "submit")
        foreign = other.reserve("actor", "submit")
        self.assertEqual(receipt, foreign)
        for invalid in (foreign, copy(receipt), replace(receipt), None):
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, PermissionError)):
                    ledger.validate(invalid, "actor")
                with self.assertRaises((ValueError, PermissionError)):
                    ledger.claim(invalid, "actor")
                self.assertFalse(ledger.finish(invalid, outcome=ReceiptOutcome.COMPLETED))
                self.assertFalse(ledger.abandon(invalid))
                ledger.validate(receipt, "actor")
        with self.assertRaises(PermissionError):
            ledger.validate(receipt, "other-actor")
        with self.assertRaises(PermissionError):
            ledger.claim(receipt, "other-actor")
        self.assertTrue(ledger.abandon(receipt))
        before = ledger.records()
        with self.assertRaises((ValueError, PermissionError)):
            ledger.validate(receipt, "actor")
        self.assertFalse(ledger.finish(receipt, outcome=ReceiptOutcome.REJECTED))
        self.assertFalse(ledger.abandon(receipt))
        self.assertEqual(ledger.records(), before)
        other.validate(foreign, "actor")

    def test_finish_validates_outcome_and_job_id_before_changing_pending(self) -> None:
        ledger = RequestReceiptLedger(ReceiptLimits(1, 2), clock=lambda: 3)
        receipt = ledger.reserve("actor", "submit")
        for invalid in (None, "completed", [], 1):
            with self.subTest(outcome=invalid):
                with self.assertRaises(ValueError):
                    ledger.finish(receipt, outcome=invalid)
                ledger.validate(receipt, "actor")
        for invalid in ("", "a" * 129, "結果", [], {}, 1):
            with self.subTest(job_id=invalid):
                with self.assertRaises(ValueError):
                    ledger.finish(receipt, outcome=ReceiptOutcome.COMPLETED, job_id=invalid)
                ledger.validate(receipt, "actor")
        self.assertTrue(ledger.finish(receipt, outcome=ReceiptOutcome.REJECTED))
        self.assertEqual(ledger.records(), (ReceiptRecord(receipt, ReceiptOutcome.REJECTED),))

    def test_history_is_bounded_by_completion_and_snapshot_sorted_by_sequence(self) -> None:
        ledger = RequestReceiptLedger(ReceiptLimits(4, 2), clock=lambda: 4)
        tickets = [ledger.reserve("actor", "submit") for _ in range(4)]
        for index in (2, 1, 3):
            self.assertTrue(ledger.finish(tickets[index], outcome=ReceiptOutcome.COMPLETED))
        records = ledger.records()
        self.assertEqual([record.receipt.sequence for record in records], [1, 2, 4])
        self.assertEqual([record.outcome for record in records], [None, ReceiptOutcome.COMPLETED, ReceiptOutcome.COMPLETED])
        self.assertTrue(ledger.abandon(tickets[0]))
        self.assertEqual([record.receipt.sequence for record in ledger.records()], [1, 4])
        self.assertFalse(ledger.abandon(tickets[2]))  # Already evicted from history.

    def test_close_abandons_pending_with_bounded_history_and_is_idempotent(self) -> None:
        clock = Mock(return_value=8)
        ledger = RequestReceiptLedger(ReceiptLimits(3, 2), clock=clock)
        completed = ledger.reserve("actor", "submit")
        ledger.finish(completed, outcome=ReceiptOutcome.COMPLETED)
        pending = [ledger.reserve("actor", "submit") for _ in range(3)]
        ledger.claim(pending[0], "actor")
        ledger.claim(pending[2], "actor")
        ledger.close()
        expected = tuple(ReceiptRecord(ticket, ReceiptOutcome.ABANDONED) for ticket in pending[-2:])
        self.assertEqual(ledger.records(), expected)
        ledger.close()
        self.assertEqual(ledger.records(), expected)
        for ticket in pending:
            with self.assertRaises((ValueError, PermissionError)):
                ledger.validate(ticket, "actor")
            with self.assertRaises((ValueError, PermissionError)):
                ledger.claim(ticket, "actor")
            self.assertFalse(ledger.abandon(ticket))
        with self.assertRaises((ValueError, RuntimeError)):
            ledger.reserve("actor", "submit")
        self.assertEqual(clock.call_count, 4)
        empty = RequestReceiptLedger(ReceiptLimits(1, 1))
        empty.close()
        empty.close()
        self.assertEqual(empty.records(), ())

    def test_snapshots_and_ticket_fields_are_immutable_and_do_not_follow_state(self) -> None:
        ledger = RequestReceiptLedger(ReceiptLimits(1, 1), clock=lambda: 9)
        ticket = ledger.reserve("actor", "submit")
        snapshot = ledger.records()
        self.assertIs(type(snapshot), tuple)
        with self.assertRaises(FrozenInstanceError):
            ticket.principal_id = "other"
        with self.assertRaises(FrozenInstanceError):
            snapshot[0].outcome = ReceiptOutcome.COMPLETED
        with self.assertRaises(TypeError):
            snapshot[0] = ReceiptRecord(ticket, ReceiptOutcome.COMPLETED)
        ledger.finish(ticket, outcome=ReceiptOutcome.COMPLETED, job_id="job-1")
        self.assertEqual(snapshot, (ReceiptRecord(ticket, None),))
        self.assertEqual(ledger.records(), (ReceiptRecord(ticket, ReceiptOutcome.COMPLETED, "job-1"),))
        self.assertEqual(set(vars(ticket)), {"sequence", "received_at_ns", "principal_id", "operation_id", "clock_domain"})

    def test_active_abandon_preserves_ticket_until_worker_finishes(self) -> None:
        for outcome in (ReceiptOutcome.COMPLETED, ReceiptOutcome.REJECTED):
            with self.subTest(outcome=outcome):
                ledger = RequestReceiptLedger(ReceiptLimits(1, 2), clock=lambda: 10)
                ticket = ledger.reserve("actor", "submit")
                ledger.claim(ticket, "actor")
                ledger.validate(ticket, "actor")
                before = ledger.records()
                self.assertEqual(before, (ReceiptRecord(ticket, None),))
                with self.assertRaises((ValueError, PermissionError)):
                    ledger.claim(ticket, "actor")
                self.assertFalse(ledger.abandon(ticket))
                self.assertEqual(ledger.records(), before)
                with self.assertRaises(ReceiptCapacityError):
                    ledger.reserve("actor", "submit")
                self.assertTrue(ledger.finish(ticket, outcome=outcome, job_id="job-1"))
                self.assertEqual(ledger.records(), (ReceiptRecord(ticket, outcome, "job-1"),))
                self.assertFalse(ledger.finish(ticket, outcome=outcome))
                with self.assertRaises((ValueError, PermissionError)):
                    ledger.claim(ticket, "actor")
                replacement = ledger.reserve("actor", "submit")
                ledger.claim(replacement, "actor")
                self.assertTrue(ledger.finish(replacement, outcome=ReceiptOutcome.COMPLETED))

    def test_concurrent_double_claim_activates_exactly_once(self) -> None:
        ledger = RequestReceiptLedger(ReceiptLimits(1, 1), clock=lambda: 11)
        ticket = ledger.reserve("actor", "submit")
        start = Event()
        results = Queue()

        def claim() -> None:
            try:
                if not start.wait(timeout=3):
                    raise RuntimeError("claim test start timed out")
                ledger.claim(ticket, "actor")
                results.put(True)
            except Exception as error:
                results.put(error)

        workers = [Thread(target=claim, daemon=True) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
        finally:
            start.set()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "claim did not release the ledger lock")
        outcomes = [results.get_nowait() for _ in workers]
        self.assertEqual(sum(result is True for result in outcomes), 1)
        self.assertEqual(sum(isinstance(result, (ValueError, PermissionError)) for result in outcomes), 1)
        self.assertEqual(ledger.records(), (ReceiptRecord(ticket, None),))
        self.assertFalse(ledger.abandon(ticket))
        self.assertTrue(ledger.finish(ticket, outcome=ReceiptOutcome.COMPLETED))

    def test_concurrent_reserve_has_unique_order_and_atomic_pending_limit(self) -> None:
        clock = Mock(return_value=10)
        ledger = RequestReceiptLedger(ReceiptLimits(6, 2), clock=clock)
        start = Event()
        results = Queue()

        def reserve() -> None:
            try:
                if not start.wait(timeout=3):
                    raise RuntimeError("concurrent test start timed out")
                results.put(ledger.reserve("actor", "submit"))
            except Exception as error:
                results.put(error)

        workers = [Thread(target=reserve, daemon=True) for _ in range(12)]
        try:
            for worker in workers:
                worker.start()
        finally:
            start.set()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "reserve did not release the ledger lock")
        outcomes = [results.get_nowait() for _ in workers]
        tickets = [result for result in outcomes if isinstance(result, RequestReceipt)]
        failures = [result for result in outcomes if not isinstance(result, RequestReceipt)]
        self.assertEqual(sorted(ticket.sequence for ticket in tickets), list(range(1, 7)))
        self.assertEqual(len(failures), 6)
        self.assertTrue(all(isinstance(error, ReceiptCapacityError) for error in failures))
        self.assertEqual(clock.call_count, 6)
        self.assertEqual([record.receipt.sequence for record in ledger.records()], list(range(1, 7)))
        for ticket in tickets:
            ledger.validate(ticket, "actor")


if __name__ == "__main__":
    unittest.main()
