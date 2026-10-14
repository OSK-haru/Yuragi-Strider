"""Finite common Programs and own-result scheduling, without a live QPU runner."""

from dataclasses import FrozenInstanceError, replace
import json
import unittest

from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.job_service import JobResultView, JobStatusView
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.program_request import CircuitProgram
from core.attack_simulation.victim_workload import (
    Branch, CountAtLeast, DeclaredDurations, Outcome, OwnProjectedResult, Phase,
    RegisteredProgram, Submission, TimedStart, WorkloadCapacityError, WorkloadPlan,
    WorkloadStep, advance_result, begin_dispatch, bind_submission, close_workload,
    dispatch_operation, due_events, read_own_result, start_workload,
)


def position(time, order=999):
    return EventPosition("world", time, order)


def program():
    return RegisteredProgram("bell", CircuitProgram.from_editor_json(json.dumps({
        "logical_qubits": 2, "initial_states": [0, 0], "classical_bits": 2,
        "columns": [
            {"step": 0, "gates": [{"type": "H", "targets": [0]}]},
            {"step": 1, "gates": [{"type": "CNOT", "targets": [1], "controls": [0]}]},
            {"step": 2, "gates": [{"type": "MEASURE", "targets": [0],
                                    "classical_targets": [0]},
                                   {"type": "MEASURE", "targets": [1],
                                    "classical_targets": [1]}]},
        ],
    })))


def step(name, *, branches=(), visits=1, durations=None):
    return WorkloadStep(name, "bell", "qpu-register", 4,
                        durations or DeclaredDurations(2, 3, 5), visits, branches)


def plan(steps=None, starts=None, **kwargs):
    steps = steps or (step("first"),)
    return WorkloadPlan("plan", "actor", "world", (program(),), steps,
                        starts or (TimedStart(steps[0].step_id, 10),),
                        kwargs.pop("max_events", sum(s.max_visits for s in steps)),
                        kwargs.pop("max_pending", 1), **kwargs)


class Client:
    """Test double for the trusted, authorized common-client assembly boundary."""
    principal_id = "actor"

    def __init__(self):
        self.calls = []
        self.jobs = {}
        self.keys = {}
        self.denied = False
        self.host_worker_delay = 0  # Deliberately unavailable to the workload.

    def submit_declared_program(self, request, *, arrival):
        if self.denied:
            raise PermissionError("common grant denies submission")
        self.calls.append((request, arrival))
        if request.idempotency_key not in self.keys:
            job = f"job-{len(self.keys) + 1}"
            self.keys[request.idempotency_key] = job
            self.jobs[job] = JobResultView(job, JobStatus.SUCCEEDED,
                                          {"shots": 4, "counts": {"00": 1, "11": 3}})
        return JobStatusView(self.keys[request.idempotency_key], JobStatus.QUEUED)

    def get_result(self, job_id):
        return self.jobs[job_id]


def submit(state, event_id, client):
    event = state.events[event_id - 1]
    reserved = begin_dispatch(state, event_id, through=event.arrival)
    return bind_submission(reserved, event_id, dispatch_operation(reserved, event_id, client))


