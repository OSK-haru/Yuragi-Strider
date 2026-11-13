"""Residual ZZ between two concurrent regions: one joint world, advanced in time order (D85)."""

import math
import unittest
from dataclasses import replace

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


def registry(success=1.0):
    model = PhenomenologicalResetModel(ImperfectResetSpec(success, (1.0, 0.0)))
    reg = ResetModelRegistry()
    reg.register(model)
    return model, reg


def build(*, transfers=(), zz=None, t1=math.inf, tphi=math.inf, reset_success=1.0, region_access=None,
          durations=(1.0, 1.0)):
    model, reg = registry(reset_success)
    idle = IndependentIdleModel((IdleQubitSpec(t1, tphi, 0.0),) * 2, 2)
    runtime = SharedQpuRuntime(
        state_store=regional_store(), reset_models=reg,
        config=SharedQpuRuntimeConfig(model.model_id, model.configuration_id,
                                      placement_policy=PlacementPolicy.TENANT_DEDICATED_REGION,
                                      max_local_qubits=2, entry_reuse_tolerance=0.0),
        tenant_regions=REGIONS, regional_reset_models={(0, 1): reg, (2, 3): reg},
        regional_idle_models={(0, 1): idle, (2, 3): idle},
        regional_service_durations={(0, 1): durations, (2, 3): durations}, region_access=region_access)
    executor = DeclaredQpuExecutor(
        runtime=runtime, clock_domain="world", max_jobs=16, max_events_per_job=64, max_unresolved=16,
        idle_model=idle, concurrent_regions=ConcurrentRegions(tuple(transfers), residual_zz=zz))
    executor.frontier.set_target(pos(1000, 99))
    executor.frontier.seal_ingress(pos(1000, 99))
    return runtime, executor


def submit(runtime, tenant, columns, key, shots=1, seed=1):
    op = operation(columns, REGIONS[tenant], shots=shots, seed=seed)
    return runtime.submit(JobCommand(tenant, op, key, layout=PhysicalQubitLayout("qpu-1", REGIONS[tenant])))


def register(executor, job, start, order, previous_tenant=None):
    """Declare the job's window; ``previous_tenant`` is who holds its region when it starts (handover)."""
    lane = executor._lanes[executor.runtime.region_of(job.command)]
    snapshot = lane.store.read()
    if previous_tenant is not None:
        snapshot = replace(snapshot, owner_tenant_id=previous_tenant)
    duration = executor._duration(job, snapshot)
    executor.register_job(job.job_id, JobExecutionWindow(pos(start - 0.5, order), pos(start, order + 1),
                                                         pos(start + duration, order + 2),
                                                         pos(start + duration + 1.0, order + 3)))


def submit_on(runtime, tenant, region, columns, key, *, qubits=None, seed=1):
    """A job of ``tenant`` laid out on ``region``'s first ``qubits`` qubits (any region, not only its home)."""
    qubits = len(region) if qubits is None else qubits
    physical = tuple(region[:qubits])
    op = operation(columns, physical, seed=seed, qubits=qubits)
    return runtime.submit(JobCommand(tenant, op, key, layout=PhysicalQubitLayout("qpu-1", physical)))


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


