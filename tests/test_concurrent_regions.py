"""Concurrent sharing: two regions run side by side, coupled only by the drive leak between them."""

import math
import unittest

import numpy as np

from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.concurrent_lanes import ConcurrentRegions
from core.attack_simulation.declared_execution import DeclaredQpuExecutor, JobExecutionWindow
from core.attack_simulation.event_commit_boundary import EventOrderError, EventPosition
from core.attack_simulation.jobs import JobCommand, JobStatus
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from core.experimental_isolation_physics.imperfect_reset import ImperfectResetSpec
from core.experimental_isolation_physics.reset_model import PhenomenologicalResetModel, ResetModelRegistry
from tests.test_local_program_runtime import program_json
from tests.test_regional_state_bank import regional_store

REGIONS = {"victim": (0, 1), "attacker": (2, 3)}
WORLD = "world"


def pos(time_us, order=0):
    return EventPosition(WORLD, time_us, order)


def build(transfers=(), idle_t1=math.inf):
    model = PhenomenologicalResetModel(ImperfectResetSpec(1.0, (1.0, 0.0)))
    registry = ResetModelRegistry()
    registry.register(model)
    idle = IndependentIdleModel((IdleQubitSpec(idle_t1, math.inf, 0.0),) * 2, 2)
    runtime = SharedQpuRuntime(
        state_store=regional_store(), reset_models=registry,
        config=SharedQpuRuntimeConfig(model.model_id, model.configuration_id,
                                      placement_policy=PlacementPolicy.TENANT_DEDICATED_REGION,
                                      max_local_qubits=2),
        tenant_regions=REGIONS,
        regional_reset_models={(0, 1): registry, (2, 3): registry},
        regional_idle_models={(0, 1): idle, (2, 3): idle},
        regional_service_durations={(0, 1): (1.0, 1.0), (2, 3): (1.0, 1.0)})
    executor = DeclaredQpuExecutor(
        runtime=runtime, clock_domain=WORLD, max_jobs=16, max_events_per_job=16, max_unresolved=16,
        idle_model=idle, concurrent_regions=ConcurrentRegions(tuple(transfers)))
    executor.frontier.set_target(pos(1000, 99))
    executor.frontier.seal_ingress(pos(1000, 99))
    return runtime, executor


NATIVES = {str(q): {"x": {"duration_us": 0.02, "gate_error": 0.0}, "sx": {"duration_us": 0.02, "gate_error": 0.0},
                    "rz": {"duration_us": 0.0, "gate_error": 0.0}} for q in range(4)}
COMPILER = LocalCircuitCompiler(max_program_qubits=2, physical_qubit_ids=(0, 1, 2, 3),
                                single_qubit_native_gates=NATIVES, single_qubit_gate_decompositions={"X": ("x",)})


def submit(runtime, tenant, gates, key, shots=1, qubits=2):
    request = ProgramSubmitRequest(
        CircuitProgram.from_editor_json(program_json(qubits, gates)), "qpu-1", shots, key)
    operation = COMPILER.compile(request, sampling_seed=1, physical_qubit_ids=REGIONS[tenant][:qubits])
    return runtime.submit(JobCommand(tenant, operation, key, layout=PhysicalQubitLayout(
        "qpu-1", REGIONS[tenant][:qubits])))


def window_for(executor, job, start, order):
    lane = executor._lanes[REGIONS[job.command.tenant_id]]
    duration = executor._duration(job, lane.store.read())
    return JobExecutionWindow(pos(start - 0.5, order), pos(start, order + 1), pos(start + duration, order + 2),
                              pos(start + duration + 1.0, order + 3))


def run_in_order(executor):
    """Confirm START and FINISH events in position order, as the live driver does."""
    log = []
    while (step := executor.next_physical()) is not None:
        kind, job, window = step
        log.append((kind, job.command.tenant_id))
        if kind == "start":
            executor.begin_next()
        else:
            executor.commit(executor.prepare_next())
    return log


def excited(store, slot):
    rho = np.asarray(store.read().density_matrix)
    return float(sum(rho[i, i].real for i in range(4) if (i >> (1 - slot)) & 1))


DELAY = [[{"type": "I", "targets": [0], "params": {"duration_us": 5.0}}]]
PULSE = [[{"type": "X", "targets": [0]}]]


