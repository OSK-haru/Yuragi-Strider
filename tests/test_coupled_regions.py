"""Residual ZZ between two concurrent regions: one joint world, advanced in time order (D85)."""

import math
import unittest

import numpy as np
from scipy.linalg import expm

from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.concurrent_lanes import ConcurrentRegions
from core.attack_simulation.coupled_regions import (
    CoupledRegionWorld, RegionSide, ResidualZZSettings, WorldJob,
)
from core.attack_simulation.declared_execution import DeclaredQpuExecutor, JobExecutionWindow
from core.attack_simulation.jobs import JobCommand
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.attack_simulation.shot_activity import instruction_starts
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer, ResidualZZCoupling
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from core.experimental_isolation_physics.imperfect_reset import ImperfectResetSpec
from core.experimental_isolation_physics.reset_model import PhenomenologicalResetModel, ResetModelRegistry
from tests.test_concurrent_regions import pos, run_in_order
from tests.test_local_program_runtime import program_json
from tests.test_regional_state_bank import regional_store

REGIONS = {"victim": (0, 1), "attacker": (2, 3)}
SX_US = 0.02


def compiler(gate_us=SX_US, qubits=(0, 1, 2, 3, 4, 5)):
    natives = {str(q): {"x": {"duration_us": gate_us, "gate_error": 0.0},
                        "sx": {"duration_us": gate_us, "gate_error": 0.0},
                        "rz": {"duration_us": 0.0, "gate_error": 0.0}} for q in qubits}
    return LocalCircuitCompiler(max_program_qubits=3, physical_qubit_ids=tuple(qubits),
                                single_qubit_native_gates=natives,
                                single_qubit_gate_decompositions={"X": ("x",), "H": ("rz", "sx", "rz")})


def operation(columns, physical, *, shots=1, seed=1, gate_us=SX_US, qubits=None):
    qubits = len(physical) if qubits is None else qubits
    request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(qubits, columns)), "qpu-1",
                                   shots, f"r{seed}")
    return compiler(gate_us).compile(request, sampling_seed=seed, physical_qubit_ids=physical)


def registry():
    model = PhenomenologicalResetModel(ImperfectResetSpec(1.0, (1.0, 0.0)))
    reg = ResetModelRegistry()
    reg.register(model)
    return model, reg


def build(*, transfers=(), zz=None, t1=math.inf, tphi=math.inf):
    model, reg = registry()
    idle = IndependentIdleModel((IdleQubitSpec(t1, tphi, 0.0),) * 2, 2)
    runtime = SharedQpuRuntime(
        state_store=regional_store(), reset_models=reg,
        config=SharedQpuRuntimeConfig(model.model_id, model.configuration_id,
                                      placement_policy=PlacementPolicy.TENANT_DEDICATED_REGION,
                                      max_local_qubits=2, entry_reuse_tolerance=0.0),
        tenant_regions=REGIONS, regional_reset_models={(0, 1): reg, (2, 3): reg},
        regional_idle_models={(0, 1): idle, (2, 3): idle},
        regional_service_durations={(0, 1): (1.0, 1.0), (2, 3): (1.0, 1.0)})
    executor = DeclaredQpuExecutor(
        runtime=runtime, clock_domain="world", max_jobs=16, max_events_per_job=64, max_unresolved=16,
        idle_model=idle, concurrent_regions=ConcurrentRegions(tuple(transfers), residual_zz=zz))
    executor.frontier.set_target(pos(1000, 99))
    executor.frontier.seal_ingress(pos(1000, 99))
    return runtime, executor


def submit(runtime, tenant, columns, key, shots=1, seed=1):
    op = operation(columns, REGIONS[tenant], shots=shots, seed=seed)
    return runtime.submit(JobCommand(tenant, op, key, layout=PhysicalQubitLayout("qpu-1", REGIONS[tenant])))


def register(executor, job, start, order):
    lane = executor._lanes[REGIONS[job.command.tenant_id]]
    duration = executor._duration(job, lane.store.read())
    executor.register_job(job.job_id, JobExecutionWindow(pos(start - 0.5, order), pos(start, order + 1),
                                                         pos(start + duration, order + 2),
                                                         pos(start + duration + 1.0, order + 3)))


def ramsey(wait_us):
    return [[{"type": "H", "targets": [1]}], [{"type": "I", "targets": [1], "params": {"duration_us": wait_us}}],
            [{"type": "H", "targets": [1]}]]


