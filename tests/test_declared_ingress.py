"""Bounded declared metadata tests; real common-service integration belongs to API."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
from threading import Barrier
import unittest
from unittest.mock import patch

from core.attack_simulation.declared_ingress import (
    DeclaredArrival, DeclaredIngressBook, DeclaredIngressCapacityError,
)
from core.attack_simulation.event_commit_boundary import (
    EventOrderError, EventPosition, UnresolvedIngressError,
)
from core.attack_simulation.job_service import JobStatusView
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.program_request import CircuitProgram
from core.attack_simulation.victim_workload import (
    Branch, CausalBarrier, DeclaredDurations, Outcome, OwnProjectedResult,
    Phase, RegisteredProgram, Submission, TimedStart, WorkloadPlan, WorkloadStep,
    advance_result, begin_dispatch, bind_submission, dispatch_operation, start_workload,
)


def position(time_us, order=999, domain="world"):
    return EventPosition(domain, time_us, order)


def workload(actor="alice", *, times=(10,), branch=False):
    program = RegisteredProgram("pair", CircuitProgram.from_editor_json(json.dumps({
        "logical_qubits": 2, "initial_states": [0, 0], "classical_bits": 2,
        "columns": [{"step": 0, "gates": [{"type": "H", "targets": [0]}]},
                    {"step": 1, "gates": [{"type": "CNOT", "targets": [1],
                                           "controls": [0]}]}],
    })))
    steps = (
        WorkloadStep("first", "pair", "qpu", 4, DeclaredDurations(0, 0, 0), 1,
                     (Branch(Outcome.SUCCEEDED, "last"),) if branch else ()),
        WorkloadStep("last", "pair", "qpu", 4, DeclaredDurations(0, 0, 0), 1),
    )
    plan = WorkloadPlan(f"plan-{actor}", actor, "world", (program,), steps,
                        tuple(TimedStart("first", time) for time in times),
                        2 * len(times), 2 * len(times))
    return start_workload(plan, run_id=f"run-{actor}")


class DeclaredIngressTests(unittest.TestCase):
    def book(self, pending=8, history=8):
        return DeclaredIngressBook("world", pending, history)

    def test_proven_result_bound_survives_republication_but_not_terminal_transition(self):
        state = workload(branch=True)
        book = self.book()
        book.publish(state.plan.plan_id, state)
        token = book.next_due(position(100))
        state = begin_dispatch(state, token.event_id, through=position(100))
        book.publish(state.plan.plan_id, state)
        state = bind_submission(state, token.event_id, Submission("job-one"))
        book.retire(token, state)
        book.refine_barrier(state.plan.plan_id, 1, position(50, 0))
        before = book.snapshot()
        with self.assertRaises(EventOrderError):
            book.refine_barrier(state.plan.plan_id, 1, position(40, 0))
        self.assertEqual(book.snapshot(), before)
        book.publish(state.plan.plan_id, state)
        self.assertEqual(book.snapshot().barriers[0].earliest_next_arrival, position(50, 0))
        book.confirm_admission_cutoff(position(40))
        state = advance_result(state, 1, available=position(50),
            result=OwnProjectedResult("job-one", Outcome.SUCCEEDED))
        book.publish(state.plan.plan_id, state)
        self.assertEqual(book.snapshot().barriers, ())
        self.assertGreater(book.snapshot().reservations[0].position.key, position(50).key)
        with self.assertRaises(ValueError):
            book.refine_barrier(state.plan.plan_id, 1, position(60))

    def test_chronological_selection_and_global_ties_ignore_registration_order(self):
        declarations = (("z", 1, position(10, 1)), ("a", 10, position(10, 1)),
                        ("a", 2, position(10, 1)), ("b", 1, position(5, 9)),
                        ("z", 2, position(10, 0)))
        traces = []
        for values in (declarations, tuple(reversed(declarations))):
            book = self.book()
            for producer, event, arrival in values:
                book.reserve(producer, event, arrival)
            self.assertIsNone(book.next_due(position(4)))
            trace = []
            for _ in declarations:
                token = book.next_due(position(20))
                trace.append((token.producer_id, token.event_id, token.position))
                book.retire(token, decision="admitted")
            self.assertIsNone(book.next_due(position(20)))
            traces.append(trace)
        self.assertEqual(traces[0], traces[1])
        self.assertEqual([(p, e) for p, e, _ in traces[0]],
                         [("b", 1), ("z", 2), ("a", 2), ("a", 10), ("z", 1)])
        self.assertIsNone(book.snapshot().confirmed_cutoff)  # No silent seal.

    def test_reservation_is_immutable_idempotent_and_retired_replays_stay_retired(self):
        book = self.book()
        token = book.reserve("source", 1, position(10))
        self.assertIs(book.reserve("source", 1, position(10)), token)
        self.assertIsNone(book.decision(token))
        with self.assertRaises(FrozenInstanceError):
            token.event_id = 2
        with self.assertRaises(FrozenInstanceError):
            token.position.order = 2
        self.assertIs(book.next_due(position(10)), token)
        record = book.retire(token, decision="denied")
        book.confirm_admission_cutoff(position(10))
        before = book.snapshot()
        self.assertIs(book.reserve("source", 1, position(10)), token)
        self.assertIs(book.retire(token, decision="denied"), record)
        self.assertEqual(book.decision(token), "denied")
        self.assertIsNone(book.next_due(position(100)))
        self.assertEqual(book.snapshot(), before)
        for action in (lambda: book.reserve("source", 1, position(11)),
                       lambda: book.retire(token, decision="admitted")):
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(book.snapshot(), before)

    def test_invalid_limits_ids_positions_and_foreign_tokens_do_not_mutate(self):
        for invalid in (0, -1, True, 1.5):
            for limits in ((invalid, 1), (1, invalid)):
                with self.assertRaises(ValueError):
                    self.book(*limits)
        book = self.book()
        token = book.reserve("alice", 1, position(10))
        other = self.book().reserve("alice", 1, position(10))
        before = book.snapshot()
        actions = [lambda value=value: book.reserve(value, 2, position(20))
                   for value in ("", "a" * 129, "日本", "a b", 123)]
        actions += [lambda value=value: book.reserve("alice", value, position(20))
                    for value in (0, -1, True, 1.0)]
        actions += [lambda: book.reserve("alice", 2, position(20, domain="other")),
                    lambda: book.reserve("alice", 2, object()),
                    lambda: book.next_due(position(20, domain="other"))]
        actions += [lambda value=value: book.retire(value, decision="denied")
                    for value in (replace(token), other, object())]
        for action in actions:
            with self.assertRaises((ValueError, TypeError)):
                action()
            self.assertEqual(book.snapshot(), before)
        with self.assertRaises(UnresolvedIngressError):
            book.retire(token, decision="admitted")  # Not granted.
        self.assertEqual(book.snapshot(), before)

    def test_explicit_cutoff_rejects_late_arrival_and_barrier_without_mutation(self):
        book = self.book()
        book.confirm_admission_cutoff(position(10, 2))
        before = book.snapshot()
        for arrival in (position(9), position(10, 1), position(10, 2)):
            with self.assertRaises(EventOrderError):
                book.reserve("source", 1, arrival)
        with self.assertRaises(EventOrderError):
            book.publish_snapshot("source", (DeclaredArrival(1, position(11)),),
                                  (CausalBarrier(2, position(10, 2)),))
        with self.assertRaises(EventOrderError):
            book.confirm_admission_cutoff(position(10, 1))
        self.assertEqual(book.snapshot(), before)
        book.reserve("source", 1, position(10, 3))

    def test_publish_two_actors_and_terminal_dispatch_without_result_branch(self):
        book = self.book()
        alice, bob = workload("alice"), workload("bob")
        bob_token, = book.publish("bob", bob)
        alice_token, = book.publish("alice", alice)
        self.assertIs(book.next_due(position(10)), alice_token)
        dispatch = begin_dispatch(alice, 1, through=position(10))
        self.assertEqual(dispatch.barriers(), ())
        book.publish("alice", dispatch)
        self.assertIs(book.next_due(position(100)), alice_token)
        self.assertEqual(book.snapshot().reservations, (alice_token, bob_token))
        waiting = bind_submission(dispatch, 1, Submission("job-a"))
        before = book.snapshot()
        with self.assertRaises(UnresolvedIngressError):
            book.publish("alice", waiting)
        self.assertEqual(book.snapshot(), before)
        record = book.retire(alice_token, waiting)
        self.assertEqual(record.decision, "admitted")
        self.assertEqual(book.snapshot().reservations, (bob_token,))
        self.assertIs(book.next_due(position(100)), bob_token)

    def test_own_result_barrier_excludes_only_matching_source_and_event(self):
        book = self.book()
        book.publish_snapshot("alice", (DeclaredArrival(1, position(10, 1), True),
                                         DeclaredArrival(2, position(20, 1))),
                              (CausalBarrier(1, position(10, 0)),))
        token = book.next_due(position(100))
        self.assertEqual(token.key, ("alice", 1))
        book.retire(token, decision="admitted")
        self.assertIsNone(book.next_due(position(100)))  # Same producer, different event.
        self.assertEqual(book.snapshot().barriers[0].key, ("alice", 1))
        book.publish_snapshot("alice", (DeclaredArrival(2, position(20, 1)),), ())
        self.assertEqual(book.next_due(position(100)).key, ("alice", 2))

        book = self.book()
        book.publish_snapshot("a", (), (CausalBarrier(1, position(10, 0)),))
        book.reserve("b", 1, position(10, 1))
        self.assertIsNone(book.next_due(position(100)))  # Equal IDs across sources are foreign.

    def test_blocked_earliest_candidate_never_allows_later_dispatch_to_bypass(self):
        book = self.book()
        book.publish_snapshot("a", (DeclaredArrival(1, position(10), True),), ())
        book.reserve("b", 1, position(20))
        book.publish_snapshot("c", (), (CausalBarrier(1, position(5)),))
        before = book.snapshot()
        self.assertIsNone(book.next_due(position(100)))
        self.assertEqual(book.snapshot(), before)
        book.publish_snapshot("c", (), ())
        token = book.next_due(position(100))
        self.assertEqual(token.key, ("a", 1))
        self.assertIs(book.next_due(position(100)), token)
        self.assertIs(book.snapshot(position(100)).eligible, token)

    def test_atomic_waiting_retirement_keeps_barrier_then_result_replaces_it(self):
        book = self.book()
        alice = workload("alice", branch=True)
        bob = workload("bob", times=(40,))
        book.publish("alice", alice)
        bob_token, = book.publish("bob", bob)
        token = book.next_due(position(100))
        dispatch = begin_dispatch(alice, 1, through=position(100))
        book.publish("alice", dispatch)
        waiting = bind_submission(dispatch, 1, Submission("job-a"))
        book.retire(token, waiting)
        snapshot = book.snapshot(position(100))
        self.assertEqual(snapshot.reservations, (bob_token,))
        self.assertEqual(snapshot.barriers[0].key, ("alice", 1))
        self.assertIsNone(snapshot.eligible)
        self.assertIsNone(book.next_due(position(100)))
        self.assertIsNone(book.snapshot().confirmed_cutoff)
        with self.assertRaises(UnresolvedIngressError):
            book.confirm_admission_cutoff(position(30))
        # A stale dispatch replay cannot resurrect an already admitted arrival.
        self.assertIs(book.publish("alice", dispatch)[0], token)
        self.assertEqual(book.snapshot().reservations, (bob_token,))
        book.publish("alice", waiting)
        successor = advance_result(waiting, 1, available=position(30),
                                   result=OwnProjectedResult("job-a", Outcome.SUCCEEDED))
        book.publish("alice", successor)
        snapshot = book.snapshot(position(100))
        self.assertEqual(snapshot.barriers, ())
        self.assertEqual([(t.producer_id, t.event_id) for t in snapshot.reservations],
                         [("alice", 2), ("bob", 1)])
        self.assertEqual(book.next_due(position(100)).key, ("alice", 2))

    def test_atomic_denial_retirement_and_completion_replay(self):
        book = self.book()
        state = workload(branch=True)
        book.publish("alice", state)
        token = book.next_due(position(100))
        dispatch = begin_dispatch(state, 1, through=position(100))
        book.publish("alice", dispatch)
        waiting = bind_submission(dispatch, 1, Submission(None, denied=True))
        record = book.retire(token, waiting)
        self.assertEqual(book.decision(token), "denied")
        before = book.snapshot()
        self.assertIs(book.retire(token, waiting), record)
        self.assertEqual(book.snapshot(), before)
        terminal = advance_result(waiting, 1, available=position(20))
        book.publish("alice", terminal)
        self.assertEqual(book.snapshot().barriers, ())
        self.assertIsNone(book.next_due(position(100)))

    def test_exception_replay_keeps_dispatch_token_and_identical_common_key(self):
        class FlakyClient:
            principal_id = "alice"

            def __init__(self):
                self.calls = []
                self.keys = {}

            def submit_declared_program(self, request, *, arrival):
                self.calls.append((request.idempotency_key, arrival))
                self.keys.setdefault(request.idempotency_key, "job-a")
                if len(self.calls) == 1:
                    raise RuntimeError("response lost after common admission")
                return JobStatusView(self.keys[request.idempotency_key], JobStatus.QUEUED)

        book, client, state = self.book(), FlakyClient(), workload()
        book.publish("alice", state)
        token = book.next_due(position(100))
        dispatch = begin_dispatch(state, 1, through=position(100))
        book.publish("alice", dispatch)
        book.reserve("bob", 1, position(20))
        before = book.snapshot()
        with self.assertRaises(RuntimeError):
            dispatch_operation(dispatch, 1, client)
        self.assertEqual(book.snapshot(), before)
        self.assertIs(book.next_due(position(100)), token)
        self.assertIs(book.publish("alice", dispatch)[0], token)
        submitted = dispatch_operation(dispatch, 1, client)
        book.retire(token, bind_submission(dispatch, 1, submitted))
        self.assertEqual(client.calls[0], client.calls[1])
        self.assertEqual(len(client.keys), 1)
        self.assertEqual(book.next_due(position(100)).producer_id, "bob")

    def test_atomic_publish_failures_preserve_both_sources_and_identity_binding(self):
        book = self.book(pending=2)
        alice = workload("alice", branch=True)
        token, = book.publish("alice", alice)
        other = book.reserve("bob", 1, position(50))
        dispatch = begin_dispatch(alice, 1, through=position(100))
        before = book.snapshot()
        extra = replace(dispatch.events[0], event_id=2, arrival=position(20),
                        phase=Phase.SCHEDULED)
        invalid_states = (
            replace(dispatch, events=(dispatch.events[0], extra)),  # Global capacity.
            replace(dispatch, run_id="other-run"),
            replace(dispatch, events=(replace(dispatch.events[0], arrival=position(11)),)),
            replace(dispatch, events=()),  # Cannot drop unresolved scheduled ingress.
            replace(dispatch, events=(replace(dispatch.events[0], event_id=True),)),
            replace(dispatch, events=(dispatch.events[0], dispatch.events[0])),
            replace(dispatch, closed=True),
        )
        for state in invalid_states:
            with self.assertRaises((ValueError, RuntimeError)):
                book.publish("alice", state)
            self.assertEqual(book.snapshot(), before)
        self.assertEqual(before.reservations, (token, other))
        # A failed first publication must not bind this producer to its run.
        with self.assertRaises(DeclaredIngressCapacityError):
            book.publish("new", workload("new"))
        self.assertEqual(book.snapshot(), before)
        book.next_due(position(100))
        waiting = bind_submission(dispatch, 1, Submission("job-a"))
        book.retire(token, waiting)
        book.publish("new", replace(workload("new"), run_id="replacement-run"))

    def test_atomic_completion_failure_does_not_retire_or_publish_anything(self):
        book = self.book(pending=1)
        state = workload(branch=True)
        book.publish("alice", state)
        token = book.next_due(position(100))
        dispatch = begin_dispatch(state, 1, through=position(100))
        book.publish("alice", dispatch)
        waiting = bind_submission(dispatch, 1, Submission("job-a"))
        extra = replace(waiting.events[0], event_id=2, arrival=position(20))
        bad = replace(waiting, events=waiting.events + (extra,))  # Two result barriers.
        before = book.snapshot()
        for state in (bad, dispatch, replace(waiting, run_id="other"),
                      replace(waiting, events=(replace(waiting.events[0], job_id=None),))):
            with self.assertRaises((ValueError, RuntimeError)):
                book.retire(token, state)
            self.assertEqual(book.snapshot(), before)
            self.assertIsNone(book.decision(token))
        with self.assertRaises(ValueError):
            book.retire(token, waiting, decision="denied")
        with self.assertRaises(ValueError):
            book.retire(token, decision="admitted")
        self.assertEqual(book.snapshot(), before)
        book.retire(token, waiting)
        self.assertEqual(book.decision(token), "admitted")

    def test_explicit_snapshot_atomic_invalid_barrier_and_dispatch_preservation(self):
        book = self.book(pending=2)
        book.publish_snapshot("a", (DeclaredArrival(1, position(10), True),),
                              (CausalBarrier(1, position(10, 0)),))
        before = book.snapshot()
        updates = (
            ((DeclaredArrival(1, position(10)),),
             (CausalBarrier(1, position(20)), CausalBarrier(1, position(30)))),
            ((DeclaredArrival(1, position(10)),), (CausalBarrier(True, position(20)),)),
            ((DeclaredArrival(1, position(10)),),
             (CausalBarrier(1, position(20, domain="other")),)),
            ((), ()),
            ((DeclaredArrival(1, position(11)),), ()),
        )
        for arrivals, barriers in updates:
            with self.assertRaises((ValueError, RuntimeError)):
                book.publish_snapshot("a", arrivals, barriers)
            self.assertEqual(book.snapshot(), before)
        book.publish_snapshot("a", (DeclaredArrival(1, position(10)),), ())
        self.assertEqual(book.snapshot().dispatching, before.dispatching)

    def test_global_barrier_limit_and_empty_workload_binding_limit_are_bounded(self):
        book = self.book(pending=1, history=1)
        book.publish_snapshot("a", (), (CausalBarrier(1, position(10)),))
        before = book.snapshot()
        with self.assertRaises(DeclaredIngressCapacityError):
            book.publish_snapshot("b", (DeclaredArrival(1, position(20)),),
                                  (CausalBarrier(1, position(30)),))
        self.assertEqual(book.snapshot(), before)
        empty = replace(workload(), events=())
        book.publish("first", empty)
        book.publish("second", empty)
        before = book.snapshot()
        with self.assertRaises(DeclaredIngressCapacityError):
            book.publish("third", empty)
        self.assertEqual(book.snapshot(), before)
        book.publish("first", empty)  # Exact binding replay remains allowed at capacity.

    def test_retirement_preserves_other_pending_events_and_replay_cannot_release_next_grant(self):
        book = self.book()
        state = workload(times=(10, 20))
        first, second = book.publish("alice", state)
        book.next_due(position(100))
        dispatch = begin_dispatch(state, 1, through=position(100))
        book.publish("alice", dispatch)
        waiting = bind_submission(dispatch, 1, Submission("job-a"))
        book.retire(first, waiting)
        self.assertEqual(book.snapshot().reservations, (second,))
        self.assertIs(book.next_due(position(100)), second)
        before = book.snapshot()
        with self.assertRaises(UnresolvedIngressError):
            book.retire(first, waiting)
        self.assertEqual(book.snapshot(), before)
        second_dispatch = begin_dispatch(waiting, 2, through=position(100))
        book.publish("alice", second_dispatch)
        book.retire(second, bind_submission(second_dispatch, 2, Submission("job-b")))
        self.assertEqual(len(book.snapshot().history), 2)

    def test_workload_source_cannot_be_replaced_by_explicit_snapshot_or_foreign_plan(self):
        book = self.book()
        state = workload()
        book.publish("source", state)
        before = book.snapshot()
        for action in (lambda: book.publish_snapshot("source", (), ()),
                       lambda: book.publish("source", replace(state, plan=replace(
                           state.plan, actor_id="another-actor")))):
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(book.snapshot(), before)

    def test_new_earlier_candidates_or_blocking_barriers_cannot_invalidate_grant(self):
        book = self.book()
        token = book.reserve("b", 1, position(10, 1))
        self.assertIs(book.next_due(position(100)), token)
        before = book.snapshot()
        for action in (lambda: book.reserve("a", 1, position(10, 1)),
                       lambda: book.reserve("z", 1, position(9)),
                       lambda: book.publish_snapshot("a", (),
                           (CausalBarrier(1, position(10, 1)),))):
            with self.assertRaises(EventOrderError):
                action()
            self.assertEqual(book.snapshot(), before)
        later = book.reserve("z", 1, position(10, 1))
        book.retire(token, decision="admitted")
        self.assertIs(book.next_due(position(100)), later)

    def test_retired_admission_order_rejects_backinsertion_without_confirmed_cutoff(self):
        book = self.book()
        token = book.reserve("b", 2, position(10, 1))
        book.next_due(position(100))
        book.retire(token, decision="admitted")
        before = book.snapshot()
        self.assertIsNone(before.confirmed_cutoff)
        self.assertIs(before.last_retired, token)
        for producer, event, arrival in (("z", 1, position(9)),
                                         ("a", 99, position(10, 1)),
                                         ("b", 1, position(10, 1))):
            with self.assertRaises(EventOrderError):
                book.reserve(producer, event, arrival)
            with self.assertRaises(EventOrderError):
                book.publish_snapshot(producer, (DeclaredArrival(event, arrival),), ())
            self.assertEqual(book.snapshot(), before)
        # Replay survives the floor, but does not recreate pending state.
        self.assertIs(book.reserve("b", 2, position(10, 1)), token)
        self.assertIs(book.publish_snapshot("b", (DeclaredArrival(2, position(10, 1)),), ())[0],
                      token)
        self.assertEqual(book.snapshot(), before)
        next_token = book.reserve("b", 3, position(10, 1))  # Later full tie key.
        book.next_due(position(100))
        book.retire(next_token, decision="denied")
        self.assertIs(book.snapshot().last_retired, next_token)
        book.retire(token, decision="admitted")
        self.assertIs(book.snapshot().last_retired, next_token)  # Replay cannot rewind it.

    def test_barrier_replacement_cannot_reveal_a_successor_before_retired_admission(self):
        book = self.book()
        token = book.reserve("b", 2, position(10, 1))
        book.next_due(position(100))
        book.retire(token, decision="admitted")
        before = book.snapshot()
        for producer, origin, lower_bound in (("a", 2, position(10, 1)),
                                              ("b", 1, position(9)),
                                              ("b", 3, position(10, 1))):
            with self.assertRaises(EventOrderError):
                book.publish_snapshot(producer, (), (CausalBarrier(origin, lower_bound),))
            self.assertEqual(book.snapshot(), before)
        # Its own result dependency is exempt only for the exact source/event.
        book.publish_snapshot("b", (), (CausalBarrier(2, position(10, 0)),))
        self.assertEqual(book.snapshot().barriers[0].key, token.key)
        self.assertIsNone(book.next_due(position(100)))
        before = book.snapshot()
        with self.assertRaises(EventOrderError):
            book.publish_snapshot("b", (DeclaredArrival(3, position(9)),), ())
        self.assertEqual(book.snapshot(), before)

    def test_atomic_completion_cannot_introduce_an_arrival_before_its_own_decision(self):
        book = self.book()
        state = workload()
        token, = book.publish("alice", state)
        book.next_due(position(100))
        dispatch = begin_dispatch(state, 1, through=position(100))
        book.publish("alice", dispatch)
        waiting = bind_submission(dispatch, 1, Submission("job-a"))
        earlier = replace(waiting.events[0], event_id=2, phase=Phase.SCHEDULED,
                          arrival=position(9), job_id=None)
        before = book.snapshot()
        with self.assertRaises(EventOrderError):
            book.retire(token, replace(waiting, events=waiting.events + (earlier,)))
        self.assertEqual(book.snapshot(), before)
        self.assertIsNone(book.decision(token))
        book.retire(token, waiting)

    def test_pending_and_history_limits_are_hard_and_no_decision_eviction_occurs(self):
        book = self.book(pending=1, history=1)
        token = book.reserve("a", 1, position(10))
        before = book.snapshot()
        with self.assertRaises(DeclaredIngressCapacityError):
            book.reserve("b", 1, position(20))
        self.assertEqual(book.snapshot(), before)
        self.assertIs(book.reserve("a", 1, position(10)), token)
        book.next_due(position(100))
        book.retire(token, decision="admitted")
        later = book.reserve("b", 1, position(20))
        before = book.snapshot()
        with self.assertRaises(DeclaredIngressCapacityError):
            book.next_due(position(100))
        self.assertEqual(book.snapshot(), before)
        self.assertIsNone(book.snapshot(position(100)).eligible)
        self.assertEqual(book.decision(token), "admitted")
        self.assertEqual(book.snapshot().reservations, (later,))
        self.assertIs(book.reserve("a", 1, position(10)), token)

    def test_cutoff_never_crosses_known_ingress_or_causal_result(self):
        book = self.book()
        token = book.reserve("a", 1, position(10))
        book.confirm_admission_cutoff(position(9))
        before = book.snapshot()
        with self.assertRaises(UnresolvedIngressError):
            book.confirm_admission_cutoff(position(10))
        self.assertEqual(book.snapshot(), before)
        book.next_due(position(100))
        book.retire(token, decision="admitted")
        book.publish_snapshot("a", (), (CausalBarrier(1, position(20)),))
        book.confirm_admission_cutoff(position(19))
        before = book.snapshot()
        with self.assertRaises(UnresolvedIngressError):
            book.confirm_admission_cutoff(position(20))
        self.assertEqual(book.snapshot(), before)

    def test_snapshot_and_close_retain_evidence_and_never_read_host_time(self):
        book = self.book()
        with patch("time.monotonic_ns", side_effect=AssertionError("host time forbidden")):
            token = book.reserve("a", 1, position(10))
            before = book.snapshot(position(100))
            self.assertIs(before.eligible, token)
            self.assertIsNone(book.snapshot().granted)  # Snapshot is not a grant.
            book.next_due(position(100))
        before = book.snapshot()
        book.close()
        book.close()
        self.assertEqual(book.snapshot(), replace(before, closed=True))
        self.assertIsNone(book.decision(token))
        for action in (lambda: book.reserve("a", 1, position(10)),
                       lambda: book.next_due(position(100)),
                       lambda: book.publish("workload", workload()),
                       lambda: book.publish_snapshot("a", (), ()),
                       lambda: book.retire(token, decision="denied"),
                       lambda: book.confirm_admission_cutoff(position(5))):
            with self.assertRaises(RuntimeError):
                action()
        self.assertEqual(book.snapshot(), replace(before, closed=True))
        with self.assertRaises(FrozenInstanceError):
            before.closed = True

    def test_racing_duplicate_reservations_return_one_exact_token(self):
        book = self.book(pending=1)
        gate = Barrier(2, timeout=2)

        def reserve():
            gate.wait()
            return book.reserve("a", 1, position(10))

        with ThreadPoolExecutor(max_workers=2) as pool:
            left, right = pool.submit(reserve), pool.submit(reserve)
            first, second = left.result(timeout=3), right.result(timeout=3)
        self.assertIs(first, second)
        self.assertEqual(book.snapshot().reservations, (first,))

    def test_completion_race_snapshot_sees_reservation_or_complete_result_barrier(self):
        book = self.book()
        state = workload(branch=True)
        token, = book.publish("alice", state)
        book.next_due(position(100))
        dispatch = begin_dispatch(state, 1, through=position(100))
        book.publish("alice", dispatch)
        waiting = bind_submission(dispatch, 1, Submission("job-a"))
        gate = Barrier(2, timeout=2)

        def retire():
            gate.wait()
            return book.retire(token, waiting)

        def snapshot():
            gate.wait()
            return book.snapshot(position(100))

        with ThreadPoolExecutor(max_workers=2) as pool:
            completion, observation = pool.submit(retire), pool.submit(snapshot)
            seen = observation.result(timeout=3)
            completion.result(timeout=3)
        self.assertEqual([b.key for b in seen.barriers], [("alice", 1)])
        if token in seen.reservations:
            self.assertEqual(seen.history, ())
            self.assertIs(seen.granted, token)
        else:
            self.assertEqual([(r.reservation, r.decision) for r in seen.history],
                             [(token, "admitted")])
            self.assertIsNone(seen.granted)
        self.assertEqual(book.snapshot().reservations, ())
        self.assertEqual([b.key for b in book.snapshot().barriers], [("alice", 1)])


if __name__ == "__main__":
    unittest.main()