class ConcurrentRegionsTests(unittest.TestCase):
    def test_two_regions_run_side_by_side_without_a_transfer(self):
        runtime, executor = build()
        victim = submit(runtime, "victim", DELAY, "v")
        attacker = submit(runtime, "attacker", PULSE, "a")
        executor.register_job(victim.job_id, window_for(executor, victim, 2.0, 0))
        executor.register_job(attacker.job_id, window_for(executor, attacker, 3.0, 4))
        self.assertLess(executor._windows[attacker.job_id].start.time_us,
                        executor._windows[victim.job_id].finish.time_us)
        order = run_in_order(executor)
        self.assertEqual(order, [("start", "victim"), ("start", "attacker"),
                                 ("finish", "attacker"), ("finish", "victim")])
        for job in (victim, attacker):
            self.assertIs(runtime.scheduler.get(job.job_id).status, JobStatus.SUCCEEDED)
        self.assertEqual(runtime.scheduler.get(victim.job_id).result["counts"], {"00": 1})
        self.assertEqual(runtime.scheduler.get(attacker.job_id).result["counts"], {"10": 1})
        clocks = {region: lane.store.read().device_time_us for region, lane in executor._lanes.items()}
        self.assertNotEqual(clocks[(0, 1)], clocks[(2, 3)])

    def test_a_leak_from_the_attacker_rotates_only_the_victim(self):
        # A full-strength transfer turns the attacker's pi pulse into a pi rotation of the victim.
        runtime, executor = build([DriveCrosstalkTransfer(2, 0, 1.0)])
        victim = submit(runtime, "victim", DELAY, "v")
        attacker = submit(runtime, "attacker", PULSE, "a")
        executor.register_job(victim.job_id, window_for(executor, victim, 2.0, 0))
        executor.register_job(attacker.job_id, window_for(executor, attacker, 3.0, 4))
        run_in_order(executor)
        self.assertEqual(runtime.scheduler.get(victim.job_id).result["counts"], {"10": 1})
        # No declared transfer back: the attacker's own result is untouched.
        self.assertEqual(runtime.scheduler.get(attacker.job_id).result["counts"], {"10": 1})

    def test_the_other_direction_is_independent(self):
        runtime, executor = build([DriveCrosstalkTransfer(2, 0, 1.0), DriveCrosstalkTransfer(0, 2, 1.0)])
        victim = submit(runtime, "victim", PULSE, "v")
        attacker = submit(runtime, "attacker", DELAY, "a")
        executor.register_job(victim.job_id, window_for(executor, victim, 2.0, 0))
        executor.register_job(attacker.job_id, window_for(executor, attacker, 2.5, 4))
        run_in_order(executor)
        self.assertEqual(runtime.scheduler.get(attacker.job_id).result["counts"], {"10": 1})
        self.assertEqual(runtime.scheduler.get(victim.job_id).result["counts"], {"10": 1})

    def test_a_job_that_starts_after_the_other_finished_still_feels_the_gap_leak(self):
        c = 0.1
        runtime, executor = build([DriveCrosstalkTransfer(2, 1, c)])
        attacker = submit(runtime, "attacker", PULSE, "a")
        victim = submit(runtime, "victim", DELAY, "v", qubits=1)
        executor.register_job(attacker.job_id, window_for(executor, attacker, 2.0, 0))
        executor.register_job(victim.job_id, window_for(executor, victim, 30.0, 4))
        run_in_order(executor)
        # Region qubit 1 is outside the victim's program and sat idle while the pulse leaked onto it
        # (a program qubit would have been reset when its job began).
        self.assertAlmostEqual(excited(executor._lanes[(0, 1)].store, 1),
                               math.sin(c * math.pi / 2) ** 2, places=3)

    def test_no_transfer_means_no_effect_even_when_windows_overlap(self):
        runtime, executor = build([DriveCrosstalkTransfer(3, 1, 0.5)])
        victim = submit(runtime, "victim", DELAY, "v")
        attacker = submit(runtime, "attacker", PULSE, "a")
        executor.register_job(victim.job_id, window_for(executor, victim, 2.0, 0))
        executor.register_job(attacker.job_id, window_for(executor, attacker, 3.0, 4))
        run_in_order(executor)
        self.assertAlmostEqual(excited(executor._lanes[(0, 1)].store, 0), 0.0, places=12)

    def test_a_lane_waits_for_its_own_predecessor_not_for_the_other_region(self):
        runtime, executor = build()
        first = submit(runtime, "victim", DELAY, "v1")
        second = submit(runtime, "victim", DELAY, "v2")
        other = submit(runtime, "attacker", PULSE, "a")
        executor.register_job(first.job_id, window_for(executor, first, 2.0, 0))
        executor.register_job(second.job_id, window_for(executor, second, 12.0, 4))
        executor.register_job(other.job_id, window_for(executor, other, 3.0, 8))
        order = run_in_order(executor)
        self.assertLess(order.index(("start", "attacker")), order.index(("finish", "victim")))
        self.assertIs(runtime.scheduler.get(second.job_id).status, JobStatus.SUCCEEDED)

    def test_colliding_event_positions_across_regions_are_rejected(self):
        runtime, executor = build()
        victim = submit(runtime, "victim", DELAY, "v")
        attacker = submit(runtime, "attacker", PULSE, "a")
        window = window_for(executor, victim, 2.0, 0)
        executor.register_job(victim.job_id, window)
        clash = JobExecutionWindow(pos(1.0, 9), window.start, pos(40.0, 9), pos(41.0, 9))
        with self.assertRaises(EventOrderError):
            executor.register_job(attacker.job_id, clash)


if __name__ == "__main__":
    unittest.main()