class ExecutorTests(unittest.TestCase):
    def test_zero_coupling_reproduces_the_region_by_region_path(self):
        victim_columns = ramsey(2.0) + [[{"type": "X", "targets": [0]}]]
        attacker_columns = [[{"type": "H", "targets": [0]}], [{"type": "I", "targets": [1], "params": {"duration_us": 1.5}}],
                            [{"type": "X", "targets": [1]}]]
        results = []
        for zz in (None, ResidualZZSettings((ResidualZZCoupling(1, 2, 0.0),), model="exact")):
            runtime, executor = build(transfers=[DriveCrosstalkTransfer(2, 1, 0.3), DriveCrosstalkTransfer(1, 2, 0.2)],
                                      zz=zz, t1=40.0, tphi=60.0)
            victim = submit(runtime, "victim", victim_columns, "v", shots=4, seed=3)
            attacker = submit(runtime, "attacker", attacker_columns, "a", shots=3, seed=5)
            register(executor, victim, 2.0, 0)
            register(executor, attacker, 2.7, 4)
            run_in_order(executor)
            results.append((runtime.scheduler.get(victim.job_id).result, runtime.scheduler.get(attacker.job_id).result,
                            {region: np.asarray(lane.store.read().density_matrix) for region, lane in
                             executor._lanes.items()},
                            {region: [(event.device_time_start_us, event.device_time_end_us)
                                      for event in lane.store.events()]
                             for region, lane in executor._lanes.items()}))
        plain, coupled = results
        self.assertEqual(plain[0], coupled[0])
        self.assertEqual(plain[1], coupled[1])
        for region in plain[2]:
            np.testing.assert_allclose(coupled[2][region], plain[2][region], atol=1e-10)
            np.testing.assert_allclose(coupled[3][region], plain[3][region], atol=1e-12)

    def test_the_victim_reads_the_neighbor_through_the_coupling(self):
        # zeta * (time between the two H) = pi: the victim's Ramsey fringe flips with the attacker's qubit.
        wait = 4.0
        zeta = math.pi / (wait + SX_US)
        outcomes = {}
        for neighbor in (0, 1):
            runtime, executor = build(zz=ResidualZZSettings((ResidualZZCoupling(1, 2, zeta),), model="exact"))
            attacker_columns = ([[{"type": "X", "targets": [0]}]] if neighbor else []) + [
                [{"type": "I", "targets": [0], "params": {"duration_us": 20.0}}]]
            attacker = submit(runtime, "attacker", attacker_columns, "a")
            victim = submit(runtime, "victim", ramsey(wait), "v")
            register(executor, attacker, 1.0, 0)
            register(executor, victim, 3.0, 4)
            run_in_order(executor)
            outcomes[neighbor] = runtime.scheduler.get(victim.job_id).result["counts"]
        self.assertEqual(outcomes[0], {"00": 1})
        self.assertEqual(outcomes[1], {"01": 1})


def dense_reference(jobs, sides, couplings, until_us):
    """Union-register Lindblad evolution between instantaneous gates, independent of the world's machinery.

    Gates have zero duration here, so every qubit relaxes all the time and ZZ acts throughout.
    """
    sizes = [len(side.region) for side in sides]
    n = sum(sizes)
    offset = (0, sizes[0])
    dim = 1 << n

    def single(op, q):
        out = np.eye(1)
        for k in range(n):
            out = np.kron(out, op if k == q else np.eye(2))
        return out

    number = np.array([[0, 0], [0, 1]], dtype=complex)
    hamiltonian = np.zeros((dim, dim), dtype=complex)
    for (sa, qa), (sb, qb), zeta in couplings:
        hamiltonian += zeta * single(number, offset[sa] + qa) @ single(number, offset[sb] + qb)
    collapse = []
    for s, side in enumerate(sides):
        for q, spec in enumerate(side.idle_model.qubits):
            if math.isfinite(spec.t1_us):
                collapse.append(math.sqrt(1.0 / spec.t1_us) * single(np.array([[0, 1], [0, 0]], dtype=complex),
                                                                      offset[s] + q))
            if math.isfinite(spec.tphi_us):
                collapse.append(math.sqrt(0.5 / spec.tphi_us) * single(np.diag([1, -1]).astype(complex),
                                                                       offset[s] + q))
    eye = np.eye(dim)
    generator = -1j * (np.kron(eye, hamiltonian) - np.kron(hamiltonian.T, eye))
    for c in collapse:
        cc = c.conj().T @ c
        generator += np.kron(c.conj(), c) - 0.5 * np.kron(eye, cc) - 0.5 * np.kron(cc.T, eye)

    events = []
    for job in jobs:
        starts, _ = instruction_starts(job.operation, reset_duration_us=sides[job.side].reset_us)
        for instruction, start in zip(job.operation.instructions, starts):
            if instruction.kind == "unitary":
                events.append((job.start_us + start, job.side, instruction))
    events = sorted((item for item in events if item[0] <= until_us), key=lambda item: item[0])
    rho = np.zeros((dim, dim), dtype=complex)
    rho[0, 0] = 1.0
    now = 0.0
    for time, side, instruction in events + [(until_us, None, None)]:
        vec = rho.reshape(-1, order="F")
        rho = (expm(generator * (time - now)) @ vec).reshape(dim, dim, order="F")
        now = time
        if instruction is None:
            break
        targets = [offset[side] + q for q in instruction.qubits]
        if len(targets) != 1:
            raise ValueError("the reference only takes one-qubit gates")
        full = single(np.asarray(instruction.unitary, dtype=complex), targets[0])
        rho = full @ rho @ full.conj().T
    return rho


