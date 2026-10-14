"""Core live capture/book evidence; no HTTP, service execution or physical seal."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier, Event, RLock
import unittest
from unittest.mock import patch

from core.attack_simulation.declared_ingress import (
    DeclaredArrival, DeclaredIngressBook, DeclaredIngressCapacityError,
)
from core.attack_simulation.declared_world_clock import DeclaredWorldClock, WorldClockSettings
from core.attack_simulation.event_commit_boundary import EventOrderError, EventPosition, UnresolvedIngressError
from core.attack_simulation.live_ingress import LiveIngress, LiveIngressSettings
from core.attack_simulation.request_receipts import ReceiptLimits, ReceiptOutcome, RequestReceiptLedger
from core.attack_simulation.victim_workload import CausalBarrier, Submission, begin_dispatch, bind_submission
from tests.test_declared_ingress import workload


class Host:
    def __init__(self, value=1_000_000):
        self.value = value
        self.calls = 0
        self.pause = False
        self.entered, self.release = Event(), Event()

    def __call__(self):
        self.calls += 1
        value = self.value
        if self.pause:
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError("test host barrier timed out")
        return value


def position(time, order=0, domain="world"):
    return EventPosition(domain, time, order)


def settings(delay=0, pending=8, history=8):
    return LiveIngressSettings("world", "host_monotonic_ns", delay, "explicit test assumption", pending, history)


def assembly(*, delay=0, pending=8, history=8, rate=1, origin=1_000_000, start=True):
    host = Host(origin)
    clock = DeclaredWorldClock(WorldClockSettings("world", rate), host_ns=host)
    book = DeclaredIngressBook("world", pending, history)
    gate = RLock()
    live = LiveIngress(settings(delay, pending, history), clock=clock, book=book, gate=gate, producer_id="live")
    if start:
        clock.mark_ready(runtime_ready=True, ingress_ready=True, workload_ready=True, manifest_frozen=True)
        clock.start()
    return live, host, clock, book, gate


class LiveIngressTests(unittest.TestCase):
    def test_required_frozen_configuration_and_no_constructor_sampling(self):
        live, host, clock, book, _ = assembly(start=False)
        self.assertEqual(host.calls, 0)
        self.assertEqual(live.settings.outbound_delay_us, 0)
        with self.assertRaises(FrozenInstanceError):
            live.settings.basis = "changed"
        for field, values in {
            "outbound_delay_us": [True, -1, "0", float("nan"), float("inf"), 10**1000],
            "basis": [None, "", "  ", "x" * 1025],
            "clock_domain": ["", None, "非ASCII"],
            "host_clock_domain": ["world", None, "other_host"],
            "max_pending": [0, -1, True, 1.5],
            "max_history": [0, -1, True, 1.5],
        }.items():
            for value in values:
                with self.subTest(field=field, value=str(value)[:15]), self.assertRaises(ValueError):
                    replace(settings(), **{field: value})
        with self.assertRaises(TypeError):
            LiveIngressSettings("world", "host_monotonic_ns", 0, "basis", 8)
        with self.assertRaises(RuntimeError):
            live.capture("alice", "program.submit")
        self.assertEqual(live.snapshot().reservations, ())
        self.assertEqual(live._receipts.records(), ())

    def test_prestart_shared_domain_capacity_and_namespace_checks(self):
        live, host, clock, book, gate = assembly(start=False)
        for wrong_book in (DeclaredIngressBook("other", 8, 8), DeclaredIngressBook("world", 1, 8)):
            with self.assertRaises(ValueError):
                LiveIngress(settings(), clock=clock, book=wrong_book, gate=gate, producer_id="other")
        book.reserve("occupied", 1, position(10))
        with self.assertRaises(ValueError):
            LiveIngress(settings(), clock=clock, book=book, gate=gate, producer_id="occupied")
        clock.mark_ready(runtime_ready=True, ingress_ready=True, workload_ready=True, manifest_frozen=True)
        clock.start()
        with self.assertRaises(RuntimeError):
            LiveIngress(settings(), clock=clock, book=book, gate=gate, producer_id="other")

    def test_same_hostcall_exact_receipt_and_explicit_zero_positive_delay(self):
        for delay in (0, 7.5):
            with self.subTest(delay=delay):
                live, host, clock, book, _ = assembly(delay=delay, rate=2)
                host.value += 2500
                calls = host.calls
                receipt = live.capture("alice", "program.submit")
                token = live.reservation(receipt)
                self.assertEqual(host.calls, calls + 1)
                self.assertIs(live._receipts.records()[0].receipt, receipt)
                self.assertIs(book.snapshot().reservations[0], token)
                self.assertEqual(receipt.received_at_ns, clock._last_ns)
                self.assertEqual(receipt.clock_domain, live.settings.host_clock_domain)
                self.assertEqual(token.position, position(5 + delay))
                self.assertEqual(clock._map_receipt_ns(receipt.received_at_ns, order=0), position(5))
                self.assertFalse(hasattr(live, "host_trace"))
                self.assertFalse(hasattr(live, "map_receipt_ns"))
                with self.assertRaises(TypeError):
                    live.capture("alice", "program.submit", body={"ignored": True})

    def test_invalid_identity_has_no_hostcall_receipt_or_reservation(self):
        live, host, *_ = assembly()
        for principal, operation in (("", "program.submit"), (object(), "program.submit"), ("alice", "")):
            calls = host.calls
            with self.assertRaises(ValueError):
                live.capture(principal, operation)
            self.assertEqual(host.calls, calls)
        self.assertEqual(live._receipts.records(), ())
        self.assertEqual(live.snapshot().reservations, ())

    def test_same_ns_future_receipt_is_possible_and_same_time_never_confirmed(self):
        live, host, *_ = assembly()
        host.value += 1000
        sampled = live.sample()
        live.confirm_admission_cutoff(position(0.5, 10**6))
        for order in (0, 1, 10**100):
            with self.assertRaises(EventOrderError):
                live.confirm_admission_cutoff(position(sampled.time_us, order))
        first = live.capture("alice", "program.submit")
        second = live.capture("victim", "job.cancel")
        self.assertEqual(first.received_at_ns, second.received_at_ns)
        self.assertEqual(live.reservation(first).position, sampled)
        self.assertEqual(live.reservation(second).position, sampled)
        self.assertNotEqual(live.reservation(first).key, live.reservation(second).key)
        self.assertEqual(live.snapshot().confirmed_cutoff, position(0.5, 10**6))

    def test_backward_invalid_host_and_world_overflow_preserve_book_and_ledger(self):
        live, host, clock, *_ = assembly()
        host.value += 10_000
        good = live.sample()
        for value in (host.value - 1, -1, True, 1.5, 10**1000):
            host.value = value
            before = live.snapshot()
            with self.assertRaises(ValueError):
                live.capture("alice", "program.submit")
            self.assertEqual(live.snapshot(), before)
            self.assertEqual(live._receipts.records(), ())
        host.value = clock._last_ns
        self.assertEqual(live.sample(), good)
        live.capture("alice", "program.submit")

    def test_float_host_progress_collapse_and_underflow_do_not_become_receipts(self):
        for rate, huge in ((1, 10**22), (5e-324, 1)):
            live, host, *_ = assembly(rate=rate, origin=0)
            if rate == 1:
                host.value = huge
                live.sample()
                host.value += 1
            else:
                host.value = huge
            for _ in range(2):  # Repeating the rejected ns must not bypass precision validation.
                with self.assertRaisesRegex(ValueError, "collapsed"):
                    live.capture("alice", "program.submit")
            self.assertEqual(live._receipts.records(), ())
            self.assertEqual(live.snapshot().reservations, ())

    def test_delay_addition_collapse_overflow_and_distinct_arrival_collapse(self):
        for delay, ns in ((1, 10**22), (1e308, 10**311)):
            live, host, *_ = assembly(delay=delay, origin=0)
            host.value = ns
            with self.assertRaises(ValueError):
                live.capture("alice", "program.submit")
            self.assertEqual(live._receipts.records(), ())
            self.assertEqual(live.snapshot().reservations, ())
        live, host, *_ = assembly(delay=1e20, origin=0)
        first = live.capture("alice", "program.submit")
        host.value = 1000
        with self.assertRaisesRegex(ValueError, "distinct receipt"):
            live.capture("victim", "job.cancel")
        self.assertEqual(live.snapshot().reservations, (live.reservation(first),))
        self.assertEqual(len(live._receipts.records()), 1)

    def test_pending_capacity_stops_before_hostcall_and_preserves_every_source(self):
        live, host, *_ = assembly(pending=1)
        token, = live.publish_snapshot("victim", (DeclaredArrival(1, position(5)),), ())
        calls, before = host.calls, live.snapshot()
        with self.assertRaises(DeclaredIngressCapacityError):
            live.capture("alice", "program.submit")
        self.assertEqual(host.calls, calls)
        self.assertEqual(live.snapshot(), before)
        self.assertEqual(live._receipts.records(), ())
        self.assertIs(live.snapshot().reservations[0], token)

    def test_non_evicting_history_blocks_before_next_service_callback(self):
        live, host, *_ = assembly(history=1)
        first, second = (live.capture("alice", "program.submit") for _ in range(2))
        host.value += 2000
        token = live.next_due(position(1))
        self.assertIs(token, live.reservation(first))
        retired = live.retire(token, decision="admitted", job_id="job-one")
        callback = []
        with self.assertRaises(DeclaredIngressCapacityError):
            grant = live.next_due(position(1))
            callback.append(grant)
        calls = host.calls
        with self.assertRaises(DeclaredIngressCapacityError):
            live.capture("alice", "program.submit")
        self.assertEqual(host.calls, calls)
        self.assertEqual(callback, [])
        self.assertEqual(live.snapshot().history, (retired,))
        self.assertEqual(live.snapshot().reservations, (live.reservation(second),))
        self.assertEqual(len(live._receipts.records()), 2)
        self.assertIs(live._receipts.records()[0].receipt, first)

    def test_late_compile_failed_prepare_lost_response_keep_exact_grant_and_cutoff_blocked(self):
        live, host, *_ = assembly()
        receipt = live.capture("alice", "program.submit")  # Before host compilation.
        token = live.reservation(receipt)
        entered, release = Event(), Event()

        def compile_program():
            self.assertIs(live.snapshot().reservations[0], token)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test compilation barrier timed out")
            raise RuntimeError("failed host preparation")

        with ThreadPoolExecutor(max_workers=1) as pool:
            compiling = pool.submit(compile_program)
            self.assertTrue(entered.wait(2))
            try:
                host.value += 10_000
                self.assertEqual(live.sample(), position(10))
                with self.assertRaises(UnresolvedIngressError):
                    live.confirm_admission_cutoff(position(5))
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "failed host"):
                compiling.result(timeout=3)
        for failure in ("host prepare failure", "response lost"):
            with self.subTest(failure=failure):
                self.assertIs(live.next_due(position(5)), token)
                # A callback error has no retirement side effect; the caller
                # retries the exact grant after the common service lost a reply.
                with self.assertRaisesRegex(RuntimeError, failure):
                    raise RuntimeError(failure)
                self.assertEqual(live._receipts.records()[0].outcome, None)
                with self.assertRaises(UnresolvedIngressError):
                    live.confirm_admission_cutoff(position(5))
        self.assertFalse(hasattr(live, "abandon"))
        live.retire(token, decision="denied")
        live.confirm_admission_cutoff(position(5))
        self.assertEqual(live.snapshot().confirmed_cutoff, position(5))

    def test_exact_foreign_receipts_tokens_and_ungranted_retirement_are_rejected(self):
        live, host, *_ = assembly()
        receipt = live.capture("alice", "program.submit")
        token = live.reservation(receipt)
        foreign_ledger = RequestReceiptLedger(ReceiptLimits(8, 8), clock=lambda: receipt.received_at_ns)
        foreign_receipt = foreign_ledger.reserve("alice", "program.submit")
        self.assertEqual(foreign_receipt, receipt)
        for wrong in (foreign_receipt, replace(receipt), object()):
            with self.assertRaises(ValueError):
                live.reservation(wrong)
        foreign_book = DeclaredIngressBook("world", 8, 8)
        foreign_token = foreign_book.reserve(token.producer_id, token.event_id, token.position)
        for wrong in (foreign_token, replace(token), object()):
            with self.assertRaises(ValueError):
                live.retire(wrong, decision="admitted")
        with self.assertRaises(UnresolvedIngressError):
            live.retire(token, decision="admitted")
        host.value += 1000
        self.assertIs(live.next_due(position(0)), token)
        before = live.snapshot()
        with self.assertRaises(ValueError):
            live.retire(token, decision="denied", job_id="not-a-denial")
        with self.assertRaises(ValueError):
            live.retire(token, decision="admitted", job_id="")
        self.assertEqual(live.snapshot(), before)

    def test_retirement_replay_preserves_decision_receipt_and_later_grant(self):
        live, host, *_ = assembly()
        first, second = (live.capture("alice", "program.submit") for _ in range(2))
        host.value += 1000
        token = live.next_due(position(0))
        record = live.retire(token, decision="admitted", job_id="job-one")
        self.assertIs(live.retire(token, decision="admitted", job_id="job-one"), record)
        later = live.next_due(position(0))
        self.assertIs(later, live.reservation(second))
        self.assertIs(live.retire(token, decision="admitted", job_id="job-one"), record)
        self.assertIs(live.snapshot().granted, later)
        for decision, job_id in (("denied", None), ("admitted", "job-other")):
            with self.assertRaises(ValueError):
                live.retire(token, decision=decision, job_id=job_id)
        self.assertEqual(live._receipts.records()[0].outcome, ReceiptOutcome.COMPLETED)
        self.assertIs(live._receipts.records()[0].receipt, first)

    def test_all_sources_and_result_barriers_share_book_and_cannot_drop_arrivals(self):
        live, host, clock, book, _ = assembly(start=False)
        state = workload(branch=True)
        token, = live.publish("victim", state)
        self.assertIs(book.snapshot().reservations[0], token)
        with self.assertRaises(ValueError):
            live.publish_snapshot("live", (), ())
        with self.assertRaises(UnresolvedIngressError):
            live.publish_snapshot("victim-explicit", (DeclaredArrival(1, position(5)),), ())
            live.publish_snapshot("victim-explicit", (), ())
        clock.mark_ready(runtime_ready=True, ingress_ready=True, workload_ready=True, manifest_frozen=True)
        clock.start()
        host.value += 100_000
        explicit = live.next_due(position(50))
        live.retire(explicit, decision="denied")
        self.assertIs(live.next_due(position(50)), token)
        dispatch = begin_dispatch(state, token.event_id, through=position(50))
        live.publish("victim", dispatch)
        waiting = bind_submission(dispatch, token.event_id, Submission("job-victim"))
        live.retire(token, waiting)
        self.assertEqual(len(book.snapshot().barriers), 1)
        with self.assertRaises(UnresolvedIngressError):
            live.confirm_admission_cutoff(position(50))
        live.refine_barrier("victim", 1, position(60))
        live.confirm_admission_cutoff(position(50))

    def test_cutoff_refusals_are_atomic_monotone_and_domain_checked(self):
        live, host, *_ = assembly()
        host.value += 10_000
        live.confirm_admission_cutoff(position(5))
        before = live.snapshot()
        for bad in (position(4), position(10, 10**30), position(100), position(6, domain="other")):
            with self.assertRaises(ValueError):
                live.confirm_admission_cutoff(bad)
            self.assertEqual(live.snapshot(), before)
        self.assertEqual(live.sample(), position(10))

    def test_book_refusal_creates_no_orphan_receipt_and_reuses_next_identity(self):
        live, host, *_ = assembly()
        # Simulate a trusted source configuration error, NOT safe live sealing.
        live._book.confirm_admission_cutoff(position(1))
        with self.assertRaises(EventOrderError):
            live.capture("alice", "program.submit")
        self.assertEqual(live._receipts.records(), ())
        self.assertEqual(live.snapshot().reservations, ())
        host.value += 2000
        receipt = live.capture("alice", "program.submit")
        self.assertEqual(receipt.sequence, 1)
        self.assertEqual(live.reservation(receipt).event_id, 1)

    def test_duplicate_live_namespace_cannot_rebind_another_owners_exact_token(self):
        live, host, clock, book, gate = assembly(start=False)
        duplicate = LiveIngress(settings(), clock=clock, book=book, gate=gate, producer_id="live")
        clock.mark_ready(runtime_ready=True, ingress_ready=True, workload_ready=True, manifest_frozen=True)
        clock.start()
        first = live.capture("alice", "program.submit")
        with self.assertRaisesRegex(ValueError, "foreign evidence"):
            duplicate.capture("victim", "program.submit")
        self.assertEqual(duplicate._receipts.records(), ())
        self.assertIs(book.snapshot().reservations[0], live.reservation(first))

    def test_same_time_global_grant_waits_for_past_time_to_prevent_late_live_backinsert(self):
        live, host, *_ = assembly()
        declared, = live.publish_snapshot("z-victim", (DeclaredArrival(1, position(0)),), ())
        with self.assertRaises(EventOrderError):
            live.next_due(position(0, 10**20))
        receipt = live.capture("alice", "program.submit")
        host.value += 1000
        self.assertIs(live.next_due(position(0)), live.reservation(receipt))
        live.retire(live.reservation(receipt), decision="denied")
        self.assertIs(live.next_due(position(0)), declared)

    def test_close_preserves_pending_and_retired_evidence_without_ledger_eviction(self):
        live, host, *_ = assembly(history=1)
        first, second, third = (live.capture("alice", "program.submit") for _ in range(3))
        host.value += 1000
        token = live.next_due(position(0))
        live.retire(token, decision="denied")
        before, receipts, calls = live.snapshot(), live._receipts.records(), host.calls
        live.close()
        live.close()
        self.assertEqual(live.snapshot(), replace(before, closed=True))
        self.assertEqual(live._receipts.records(), receipts)
        self.assertEqual(host.calls, calls)
        self.assertIs(live.reservation(first), token)
        for action in (live.sample, lambda: live.capture("alice", "program.submit"),
                       lambda: live.next_due(position(0)), lambda: live.retire(token, decision="denied"),
                       lambda: live.confirm_admission_cutoff(position(0)),
                       lambda: live.publish_snapshot("victim", (), ())):
            with self.assertRaises(RuntimeError):
                action()
        self.assertEqual(len(live.snapshot().reservations), 2)
        self.assertIs(live._receipts.records()[1].receipt, second)
        self.assertIs(live._receipts.records()[2].receipt, third)

    def test_capture_sample_confirm_race_cannot_cross_capture_in_progress(self):
        live, host, *_ = assembly()
        host.value += 5000
        host.pause = True
        with ThreadPoolExecutor(max_workers=3) as pool:
            capture = pool.submit(live.capture, "alice", "program.submit")
            self.assertTrue(host.entered.wait(2))
            sample = pool.submit(live.sample)
            confirm = pool.submit(live.confirm_admission_cutoff, position(6))
            host.value += 5000
            host.release.set()
            receipt = capture.result(timeout=3)
            self.assertEqual(sample.result(timeout=3), position(10))
            with self.assertRaises(UnresolvedIngressError):
                confirm.result(timeout=3)
        self.assertEqual(live.reservation(receipt).position, position(5))
        self.assertIsNone(live.snapshot().confirmed_cutoff)

    def test_confirm_sample_in_progress_cannot_cross_future_same_ns_capture(self):
        live, host, *_ = assembly()
        host.value += 5000
        host.pause = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            confirm = pool.submit(live.confirm_admission_cutoff, position(4))
            self.assertTrue(host.entered.wait(2))
            capture = pool.submit(live.capture, "alice", "program.submit")
            host.release.set()
            confirm.result(timeout=3)
            receipt = capture.result(timeout=3)
        self.assertEqual(live.reservation(receipt).position, position(5))
        self.assertEqual(live.snapshot().confirmed_cutoff, position(4))

    def test_retire_close_race_retains_either_exact_grant_or_full_completion(self):
        live, host, *_ = assembly()
        receipt = live.capture("alice", "program.submit")
        host.value += 1000
        token = live.next_due(position(0))
        rendezvous = Barrier(2, timeout=2)

        def retire():
            rendezvous.wait()
            return live.retire(token, decision="admitted", job_id="job-one")

        def close():
            rendezvous.wait()
            live.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            completion, shutdown = pool.submit(retire), pool.submit(close)
            try:
                result = completion.result(timeout=3)
            except RuntimeError:
                result = None
            shutdown.result(timeout=3)
        seen = live.snapshot()
        self.assertTrue(seen.closed)
        if result is None:
            self.assertIs(seen.granted, token)
            self.assertEqual(seen.reservations, (token,))
            self.assertIsNone(live._receipts.records()[0].outcome)
        else:
            self.assertIsNone(seen.granted)
            self.assertEqual(seen.history, (result,))
            self.assertEqual(live._receipts.records()[0].job_id, "job-one")
        self.assertIs(live._receipts.records()[0].receipt, receipt)

    def test_simultaneous_equal_ns_capture_sample_confirm_and_close_are_serialized(self):
        live, host, *_ = assembly()
        host.value += 5000
        rendezvous = Barrier(4, timeout=2)

        def run(action):
            rendezvous.wait()
            return action()

        actions = (lambda: live.capture("alice", "program.submit"),
                   lambda: live.capture("victim", "job.cancel"),
                   live.sample, lambda: live.confirm_admission_cutoff(position(4)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(run, action) for action in actions]
            first, second, sampled, _ = [f.result(timeout=3) for f in futures]
        self.assertEqual(first.received_at_ns, second.received_at_ns)
        self.assertEqual(sampled, position(5))
        self.assertEqual(len(live.snapshot().reservations), 2)
        self.assertEqual(live.snapshot().confirmed_cutoff, position(4))
        rendezvous = Barrier(2, timeout=2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            capture = pool.submit(run, lambda: live.capture("alice", "job.status"))
            close = pool.submit(run, live.close)
            try:
                receipt = capture.result(timeout=3)
            except RuntimeError:
                receipt = None
            close.result(timeout=3)
        self.assertEqual(len(live.snapshot().reservations), 2 + (receipt is not None))
        self.assertEqual(len(live._receipts.records()), 2 + (receipt is not None))

    def test_sample_confirm_share_provided_gate_and_host_callback_cannot_reenter(self):
        live, host, clock, _, gate = assembly()
        host.value += 1000
        started = Event()

        def sample():
            started.set()
            return live.sample()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with gate:
                future = pool.submit(sample)
                self.assertTrue(started.wait(2))
                self.assertFalse(future.done())
            self.assertEqual(future.result(timeout=3), position(1))
        source = clock._host_ns

        def reentrant():
            live.capture("alice", "program.submit")
            return source()

        with patch.object(clock, "_host_ns", reentrant):
            with self.assertRaisesRegex(RuntimeError, "reenter"):
                live.capture("alice", "program.submit")
        self.assertEqual(live.snapshot().reservations, ())


if __name__ == "__main__":
    unittest.main()
