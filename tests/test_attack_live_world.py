"""Common live catch-up and isolated-host-delay acceptance, no sockets/UI."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import math
from threading import Event
from time import monotonic_ns
import time
import unittest
from unittest.mock import patch

import numpy as np

from api.attack_live_world import LiveWorkloadWorld
from api.attack_sessions import SessionBusyError, SessionCapacityError, WebOwnerContext
from api.attack_workloads import DeclaredWorkloadRunner, WorkloadInfrastructureError
from core.attack_simulation.declared_execution import DeclaredQpuExecutor
from core.attack_simulation.job_service import JobResultNotReadyError
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.live_time_policy import HOST_CLOCK
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.scenario_policy import ScenarioMode
from core.attack_simulation.victim_workload import Branch, Outcome
from core.attack_simulation.workload_config import serialize_workload_plan
from core.experimental_isolation_physics.idle_evolution import IndependentIdleModel, IdleQubitSpec
from tests.test_attack_sessions import OPERATIONS, session_fixture
from tests.test_attack_workloads import Host, plan
from tests.test_attack_world import TIMING, at
from tests.test_live_service_config import LIVE, CONTROL
from tests.test_local_program_runtime import program_json

_DEFAULT_TIME_POLICY = object()


def setup(*, mode=ScenarioMode.FREE, visits=1, service_timing=TIMING, pending=32,
          history=64, topology_binding=None, owner_context=None, attacker_operations=OPERATIONS,
          victim_workload=True, initializations=("prepare",),
          live_time_policy=_DEFAULT_TIME_POLICY, build_world=True, host_clock_source=None,
          max_jobs=32, retained_jobs=None):
    definition = plan(visits=visits) if victim_workload else None
    if victim_workload and visits > 1:
        definition = replace(definition, steps=(replace(definition.steps[0], branches=(
            Branch(Outcome.SUCCEEDED, "step"), Branch(Outcome.CANCELLED, "step"))),))
    args = session_fixture(max_jobs=max_jobs, max_receipt_history=128,
        victim_workload=serialize_workload_plan(definition) if victim_workload else None,
        world_clock={"clock_domain": "world", "rate": 1}, service_timing=service_timing,
        scenario_mode=mode, live_ingress=LIVE, control_timing=CONTROL,
        topology_binding=topology_binding, attacker_operations=attacker_operations,
        initializations=initializations,
        live_time_policy=(
            {"mode": HOST_CLOCK} if live_time_policy is _DEFAULT_TIME_POLICY
            else live_time_policy))
    host = Host()
    if owner_context is not None:
        args[5]._bind_web_owner(owner_context)
    runner = DeclaredWorkloadRunner(session=args[5],
        credentials={"v": args[4]["v"]} if victim_workload else {},
        run_id="run", max_pending=pending, max_history=history,
        host_ns=host if host_clock_source is None else host_clock_source)
    width = len(args[1].read().physical_qubit_ids)
    executor = DeclaredQpuExecutor(runtime=args[2], clock_domain="world", max_jobs=32,
        max_events_per_job=64, max_unresolved=32,
        idle_model=IndependentIdleModel((IdleQubitSpec(math.inf, math.inf, 0),) * width, width))
    world = (LiveWorkloadWorld(runner=runner, executor=executor, producer_id="live",
                               retained_jobs=retained_jobs)
             if build_world else None)
    if world is not None:
        world.start()
    return args, host, runner, executor, world


def request(key="live-one", shots=2):
    return ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(2)), "qpu-1", shots, key)


class AttackLiveWorldTests(unittest.TestCase):
    def test_host_clock_policy_tracks_host_elapsed_time(self):
        args, host, runner, executor, world = setup(host_clock_source=monotonic_ns)
        time.sleep(0.005)
        self.assertGreater(world.target().time_us, 0.0)

    def test_live_time_policy_is_required_and_explicit_wait_rejects_host_clock(self):
        args, host, runner, executor, _ = setup(
            live_time_policy={"mode": "explicit_wait", "max_wait_us": 1000,
                              "max_steps_per_wait": 100}, build_world=False)
        # This fixture uses a normal host clock source: explicit_wait must not
        # silently inherit its elapsed wall time.
        from api.attack_live_world import LiveWorkloadWorld
        with self.assertRaisesRegex(ValueError, "manually advanceable"):
            LiveWorkloadWorld(runner=runner, executor=executor, producer_id="live")

        args, host, runner, executor, _ = setup(live_time_policy=None, build_world=False)
        # An omitted manifest policy has no inferred default.
        with self.assertRaisesRegex(ValueError, "live_time_policy"):
            LiveWorkloadWorld(runner=runner, executor=executor, producer_id="live")

    def test_host_job_capacity_is_a_released_denial_not_a_stalled_pump(self):
        args, host, runner, executor, world = setup(victim_workload=False, max_jobs=1)
        at(host, 2)
        first = world.submit(args[4]["a"], request("first"))
        second = world.submit(args[4]["a"], request("second"))

        at(host, 1000)
        report = world.pump(max_transitions=64)

        self.assertTrue(report.finished)
        self.assertIsNone(world.reply(args[4]["a"], first).error_code)
        self.assertEqual(world.reply(args[4]["a"], second).error_code, "HOST_CAPACITY_REACHED")

    def test_queued_job_cap_denies_new_work_but_not_replays(self):
        args, host, runner, executor, world = setup(victim_workload=False, build_world=False)
        session = args[5]
        session._limits = replace(session._limits, max_queued_jobs=1)
        world = LiveWorkloadWorld(runner=runner, executor=executor, producer_id="live")
        world.start()
        at(host, 2)
        first = world.submit(args[4]["a"], request("first"))
        second = world.submit(args[4]["a"], request("second"))
        replay = world.submit(args[4]["a"], request("first"))
        at(host, 3)  # Admission decisions are made at arrival.
        world.pump(max_transitions=64)
        at(host, 1000)
        world.pump(max_transitions=64)
        self.assertIsNone(world.reply(args[4]["a"], first).error_code)
        self.assertEqual(world.reply(args[4]["a"], second).error_code, "HOST_CAPACITY_REACHED")
        self.assertEqual(world.reply(args[4]["a"], replay).value.job_id,
                         world.reply(args[4]["a"], first).value.job_id)

    def test_job_history_keeps_the_newest_records_and_reports_older_ones_expired(self):
        args, host, runner, executor, world = setup(victim_workload=False, retained_jobs=2)
        credential, time_us, jobs = args[4]["a"], 2, []
        for index in range(5):  # The executor bound alone (32) never trims.
            at(host, time_us)
            receipt = world.submit(credential, request(f"job-{index}"))
            time_us += 1000
            at(host, time_us)
            world.pump(max_transitions=64)
            jobs.append(world.reply(credential, receipt).value.job_id)
        at(host, time_us + 1000)
        world.pump(max_transitions=64)

        retained = list(args[2].scheduler._jobs)
        self.assertEqual(retained, jobs[-2:])
        self.assertEqual(set(executor._windows) | set(executor._releases), set(jobs[-2:]))
        self.assertEqual(set(world._arrivals), set(jobs[-2:]))
        self.assertEqual({event.job_id for event in args[1].events()}, set(jobs[-2:]))

        at(host, time_us + 2000)
        expired = world.status(credential, jobs[0])
        kept = world.status(credential, jobs[-1])
        replay = world.submit(credential, request("job-0"))
        at(host, time_us + 4000)
        world.pump(max_transitions=64)
        self.assertEqual(world.reply(credential, expired).error_code, "JOB_EXPIRED")
        self.assertEqual(world.reply(credential, kept).value.status, JobStatus.SUCCEEDED)
        self.assertEqual(world.reply(credential, replay).error_code, "JOB_EXPIRED")
        self.assertEqual(args[2].scheduler.queued_job_ids(), ())

    def test_latest_physical_job_is_never_discarded(self):
        args, host, runner, executor, world = setup(victim_workload=False, retained_jobs=1)
        at(host, 2)
        receipt = world.submit(args[4]["a"], request("only"))
        at(host, 1000)
        world.pump(max_transitions=64)
        job_id = world.reply(args[4]["a"], receipt).value.job_id
        with self.assertRaisesRegex(ValueError, "latest physical job"):
            executor.forget_job(job_id, owner=world._controls._owner)
        with self.assertRaises(PermissionError):
            executor.forget_job(job_id, owner=object())

    def test_live_world_runs_attacker_only_without_a_declared_actor_and_retires_denial(self):
        args, host, runner, executor, world = setup(victim_workload=False)
        self.assertEqual(runner.states(), {})
        at(host, 2)
        denied = world.submit(args[4]["a"], request("invalid", shots=33))
        accepted = world.submit(args[4]["a"], request("valid"))

        at(host, 1000)
        report = world.pump(max_transitions=64)

        self.assertTrue(report.finished)
        self.assertFalse(report.limit_reached)
        self.assertEqual(world.reply(args[4]["a"], denied).error_code, "INVALID_PROGRAM")
        reply = world.reply(args[4]["a"], accepted)
        self.assertIsNone(reply.error_code)
        job = args[2].scheduler.get(reply.value.job_id)
        self.assertIs(job.status, JobStatus.SUCCEEDED)
        self.assertEqual(job.command.operation.logical_qubits, 2)
        self.assertEqual(len(executor._finished), 1)
        frontier = executor.frontier.snapshot()
        self.assertIsNotNone(frontier.confirmed)
        self.assertIsNotNone(frontier.sealed_through)
        self.assertEqual(runner.states(), {})

    def test_actorless_live_world_disposal_rejects_further_pump(self):
        args, host, _, _, world = setup(victim_workload=False)
        at(host, 2)
        receipt = world.submit(args[4]["a"], request())
        self.assertIsNotNone(receipt)

        world.close()

        with self.assertRaises(WorkloadInfrastructureError):
            world.pump(max_transitions=1)

    def test_both_modes_live_program_and_finite_victim_use_common_multiple_qubit_runtime(self):
        for mode in ScenarioMode:
            args, host, runner, executor, world = setup(mode=mode, visits=2)
            at(host, 5)
            receipt = world.submit(args[4]["a"], request())
            self.assertEqual(args[2].scheduler.queued_job_ids(), ())
            self.assertEqual(args[5].receipt_records(), ())
            at(host, 1000)
            self.assertTrue(world.pump(max_transitions=64).finished)
            reply = world.reply(args[4]["a"], receipt)
            self.assertIsNone(reply.error_code)
            job = args[2].scheduler.get(reply.value.job_id)
            self.assertEqual(job.command.operation.logical_qubits, 2)
            self.assertIs(job.ingress_receipt, receipt)
            self.assertIs(job.status, JobStatus.SUCCEEDED)
            self.assertTrue(runner.states()["plan-v"].finished)
            self.assertEqual(len(executor._finished), 3)
            self.assertNotIn("received_at_ns", vars(reply))

    def test_same_sample_is_not_sealed_and_repeated_equal_ns_receipts_keep_unique_order(self):
        args, host, runner, executor, world = setup()
        at(host, 10)
        first = world.submit(args[4]["a"], request("one"))
        second = world.submit(args[4]["a"], request("two"))
        self.assertEqual(first.received_at_ns, second.received_at_ns)
        self.assertNotEqual(first.sequence, second.sequence)
        at(host, 17.5)
        world.pump(max_transitions=64)
        self.assertNotIn(first.sequence, {r.reservation.event_id for r in runner._book.snapshot().history
            if r.reservation.producer_id == "live"})
        at(host, 1000)
        world.pump(max_transitions=64)
        ids = [world.reply(args[4]["a"], r).value.job_id for r in (first, second)]
        self.assertEqual(sorted(ids, key=lambda j: args[2].scheduler.get(j).acceptance_sequence), ids)

    def test_exact_receipt_principal_scope_and_host_limits_do_not_mutate_physics(self):
        args, host, _, _, world = setup(history=2)
        at(host, 1)
        receipt = world.submit(args[4]["a"], request())
        with self.assertRaises(ValueError):
            world.reply(args[4]["a"], replace(receipt))
        with self.assertRaises(PermissionError):
            world.reply(args[4]["v"], receipt)
        with self.assertRaises(PermissionError):
            world.submit("not-issued", request())
        with self.assertRaises(JobResultNotReadyError):
            world.reply(args[4]["a"], receipt)
        world.submit(args[4]["a"], request("two"))
        with self.assertRaises(SessionCapacityError):
            world.submit(args[4]["a"], request("three"))
        self.assertEqual(args[1].events(), ())
        self.assertEqual(args[2].scheduler.queued_job_ids(), ())

    def test_queued_cancel_replaces_dependency_and_advances_common_cancelled_branch(self):
        args, host, runner, executor, world = setup(visits=2,
            service_timing={**TIMING, "processing_delay_us": 100})
        at(host, 20)
        world.pump(max_transitions=1)
        job = runner.states()["plan-v"].events[0].job_id
        receipt = world.cancel(args[4]["v"], job)
        at(host, 35)
        world.pump(max_transitions=64)
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.CANCELLED)
        trace = executor._cancellations[job]
        self.assertEqual(trace.application.time_us, 29.5)
        self.assertEqual(trace.release.time_us, 32.5)
        self.assertIsNone(world.reply(args[4]["v"], receipt).error_code)
        self.assertIs(runner.states()["plan-v"].events[0].outcome, Outcome.CANCELLED)
        self.assertEqual(runner.states()["plan-v"].events[1].arrival.time_us, 42.5)
        self.assertEqual(args[1].events(), ())
        at(host, 1000)
        self.assertTrue(world.pump(max_transitions=64).finished)

    def test_cancellation_after_start_is_a_declared_rejection_without_physical_reset(self):
        args, host, runner, executor, world = setup()
        at(host, 16)
        world.pump(max_transitions=2)
        job = runner.states()["plan-v"].events[0].job_id
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.RUNNING)
        receipt = world.cancel(args[4]["v"], job)
        at(host, 1000)
        world.pump(max_transitions=64)
        self.assertEqual(world.reply(args[4]["v"], receipt).error_code, "CANCELLATION_REJECTED")
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.SUCCEEDED)
        self.assertNotIn(job, executor._cancellations)

    def test_compile_exception_retains_exact_arrival_and_explicit_retry_admits_once(self):
        args, host, runner, _, world = setup()
        at(host, 2)
        receipt = world.submit(args[4]["a"], request())
        at(host, 1000)
        with patch.object(args[5]._service, "_prepare_program", side_effect=RuntimeError("test host failure")):
            with self.assertRaises(RuntimeError):
                world.pump(max_transitions=64)
        self.assertIs(runner._book.snapshot().granted, world._ingress.reservation(receipt))
        self.assertEqual(args[1].events(), ())
        self.assertTrue(world.pump(max_transitions=64).finished)
        self.assertEqual(len(args[2].scheduler._jobs), 2)

    def test_capture_during_blocked_compile_records_before_worker_wait_and_no_parallel_pump(self):
        args, host, _, _, world = setup()
        at(host, 2)
        first = world.submit(args[4]["a"], request())
        at(host, 1000)
        entered, release = Event(), Event()
        original = args[5]._service._prepare_program
        def paused(*a, **kw):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            return original(*a, **kw)
        with patch.object(args[5]._service, "_prepare_program", side_effect=paused), ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(world.pump, max_transitions=64)
            try:
                self.assertTrue(entered.wait(2))
                at(host, 1100)
                second = world.submit(args[4]["a"], request("second"))
                self.assertEqual(second.received_at_ns, host.value)
                with self.assertRaises(SessionBusyError):
                    world.pump(max_transitions=64)
            finally:
                release.set()
            task.result(timeout=8)
        at(host, 2000)
        world.pump(max_transitions=64)
        self.assertNotEqual(world.reply(args[4]["a"], first).value.job_id,
                            world.reply(args[4]["a"], second).value.job_id)

    def test_slow_numeric_catchup_and_small_pump_budget_preserve_declared_trace(self):
        outputs = []
        for batch, delay in ((64, False), (1, True)):
            args, host, runner, executor, world = setup(visits=3)
            args[5]._service._sampling_salt = bytes(32)  # Deterministic test entropy only.
            at(host, 2)
            receipt = world.submit(args[4]["a"], request())
            at(host, 1000)
            original = executor.prepare_next
            def slow():
                if delay:
                    host.value += 10_000_000
                return original()
            with patch.object(executor, "prepare_next", side_effect=slow):
                for _ in range(32):
                    report = world.pump(max_transitions=batch)
                    if report.finished:
                        break
                else:
                    self.fail("bounded catch-up did not settle")
            live_job = world.reply(args[4]["a"], receipt).value.job_id
            jobs = [args[2].scheduler.get(live_job)] + [args[2].scheduler.get(e.job_id)
                for s in runner.states().values() for e in s.events]
            outputs.append(([(j.command.tenant_id, j.status, j.result) for j in jobs],
                [(e.arrival, e.result_arrival, e.outcome) for s in runner.states().values() for e in s.events],
                [(e.event_kind, e.device_time_start_us, e.device_time_end_us, e.owner_after) for e in args[1].events()],
                args[1].read().density_matrix, executor.frontier.snapshot().confirmed))
        self.assertEqual(outputs[0][:-2], outputs[1][:-2])
        np.testing.assert_array_equal(outputs[0][-2], outputs[1][-2])
        self.assertEqual(outputs[0][-1], outputs[1][-1])

    def test_cancelled_fifo_predecessor_replans_unstarted_successor_not_finished_physics(self):
        args, host, runner, executor, world = setup(service_timing={**TIMING, "processing_delay_us": 100})
        at(host, 2)
        first = world.submit(args[4]["a"], request("first", shots=20))
        second = world.submit(args[4]["a"], request("second", shots=2))
        at(host, 20)
        world.pump(max_transitions=3)
        first_job = world._operations[first.sequence].value.job_id
        second_job = world._operations[second.sequence].value.job_id
        previous = executor._windows[second_job]
        cancel = world.cancel(args[4]["a"], first_job)
        at(host, 35)
        world.pump(max_transitions=8)
        self.assertIs(args[2].scheduler.get(first_job).status, JobStatus.CANCELLED)
        self.assertLess(executor._windows[second_job].start.key, previous.start.key)
        self.assertEqual(executor._windows[second_job].arrival, previous.arrival)
        self.assertEqual(args[1].events(), ())
        self.assertIsNone(world.reply(args[4]["a"], cancel).error_code)
        at(host, 1000)
        world.pump(max_transitions=64)
        self.assertIs(args[2].scheduler.get(second_job).status, JobStatus.SUCCEEDED)

    def test_observation_retains_queued_status_and_unready_result_at_arrival_not_late_projection(self):
        args, host, runner, executor, world = setup(service_timing={**TIMING, "processing_delay_us": 100})
        at(host, 20)
        world.pump(max_transitions=1)
        job = runner.states()["plan-v"].events[0].job_id
        status = world.status(args[4]["v"], job)
        result = world.result(args[4]["v"], job)
        at(host, 1000)
        world.pump(max_transitions=64)
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.SUCCEEDED)
        self.assertIs(world.reply(args[4]["v"], status).value.status, JobStatus.QUEUED)
        self.assertEqual(world.reply(args[4]["v"], result).error_code, "RESULT_NOT_READY")
        now = world.result(args[4]["v"], job)
        with self.assertRaises(JobResultNotReadyError):
            world.reply(args[4]["v"], now)
        at(host, 1020)
        world.pump(max_transitions=8)
        value = world.reply(args[4]["v"], now).value
        value.result["counts"].clear()
        self.assertTrue(world.reply(args[4]["v"], now).value.result["counts"])

    def test_foreign_and_nonexistent_jobs_have_same_projection(self):
        args, host, runner, _, world = setup()
        at(host, 1000)
        world.pump(max_transitions=64)
        job = runner.states()["plan-v"].events[0].job_id
        foreign = world.status(args[4]["a"], job)
        missing = world.status(args[4]["a"], "missing")
        at(host, 1020)
        world.pump(max_transitions=16)
        self.assertEqual(world.reply(args[4]["a"], foreign), world.reply(args[4]["a"], missing))

    def test_raw_http_fence_manual_runner_and_closed_proofs_remain_unavailable(self):
        args, host, runner, executor, world = setup()
        at(host, 20)
        with self.assertRaises(PermissionError):
            args[5].receive(args[4]["a"], "program.submit")
        with self.assertRaises(PermissionError):
            runner.dispatch_next()
        with self.assertRaises(PermissionError):
            runner.seal_physical_through(executor, world.target(), _coordinator_owner=world._owner)
        self.assertEqual(args[2].scheduler.queued_job_ids(), ())
        self.assertEqual(args[1].events(), ())

    def test_large_topology_remains_metadata_with_multiple_qubit_region(self):
        from core.attack_simulation.qpu_topology import RetainedRegionBinding
        from tests.test_qpu_topology import large_topology
        topology = large_topology()
        args, host, runner, executor, world = setup(topology_binding=RetainedRegionBinding(topology, (7, 17, 207), 4))
        at(host, 2)
        receipt = world.submit(args[4]["a"], request())
        at(host, 1000)
        self.assertTrue(world.pump(max_transitions=64).finished)
        self.assertIs(args[1].region_binding.topology, topology)
        self.assertEqual(len(topology.physical_qubit_ids), 300)
        self.assertEqual(np.asarray(args[1].read().density_matrix).shape, (8, 8))
        self.assertIsNone(world.reply(args[4]["a"], receipt).error_code)

    def test_discard_preserves_pending_evidence_without_reset_or_setting_thaw(self):
        args, host, _, _, world = setup()
        at(host, 2)
        receipt = world.submit(args[4]["a"], request())
        token = world._ingress.reservation(receipt)
        world.close()
        self.assertIs(world._ingress.reservation(receipt), token)
        self.assertEqual(args[1].events(), ())
        with self.assertRaises(RuntimeError):
            args[0].set_assumption(None)
        with self.assertRaises((RuntimeError, PermissionError)):
            world.pump(max_transitions=1)

    def test_numeric_interruption_preserves_started_job_then_explicit_retry(self):
        args, host, runner, executor, world = setup()
        at(host, 1000)
        with patch.object(executor, "prepare_next", side_effect=RuntimeError("test numeric failure")):
            with self.assertRaises(RuntimeError):
                world.pump(max_transitions=64)
        job = runner.states()["plan-v"].events[0].job_id
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.RUNNING)
        before = args[1].events()
        self.assertTrue(world.pump(max_transitions=64).finished)
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.SUCCEEDED)
        self.assertEqual(args[1].events()[:len(before)], before)

    def test_live_owner_scope_does_not_grant_access_to_foreign_site_environment(self):
        owner = WebOwnerContext()
        args, host, _, _, world = setup(owner_context=owner)
        at(host, 2)
        for context in (None, WebOwnerContext()):
            with self.assertRaises(PermissionError):
                world.submit(args[4]["a"], request(), owner_context=context)
        receipt = world.submit(args[4]["a"], request(), owner_context=owner)
        at(host, 1000)
        world.pump(max_transitions=64)
        with self.assertRaises(PermissionError):
            world.reply(args[4]["a"], receipt, owner_context=WebOwnerContext())
        self.assertIsNone(world.reply(args[4]["a"], receipt, owner_context=owner).error_code)

    def test_runner_metadata_copy_never_waits_under_physical_gate(self):
        args, host, runner, executor, world = setup(visits=2)
        at(host, 1000)
        original = runner.states
        def copy():
            self.assertFalse(executor._gate._is_owned())
            return original()
        with patch.object(runner, "states", side_effect=copy):
            self.assertTrue(world.pump(max_transitions=64).finished)

    def test_discard_during_numeric_work_fences_finish_and_preserves_started_effects(self):
        args, host, runner, executor, world = setup()
        at(host, 1000)
        entered, release, fenced = Event(), Event(), Event()
        original = executor.prepare_next
        close = args[5]._fence_close
        def paused():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test numeric timeout")
            return original()
        def fence():
            close()
            fenced.set()
        with patch.object(executor, "prepare_next", side_effect=paused), \
                patch.object(args[5], "_fence_close", side_effect=fence), ThreadPoolExecutor(max_workers=2) as pool:
            pumping = pool.submit(world.pump, max_transitions=64)
            disposing = None
            try:
                self.assertTrue(entered.wait(2))
                before = args[1].read(), args[1].events()
                disposing = pool.submit(world.close)
                self.assertTrue(fenced.wait(2))
            finally:
                release.set()
            with self.assertRaises((RuntimeError, PermissionError)):
                pumping.result(timeout=8)
            disposing.result(timeout=8)
        self.assertEqual((args[1].read(), args[1].events()), before)
        self.assertIs(args[2].scheduler.get(runner.states()["plan-v"].events[0].job_id).status, JobStatus.RUNNING)

    def test_cancel_acknowledgment_does_not_require_an_extra_status_read_grant(self):
        args, host, runner, executor, world = setup(
            service_timing={**TIMING, "processing_delay_us": 100},
            attacker_operations=("program.submit", "job.cancel"))
        at(host, 2)
        submit = world.submit(args[4]["a"], request())
        at(host, 20)
        world.pump(max_transitions=1)
        job = world._operations[submit.sequence].value.job_id
        cancel = world.cancel(args[4]["a"], job)
        status = world.status(args[4]["a"], job)
        at(host, 35)
        world.pump(max_transitions=16)
        self.assertIs(args[2].scheduler.get(job).status, JobStatus.CANCELLED)
        self.assertIsNone(world.reply(args[4]["a"], cancel).error_code)
        self.assertIs(world.reply(args[4]["a"], cancel).value.status, JobStatus.CANCELLED)
        self.assertEqual(world.reply(args[4]["a"], status).error_code, "OPERATION_DENIED")

    def test_start_tie_and_duplicate_cancel_are_normal_rejections_not_pump_failures(self):
        for duplicate in (False, True):
            args, host, runner, executor, world = setup(
                service_timing={**TIMING, "processing_delay_us": 100})
            at(host, 20)
            world.pump(max_transitions=1)
            job = runner.states()["plan-v"].events[0].job_id
            if not duplicate:
                at(host, 105.5)  # arrival 113 + processing 2 == declared START 115.
            first = world.cancel(args[4]["v"], job)
            second = world.cancel(args[4]["v"], job) if duplicate else None
            at(host, 1000)
            self.assertTrue(world.pump(max_transitions=64).finished)
            if duplicate:
                self.assertIsNone(world.reply(args[4]["v"], first).error_code)
                self.assertEqual(world.reply(args[4]["v"], second).error_code, "CANCELLATION_REJECTED")
                self.assertIs(args[2].scheduler.get(job).status, JobStatus.CANCELLED)
                self.assertEqual(len(executor._cancellations), 1)
            else:
                self.assertEqual(world.reply(args[4]["v"], first).error_code, "CANCELLATION_REJECTED")
                self.assertIs(args[2].scheduler.get(job).status, JobStatus.SUCCEEDED)

    def test_service_input_denial_retires_receipt_and_keeps_other_sources_progressing(self):
        args, host, runner, executor, world = setup()
        at(host, 2)
        denied = world.submit(args[4]["a"], request("invalid", shots=33))
        accepted = world.submit(args[4]["a"], request("valid", shots=2))
        at(host, 1000)
        self.assertTrue(world.pump(max_transitions=64).finished)
        self.assertEqual(world.reply(args[4]["a"], denied).error_code, "INVALID_PROGRAM")
        self.assertIsNone(world.reply(args[4]["a"], accepted).error_code)
        self.assertTrue(runner.states()["plan-v"].finished)
        self.assertEqual(len(executor._finished), 2)
        self.assertIsNone(runner._book.snapshot().granted)

    def test_close_between_service_denial_and_publication_preserves_unresolved_evidence(self):
        from api.attack_sessions import AttackSession
        for malformed in (False, True):
            args, host, runner, executor, world = setup(
                attacker_operations=OPERATIONS if malformed else ())
            at(host, 2)
            receipt = world.submit(args[4]["a"], request(shots=33 if malformed else 2))
            at(host, 1000)
            original = AttackSession.commit_workload_transition
            def discard_before_denial(session, owner, credential, principal_id, update):
                if getattr(update, "__name__", "") == "deny":
                    args[6].discard("world")
                return original(session, owner, credential, principal_id, update)
            with patch.object(AttackSession, "commit_workload_transition", new=discard_before_denial):
                with self.assertRaises(PermissionError):
                    world.pump(max_transitions=64)
            op = world._operations[receipt.sequence]
            self.assertFalse(op.applied)
            self.assertIs(runner._book.snapshot().granted, op.reservation)
            self.assertIsNone(runner._book.decision(op.reservation))
            self.assertEqual(args[1].events(), ())


if __name__ == "__main__":
    unittest.main()