def dense_reference_with_resets(jobs, sides, couplings, until_us, initial, success):
    """Union-register Lindblad evolution with timed resets, written apart from the world's machinery.

    A reset is ``E(rho) = (1 - s) rho + s |0><0| Tr`` on one qubit. Like IBM's reset (measure, then X on
    outcome 1) it acts at the *end* of its interval; the qubit is busy (no relaxation, ZZ still on) from
    its start. Unitaries have zero duration. A handover job
    resets its layout first and starts its shots one reset time later.
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
    eye = np.eye(dim)

    def generator(busy):
        out = -1j * (np.kron(eye, hamiltonian) - np.kron(hamiltonian.T, eye))
        for s, side in enumerate(sides):
            for q, spec in enumerate(side.idle_model.qubits):
                g = offset[s] + q
                if g in busy:
                    continue
                ops = []
                if math.isfinite(spec.t1_us):
                    ops.append(math.sqrt(1.0 / spec.t1_us) * single(np.array([[0, 1], [0, 0]], dtype=complex), g))
                if math.isfinite(spec.tphi_us):
                    ops.append(math.sqrt(0.5 / spec.tphi_us) * single(np.diag([1, -1]).astype(complex), g))
                for c in ops:
                    cc = c.conj().T @ c
                    out = out + np.kron(c.conj(), c) - 0.5 * np.kron(eye, cc) - 0.5 * np.kron(cc.T, eye)
        return out

    def reset(rho, g):
        lowered = single(np.array([[1, 0], [0, 0]], dtype=complex), g)
        raised = single(np.array([[0, 1], [0, 0]], dtype=complex), g)
        to_zero = lowered @ rho @ lowered + raised @ rho @ raised.conj().T
        return (1.0 - success) * rho + success * to_zero

    events = []   # (time, order, kind, global qubits, payload)
    for job in jobs:
        side = sides[job.side]
        first = job.start_us
        if job.handover:
            for slot in job.slots:
                events.append((job.start_us, len(events), "reset", offset[job.side] + slot, side.reset_us))
            first += side.reset_us
        starts, _ = instruction_starts(job.operation, reset_duration_us=side.reset_us)
        for instruction, start in zip(job.operation.instructions, starts):
            if start is None:
                continue
            targets = [offset[job.side] + job.slots[q] for q in instruction.qubits]
            if instruction.kind in {"initialize", "reset"}:
                for g in targets:
                    events.append((first + start, len(events), "reset", g, side.reset_us))
            elif instruction.kind == "unitary":
                if len(targets) != 1:
                    raise ValueError("the reference only takes one-qubit gates")
                events.append((first + start, len(events), "unitary", targets[0], instruction.unitary))
    events = sorted((e for e in events if e[0] <= until_us), key=lambda e: (e[0], e[1]))
    busy_until = {}
    rho = np.array(initial, dtype=complex)
    now = 0.0
    pending = list(events)
    while True:
        busy = {g for g, end in busy_until.items() if end > now}
        ends = [end for end in busy_until.values() if now < end < until_us]
        next_time = min([pending[0][0]] if pending else [until_us])
        step_to = min([next_time] + ends + [until_us])
        if step_to > now:
            vec = rho.reshape(-1, order="F")
            rho = (expm(generator(busy) * (step_to - now)) @ vec).reshape(dim, dim, order="F")
            now = step_to
        if pending and pending[0][0] <= now:
            _, _, kind, g, payload = pending.pop(0)
            if kind == "reset":
                busy_until[g] = max(busy_until.get(g, 0.0), now + payload)
                if now + payload <= until_us:
                    # the channel itself lands at the end; it goes ahead of anything else at that time
                    pending.append((now + payload, -1, "reset_end", g, None))
                    pending.sort(key=lambda e: (e[0], e[1]))
            elif kind == "reset_end":
                rho = reset(rho, g)
            else:
                full = single(np.asarray(payload, dtype=complex), g)
                rho = full @ rho @ full.conj().T
            continue
        if now >= until_us:
            return rho


class HandoverTests(unittest.TestCase):
    """A region may change tenant: the next tenant's job first runs the service reset on its layout."""

    def run_handover(self, zz, *, reset_success=0.6, t1=40.0, tphi=60.0, transfers=()):
        runtime, executor = build(zz=zz, t1=t1, tphi=tphi, reset_success=reset_success, transfers=transfers,
                                  region_access={"attacker": (REGIONS["attacker"], REGIONS["victim"])})
        victim = submit_on(runtime, "victim", REGIONS["victim"], [[{"type": "X", "targets": [0]},
                                                                  {"type": "X", "targets": [1]}]], "v")
        neighbor = submit_on(runtime, "attacker", REGIONS["attacker"],
                             [[{"type": "H", "targets": [0]}],
                              [{"type": "I", "targets": [0], "params": {"duration_us": 4.5}}],
                              [{"type": "X", "targets": [0]}],     # at about 6.5 us, inside the handover reset
                              [{"type": "I", "targets": [0], "params": {"duration_us": 6.0}}]], "a1", seed=4)
        # The attacker's next job takes over the victim's region and reads what the reset left on qubit 0.
        takeover = submit_on(runtime, "attacker", REGIONS["victim"], [], "a2", qubits=1, seed=9)
        register(executor, neighbor, 1.0, 0)
        register(executor, victim, 2.0, 4)
        register(executor, takeover, 6.0, 8, previous_tenant="victim")
        run_in_order(executor)
        return runtime, executor, (victim, neighbor, takeover)

    def test_zero_coupling_handover_matches_the_region_by_region_path(self):
        # The neighbor's drive leaks onto the victim region, also while the handover reset runs there.
        transfers = [DriveCrosstalkTransfer(2, 0, 0.0, 0.3), DriveCrosstalkTransfer(2, 1, 0.2, 0.1)]
        outcomes = []
        for zz in (None, ResidualZZSettings((ResidualZZCoupling(1, 2, 0.0),), model="exact")):
            runtime, executor, jobs = self.run_handover(zz, transfers=transfers)
            store = executor._lanes[REGIONS["victim"]].store
            outcomes.append(([runtime.scheduler.get(job.job_id).result for job in jobs],
                             np.asarray(store.read().density_matrix),
                             [(e.event_kind, e.device_time_start_us, e.device_time_end_us, e.owner_after)
                              for e in store.events()]))
        plain, coupled = outcomes
        self.assertEqual(plain[0], coupled[0])
        np.testing.assert_allclose(coupled[1], plain[1], atol=1e-10)
        self.assertEqual([e[0] for e in plain[2]], [e[0] for e in coupled[2]])
        for a, b in zip(plain[2], coupled[2]):
            self.assertAlmostEqual(a[1], b[1], places=12)
            self.assertAlmostEqual(a[2], b[2], places=12)
            self.assertEqual(a[3], b[3])
        handover = [e for e in plain[2] if e[0] == "service_reset"]
        self.assertEqual(len(handover), 1)
        self.assertAlmostEqual(handover[0][2] - handover[0][1], 1.0)       # the region's reset duration
        self.assertIsNone(handover[0][3])

    def test_the_reset_residue_reaches_the_next_tenant_through_the_joint_world(self):
        zz = ResidualZZSettings((ResidualZZCoupling(1, 2, 0.3),), model="exact")
        _, executor, jobs = self.run_handover(zz, reset_success=0.0, t1=math.inf, tphi=math.inf)
        # Handover and initializing resets that never succeed hand the victim's |1> on qubit 0 to the next tenant.
        self.assertEqual(executor.runtime.scheduler.get(jobs[2].job_id).result["counts"]["1"], 1)
        _, executor, jobs = self.run_handover(zz, reset_success=1.0, t1=math.inf, tphi=math.inf)
        self.assertEqual(executor.runtime.scheduler.get(jobs[2].job_id).result["counts"]["0"], 1)