class VictimWorkloadTests(unittest.TestCase):
    def test_single_and_independent_timed_group_have_explicit_due_schedules(self):
        definition = plan((step("late"), step("early")),
                          (TimedStart("late", 40), TimedStart("early", 10)), max_pending=2)
        state = start_workload(definition, run_id="run")
        self.assertEqual([e.arrival.time_us for e in state.candidates()], [15, 45])
        self.assertEqual(due_events(state, position(14)), ())
        self.assertEqual([e.step_id for e in due_events(state, position(20))], ["early"])
        with self.assertRaises(ValueError):
            begin_dispatch(state, 1, through=position(44))
        state = submit(state, 2, Client())
        self.assertEqual([e.step_id for e in due_events(state, position(100))], ["late"])
        self.assertIs(state.events[1].phase, Phase.WAITING)
        self.assertEqual(state.barriers(), ())  # Terminal step cannot create a future arrival.
        with self.assertRaises(ValueError):
            due_events(state, EventPosition("another-world", 100, 0))

    def test_multiqubit_reference_dispatch_has_no_reset_bit_or_actor_bypass(self):
        state, client = start_workload(plan(), run_id="run"), Client()
        with self.assertRaises(ValueError):
            dispatch_operation(state, 1, client)
        reserved = begin_dispatch(state, 1, through=position(15))
        first = dispatch_operation(reserved, 1, client)
        retry = dispatch_operation(reserved, 1, client)
        self.assertEqual(first, retry)
        self.assertEqual(len(client.keys), 1)
        request, arrival = client.calls[0]
        self.assertEqual(request.program.logical_qubits, 2)
        self.assertEqual(request.device_id, "qpu-register")
        self.assertEqual(arrival.time_us, 15)
        self.assertEqual(set(request.to_application_payload()), {"device_id", "shots", "circuit_config"})
        self.assertFalse(hasattr(request, "output_bit"))
        self.assertFalse(hasattr(request, "actor_id"))
        other = start_workload(plan(), run_id="new-run")
        other = begin_dispatch(other, 1, through=position(15))
        self.assertNotEqual(dispatch_operation(other, 1, client).job_id, first.job_id)

    def test_own_counts_branch_and_barrier_exist_before_client_call(self):
        branches = (Branch(Outcome.SUCCEEDED, "yes", CountAtLeast("11", 2)),
                    Branch(Outcome.SUCCEEDED, "no"))
        definition = plan((step("first", branches=branches), step("yes"), step("no")))
        state, client = start_workload(definition, run_id="run"), Client()
        reserved = begin_dispatch(state, 1, through=position(15))
        barrier = reserved.barriers()[0]
        self.assertEqual(client.calls, [])
        self.assertEqual(reserved.ingress_reservations(), (reserved.events[0],))
        self.assertEqual(reserved.ingress_reservations()[0].arrival, position(15, 1))
        self.assertEqual(barrier.producer_event_id, 1)
        self.assertEqual(barrier.earliest_next_arrival, position(25, 0))
        state = bind_submission(reserved, 1, dispatch_operation(reserved, 1, client))
        self.assertEqual(state.ingress_reservations(), ())
        self.assertEqual(state.barriers(), reserved.barriers())
        result = read_own_result(state, 1, client)
        state = advance_result(state, 1, available=position(100), result=result)
        self.assertEqual(state.events[0].result_arrival.time_us, 105)
        self.assertEqual(state.events[1].step_id, "yes")
        self.assertEqual(state.events[1].arrival.time_us, 110)
        self.assertEqual(state.barriers(), ())
        self.assertEqual(state.candidates(), (state.events[1],))
        with self.assertRaises(ValueError):
            advance_result(state, 1, available=position(100), result=result)

    def test_prepare_and_inherit_use_same_program_submission_and_authorization(self):
        class GrantClient(Client):
            def __init__(self, allow_inherit):
                super().__init__()
                self.allow_inherit = allow_inherit
                self.attempts = []

            def submit_declared_program(self, request, *, arrival):
                self.attempts.append(request)
                if request.initialization == "inherit" and not self.allow_inherit:
                    raise PermissionError("common state-inherit grant is absent")
                return super().submit_declared_program(request, arrival=arrival)

        for mode in ("prepare", "inherit"):
            for granted in (False, True):
                definition = plan((replace(step("first"), initialization=mode),))
                client = GrantClient(granted)
                state = submit(start_workload(definition, run_id="run"), 1, client)
                self.assertEqual(len(client.attempts), 1)
                self.assertEqual(client.attempts[0].initialization, mode)
                self.assertEqual(definition.steps[0].operation_id, "program.submit")
                if mode == "inherit" and not granted:
                    self.assertEqual(client.calls, [])
                    self.assertIs(state.events[0].outcome, Outcome.DENIED)
                    self.assertIsNone(state.events[0].job_id)
                else:
                    self.assertIsNotNone(state.events[0].job_id)
        self.assertEqual(step("first").initialization, "prepare")
        for invalid in ("internal.victim.prepare", "reset", None, True):
            with self.assertRaises(ValueError):
                replace(step("first"), initialization=invalid)

    def test_false_counts_branch_and_success_without_counts_use_fallback(self):
        definition = plan((step("first", branches=(
            Branch(Outcome.SUCCEEDED, "yes", CountAtLeast("11", 4)),
            Branch(Outcome.SUCCEEDED, "no"))), step("yes"), step("no")))
        for result in (OwnProjectedResult("job-1", Outcome.SUCCEEDED, 4, (("11", 3), ("00", 1))),
                       OwnProjectedResult("job-1", Outcome.SUCCEEDED)):
            state = submit(start_workload(definition, run_id="run"), 1, Client())
            state = advance_result(state, 1, available=position(20), result=result)
            self.assertEqual(state.events[1].step_id, "no")

    def test_finite_result_dependent_repeat_and_mutual_cycle_stop_at_bounds(self):
        definitions = (
            plan((step("loop", visits=3, branches=(Branch(Outcome.SUCCEEDED, "loop"),)),)),
            plan((step("a", visits=2, branches=(Branch(Outcome.SUCCEEDED, "b"),)),
                  step("b", visits=2, branches=(Branch(Outcome.SUCCEEDED, "a"),)))),
        )
        for definition, expected in zip(definitions, (3, 4)):
            state, client = start_workload(definition, run_id="run"), Client()
            for event_id in range(1, expected + 1):
                state = submit(state, event_id, client)
                result = read_own_result(state, event_id, client)
                state = advance_result(state, event_id,
                                       available=position(state.events[-1].arrival.time_us + 10),
                                       result=result)
            self.assertTrue(state.finished)
            self.assertEqual(len(state.events), expected)
            self.assertEqual(state.barriers(), ())
            self.assertEqual(state.candidates(), ())
        for visits in (None, 0, -1, True, 4097):
            with self.assertRaises(ValueError):
                step("bad", visits=visits)

    def test_zero_delay_sequence_still_advances_explicit_tie_order(self):
        zero = DeclaredDurations(0, 0, 0)
        state = start_workload(plan((step("a", durations=zero,
            branches=(Branch(Outcome.SUCCEEDED, "b"),)), step("b", durations=zero))), run_id="run")
        state = submit(state, 1, Client())
        state = advance_result(state, 1, available=position(10, 1000),
                               result=OwnProjectedResult("job-1", Outcome.SUCCEEDED))
        self.assertEqual(state.events[1].arrival.time_us, 10)
        self.assertGreater(state.events[1].arrival.order, 1000)

    def test_permission_denial_failure_cancel_and_unhandled_terminal_lifecycle(self):
        for outcome, status in ((Outcome.FAILED, JobStatus.FAILED),
                                (Outcome.CANCELLED, JobStatus.CANCELLED),
                                (Outcome.DENIED, JobStatus.REJECTED)):
            definition = plan((step("a", branches=(Branch(outcome, "b"),)), step("b")))
            client = Client()
            state = submit(start_workload(definition, run_id="run"), 1, client)
            client.jobs["job-1"] = JobResultView("job-1", status, None, "declared-case")
            state = advance_result(state, 1, available=position(30),
                                   result=read_own_result(state, 1, client))
            self.assertIs(state.events[0].outcome, outcome)
            self.assertEqual(state.events[1].step_id, "b")
        definition = plan((step("a", branches=(Branch(Outcome.DENIED, "b"),)), step("b")))
        for wrong_actor in (False, True):
            client = Client()
            if wrong_actor:
                client.principal_id = "another-actor"
            else:
                client.denied = True  # Zero grant is legal at assembly, denied on operation.
            state = submit(start_workload(definition, run_id="run"), 1, client)
            self.assertEqual(client.calls, [])
            self.assertIs(state.events[0].outcome, Outcome.DENIED)
            self.assertTrue(state.barriers())
            state = advance_result(state, 1, available=position(30))
            self.assertEqual(state.events[1].arrival.time_us, 40)
        state = submit(start_workload(plan(), run_id="run"), 1, Client())
        state = advance_result(state, 1, available=position(20),
                               result=OwnProjectedResult("job-1", Outcome.FAILED))
        self.assertTrue(state.finished)
        self.assertIs(state.events[0].outcome, Outcome.FAILED)

    def test_no_other_job_truth_or_host_failure_can_advance_a_result(self):
        definition = plan((step("a", branches=(Branch(Outcome.SUCCEEDED, "b"),)), step("b")))
        client = Client()
        state = submit(start_workload(definition, run_id="run"), 1, client)
        saved = state
        with self.assertRaises(PermissionError):
            advance_result(state, 1, available=position(30),
                           result=OwnProjectedResult("other-job", Outcome.SUCCEEDED))
        for view, error in (
            (JobResultView("other-job", JobStatus.SUCCEEDED, None), PermissionError),
            (JobResultView("job-1", JobStatus.RUNNING, None), RuntimeError),
            (JobResultView("job-1", JobStatus.INTERNAL_ERROR, None), RuntimeError),
            (JobResultView("job-1", JobStatus.SUCCEEDED, {"density_matrix": [1]}), ValueError),
            (JobResultView("job-1", JobStatus.SUCCEEDED,
                           {"shots": 4, "counts": {"11": 3}}), ValueError),
        ):
            client.jobs["job-1"] = view
            with self.assertRaises(error):
                read_own_result(state, 1, client)
            self.assertEqual(state, saved)
            self.assertTrue(state.barriers())
        client.principal_id = "other"
        with self.assertRaises(PermissionError):
            read_own_result(state, 1, client)
        for available in (position(14), EventPosition("other", 30, 0)):
            with self.assertRaises(ValueError):
                advance_result(state, 1, available=available,
                               result=OwnProjectedResult("job-1", Outcome.SUCCEEDED))

    def test_version_references_frozen_settings_and_capacity_guards(self):
        definition = plan()
        with self.assertRaises(FrozenInstanceError):
            definition.actor_id = "new-actor"
        for changes in ({"version": 2}, {"programs": []}, {"max_events": 0},
                        {"max_pending": True}, {"steps": (step("a", visits=2),)},
                        {"starts": (TimedStart("missing", 0),)},
                        {"steps": (replace(step("first"), program_ref="missing"),)},
                        {"steps": (step("first", branches=(Branch(Outcome.SUCCEEDED, "missing"),)),)}):
            with self.assertRaises(ValueError):
                replace(definition, **changes)
        with self.assertRaises(WorkloadCapacityError):
            plan((step("a"), step("b")), (TimedStart("a", 0), TimedStart("b", 0)))
        with self.assertRaises(ValueError):
            replace(step("a"), operation_id="internal.prepare_victim")
        with self.assertRaises(ValueError):
            step("a", branches=(Branch(Outcome.SUCCEEDED, "a"),
                                Branch(Outcome.SUCCEEDED, "a", CountAtLeast("11", 1))))
        with self.assertRaises(ValueError):
            DeclaredDurations(0, float("nan"), 0)
        with self.assertRaises(ValueError):
            OwnProjectedResult("job", Outcome.SUCCEEDED, 257,
                               tuple((format(i, "09b"), 1) for i in range(257)))
        state = submit(start_workload(definition, run_id="run"), 1, Client())
        closed = close_workload(state)
        self.assertEqual(closed.events, state.events)
        for call in (lambda: due_events(closed, position(100)),
                     lambda: advance_result(closed, 1, available=position(100)),
                     lambda: begin_dispatch(closed, 1, through=position(100))):
            with self.assertRaises(RuntimeError):
                call()

    def test_host_delay_invariance_and_replay_of_declared_sequence(self):
        definition = plan((step("loop", visits=3,
            branches=(Branch(Outcome.SUCCEEDED, "loop", CountAtLeast("11", 2)),)),))

        def trace(host_delay):
            client = Client()
            client.host_worker_delay = host_delay
            state = start_workload(definition, run_id="run")
            snapshots = []
            for event_id, available in enumerate((100, 200, 300), 1):
                state = submit(state, event_id, client)
                snapshots.append(state.barriers())
                result = read_own_result(state, event_id, client)
                state = advance_result(state, event_id, available=position(available), result=result)
            self.assertTrue(state.finished)
            return state, snapshots, client.calls

        baseline = trace(0)
        self.assertEqual(trace(9_000_000_000), baseline)
        self.assertEqual(trace(0), baseline)
        self.assertEqual([e.arrival.time_us for e in baseline[0].events], [15, 110, 210])
        self.assertEqual([e.result_arrival.time_us for e in baseline[0].events], [105, 205, 305])

    def test_independent_result_completion_order_cannot_choose_successor_ids_or_repeat_bounds(self):
        zero = DeclaredDurations(0, 0, 0)
        definition = plan((step("loop", visits=2, durations=zero,
                                branches=(Branch(Outcome.SUCCEEDED, "loop"),)),),
                          (TimedStart("loop", 10), TimedStart("loop", 10)),
                          max_events=4, max_pending=2)

        def run(completion_order):
            client = Client()
            state = start_workload(definition, run_id="run")
            state = submit(state, 1, client)
            state = submit(state, 2, client)
            for event_id in completion_order:
                state = advance_result(state, event_id, available=position(20),
                                       result=read_own_result(state, event_id, client))
            return state

        baseline, delayed = run((1, 2)), run((2, 1))
        self.assertEqual(baseline, delayed)
        self.assertEqual([e.event_id for e in baseline.candidates()], [3, 4])
        self.assertNotEqual(baseline.events[2].arrival.order, baseline.events[3].arrival.order)
        self.assertEqual(len(baseline.events), 4)
        with self.assertRaises(ValueError):
            replace(definition, max_events=2)


if __name__ == "__main__":
    unittest.main()