class WorldTests(unittest.TestCase):
    def sides(self, size, t1, tphi):
        model, reg = registry()
        idle = IndependentIdleModel((IdleQubitSpec(t1, tphi, 0.0),) * size, size)
        return (RegionSide(tuple(range(size)), idle, reg, model.model_id, 0.0, 1.0),
                RegionSide(tuple(range(size, 2 * size)), idle, reg, model.model_id, 0.0, 1.0))

    def scenario(self, size):
        """Gates of zero duration at staggered times on both sides, with superpositions on the coupled qubits."""
        a = [[{"type": "H", "targets": [q]} for q in range(size)],
             [{"type": "I", "targets": [size - 1], "params": {"duration_us": 1.3}}],
             [{"type": "X", "targets": [0]}], [{"type": "H", "targets": [size - 1]}],
             [{"type": "I", "targets": [size - 1], "params": {"duration_us": 0.9}}],
             [{"type": "H", "targets": [size - 1]}]]
        b = [[{"type": "H", "targets": [0]}], [{"type": "I", "targets": [0], "params": {"duration_us": 0.7}}],
             [{"type": "X", "targets": [0]}], [{"type": "I", "targets": [0], "params": {"duration_us": 1.1}}],
             [{"type": "H", "targets": [0]}, {"type": "H", "targets": [size - 1]}]]
        physical_a, physical_b = tuple(range(size)), tuple(range(size, 2 * size))
        op_a = operation(a, physical_a, gate_us=0.0)
        op_b = operation(b, physical_b, gate_us=0.0)
        # ``prepare`` adds an initializing reset; with zero reset time on a fresh |0> it changes nothing.
        return (WorldJob("a", 0, op_a, tuple(range(size)), 0.25, 99.0),
                WorldJob("b", 1, op_b, tuple(range(size)), 0.6, 99.0))

    def joint(self, world):
        a, b = world.factors
        return np.einsum("kij,klm->iljm", a, b).reshape(a.shape[1] * b.shape[1], -1)

    def run_world(self, size, settings, until, t1=30.0, tphi=50.0):
        sides = self.sides(size, t1, tphi)
        zero = np.zeros((1 << size, 1 << size), dtype=complex)
        zero[0, 0] = 1.0
        world = CoupledRegionWorld(sides, settings, (zero, zero), repetition_delay_us=0.0)
        jobs = self.scenario(size)
        world.advance_to(until, jobs)
        world.settle(0, until)
        world.settle(1, until)
        return world, sides, jobs

    def test_exact_world_matches_an_independent_dense_lindblad_solution(self):
        size, until = 2, 2.35
        settings = ResidualZZSettings((ResidualZZCoupling(1, 2, 1.7),), model="exact")
        world, sides, jobs = self.run_world(size, settings, until)
        reference = dense_reference(jobs, sides, [((0, 1), (1, 0), 1.7)], until)
        np.testing.assert_allclose(self.joint(world), reference, atol=1e-10)

    def test_two_pairs_through_one_qubit_match_the_reference(self):
        size, until = 2, 2.35
        couplings = (ResidualZZCoupling(1, 2, 1.1), ResidualZZCoupling(1, 3, 0.6))
        world, sides, jobs = self.run_world(size, ResidualZZSettings(couplings, model="exact"), until)
        reference = dense_reference(jobs, sides, [((0, 1), (1, 0), 1.1), ((0, 1), (1, 1), 0.6)], until)
        np.testing.assert_allclose(self.joint(world), reference, atol=1e-10)

    def test_low_rank_stays_within_its_dropped_norm(self):
        size, until = 3, 2.35
        coupling = (ResidualZZCoupling(2, 3, 2.0),)
        exact, _, _ = self.run_world(size, ResidualZZSettings(coupling, model="exact"), until)
        approx, _, _ = self.run_world(size, ResidualZZSettings(coupling, model="low_rank", max_rank=4,
                                                               truncation=1e-3), until)
        self.assertLessEqual(approx.stats.max_rank, 4)
        error = np.linalg.norm(self.joint(approx) - self.joint(exact))
        self.assertLessEqual(error, approx.stats.dropped_norm + 1e-12)
        self.assertLess(error, 5e-2)


if __name__ == "__main__":
    unittest.main()