class ResetFeedbackTests(unittest.TestCase):
    """IBM's reset is a measurement, then X on outcome 1: a leak during the measurement is erased, one
    during the feedback that follows survives (it hits the collapsed branch and is not corrected)."""

    def attacker_reads(self, attacker_start, zz):
        # reset 1.0 us of which the measurement is the first 0.4 us; the victim's pi pulse is at 3.0 us
        runtime, executor = build(transfers=[DriveCrosstalkTransfer(0, 2, 1.0)], zz=zz, durations=(1.0, 0.4))
        victim = submit_on(runtime, "victim", REGIONS["victim"], [[{"type": "X", "targets": [0]}]], "v", qubits=1)
        attacker = submit_on(runtime, "attacker", REGIONS["attacker"],
                             [[{"type": "I", "targets": [0], "params": {"duration_us": 2.0}}]], "a", qubits=1)
        register(executor, victim, 2.0, 0)
        register(executor, attacker, attacker_start, 4)
        run_in_order(executor)
        counts = runtime.scheduler.get(attacker.job_id).result["counts"]
        return max(counts, key=counts.get)

    def test_a_leak_in_the_measurement_is_erased_and_one_in_the_feedback_survives(self):
        for zz in (None, ResidualZZSettings((ResidualZZCoupling(1, 2, 0.0),), model="exact")):
            with self.subTest(world=zz is not None):
                self.assertEqual(self.attacker_reads(2.8, zz), "0")    # 3.0 us is in [2.8, 3.2): measurement
                self.assertEqual(self.attacker_reads(2.5, zz), "1")    # 3.0 us is in [2.9, 3.5): feedback
                self.assertEqual(self.attacker_reads(1.5, zz), "1")    # in the wait after the reset (2.5 us)


class RegionAccessTests(unittest.TestCase):
    def test_a_tenant_runs_only_on_its_home_region_unless_granted(self):
        runtime, _ = build()
        with self.assertRaises(PermissionError):
            submit_on(runtime, "attacker", REGIONS["victim"], [], "x", qubits=1)
        runtime, _ = build(region_access={"attacker": (REGIONS["attacker"], REGIONS["victim"])})
        submit_on(runtime, "attacker", REGIONS["victim"], [], "x", qubits=1)
        with self.assertRaises(PermissionError):
            submit_on(runtime, "victim", REGIONS["attacker"], [], "y", qubits=1)
        with self.assertRaises(ValueError):
            build(region_access={"attacker": ((4, 5),)})


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

    def test_handover_with_coupling_matches_an_independent_reference(self):
        """Timed resets (busy, ZZ on), an imperfect handover reset, and the shifted shots, against SciPy."""
        # Before job a's measurement (0.25 + 0.4 reset + 2.2 waits = 2.85); b's shot starts at 1.0 after handover.
        size, until, success, reset_us = 2, 2.6, 0.7, 0.4
        model, reg = registry(success)
        idle = IndependentIdleModel((IdleQubitSpec(30.0, 50.0, 0.0),) * size, size)
        sides = (RegionSide((0, 1), idle, reg, model.model_id, reset_us, 1.0),
                 RegionSide((2, 3), idle, reg, model.model_id, reset_us, 1.0))
        a, b = self.scenario(size)
        b = replace(b, handover=True)
        plus_one = np.kron(np.array([[0.5, 0.5], [0.5, 0.5]]), np.array([[0, 0], [0, 1]])).astype(complex)
        zero = np.zeros((4, 4), dtype=complex)
        zero[0, 0] = 1.0
        couplings = (ResidualZZCoupling(1, 2, 1.3),)
        world = CoupledRegionWorld(sides, ResidualZZSettings(couplings, model="exact"), (zero, plus_one),
                                   repetition_delay_us=0.0)
        world.advance_to(until, (a, b))
        world.settle(0, until)
        world.settle(1, until)
        reference = dense_reference_with_resets((a, b), sides, [((0, 1), (1, 0), 1.3)], until,
                                                np.kron(zero, plus_one), success)
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
