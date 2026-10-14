"""Common finite-world integration, without manual windows or trusted seals."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import math
from threading import Event
import unittest
from unittest.mock import patch

from api.attack_sessions import SessionBusyError
from api.attack_workloads import DeclaredWorkloadRunner, WorkloadInfrastructureError
from api.attack_world import ClosedWorkloadWorld
from core.attack_simulation.declared_execution import DeclaredQpuExecutor
from core.attack_simulation.declared_ingress import DeclaredIngressCapacityError
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.program_request import STATE_INHERIT_OPERATION_ID
from core.attack_simulation.qpu_topology import RetainedRegionBinding
from core.attack_simulation.scenario_policy import ScenarioMode
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime
from core.attack_simulation.victim_workload import Branch, DeclaredDurations, Outcome, TimedStart
from core.attack_simulation.workload_config import serialize_workload_plan
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from tests.test_attack_sessions import OPERATIONS, session_fixture
from tests.test_attack_workloads import Host, plan
from tests.test_qpu_topology import large_topology


TIMING = {"processing_delay_us": 0, "publication_delay_us": 3, "denial_delay_us": 2,
          "basis": "Test-only explicit timing, not a provider measurement or product preset."}


def setup(*, plans=None, timing=TIMING, mode=ScenarioMode.FREE, operations=OPERATIONS,
          history=32, idle=True, topology_binding=None):
    plans = (plan(),) if plans is None else plans
    by_actor = {p.actor_id: serialize_workload_plan(p) for p in plans}
    args = session_fixture(max_jobs=32, max_receipt_history=128,
        attacker_operations=operations, initializations=("prepare", "inherit"),
        attacker_workload=by_actor.get("a"), victim_workload=by_actor.get("v"),
        world_clock={"clock_domain": "world", "rate": 1}, service_timing=timing, scenario_mode=mode,
        topology_binding=topology_binding)
    host = Host()
    runner = DeclaredWorkloadRunner(session=args[5],
        credentials={p.actor_id: args[4][p.actor_id] for p in plans}, run_id="run",
        max_pending=16, max_history=history, host_ns=host)
    width = len(args[1].read().physical_qubit_ids)
    model = IndependentIdleModel((IdleQubitSpec(math.inf, math.inf, 0),) * width, width) if idle else None
    executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=32,
        max_events_per_job=64, max_unresolved=16, idle_model=model)
    world = ClosedWorkloadWorld(runner=runner, executor=executor)
    return args, host, runner, executor, world


def at(host, time_us):
    host.value = 1_000_000_000 + int(time_us * 1000)


def trace(args, runner, executor):
    # Ignore generated job/event UUIDs; retain declared physics and own counts.
    physical = [(e.event_kind, e.device_time_end_us - e.device_time_start_us, e.owner_after)
                for e in args[1].events()]
    jobs = [args[2].scheduler.get(e.job_id) for s in runner.states().values() for e in s.events if e.job_id]
    return (physical, [(j.command.tenant_id, j.status, j.result) for j in jobs],
            [(key, [(e.arrival, e.outcome, e.result_arrival) for e in s.events])
             for key, s in runner.states().items()], args[1].read().density_matrix,
            executor.frontier.snapshot().confirmed)


class AttackWorldTests(unittest.TestCase):
    def test_closed_world_retains_large_sparse_topology_and_spectator_without_dense_expansion(self):
        topology = large_topology()
        binding = RetainedRegionBinding(topology, (7, 17, 207), 4)
        args, host, runner, executor, world = setup(plans=(plan("v"), plan("a", at=12)),
                                                  topology_binding=binding)
        world.start()
        at(host, 1000)
        self.assertTrue(world.pump(max_transitions=32).finished)
        self.assertIs(args[1].region_binding.topology, topology)
        self.assertEqual(len(topology.physical_qubit_ids), 300)
        self.assertEqual(args[1].read().physical_qubit_ids, (7, 17, 207))
        self.assertEqual(len(args[1].read().density_matrix), 8)
        self.assertEqual(len(executor._windows), 2)

    def test_finite_world_advances_multiple_qubit_actors_and_result_branches_in_both_modes(self):
        baseline = None
        for mode in (ScenarioMode.FREE, ScenarioMode.CHALLENGE):
            args, host, runner, executor, world = setup(
                plans=(plan("a", at=12, visits=2, mode="inherit"), plan("v", at=10, visits=2)), mode=mode,
                operations=OPERATIONS + (STATE_INHERIT_OPERATION_ID,))
            world.start()
            at(host, 1000)
            report = world.pump(max_transitions=64)
            self.assertTrue(report.finished)
            self.assertFalse(report.limit_reached)
            self.assertEqual(len(executor._windows), 4)
            self.assertTrue(all(j.result["shots"] == 2 for s in runner.states().values()
                                for e in s.events for j in [args[2].scheduler.get(e.job_id)]))
            self.assertEqual(len(args[1].read().physical_qubit_ids), 2)
            current = trace(args, runner, executor)
            if baseline is None:
                baseline = current
            else:
                self.assertEqual(current, baseline)
            with self.assertRaises(RuntimeError):
                args[0].set_assumption(None)

    def test_start_is_running_before_finish_is_due_and_release_waits(self):
        args, host, runner, executor, world = setup()
        world.start()
        at(host, 15)
        report = world.pump(max_transitions=8)
        event = runner.states()["plan-v"].events[0]
        window = executor._windows[event.job_id]
        self.assertIs(args[2].scheduler.get(event.job_id).status, JobStatus.RUNNING)
        self.assertEqual(executor.frontier.snapshot().confirmed, window.start)
        self.assertFalse(report.finished)
        at(host, window.finish.time_us)
        world.pump(max_transitions=8)
        self.assertIs(args[2].scheduler.get(event.job_id).status, JobStatus.SUCCEEDED)
        self.assertFalse(executor.result_is_available(event.job_id))
        at(host, window.release.time_us)
        self.assertTrue(world.pump(max_transitions=8).finished)

    def test_fractional_gate_duration_at_late_world_time_keeps_compiled_physics(self):
        from core.attack_simulation.program_request import CircuitProgram
        from core.attack_simulation.victim_workload import RegisteredProgram
        from tests.test_attack_sessions import circuit_json
        p = plan(at=10_000_000)
        p = replace(p, programs=(RegisteredProgram("two", CircuitProgram.from_editor_json(circuit_json())),))
        args, host, runner, executor, world = setup(plans=(p,))
        world.start()
        at(host, 10_000_100)
        self.assertTrue(world.pump(max_transitions=16).finished)
        event = runner.states()["plan-v"].events[0]
        job = args[2].scheduler.get(event.job_id)
        window = executor._windows[event.job_id]
        duration = executor._duration(job, replace(args[1].read(), owner_tenant_id=None))
        self.assertEqual(window.finish.time_us, window.start.time_us + duration)
        self.assertNotEqual(window.finish.time_us - window.start.time_us, duration)
        self.assertIs(job.status, JobStatus.SUCCEEDED)

    def test_result_barrier_does_not_deadlock_known_arrivals_inside_physical_interval(self):
        definition = plan("v", at=0, visits=2)
        attacker = plan("a", at=1)
        # The victim's loose old bound blocks the attacker before refinement.
        definition = replace(definition, steps=(replace(definition.steps[0],
            shots=32, durations=DeclaredDurations(0, 0, 0)),))
        attacker = replace(attacker, steps=(replace(attacker.steps[0], durations=DeclaredDurations(0, 0, 0)),))
        args, host, runner, executor, world = setup(plans=(definition, attacker))
        world.start()
        at(host, 1000)
        self.assertTrue(world.pump(max_transitions=32).finished)
        windows = list(executor._windows.values())
        self.assertLess(windows[1].arrival.time_us, windows[0].finish.time_us)
        self.assertGreater(windows[1].start.key, windows[0].finish.key)
        self.assertEqual(runner.ingress_snapshot().reservations, ())
        self.assertEqual(runner.ingress_snapshot().barriers, ())

    def test_zero_delay_repeat_and_equal_time_sources_preserve_causal_order(self):
        definitions = []
        for actor in ("v", "a"):
            p = plan(actor, at=0, visits=3)
            definitions.append(replace(p, steps=(replace(p.steps[0], durations=DeclaredDurations(0, 0, 0)),)))
        args, host, runner, executor, world = setup(plans=tuple(definitions),
            timing={**TIMING, "publication_delay_us": 0, "denial_delay_us": 0})
        world.start()
        at(host, 1000)
        self.assertTrue(world.pump(max_transitions=64).finished)
        windows = list(executor._windows.values())
        self.assertEqual(len(windows), 6)
        self.assertEqual(args[2].scheduler.get(next(iter(executor._windows))).command.tenant_id, "ta")
        self.assertTrue(all(b.start.key > a.finish.key for a, b in zip(windows, windows[1:])))

    def test_host_numeric_delay_does_not_change_windows_counts_or_successors(self):
        snapshots = []
        for delayed in (False, True):
            args, host, runner, executor, world = setup(plans=(plan(visits=3),))
            world.start()
            at(host, 1000)
            original = SharedQpuRuntime._execute
            def calculate(runtime, job):
                if delayed:
                    host.value += 900_000_000
                return original(runtime, job)
            with patch.object(SharedQpuRuntime, "_execute", calculate):
                self.assertTrue(world.pump(max_transitions=64).finished)
            snapshots.append((tuple(executor._windows.values()), trace(args, runner, executor)))
        self.assertEqual(*snapshots)

    def test_denial_is_a_timed_common_outcome_without_physical_state(self):
        p = plan("a", visits=3)
        p = replace(p, steps=(replace(p.steps[0], branches=(Branch(Outcome.DENIED, "step"),)),))
        args, host, runner, executor, world = setup(plans=(p,), operations=())
        world.start()
        at(host, 1000)
        self.assertTrue(world.pump(max_transitions=32).finished)
        self.assertEqual([e.outcome for e in runner.states()["plan-a"].events], [Outcome.DENIED] * 3)
        self.assertEqual(args[1].read().revision, 0)
        self.assertIsNone(executor.frontier.snapshot().confirmed)

    def test_bounded_pumps_resume_without_duplicate_jobs_and_idle_polls_do_not_invent_ties(self):
        args, host, runner, executor, world = setup(plans=(plan(visits=2),))
        world.start()
        at(host, 1)
        first = world.pump(max_transitions=1).target
        self.assertEqual(world.pump(max_transitions=1).target, first)
        at(host, 1000)
        report = world.pump(max_transitions=1)
        self.assertTrue(report.limit_reached)
        for _ in range(16):
            report = world.pump(max_transitions=1)
            if report.finished:
                break
        self.assertTrue(report.finished)
        self.assertEqual(len(args[2].scheduler._jobs), 2)
        with self.assertRaises(ValueError):
            world.pump(max_transitions=0)

    def test_manual_advancement_raw_http_and_unmapped_control_are_not_enabled(self):
        args, host, runner, executor, world = setup()
        with self.assertRaises(PermissionError):
            runner.start()
        world.start()
        with self.assertRaises(PermissionError):
            runner.dispatch_next()
        with self.assertRaises(PermissionError):
            args[5].request(args[4]["a"], lambda c, w: c.list_devices())
        at(host, 1000)
        world.pump(max_transitions=1)
        job_id = runner.states()["plan-v"].events[0].job_id
        # Trusted misuse cannot silently create a backdated cancellation reply.
        args[2].scheduler.cancel(job_id)
        with self.assertRaises(WorkloadInfrastructureError):
            world.pump(max_transitions=8)
        self.assertEqual(args[1].read().revision, 0)

    def test_start_failure_requires_explicit_retry_with_same_job(self):
        args, host, runner, executor, world = setup()
        world.start()
        at(host, 1000)
        original = args[1].commit_prepared
        def interrupted(batch):
            original(batch)
            raise RuntimeError("host interruption")
        with patch.object(args[1], "commit_prepared", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                world.pump(max_transitions=16)
        self.assertEqual(len(args[2].scheduler._jobs), 1)
        self.assertEqual(args[1].read().revision, 0)
        self.assertTrue(world.pump(max_transitions=16).finished)
        self.assertEqual(len(args[2].scheduler._jobs), 1)

    def test_lost_admission_and_commit_responses_replay_the_same_causal_world(self):
        for boundary in ("admission", "finish"):
            with self.subTest(boundary=boundary):
                args, host, runner, executor, world = setup(plans=(plan(visits=2),))
                world.start()
                at(host, 1000)
                method = "request" if boundary == "admission" else "run_declared_next"
                original = getattr(args[5], method)
                once = [True]
                def lost(*a, **kw):
                    result = original(*a, **kw)
                    if once[0]:
                        once[0] = False
                        raise RuntimeError("host lost owner response")
                    return result
                with patch.object(args[5], method, side_effect=lost):
                    with self.assertRaises(RuntimeError):
                        world.pump(max_transitions=32)
                self.assertTrue(world.pump(max_transitions=32).finished)
                self.assertEqual(len(args[2].scheduler._jobs), 2)
                self.assertTrue(all(s.finished for s in runner.states().values()))

    def test_host_capacity_keeps_source_and_prior_physics_without_fake_denial(self):
        args, host, runner, executor, world = setup(plans=(plan(visits=2),), history=1)
        world.start()
        at(host, 1000)
        with self.assertRaises(DeclaredIngressCapacityError):
            world.pump(max_transitions=32)
        state = runner.states()["plan-v"]
        self.assertEqual(state.events[0].outcome, Outcome.SUCCEEDED)
        self.assertEqual(len(args[2].scheduler._jobs), 1)
        self.assertEqual(len(runner.ingress_snapshot().reservations), 1)
        before = args[1].read()
        with self.assertRaises(DeclaredIngressCapacityError):
            world.pump(max_transitions=32)
        self.assertIs(args[1].read(), before)

    def test_numeric_interruption_keeps_running_and_retries_without_extra_start_or_reset(self):
        args, host, runner, executor, world = setup()
        world.start()
        at(host, 1000)
        with patch.object(SharedQpuRuntime, "_execute", side_effect=RuntimeError("numeric interruption")):
            with self.assertRaises(RuntimeError):
                world.pump(max_transitions=16)
        event = runner.states()["plan-v"].events[0]
        self.assertIs(args[2].scheduler.get(event.job_id).status, JobStatus.RUNNING)
        self.assertEqual([e.event_kind for e in args[1].events()], ["declared_idle"])
        self.assertTrue(world.pump(max_transitions=16).finished)
        self.assertEqual(len(args[2].scheduler._jobs), 1)
        self.assertEqual([e.event_kind for e in args[1].events()].count("declared_idle"), 1)

    def test_fresh_assembly_requires_frozen_timing_and_mismatched_owners_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "freeze explicit service"):
            setup(timing=None)
        with self.assertRaises(ValueError):
            setup(timing={**TIMING, "publication_delay_us": -1})
        with self.assertRaises(ValueError):
            setup(timing={**TIMING, "extra": 1})
        args, host, runner, executor, world = setup()
        with self.assertRaises(ValueError):
            ClosedWorkloadWorld(runner=runner, executor=setup()[3])
        world.start()
        with self.assertRaises(ValueError):
            ClosedWorkloadWorld(runner=runner, executor=executor)

    def test_registry_disposal_fences_seal_metadata_as_well_as_physical_execution(self):
        args, host, runner, executor, world = setup()
        world.start()
        at(host, 15)
        world.pump(max_transitions=8)
        before = runner.ingress_snapshot()
        args[6].discard("world")
        with self.assertRaises(WorkloadInfrastructureError):
            runner.seal_physical_through(executor, EventPosition("world", 15, 100),
                                         _coordinator_owner=world._owner)
        self.assertEqual(runner.ingress_snapshot(), before)
        self.assertTrue(executor.frontier.snapshot().closed)

    def test_discard_during_numeric_work_fences_finish_without_reset_and_concurrent_pump_is_rejected(self):
        args, host, runner, executor, world = setup()
        world.start()
        at(host, 1000)
        entered, release, fenced = Event(), Event(), Event()
        execute = SharedQpuRuntime._execute
        close = executor.close
        def close_and_signal():
            close()
            fenced.set()
        def held(runtime, job):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("test numerical barrier timed out")
            return execute(runtime, job)
        with patch.object(SharedQpuRuntime, "_execute", held), \
                patch.object(executor, "close", side_effect=close_and_signal), \
                ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(world.pump, max_transitions=16)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(world.target().time_us, 1000)
                with self.assertRaises(SessionBusyError):
                    world.pump(max_transitions=1)
                closing = pool.submit(world.close)
                self.assertTrue(fenced.wait(2))
                # Finish remains unpublished even if discard races numerical return.
            finally:
                release.set()
            closing.result(timeout=5)
            with self.assertRaises((PermissionError, RuntimeError)):
                running.result(timeout=5)
        self.assertTrue(executor.frontier.snapshot().closed)
        self.assertEqual([e.event_kind for e in args[1].events()], ["declared_idle"])


if __name__ == "__main__":
    unittest.main()
